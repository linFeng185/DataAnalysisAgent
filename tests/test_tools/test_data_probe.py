"""probe_data 结构化探查工具测试，覆盖功能 29.3（设计 spec/55 §7、§14）。

工具只允许在服务端绑定的授权上下文内执行五种固定原语；真实 SQLite 用于
验证行过滤、列白名单与只读性，身份与预算用替身断言边界。
"""

from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
import sqlalchemy as sa
from sqlalchemy.ext.asyncio import create_async_engine

import src.datasource.profiling as profiling_module
from src.api.auth import use_identity_context
from src.connectors.sqlite import SQLiteConnector
from src.datasource.config import DataSourceConfig
from src.datasource.profiling import ProbeRequest, ProfilingContext
from src.datasource.schema_snapshot import ColumnInfo, SchemaSnapshot, TableSchema
from src.security.sql_execution import SQLExecutionResult
from src.security.tenant_policy import RequestIdentity
from src.tools.data_probe import TOOL_NAME, build_data_probe_tools, use_data_probe_context


# 方法作用：把探查相关配置替换成测试可控的独立快照。
# Args: monkeypatch - pytest 补丁工具；overrides - 需要覆盖的配置键值。
# Returns: 被替换的配置对象。
def _patch_settings(monkeypatch, **overrides):
    import src.tools.data_probe as data_probe_module

    settings = SimpleNamespace(**overrides)
    monkeypatch.setattr(profiling_module, "get_settings", lambda: settings)
    monkeypatch.setattr(data_probe_module, "get_settings", lambda: settings)
    return settings


# 方法作用：构造带探查样本的真实 SQLite 内存数据源。
# Args: 无。
# Returns: (数据源配置, 引擎)；调用方负责 dispose。
async def _sample_db() -> tuple[DataSourceConfig, object]:
    engine = create_async_engine("sqlite+aiosqlite:///:memory:", poolclass=sa.pool.StaticPool)
    async with engine.begin() as connection:
        await connection.execute(sa.text(
            "CREATE TABLE t_workshop ("
            "id INTEGER PRIMARY KEY, workshop_code TEXT NOT NULL, "
            "workshop_name TEXT NOT NULL, factory_id INTEGER, region TEXT)"
        ))
        await connection.execute(sa.text(
            "INSERT INTO t_workshop (id, workshop_code, workshop_name, factory_id, region) VALUES "
            "(1,'W001','冲压车间',1,'华东'),(2,'W002','冲压车间',1,'华东'),"
            "(3,'W003','焊接车间',1,'华东'),(4,'W004','焊接车间',2,'华北'),"
            "(5,'W005','涂装车间',2,'华北'),(6,'W006','涂装车间',2,'华北')"
        ))
        await connection.execute(sa.text(
            "CREATE TABLE t_event (event_date TEXT, note TEXT, amount REAL)"
        ))
        await connection.execute(sa.text(
            "INSERT INTO t_event (event_date, note, amount) VALUES "
            "('2024-01-05','a',1.0),('2024-03-18','b',2.0),('2024-06-30','c',3.0)"
        ))
    datasource = DataSourceConfig(
        name="probe-sqlite", dialect="sqlite", mode="embedded",
        database=":memory:", engine=engine,
    )
    datasource.connector = SQLiteConnector(datasource).attach_engine(engine)
    datasource.schema = SchemaSnapshot(tables=[
        TableSchema(name="t_workshop", columns=[
            ColumnInfo("id", "INTEGER", is_primary_key=True),
            ColumnInfo("workshop_code", "TEXT"),
            ColumnInfo("workshop_name", "TEXT"),
            ColumnInfo("factory_id", "INTEGER"),
            ColumnInfo("region", "TEXT"),
        ], row_count_estimate=6),
        TableSchema(name="t_event", columns=[
            ColumnInfo("event_date", "TEXT"),
            ColumnInfo("note", "TEXT"),
            ColumnInfo("amount", "REAL"),
        ], row_count_estimate=3),
    ])
    return datasource, engine


# 方法作用：构造服务端探查上下文。
# Args: datasource - 兼容对象（name/dialect）；kwargs - 身份、行列策略与指纹覆盖。
# Returns: ProfilingContext。
def _context(datasource, **kwargs) -> ProfilingContext:
    return ProfilingContext(
        tenant_id=kwargs.pop("tenant_id", 3),
        user_id=kwargs.pop("user_id", 7),
        role=kwargs.pop("role", "analyst"),
        datasource=datasource.name,
        dialect=datasource.dialect,
        tables=kwargs.pop("tables", _authorized_tables()),
        **kwargs,
    )


# 方法作用：构造工具路径的授权表结构集合。
# Args: 无。
# Returns: 表名到 TableSchema 的映射。
def _authorized_tables() -> dict:
    return {
        "t_workshop": TableSchema(name="t_workshop", columns=[
            ColumnInfo("id", "INTEGER", is_primary_key=True),
            ColumnInfo("workshop_code", "TEXT"),
            ColumnInfo("workshop_name", "TEXT"),
            ColumnInfo("factory_id", "INTEGER"),
            ColumnInfo("region", "TEXT"),
        ]),
        "t_event": TableSchema(name="t_event", columns=[
            ColumnInfo("event_date", "TEXT"),
            ColumnInfo("note", "TEXT"),
            ColumnInfo("amount", "REAL"),
        ]),
    }


# 方法作用：构造与服务端绑定一致的请求身份。
# Args: user_id - 用户 ID；role - 角色；tenant_id - 租户 ID。
# Returns: RequestIdentity。
def _identity(user_id: int = 7, role: str = "analyst", tenant_id: int = 3) -> RequestIdentity:
    return RequestIdentity(tenant_id=tenant_id, user_id=user_id, role=role, username="tester")


# 方法作用：把 registry 替换成只暴露当前数据源的测试替身。
# Args: monkeypatch - pytest 补丁工具；datasource - 被测数据源。
# Returns: 替换后的 registry。
def _patch_registry(monkeypatch, datasource):
    registry = SimpleNamespace(
        resolve=AsyncMock(return_value=datasource),
        list_all=AsyncMock(return_value=[{"name": datasource.name, "dialect": datasource.dialect}]),
    )
    monkeypatch.setattr("src.datasource.registry.get_registry", lambda: registry)
    return registry


# 方法作用：构造 sql.generate 的调用快照。
# Args: prompt_id - 提示词编号。
# Returns: 最小 prepared 对象。
def _prepared(prompt_id: str = "sql.generate"):
    return SimpleNamespace(prompt_id=prompt_id)


class TestDataProbeToolScope:
    """覆盖 29.3 的工具构建作用域：任务、开关与上下文绑定。"""

    # 方法作用：未绑定上下文、非 sql.generate、关闭开关都不构建工具。
    # Args: monkeypatch - pytest 补丁工具。
    # Returns: 无返回值。
    async def test_tool_build_requires_bound_trusted_context(self, monkeypatch) -> None:
        """缺少任一条前置条件都返回空列表，不给出可调用工具。"""
        # Arrange
        datasource, engine = await _sample_db()
        _patch_registry(monkeypatch, datasource)
        _patch_settings(monkeypatch, sql_profiling_enabled=True)
        context = _context(datasource)

        try:
            # Act + Assert：未绑定上下文
            assert build_data_probe_tools(_prepared()) == []

            with use_data_probe_context(context):
                # 非 sql.generate 任务
                assert build_data_probe_tools(_prepared("analyze_result")) == []
                # 未传 prepared 也不能凭默认放行
                assert build_data_probe_tools() == []
                # 正常路径返回唯一工具
                tools = build_data_probe_tools(_prepared())
                assert [tool.name for tool in tools] == [TOOL_NAME]
                assert tools[0].args_schema is ProbeRequest

            # 关闭开关后不再构建
            _patch_settings(monkeypatch, sql_profiling_enabled=False)
            with use_data_probe_context(context):
                assert build_data_probe_tools(_prepared()) == []
        finally:
            await engine.dispose()

    # 方法作用：匿名上下文不产生工具。
    # Args: monkeypatch - pytest 补丁工具。
    # Returns: 无返回值。
    async def test_anonymous_context_builds_no_tool(self, monkeypatch) -> None:
        """user_id=0 的上下文不得获得探针工具。"""
        # Arrange
        datasource, engine = await _sample_db()
        _patch_registry(monkeypatch, datasource)
        _patch_settings(monkeypatch, sql_profiling_enabled=True)

        try:
            # Act
            with use_data_probe_context(_context(datasource, user_id=0)):
                tools = build_data_probe_tools(_prepared())

            # Assert
            assert tools == []
        finally:
            await engine.dispose()

    # 方法作用：知识检索关闭不影响已绑定的探查工具。
    # Args: monkeypatch - pytest 补丁工具。
    # Returns: 无返回值。
    async def test_tool_is_independent_from_knowledge_loop(self, monkeypatch) -> None:
        """知识工具关闭不能意外禁用探查。"""
        # Arrange
        from src.llm.knowledge_loop import KnowledgeLoopConfig, use_knowledge_loop

        datasource, engine = await _sample_db()
        _patch_registry(monkeypatch, datasource)
        _patch_settings(monkeypatch, sql_profiling_enabled=True)

        try:
            # Act
            with use_knowledge_loop(KnowledgeLoopConfig(enabled=False)):
                with use_data_probe_context(_context(datasource)):
                    tools = build_data_probe_tools(_prepared())

            # Assert
            assert [tool.name for tool in tools] == [TOOL_NAME]
        finally:
            await engine.dispose()


class TestDataProbePrimitives:
    """覆盖 29.3 的五种原语在真实 SQLite 上的结果契约。"""

    # 方法作用：entity_lookup 在样本内按字面包含查找，精确匹配优先且 %/_ 无通配语义。
    # Args: monkeypatch - pytest 补丁工具。
    # Returns: 无返回值。
    async def test_entity_lookup_is_literal_and_exact_first(self, monkeypatch) -> None:
        """% 与 _ 只作普通字符，精确值排在候选之前。"""
        # Arrange
        datasource, engine = await _sample_db()
        _patch_registry(monkeypatch, datasource)
        _patch_settings(monkeypatch, sql_probe_limit=50)
        context = _context(datasource)

        try:
            with use_identity_context(_identity()), use_data_probe_context(context):
                tool = build_data_probe_tools(_prepared())[0]

                # Act
                literal = await tool.ainvoke({
                    "operation": "entity_lookup", "table": "t_workshop",
                    "column": "region", "pattern": "%",
                })
                exact = await tool.ainvoke({
                    "operation": "entity_lookup", "table": "t_workshop",
                    "column": "region", "pattern": "华东",
                })

            # Assert：% 不是通配符，样本内没有字面 % 值
            assert literal["success"] is True
            assert literal["rows"] == []
            assert literal["scope"] == "complete"
            assert exact["rows"][0]["value"] == "华东"
            assert exact["rows"][0]["count"] == 3
            assert exact["rows"][0]["exact"] is True
        finally:
            await engine.dispose()

    # 方法作用：value_domain 返回值域与频次，列数不超过上限。
    # Args: monkeypatch - pytest 补丁工具。
    # Returns: 无返回值。
    async def test_value_domain_returns_frequency(self, monkeypatch) -> None:
        """值域行按频次降序且不是 distinct_count 的替代。"""
        # Arrange
        datasource, engine = await _sample_db()
        _patch_registry(monkeypatch, datasource)
        _patch_settings(monkeypatch, sql_probe_limit=50)
        context = _context(datasource)

        try:
            with use_identity_context(_identity()), use_data_probe_context(context):
                tool = build_data_probe_tools(_prepared())[0]

                # Act
                result = await tool.ainvoke({
                    "operation": "value_domain", "table": "t_workshop", "column": "workshop_name",
                })

            # Assert
            assert result["success"] is True
            assert result["columns"] == ["workshop_name"]
            assert result["row_count"] == 3
            assert result["rows"][0] == {"value": "冲压车间", "count": 2}
            assert result["scope"] == "complete"
        finally:
            await engine.dispose()

    # 方法作用：column_repeat 给出唯一性结论与最高重复值。
    # Args: monkeypatch - pytest 补丁工具。
    # Returns: 无返回值。
    async def test_column_repeat_returns_uniqueness(self, monkeypatch) -> None:
        """唯一列与重复列结论必须与画像口径一致。"""
        # Arrange
        datasource, engine = await _sample_db()
        _patch_registry(monkeypatch, datasource)
        _patch_settings(monkeypatch, sql_probe_limit=50)
        context = _context(datasource)

        try:
            with use_identity_context(_identity()), use_data_probe_context(context):
                tool = build_data_probe_tools(_prepared())[0]

                # Act
                unique = await tool.ainvoke({
                    "operation": "column_repeat", "table": "t_workshop", "column": "id",
                })
                repeated = await tool.ainvoke({
                    "operation": "column_repeat", "table": "t_workshop", "column": "workshop_name",
                })

            # Assert
            assert unique["rows"][0]["uniqueness"] == "unique"
            assert unique["rows"][0]["distinct_count"] == 6
            assert repeated["rows"][0]["uniqueness"] == "duplicate"
            assert repeated["rows"][0]["top_value"] == "冲压车间"
            assert repeated["rows"][0]["top_frequency"] == 2
        finally:
            await engine.dispose()

    # 方法作用：time_range 只对类型兼容的样本给范围，文本格式未知时不声称时间范围。
    # Args: monkeypatch - pytest 补丁工具。
    # Returns: 无返回值。
    async def test_time_range_requires_compatible_column(self, monkeypatch) -> None:
        """日期文本列给 min/max，未知文本列明确不兼容。"""
        # Arrange
        datasource, engine = await _sample_db()
        _patch_registry(monkeypatch, datasource)
        _patch_settings(monkeypatch, sql_probe_limit=50)
        context = _context(datasource)

        try:
            with use_identity_context(_identity()), use_data_probe_context(context):
                tool = build_data_probe_tools(_prepared())[0]

                # Act
                temporal = await tool.ainvoke({
                    "operation": "time_range", "table": "t_event", "column": "event_date",
                })
                text = await tool.ainvoke({
                    "operation": "time_range", "table": "t_event", "column": "note",
                })

            # Assert
            assert temporal["rows"][0]["compatible"] is True
            assert temporal["rows"][0]["min"] == "2024-01-05"
            assert temporal["rows"][0]["max"] == "2024-06-30"
            assert text["rows"][0]["compatible"] is False
            assert text["rows"][0]["min"] is None
        finally:
            await engine.dispose()

    # 方法作用：table_sample 只返回请求的授权列并强制行数上限。
    # Args: monkeypatch - pytest 补丁工具。
    # Returns: 无返回值。
    async def test_table_sample_limits_rows_and_columns(self, monkeypatch) -> None:
        """样例行数受 sql_probe_limit 约束，列限于请求集合。"""
        # Arrange
        datasource, engine = await _sample_db()
        _patch_registry(monkeypatch, datasource)
        _patch_settings(monkeypatch, sql_probe_limit=2)
        context = _context(datasource)

        try:
            with use_identity_context(_identity()), use_data_probe_context(context):
                tool = build_data_probe_tools(_prepared())[0]

                # Act
                result = await tool.ainvoke({
                    "operation": "table_sample", "table": "t_workshop",
                    "columns": ["id", "region"], "limit": 10,
                })

            # Assert
            assert result["success"] is True
            assert result["columns"] == ["id", "region"]
            assert result["row_count"] == 2
            assert result["truncated"] is True
            assert result["scope"] == "sample"
            assert all(set(row) == {"id", "region"} for row in result["rows"])
        finally:
            await engine.dispose()

    # 方法作用：探查执行的全是只读 SELECT。
    # Args: monkeypatch - pytest 补丁工具。
    # Returns: 无返回值。
    async def test_probe_executes_only_select(self, monkeypatch) -> None:
        """任何原语都不得生成写语句。"""
        # Arrange
        datasource, engine = await _sample_db()
        _patch_registry(monkeypatch, datasource)
        _patch_settings(monkeypatch, sql_probe_limit=50)
        real_executor = profiling_module.validate_and_execute_sql
        calls: list[str] = []

        async def recording_executor(sql, datasource_name, dialect="", *, explain=True, max_rows=None, tenant_id=None):
            calls.append(sql)
            return await real_executor(
                sql, datasource_name, dialect,
                explain=explain, max_rows=max_rows, tenant_id=tenant_id,
            )

        monkeypatch.setattr(profiling_module, "validate_and_execute_sql", recording_executor)
        context = _context(datasource)

        try:
            with use_identity_context(_identity()), use_data_probe_context(context):
                tool = build_data_probe_tools(_prepared())[0]

                # Act
                for payload in (
                    {"operation": "value_domain", "table": "t_workshop", "column": "region"},
                    {"operation": "column_repeat", "table": "t_workshop", "column": "id"},
                    {"operation": "table_sample", "table": "t_workshop", "columns": ["id"]},
                ):
                    assert (await tool.ainvoke(payload))["success"] is True

            # Assert
            assert calls
            assert all(sql.strip().upper().startswith("SELECT") for sql in calls)
            assert all(";" not in sql.strip().rstrip(";") for sql in calls)
        finally:
            await engine.dispose()

    # 方法作用：真实 SQLite 上探查不改变业务数据。
    # Args: monkeypatch - pytest 补丁工具。
    # Returns: 无返回值。
    async def test_probe_does_not_mutate_sqlite(self, monkeypatch) -> None:
        """执行前后表内容完全一致，证明探查零写入。"""
        # Arrange
        datasource, engine = await _sample_db()
        _patch_registry(monkeypatch, datasource)
        _patch_settings(monkeypatch, sql_probe_limit=50)
        context = _context(datasource)

        async def snapshot_rows() -> list[tuple]:
            async with engine.connect() as connection:
                result = await connection.execute(sa.text(
                    "SELECT id, workshop_code, workshop_name, factory_id, region "
                    "FROM t_workshop ORDER BY id"
                ))
                return [tuple(row) for row in result]

        try:
            before = await snapshot_rows()

            with use_identity_context(_identity()), use_data_probe_context(context):
                tool = build_data_probe_tools(_prepared())[0]
                # Act
                for payload in (
                    {"operation": "value_domain", "table": "t_workshop", "column": "region"},
                    {"operation": "entity_lookup", "table": "t_workshop", "column": "region", "pattern": "华东"},
                    {"operation": "table_sample", "table": "t_workshop", "columns": ["id", "region"]},
                ):
                    assert (await tool.ainvoke(payload))["success"] is True

            # Assert
            assert await snapshot_rows() == before
        finally:
            await engine.dispose()


class TestDataProbeSecurity:
    """覆盖 29.3 的授权边界：越权表列、身份、行过滤与脱敏。"""

    # 方法作用：请求未知表或未知列被拒绝。
    # Args: monkeypatch - pytest 补丁工具。
    # Returns: 无返回值。
    async def test_unknown_table_and_column_are_rejected(self, monkeypatch) -> None:
        """表名与列名必须逐项等于授权结构。"""
        # Arrange
        datasource, engine = await _sample_db()
        _patch_registry(monkeypatch, datasource)
        _patch_settings(monkeypatch, sql_probe_limit=50)
        context = _context(datasource)

        try:
            with use_identity_context(_identity()), use_data_probe_context(context):
                tool = build_data_probe_tools(_prepared())[0]

                # Act
                unknown_table = await tool.ainvoke({
                    "operation": "value_domain", "table": "t_workshop; DROP TABLE t_workshop",
                    "column": "id",
                })
                unknown_column = await tool.ainvoke({
                    "operation": "value_domain", "table": "t_workshop", "column": "not_a_column",
                })

            # Assert
            assert unknown_table["success"] is False
            assert unknown_table["error_code"] == "unknown_table"
            assert unknown_column["success"] is False
            assert unknown_column["error_code"] == "unknown_column"
        finally:
            await engine.dispose()

    # 方法作用：投影列超出列白名单时拒绝。
    # Args: monkeypatch - pytest 补丁工具。
    # Returns: 无返回值。
    async def test_column_whitelist_blocks_projection(self, monkeypatch) -> None:
        """allowed_columns 决定可投影列，越权列直接拒绝。"""
        # Arrange
        datasource, engine = await _sample_db()
        _patch_registry(monkeypatch, datasource)
        _patch_settings(monkeypatch, sql_probe_limit=50)
        context = _context(datasource, allowed_columns=["id", "region"])

        try:
            with use_identity_context(_identity()), use_data_probe_context(context):
                tool = build_data_probe_tools(_prepared())[0]

                # Act
                allowed = await tool.ainvoke({
                    "operation": "value_domain", "table": "t_workshop", "column": "region",
                })
                blocked = await tool.ainvoke({
                    "operation": "value_domain", "table": "t_workshop", "column": "workshop_code",
                })
                mixed = await tool.ainvoke({
                    "operation": "table_sample", "table": "t_workshop",
                    "columns": ["id", "workshop_code"],
                })

            # Assert
            assert allowed["success"] is True
            assert blocked["success"] is False
            assert blocked["error_code"] == "column_not_allowed"
            assert mixed["success"] is False
            assert mixed["error_code"] == "column_not_allowed"
        finally:
            await engine.dispose()

    # 方法作用：模型传入身份或数据源字段被当成非法参数拒绝。
    # Args: monkeypatch - pytest 补丁工具。
    # Returns: 无返回值。
    async def test_extra_identity_arguments_are_rejected(self, monkeypatch) -> None:
        """工具参数不能替换身份或数据源，额外字段在校验层失败。"""
        # Arrange
        from pydantic import ValidationError

        datasource, engine = await _sample_db()
        _patch_registry(monkeypatch, datasource)
        _patch_settings(monkeypatch, sql_probe_limit=50)
        context = _context(datasource)

        try:
            with use_identity_context(_identity()), use_data_probe_context(context):
                tool = build_data_probe_tools(_prepared())[0]

                # Act + Assert
                for payload in (
                    {"operation": "value_domain", "table": "t_workshop", "column": "region", "tenant_id": 1},
                    {"operation": "value_domain", "table": "t_workshop", "column": "region", "datasource": "other"},
                    {"operation": "value_domain", "table": "t_workshop", "column": "region", "row_filter_sql": "1=1"},
                ):
                    with pytest.raises(ValidationError):
                        await tool.ainvoke(payload)
        finally:
            await engine.dispose()

    # 方法作用：执行期真实身份与绑定上下文不一致时拒绝。
    # Args: monkeypatch - pytest 补丁工具。
    # Returns: 无返回值。
    async def test_identity_mismatch_is_forbidden(self, monkeypatch) -> None:
        """其他用户身份不得复用别人的探查上下文。"""
        # Arrange
        datasource, engine = await _sample_db()
        _patch_registry(monkeypatch, datasource)
        _patch_settings(monkeypatch, sql_probe_limit=50)
        context = _context(datasource)

        try:
            with use_data_probe_context(context):
                tool = build_data_probe_tools(_prepared())[0]
                with use_identity_context(_identity(user_id=99)):
                    # Act
                    result = await tool.ainvoke({
                        "operation": "value_domain", "table": "t_workshop", "column": "region",
                    })

            # Assert
            assert result["success"] is False
            assert result["error_code"] == "forbidden"
        finally:
            await engine.dispose()

    # 方法作用：匿名执行身份不得使用已绑定的探查上下文。
    # Args: monkeypatch - pytest 补丁工具。
    # Returns: 无返回值。
    async def test_anonymous_executor_is_forbidden(self, monkeypatch) -> None:
        """无真实认证身份时拒绝执行。"""
        # Arrange
        datasource, engine = await _sample_db()
        _patch_registry(monkeypatch, datasource)
        _patch_settings(monkeypatch, sql_probe_limit=50)
        context = _context(datasource)

        try:
            with use_data_probe_context(context):
                tool = build_data_probe_tools(_prepared())[0]

                # Act
                result = await tool.ainvoke({
                    "operation": "value_domain", "table": "t_workshop", "column": "region",
                })

            # Assert
            assert result["success"] is False
            assert result["error_code"] == "forbidden"
        finally:
            await engine.dispose()

    # 方法作用：行过滤在探查取样前生效，且策略列不进入结果。
    # Args: monkeypatch - pytest 补丁工具。
    # Returns: 无返回值。
    async def test_row_filter_applies_before_sampling(self, monkeypatch) -> None:
        """策略列不在白名单也必须参与 WHERE。"""
        # Arrange
        datasource, engine = await _sample_db()
        _patch_registry(monkeypatch, datasource)
        _patch_settings(monkeypatch, sql_probe_limit=50)
        context = _context(
            datasource,
            allowed_columns=["id", "region"],
            row_filter_sql="factory_id = 1",
        )

        try:
            with use_identity_context(_identity()), use_data_probe_context(context):
                tool = build_data_probe_tools(_prepared())[0]

                # Act
                result = await tool.ainvoke({
                    "operation": "value_domain", "table": "t_workshop", "column": "region",
                })

            # Assert：工厂 1 只有华东
            assert result["success"] is True
            assert result["rows"] == [{"value": "华东", "count": 3}]
            assert "factory_id" not in json.dumps(result, ensure_ascii=False)
        finally:
            await engine.dispose()

    # 方法作用：执行失败的返回体不含 SQL、异常原文与连接信息。
    # Args: monkeypatch - pytest 补丁工具。
    # Returns: 无返回值。
    async def test_failure_payload_is_scrubbed(self, monkeypatch) -> None:
        """错误只保留脱敏代码。"""
        # Arrange
        datasource, engine = await _sample_db()
        _patch_registry(monkeypatch, datasource)
        _patch_settings(monkeypatch, sql_probe_limit=50)

        async def failing(sql, datasource_name, dialect="", *, explain=True, max_rows=None, tenant_id=None):
            return SQLExecutionResult(
                success=False, sql=sql, dialect="sqlite",
                error="SELECT password_hash FROM t_users WHERE token='leak'",
                error_type="execution",
            )

        monkeypatch.setattr(profiling_module, "validate_and_execute_sql", failing)
        context = _context(datasource)

        try:
            with use_identity_context(_identity()), use_data_probe_context(context):
                tool = build_data_probe_tools(_prepared())[0]

                # Act
                result = await tool.ainvoke({
                    "operation": "value_domain", "table": "t_workshop", "column": "region",
                })

            # Assert
            payload = json.dumps(result, ensure_ascii=False)
            assert result["success"] is False
            assert result["error_code"] == "execution_error"
            assert "password_hash" not in payload
            assert "leak" not in payload
            assert "SELECT" not in payload
        finally:
            await engine.dispose()


class TestDataProbeBudget:
    """覆盖 29.3 的探查预算、工具重建与取消语义。"""

    # 方法作用：一次上下文绑定最多三次真实探查，耗尽后返回明确预算结果。
    # Args: monkeypatch - pytest 补丁工具。
    # Returns: 无返回值。
    async def test_probe_budget_is_shared_per_binding(self, monkeypatch) -> None:
        """反复构建工具不重置预算，耗尽不抛异常。"""
        # Arrange
        datasource, engine = await _sample_db()
        _patch_registry(monkeypatch, datasource)
        _patch_settings(monkeypatch, sql_probe_limit=50)
        context = _context(datasource)

        try:
            with use_identity_context(_identity()), use_data_probe_context(context):
                first = build_data_probe_tools(_prepared())[0]
                second = build_data_probe_tools(_prepared())[0]
                payload = {"operation": "value_domain", "table": "t_workshop", "column": "region"}

                # Act
                results = [await first.ainvoke(payload) for _ in range(3)]
                exhausted = await second.ainvoke(payload)

            # Assert
            assert all(result["success"] is True for result in results)
            assert exhausted["success"] is False
            assert exhausted["error_code"] == "budget_exhausted"
            assert exhausted["rows"] == []
        finally:
            await engine.dispose()

    # 方法作用：参数与授权失败不消耗真实查询预算。
    # Args: monkeypatch - pytest 补丁工具。
    # Returns: 无返回值。
    async def test_invalid_requests_do_not_consume_budget(self, monkeypatch) -> None:
        """被拒绝的调用不占用查询额度。"""
        # Arrange
        datasource, engine = await _sample_db()
        _patch_registry(monkeypatch, datasource)
        _patch_settings(monkeypatch, sql_probe_limit=50)
        context = _context(datasource)

        try:
            with use_identity_context(_identity()), use_data_probe_context(context):
                tool = build_data_probe_tools(_prepared())[0]

                # Act：未知表不消耗预算
                for _ in range(2):
                    rejected = await tool.ainvoke({
                        "operation": "value_domain", "table": "missing_table", "column": "id",
                    })
                    assert rejected["error_code"] == "unknown_table"
                allowed = await tool.ainvoke({
                    "operation": "value_domain", "table": "t_workshop", "column": "region",
                })

            # Assert
            assert allowed["success"] is True
        finally:
            await engine.dispose()

    # 方法作用：取消在工具层原样传播。
    # Args: monkeypatch - pytest 补丁工具。
    # Returns: 无返回值。
    async def test_cancellation_propagates(self, monkeypatch) -> None:
        """取消不是预算耗尽，必须向上抛出。"""
        # Arrange
        datasource, engine = await _sample_db()
        _patch_registry(monkeypatch, datasource)
        _patch_settings(monkeypatch, sql_probe_limit=50, sql_probe_timeout_seconds=5.0)

        async def hanging(sql, datasource_name, dialect="", *, explain=True, max_rows=None, tenant_id=None):
            await asyncio.sleep(5)
            return SQLExecutionResult(success=True, sql=sql, dialect="sqlite", data=[])

        monkeypatch.setattr(profiling_module, "validate_and_execute_sql", hanging)
        context = _context(datasource)

        try:
            with use_identity_context(_identity()), use_data_probe_context(context):
                tool = build_data_probe_tools(_prepared())[0]
                task = asyncio.create_task(tool.ainvoke({
                    "operation": "value_domain", "table": "t_workshop", "column": "region",
                }))
                await asyncio.sleep(0.05)

                # Act
                task.cancel()
                with pytest.raises(asyncio.CancelledError):
                    await task
        finally:
            await engine.dispose()


class TestDataProbePolicyIntersection:
    """覆盖 29.3 的空权限交集在工具路径的拒绝（spec/55 §10）。"""

    # 方法作用：快照与执行期最新列权限交集为空时，工具调用必须拒绝且零 SQL。
    # Args: monkeypatch - pytest 补丁工具。
    # Returns: 无返回值。
    async def test_empty_column_intersection_rejects_tool_probe(self, monkeypatch) -> None:
        """空交集不能解释成没有列限制，拒绝必须发生在任何 SQL 之前。"""
        # Arrange
        datasource, engine = await _sample_db()
        _patch_registry(monkeypatch, datasource)
        _patch_settings(monkeypatch, sql_probe_limit=50)

        # 模拟执行期最新权限已不再包含快照授权列。
        # Args: args/kwargs - 权限解析调用参数。
        # Returns: 仅允许 workshop_code 的最新策略。
        async def current_access(*args, **kwargs):
            return {datasource.name: {
                "allowed_columns": ["workshop_code"], "row_filter_sql": "",
            }}

        monkeypatch.setattr("src.security.permission_check.resolve_datasource_access", current_access)
        executed: list[str] = []

        # 被调用即说明拒绝晚于 SQL 构建，测试必须失败。
        # Args: sql - 被拦截的 SQL；args/kwargs - 统一执行器其余参数。
        # Returns: 不会返回，直接断言失败。
        async def forbidden_execute(sql, *args, **kwargs):
            executed.append(sql)
            raise AssertionError("空权限交集不得进入执行通道")

        monkeypatch.setattr(profiling_module, "validate_and_execute_sql", forbidden_execute)
        context = _context(datasource, allowed_columns=["id"])

        try:
            with use_identity_context(_identity()), use_data_probe_context(context):
                tool = build_data_probe_tools(_prepared())[0]

                # Act
                result = await tool.ainvoke({
                    "operation": "table_sample", "table": "t_workshop", "columns": ["id"],
                })

            # Assert
            assert result["success"] is False
            assert result["error_code"] == "column_not_allowed"
            assert result["rows"] == []
            assert executed == []
        finally:
            await engine.dispose()
