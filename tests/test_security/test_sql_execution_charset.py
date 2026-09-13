"""覆盖 26.7.1：权威 Schema 在 EXPLAIN 和执行边界的传递。"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from src.datasource.schema_snapshot import ColumnInfo, SchemaSnapshot, TableSchema
from src.security.sql_execution import validate_and_execute_sql, validate_and_explain_sql

SQL = 'SELECT "period" FROM "ordinary" UNION SELECT "period" FROM "national"'


# 构造保留真实目标解析逻辑的离线 Registry 和连接器替身。
# Args: monkeypatch - pytest 替换工具。
# Returns: 配置、连接器及解析方法替身。
@pytest.fixture
def target(monkeypatch: pytest.MonkeyPatch) -> tuple[SimpleNamespace, SimpleNamespace, AsyncMock]:
    """只替换外部连接器 IO，不替换 SQL 重写、只读校验或租户传递。"""
    schema = SchemaSnapshot(tables=[
        TableSchema(name="ordinary", columns=[ColumnInfo(name="period", type="VARCHAR2(7)")]),
        TableSchema(name="national", columns=[ColumnInfo(name="period", type="NVARCHAR2(7)")]),
    ])
    connector = SimpleNamespace(
        explain=AsyncMock(return_value={"valid": True, "errors": []}),
        execute_bounded=AsyncMock(return_value=([{"period": "2026-09"}], False)),
    )
    resolved = SimpleNamespace(dialect="oracle", connector=connector, schema=schema)
    resolver = AsyncMock(return_value=resolved)
    registry = SimpleNamespace(resolve=resolver)

    # 提供独立 Registry，保证测试不访问真实数据库。
    # Args: 无。
    # Returns: 当前测试的 Registry 替身。
    def get_registry() -> SimpleNamespace:
        return registry

    monkeypatch.setattr("src.datasource.registry.get_registry", get_registry)
    return resolved, connector, resolver


class TestSQLExecutionCharset:
    """覆盖 26.7.1 的真实模块交互、类型缺失回退和安全阻断。"""

    # 验证 EXPLAIN 与执行使用同一份权威类型且 SQL 写回后可以再次执行。
    # Args: self - 测试实例；target - 运行时目标替身。
    # Returns: 无返回值。
    @pytest.mark.integration
    async def test_explain_execute_and_sql_writeback(self, target: tuple) -> None:
        """调用方提示方言不能覆盖 Oracle 类型；二次重写不增加转换。"""
        _, connector, resolver = target

        explained = await validate_and_explain_sql(SQL, "tenant_source", "postgres", tenant_id=3)
        executed = await validate_and_execute_sql(
            explained.sql, "tenant_source", "oracle", explain=False, max_rows=5, tenant_id=3,
        )

        assert explained.success and executed.success
        assert explained.sql.count("TO_NCHAR(") == 1
        assert executed.sql == explained.sql
        assert executed.data == [{"period": "2026-09"}]
        assert executed.dialect == "oracle"
        connector.explain.assert_awaited_once_with(explained.sql)
        connector.execute_bounded.assert_awaited_once_with(explained.sql, 5)
        assert resolver.await_count == 2
        resolver.assert_awaited_with("tenant_source", tenant_id=3)

    # 验证直接执行也进行字符集修正，不能只修 EXPLAIN 独立入口。
    # Args: self - 测试实例；target - 运行时目标替身；explain - 是否先 EXPLAIN。
    # Returns: 无返回值。
    @pytest.mark.parametrize("explain", [False, True])
    async def test_direct_execution_rewrites_before_io(self, target: tuple, explain: bool) -> None:
        """有界执行和可选 EXPLAIN 接收到完全一致的修正 SQL。"""
        _, connector, _ = target

        result = await validate_and_execute_sql(SQL, "tenant_source", explain=explain, max_rows=2)

        assert result.success and result.sql.count("TO_NCHAR(") == 1
        connector.execute_bounded.assert_awaited_once_with(result.sql, 2)
        if explain:
            connector.explain.assert_awaited_once_with(result.sql)
        else:
            connector.explain.assert_not_awaited()

    # 验证缺少 Schema 时保留原始数据库错误，而不是虚报字符集问题已修复。
    # Args: self - 测试实例；target - 运行时目标替身。
    # Returns: 无返回值。
    async def test_missing_schema_preserves_semantic_failure(self, target: tuple) -> None:
        """无类型证据继续通过 EXPLAIN 拦截，保留 ORA-12704 错误供原重试使用。"""
        resolved, connector, _ = target
        resolved.schema = None
        errors = [{"type": "semantic_error", "message": "ORA-12704: character set mismatch"}]
        connector.explain.return_value = {"valid": False, "errors": errors}

        result = await validate_and_execute_sql(SQL, "tenant_source", "oracle")

        assert not result.success
        assert result.details == errors
        assert "TO_NCHAR" not in result.sql
        connector.execute_bounded.assert_not_awaited()

    # 验证重写不绕过只读安全检查，非法写入在解析数据源前被拒绝。
    # Args: self - 测试实例；target - 运行时目标替身。
    # Returns: 无返回值。
    async def test_write_is_rejected_before_schema_resolution(self, target: tuple) -> None:
        """提供字符集修正能力不能让 DELETE 进入数据库。"""
        _, connector, resolver = target

        result = await validate_and_execute_sql('DELETE FROM "ordinary"', "tenant_source", "oracle")

        assert not result.success
        resolver.assert_not_awaited()
        connector.execute_bounded.assert_not_awaited()

    # 验证图节点将修正 SQL 写回状态，下次执行和报表取到的不是失败旧 SQL。
    # Args: self - 测试实例；target - 运行时目标替身。
    # Returns: 无返回值。
    @pytest.mark.integration
    async def test_layer4_writes_repaired_sql_to_state(self, target: tuple) -> None:
        """真实 Layer4 调用统一服务后写回成功状态、SQL 和 EXPLAIN 标志。"""
        from src.graph.nodes.layer4_explain import layer4_explain_node

        _, connector, resolver = target
        state = {
            "user_query": "按月份汇总", "generated_sql": SQL,
            "datasource": "tenant_source", "dialect": "oracle", "tenant_id": 3, "user_id": 3,
        }

        update = await layer4_explain_node(state)
        result = await validate_and_execute_sql(
            update["generated_sql"], "tenant_source", "oracle", explain=False, tenant_id=3,
        )

        assert update["sql_valid"] is True
        assert update["sql_explain_checked"] is True
        assert update["explain_errors"] == []
        assert update["generated_sql"].count("TO_NCHAR(") == 1
        assert result.success and result.sql == update["generated_sql"]
        resolver.assert_awaited_with("tenant_source", tenant_id=3)
        connector.explain.assert_awaited_once_with(result.sql)
