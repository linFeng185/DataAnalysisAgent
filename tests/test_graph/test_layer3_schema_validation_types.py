"""阶段二类型与采样格式证据回归（26.2）。"""

from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

from src.datasource.schema_snapshot import ColumnInfo, SchemaSnapshot, TableSchema
from src.graph.nodes.layer3_validate import layer3_validate_node


class TestStageTwoTypeEvidence:
    """覆盖 26.2 阶段二复核不得丢失已确认的类型与格式证据。"""

    # 方法作用：用真实校验器复现类型/格式丢失，并验证权威新类型可纠正旧基准。
    # Args: self - 测试实例；monkeypatch - 补丁工具；sql - 故障 SQL；column_type - 快照类型；value_format - 快照格式；valid - 预期结果。
    # Returns: 无返回值。
    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        ("sql", "column_type", "value_format", "valid"),
        [
            ('SELECT TRUNC(o.order_date) FROM "orders" o', "VARCHAR2", "YYYY-MM-DD", False),
            ('SELECT TRUNC(o.order_date) FROM "orders" o', "", "", False),
            ('SELECT TRUNC(o.order_date) FROM "orders" o', "DATE", "", True),
            ("SELECT TO_DATE(o.order_date, 'YYYY-MM') FROM orders o", "VARCHAR2", "YYYY-MM-DD", False),
            ("SELECT TO_DATE(o.order_date, 'YYYY-MM') FROM orders o", "VARCHAR2", "", False),
            ("SELECT TO_DATE(o.order_date, 'YYYY-MM') FROM orders o", "VARCHAR2", "YYYY-MM", True),
        ],
    )
    async def test_stage_two_preserves_type_evidence(self, monkeypatch: pytest.MonkeyPatch, sql: str, column_type: str, value_format: str, valid: bool) -> None:
        """缺失属性不能清除阶段一问题，完整权威新属性可以纠正误报。"""
        # Arrange：阶段一拿到完整 VARCHAR2 日期文本证据，阶段二分别模拟完整、缺失和更新。
        snapshot = SchemaSnapshot(tables=[TableSchema(name="orders", columns=[ColumnInfo(name="order_date", type=column_type, value_format=value_format)])])
        manager = SimpleNamespace(get_or_fetch_schema=AsyncMock(return_value=snapshot))
        monkeypatch.setattr("src.knowledge.schema_manager.get_schema_manager", Mock(return_value=manager))
        state = {
            "generated_sql": sql, "datasource": "oracle", "dialect": "oracle", "tenant_id": 3,
            "relevant_tables": [{"name": "orders", "columns": [{"name": "order_date", "type": "VARCHAR2", "value_format": "YYYY-MM-DD"}]}],
        }

        # Act
        result = await layer3_validate_node(state)

        # Assert
        manager.get_or_fetch_schema.assert_awaited_once_with("oracle", user_query="", tenant_id=3)
        assert result["sql_valid"] is valid
        if not valid:
            assert result["validation_errors"][0]["issues"][0]["type"] == "type_mismatch"
            assert "order_date" in result["validation_errors"][0]["message"]

    # 方法作用：验证真实节点拦截后的类型修复提示能到达生成重试，而非只剩未知类型告警。
    # Args: self - 测试实例；monkeypatch - 补丁工具。
    # Returns: 无返回值。
    @pytest.mark.asyncio
    async def test_type_note_survives_retry_boundary(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """TRUNC 文本日期必须回喂实际类型与 TO_DATE 修法，并路由回生成节点。"""
        # Arrange
        from src.graph.nodes.generate_sql import build_retry_context
        from src.graph.workflow import after_layer3
        column = {"name": "order_date", "type": "VARCHAR2", "value_format": "YYYY-MM-DD"}
        tables = [{"name": "orders", "columns": [column]}]
        snapshot = SchemaSnapshot(tables=[TableSchema(name="orders", columns=[ColumnInfo(**column)])])
        manager = SimpleNamespace(get_or_fetch_schema=AsyncMock(return_value=snapshot))
        monkeypatch.setattr("src.knowledge.schema_manager.get_schema_manager", Mock(return_value=manager))
        state = {"generated_sql": 'SELECT TRUNC(o.order_date) FROM "orders" o', "datasource": "oracle", "dialect": "oracle", "tenant_id": 3, "relevant_tables": tables, "retry_count": 1}

        # Act
        update = await layer3_validate_node(state)
        retry_state = {**state, **update}
        hint = build_retry_context(retry_state, 1, tables)

        # Assert
        assert after_layer3(retry_state) == "generate_sql"
        assert "VARCHAR2" in hint and "TO_DATE" in hint
        assert state["generated_sql"] not in hint

    # 方法作用：验证高频类型提示渲染不新增成功日志或未知类型告警。
    # Args: self - 测试实例。
    # Returns: 无返回值。
    def test_repeated_type_rendering_is_quiet(self) -> None:
        """类型错误是已知可恢复分支，逐条渲染不重复输出 INFO/WARNING。"""
        # Arrange
        from structlog.testing import capture_logs

        from src.graph.nodes.generate_sql import _render_schema_issue_lines
        issue = {"type": "type_mismatch", "message": "使用 TO_DATE 显式转换"}

        # Act
        with capture_logs() as logs:
            results = [_render_schema_issue_lines(issue) for _ in range(30)]

        # Assert
        assert all(result == [issue["message"]] for result in results)
        assert not logs

    # 方法作用：验证快照异常仍保留阶段一阻断及一次带堆栈的边界日志。
    # Args: self - 测试实例；monkeypatch - 补丁工具。
    # Returns: 无返回值。
    @pytest.mark.asyncio
    async def test_snapshot_failure_keeps_evidence_and_stack(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """复核失败不得静默放行，也不得丢掉原异常堆栈。"""
        # Arrange
        from structlog.testing import capture_logs

        from src.graph.nodes.layer3_validate import _recheck_with_full_snapshot
        from src.security.sql_schema_validation import ValidationIssue
        manager = SimpleNamespace(get_or_fetch_schema=AsyncMock(side_effect=RuntimeError("snapshot unavailable")))
        monkeypatch.setattr("src.knowledge.schema_manager.get_schema_manager", Mock(return_value=manager))
        issues = [ValidationIssue(type="type_mismatch", datasource="oracle", table="orders", column="order_date")]

        # Act
        with capture_logs() as logs:
            result = await _recheck_with_full_snapshot("SELECT 1 FROM dual", "oracle", "oracle", issues, 3)

        # Assert
        assert result is None
        errors = [entry for entry in logs if entry.get("exc_info")]
        assert len(errors) == 1 and errors[0]["exc_info"] is True
