"""SQL 画像与结构化探查服务测试，覆盖功能 29.1 与 29.2（设计 spec/55 §5、§9、§14）。

真实 SQLite 临时库用于离线验收采集语义，MySQL/PostgreSQL/ClickHouse 用
sqlglot AST 断言方言渲染；所有用例不访问真实业务数据库和远程模型。
"""

from __future__ import annotations

import asyncio
import json
import traceback
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
import sqlalchemy as sa
from pydantic import ValidationError
from sqlalchemy.ext.asyncio import create_async_engine

import src.datasource.profiling as profiling_module
from src.api.auth import use_identity_context
from src.connectors.sqlite import SQLiteConnector
from src.datasource.config import DataSourceConfig
from src.datasource.profiling import (
    DataProfiler,
    ProbeRequest,
    ProfilingContext,
    get_data_profiler,
    render_profile,
)
from src.datasource.schema_snapshot import ColumnInfo, SchemaSnapshot, TableSchema
from src.security.sql_execution import SQLExecutionResult, normalize_sql_dialect
from src.security.tenant_policy import RequestIdentity


# 方法作用：把数据画像相关配置替换成测试可控的独立快照。
# Args: monkeypatch - pytest 补丁工具；overrides - 需要覆盖的配置键值。
# Returns: 被替换的配置对象。
def _patch_settings(monkeypatch, **overrides):
    settings = SimpleNamespace(**overrides)
    monkeypatch.setattr(profiling_module, "get_settings", lambda: settings)
    return settings


# 方法作用：构造带画像样本的真实 SQLite 内存数据源。
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
            "CREATE TABLE t_factory (factory_id INTEGER, factory_name TEXT, region TEXT)"
        ))
        await connection.execute(sa.text(
            "INSERT INTO t_factory (factory_id, factory_name, region) VALUES "
            "(1,'华东总厂','华东'),(2,'华北分厂',NULL)"
        ))
        await connection.execute(sa.text("CREATE TABLE t_empty (id INTEGER, note TEXT)"))
        await connection.execute(sa.text("CREATE TABLE t_constant (flag TEXT, code TEXT)"))
        await connection.execute(sa.text(
            "INSERT INTO t_constant (flag, code) VALUES ('ON','A'),('ON','B'),('ON','C')"
        ))
    datasource = DataSourceConfig(
        name="probe-sqlite",
        dialect="sqlite",
        mode="embedded",
        database=":memory:",
        engine=engine,
    )
    datasource.connector = SQLiteConnector(datasource).attach_engine(engine)
    datasource.schema = _schema_snapshot()
    return datasource, engine


# 方法作用：构造与样本库一致的表结构快照。
# Args: 无。
# Returns: SchemaSnapshot。
def _schema_snapshot() -> SchemaSnapshot:
    return SchemaSnapshot(tables=[
        TableSchema(name="t_workshop", columns=[
            ColumnInfo("id", "INTEGER", is_primary_key=True),
            ColumnInfo("workshop_code", "TEXT"),
            ColumnInfo("workshop_name", "TEXT"),
            ColumnInfo("factory_id", "INTEGER"),
            ColumnInfo("region", "TEXT"),
        ], row_count_estimate=6),
        TableSchema(name="t_factory", columns=[
            ColumnInfo("factory_id", "INTEGER"),
            ColumnInfo("factory_name", "TEXT"),
            ColumnInfo("region", "TEXT"),
        ], row_count_estimate=2),
        TableSchema(name="t_empty", columns=[
            ColumnInfo("id", "INTEGER"),
            ColumnInfo("note", "TEXT"),
        ], row_count_estimate=0),
        TableSchema(name="t_constant", columns=[
            ColumnInfo("flag", "TEXT"),
            ColumnInfo("code", "TEXT"),
        ], row_count_estimate=3),
    ])


# 方法作用：构造服务端画像上下文。
# Args: datasource - 兼容对象（name/dialect）；tables - 授权表结构；kwargs - 身份、行列策略与指纹覆盖。
# Returns: ProfilingContext。
def _context(datasource, tables: dict | None = None, **kwargs) -> ProfilingContext:
    return ProfilingContext(
        tenant_id=kwargs.pop("tenant_id", 3),
        user_id=kwargs.pop("user_id", 7),
        role=kwargs.pop("role", "analyst"),
        datasource=datasource.name,
        dialect=datasource.dialect,
        tables=tables if tables is not None else {
            table.name: table for table in _schema_snapshot().tables
        },
        **kwargs,
    )


# 方法作用：把 registry 替换成只暴露当前数据源的测试替身。
# Args: monkeypatch - pytest 补丁工具；datasource - 被测数据源；list_all_error - 模拟权限存储故障。
# Returns: 替换后的 registry。
def _patch_registry(monkeypatch, datasource, *, list_all_error: Exception | None = None):
    registry = SimpleNamespace(
        resolve=AsyncMock(return_value=datasource),
        list_all=(
            AsyncMock(side_effect=list_all_error)
            if list_all_error is not None
            else AsyncMock(return_value=[{"name": datasource.name, "dialect": datasource.dialect}])
        ),
    )
    monkeypatch.setattr("src.datasource.registry.get_registry", lambda: registry)
    return registry


# 方法作用：构造一次成功的统一执行结果。
# Args: sql - 已执行 SQL；rows - 结果行；truncated - 是否截断；dialect - 方言。
# Returns: SQLExecutionResult。
def _execution(sql: str, rows: list[dict], truncated: bool = False, dialect: str = "sqlite") -> SQLExecutionResult:
    return SQLExecutionResult(
        success=True, sql=sql, dialect=dialect, data=rows,
        row_count=len(rows), truncated=truncated,
    )


# 方法作用：把统一执行器替换成返回固定行并记录 SQL 的假实现。
# Args: monkeypatch - pytest 补丁工具；handler - 接收 SQL 返回 SQLExecutionResult 的回调。
# Returns: 记录执行 SQL 的列表。
def _patch_executor(monkeypatch, handler):
    calls: list[str] = []

    async def fake(sql, datasource, dialect="", *, explain=True, max_rows=None, tenant_id=None):
        calls.append(sql)
        return handler(sql)

    monkeypatch.setattr(profiling_module, "validate_and_execute_sql", fake)
    return calls


# 方法作用：为画像服务用例绑定与默认上下文一致的真实请求身份。
# Args: 无。
# Returns: 上下文管理器；用例退出后精确恢复原身份。
@pytest.fixture(autouse=True)
def _bound_profiling_identity():
    """服务入口会核对执行期真实身份，用例必须像生产一样显式绑定。"""
    identity = RequestIdentity(tenant_id=3, user_id=7, role="analyst")
    with use_identity_context(identity):
        yield


class TestProfilingContracts:
    """覆盖 29.1 的画像契约：上下文、请求与快照字段。"""

    # 方法作用：上下文拒绝模型侧可替换身份/权限的未知字段。
    # Args: 无。
    # Returns: 无返回值。
    def test_context_rejects_unknown_field(self) -> None:
        """ProfilingContext 不接受身份与权限之外的额外字段，防止模型注入。"""
        context = _context(SimpleNamespace(name="probe-sqlite", dialect="sqlite"))
        assert context.datasource == "probe-sqlite"
        with pytest.raises(ValidationError):
            ProfilingContext(
                tenant_id=1, user_id=2, datasource="ds", dialect="sqlite",
                tables={}, tenant_scope="all",
            )

    # 方法作用：上下文身份字段必须是非负整数，数据源名不得为空。
    # Args: 无。
    # Returns: 无返回值。
    def test_context_identity_bounds(self) -> None:
        """负数身份或空数据源直接拒绝构造。"""
        with pytest.raises(ValidationError):
            ProfilingContext(tenant_id=1, user_id=-1, datasource="ds")
        with pytest.raises(ValidationError):
            ProfilingContext(tenant_id=1, user_id=7, datasource="")

    # 方法作用：探查请求只接受五种固定原语，未知操作被拒绝。
    # Args: 无。
    # Returns: 无返回值。
    def test_probe_request_rejects_unknown_operation(self) -> None:
        """模型不得提交自由 SQL 或自定义操作名。"""
        with pytest.raises(ValidationError):
            ProbeRequest(operation="raw_sql", table="t_workshop")
        with pytest.raises(ValidationError):
            ProbeRequest(operation="value_domain", table="t_workshop", sql="SELECT 1")

    # 方法作用：探查请求拒绝身份、权限与数据源切换字段。
    # Args: 无。
    # Returns: 无返回值。
    def test_probe_request_rejects_identity_fields(self) -> None:
        """工具参数不能替换身份或数据源。"""
        for payload in (
            {"operation": "value_domain", "table": "t_workshop", "column": "id", "tenant_id": 9},
            {"operation": "value_domain", "table": "t_workshop", "column": "id", "user_id": 9},
            {"operation": "value_domain", "table": "t_workshop", "column": "id", "role": "super_admin"},
            {"operation": "value_domain", "table": "t_workshop", "column": "id", "datasource": "other"},
            {"operation": "value_domain", "table": "t_workshop", "column": "id", "row_filter_sql": "1=1"},
        ):
            with pytest.raises(ValidationError):
                ProbeRequest(**payload)

    # 方法作用：每种原语的必需字段缺失时给出明确拒绝。
    # Args: 无。
    # Returns: 无返回值。
    def test_probe_request_requires_operation_fields(self) -> None:
        """单列原语缺 column、entity_lookup 缺 pattern、table_sample 缺 columns 都拒绝。"""
        with pytest.raises(ValidationError):
            ProbeRequest(operation="value_domain", table="t_workshop")
        with pytest.raises(ValidationError):
            ProbeRequest(operation="entity_lookup", table="t_workshop", column="region")
        with pytest.raises(ValidationError):
            ProbeRequest(operation="table_sample", table="t_workshop")
        parsed = ProbeRequest(operation="table_sample", table="t_workshop", columns=[" id ", "region"])
        assert parsed.columns == ["id", "region"]

    # 方法作用：画像服务是进程内可复用实例，get_data_profiler 返回同一对象。
    # Args: 无。
    # Returns: 无返回值。
    def test_get_data_profiler_is_shared_instance(self) -> None:
        """共享单例避免每次调用重建缓存与锁。"""
        assert get_data_profiler() is get_data_profiler()
        assert isinstance(get_data_profiler(), DataProfiler)


class TestProfilingCollection:
    """覆盖 29.1 的有界多方言采集与唯一性判定。"""

    # 方法作用：真实 SQLite 上验证完整可见数据的唯一/重复/常量结论。
    # Args: monkeypatch - pytest 补丁工具。
    # Returns: 无返回值。
    async def test_profile_sqlite_scope_and_uniqueness(self, monkeypatch) -> None:
        """完整数据非空无 NULL 才唯一；常量列与重复列绝不标记唯一。"""
        # Arrange
        datasource, engine = await _sample_db()
        _patch_registry(monkeypatch, datasource)
        _patch_settings(monkeypatch, sql_profiling_sample_rows=100)
        profiler = DataProfiler()

        try:
            # Act
            snapshot = await profiler.profile(_context(datasource), {
                "t_workshop": ["id", "workshop_code", "workshop_name", "factory_id"],
                "t_constant": ["flag", "code"],
            })

            # Assert
            assert snapshot.errors == []
            assert snapshot.query_count == 2
            tables = {table.name: table for table in snapshot.tables}
            workshop = tables["t_workshop"]
            assert workshop.scope == "complete"
            assert workshop.sampled_rows == 6
            columns = {column.name: column for column in workshop.columns}
            assert columns["id"].uniqueness == "unique"
            assert columns["id"].distinct_count == 6
            assert columns["workshop_code"].uniqueness == "unique"
            assert columns["workshop_name"].uniqueness == "duplicate"
            assert columns["workshop_name"].distinct_count == 3
            assert columns["workshop_name"].top_frequency == 2
            assert columns["workshop_name"].constant is False
            constant = {column.name: column for column in tables["t_constant"].columns}
            assert constant["flag"].uniqueness == "duplicate"
            assert constant["flag"].constant is True
            assert constant["flag"].top_frequency == 3
            assert constant["code"].uniqueness == "unique"
        finally:
            await engine.dispose()

    # 方法作用：空表与含 NULL 列的判定不得提升为唯一。
    # Args: monkeypatch - pytest 补丁工具。
    # Returns: 无返回值。
    async def test_profile_empty_table_and_null_column(self, monkeypatch) -> None:
        """空表与全 NULL 都是 unknown；含 NULL 且无重复也不能唯一。"""
        # Arrange
        datasource, engine = await _sample_db()
        _patch_registry(monkeypatch, datasource)
        _patch_settings(monkeypatch, sql_profiling_sample_rows=100)
        profiler = DataProfiler()

        try:
            # Act
            snapshot = await profiler.profile(_context(datasource), {
                "t_empty": ["id", "note"],
                "t_factory": ["factory_id", "region"],
            })

            # Assert
            assert snapshot.errors == []
            tables = {table.name: table for table in snapshot.tables}
            empty = {column.name: column for column in tables["t_empty"].columns}
            assert empty["id"].uniqueness == "unknown"
            assert empty["id"].distinct_count == 0
            assert tables["t_empty"].scope == "complete"
            factory = {column.name: column for column in tables["t_factory"].columns}
            assert factory["factory_id"].uniqueness == "unique"
            assert factory["factory_id"].constant is False
            # 含 NULL 的列即使非空部分无重复也不能标记唯一
            assert factory["region"].null_count == 1
            assert factory["region"].uniqueness == "unknown"
        finally:
            await engine.dispose()

    # 方法作用：样本触顶即 scope=sample，且样本内无重复只能判 unknown。
    # Args: monkeypatch - pytest 补丁工具。
    # Returns: 无返回值。
    async def test_profile_sample_scope_blocks_unique_claim(self, monkeypatch) -> None:
        """样本无重复不得提升为全表唯一。"""
        # Arrange
        datasource, engine = await _sample_db()
        _patch_registry(monkeypatch, datasource)
        _patch_settings(monkeypatch, sql_profiling_sample_rows=3)
        profiler = DataProfiler()

        try:
            # Act
            snapshot = await profiler.profile(_context(datasource), {"t_workshop": ["id"]})

            # Assert
            table = snapshot.tables[0]
            assert table.scope == "sample"
            assert table.sampled_rows == 3
            assert table.columns[0].uniqueness == "unknown"
            assert table.columns[0].scope == "sample"
        finally:
            await engine.dispose()

    # 方法作用：执行器返回 truncated 时，即使行数未超样本上限也不能标 complete。
    # Args: monkeypatch - pytest 补丁工具。
    # Returns: 无返回值。
    async def test_profile_truncated_result_is_sample(self, monkeypatch) -> None:
        """不因服务端截断而把统计范围标成完整。"""
        # Arrange
        datasource = SimpleNamespace(name="probe-sqlite", dialect="sqlite")
        _patch_registry(monkeypatch, datasource)
        _patch_settings(monkeypatch, sql_profiling_sample_rows=100)
        _patch_executor(
            monkeypatch,
            lambda sql: _execution(sql, [{"id": 1}, {"id": 2}], truncated=True),
        )
        profiler = DataProfiler()

        # Act
        snapshot = await profiler.profile(_context(datasource), {"t_workshop": ["id"]})

        # Assert
        assert snapshot.tables[0].scope == "sample"
        assert snapshot.tables[0].columns[0].uniqueness == "unknown"

    # 方法作用：每表只发一次有界列投影查询，不使用整表 GROUP BY。
    # Args: monkeypatch - pytest 补丁工具。
    # Returns: 无返回值。
    async def test_profile_uses_single_bounded_projection(self, monkeypatch) -> None:
        """一表一查、投影授权列、行数上限为样本行数 + 1。"""
        # Arrange
        datasource = SimpleNamespace(name="probe-sqlite", dialect="sqlite")
        _patch_registry(monkeypatch, datasource)
        _patch_settings(monkeypatch, sql_profiling_sample_rows=5)
        calls = _patch_executor(
            monkeypatch,
            lambda sql: _execution(sql, [{"id": 1, "region": "华东"}]),
        )
        profiler = DataProfiler()

        # Act
        await profiler.profile(_context(datasource), {"t_workshop": ["id", "region"]})

        # Assert
        import sqlglot
        from sqlglot import exp

        assert len(calls) == 1
        assert "GROUP BY" not in calls[0].upper()
        tree = sqlglot.parse_one(calls[0], read="sqlite")
        assert isinstance(tree, exp.Select)
        assert [column.name for column in tree.find_all(exp.Column)] == ["id", "region"]
        assert tree.args["limit"].expression.this == "6"

    # 方法作用：MySQL/PostgreSQL/ClickHouse 按方言渲染受限查询。
    # Args: monkeypatch - pytest 补丁工具；dialect - 目标方言。
    # Returns: 无返回值。
    @pytest.mark.parametrize("dialect", ["mysql", "postgresql", "clickhouse"])
    async def test_profile_dialect_ast_rendering(self, monkeypatch, dialect: str) -> None:
        """三方言渲染必须保留投影、LIMIT 与标识符引用。"""
        # Arrange
        import sqlglot
        from sqlglot import exp

        datasource = SimpleNamespace(name=f"probe-{dialect}", dialect=dialect)
        _patch_registry(monkeypatch, datasource)
        _patch_settings(monkeypatch, sql_profiling_sample_rows=9)
        calls = _patch_executor(
            monkeypatch,
            lambda sql: _execution(sql, [{"id": 1}], dialect=dialect),
        )
        profiler = DataProfiler()
        context = _context(SimpleNamespace(name=datasource.name, dialect=dialect))

        # Act
        await profiler.profile(context, {"t_workshop": ["id"]})

        # Assert
        assert len(calls) == 1
        tree = sqlglot.parse_one(calls[0], read=normalize_sql_dialect(dialect))
        assert isinstance(tree, exp.Select)
        table = tree.find(exp.Table)
        assert table.name == "t_workshop"
        assert [column.name for column in tree.find_all(exp.Column)] == ["id"]
        assert int(tree.args["limit"].expression.this) == 10

    # 方法作用：列数超上限时按请求顺序裁剪。
    # Args: monkeypatch - pytest 补丁工具。
    # Returns: 无返回值。
    async def test_profile_trims_columns_per_table(self, monkeypatch) -> None:
        """每表画像列数受 sql_profiling_max_columns_per_table 限制。"""
        # Arrange
        datasource, engine = await _sample_db()
        _patch_registry(monkeypatch, datasource)
        _patch_settings(monkeypatch, sql_profiling_sample_rows=100, sql_profiling_max_columns_per_table=2)
        profiler = DataProfiler()

        try:
            # Act
            snapshot = await profiler.profile(
                _context(datasource),
                {"t_workshop": ["id", "workshop_code", "workshop_name", "factory_id"]},
            )

            # Assert
            assert [column.name for column in snapshot.tables[0].columns] == ["id", "workshop_code"]
        finally:
            await engine.dispose()

    # 方法作用：画像文本带完整/样本语义，样本结论不得表述为总体唯一。
    # Args: monkeypatch - pytest 补丁工具。
    # Returns: 无返回值。
    async def test_render_profile_keeps_scope_semantics(self, monkeypatch) -> None:
        """complete 与 sample 的措辞必须可区分。"""
        # Arrange
        datasource, engine = await _sample_db()
        _patch_registry(monkeypatch, datasource)
        _patch_settings(monkeypatch, sql_profiling_sample_rows=100)
        profiler = DataProfiler()
        try:
            complete_snapshot = await profiler.profile(
                _context(datasource, schema_fingerprint="complete"),
                {"t_workshop": ["id", "workshop_name"]},
            )
        finally:
            await engine.dispose()

        # Act
        text = render_profile(complete_snapshot)

        # Assert
        assert "完整" in text
        assert "唯一" in text
        assert "workshop_name" in text and "不唯一" in text

        # Arrange：样本范围下的渲染
        sample_snapshot = complete_snapshot.model_copy(deep=True)
        sample_snapshot.tables[0].scope = "sample"
        sample_snapshot.tables[0].columns[0].scope = "sample"
        sample_snapshot.tables[0].columns[0].uniqueness = "unknown"

        # Act
        sample_text = render_profile(sample_snapshot)

        # Assert
        assert "样本" in sample_text
        assert "唯一性未知" in sample_text
        assert "不得当作全表唯一" in sample_text


class TestProfilingSecurity:
    """覆盖 29.1 的执行前授权复核、列白名单与行过滤。"""

    # 方法作用：投影列超出列白名单时拒绝该表且不发起查询。
    # Args: monkeypatch - pytest 补丁工具。
    # Returns: 无返回值。
    async def test_profile_rejects_columns_outside_whitelist(self, monkeypatch) -> None:
        """allowed_columns 先于投影检查，越权列不得进入 SQL。"""
        # Arrange
        datasource = SimpleNamespace(name="probe-sqlite", dialect="sqlite")
        _patch_registry(monkeypatch, datasource)
        _patch_settings(monkeypatch, sql_profiling_sample_rows=100)
        calls = _patch_executor(monkeypatch, lambda sql: _execution(sql, []))
        profiler = DataProfiler()

        # Act
        snapshot = await profiler.profile(
            _context(datasource, allowed_columns=["id"]),
            {"t_workshop": ["id", "workshop_name"]},
        )

        # Assert
        assert snapshot.query_count == 0
        assert snapshot.tables[0].error == "column_not_allowed"
        assert calls == []
        assert "workshop_name" not in json.dumps(snapshot.model_dump(), ensure_ascii=False, default=str)

    # 方法作用：未知表与未知列被拒绝且不进入执行通道。
    # Args: monkeypatch - pytest 补丁工具。
    # Returns: 无返回值。
    async def test_profile_rejects_unknown_table_and_column(self, monkeypatch) -> None:
        """表名必须命中授权结构，列名必须存在于授权表结构内。"""
        # Arrange
        datasource = SimpleNamespace(name="probe-sqlite", dialect="sqlite")
        _patch_registry(monkeypatch, datasource)
        _patch_settings(monkeypatch, sql_profiling_sample_rows=100)
        calls = _patch_executor(monkeypatch, lambda sql: _execution(sql, []))
        profiler = DataProfiler()

        # Act
        snapshot = await profiler.profile(_context(datasource), {
            "t_workshop; DROP TABLE t_workshop": ["id"],
            "t_workshop": ["id", "not_a_column"],
        })

        # Assert
        assert snapshot.query_count == 0
        codes = {table.name: table.error for table in snapshot.tables}
        assert codes["t_workshop; DROP TABLE t_workshop"] == "unknown_table"
        assert codes["t_workshop"] == "unknown_column"
        assert calls == []

    # 方法作用：行过滤在取样前生效，且不受列白名单限制。
    # Args: monkeypatch - pytest 补丁工具。
    # Returns: 无返回值。
    async def test_row_filter_applies_without_policy_column_leak(self, monkeypatch) -> None:
        """策略列（factory_id）不在白名单也要参与 WHERE，但不得进入投影与结果。"""
        # Arrange
        datasource, engine = await _sample_db()
        _patch_registry(monkeypatch, datasource)
        _patch_settings(monkeypatch, sql_profiling_sample_rows=100)
        profiler = DataProfiler()

        try:
            # Act
            snapshot = await profiler.profile(
                _context(
                    datasource,
                    allowed_columns=["id", "workshop_name"],
                    row_filter_sql="factory_id = 1",
                ),
                {"t_workshop": ["id", "workshop_name"]},
            )

            # Assert：只统计工厂 1 的行，且输出列不含策略列
            assert snapshot.errors == []
            table = snapshot.tables[0]
            assert table.sampled_rows == 3
            assert [column.name for column in table.columns] == ["id", "workshop_name"]
            assert "factory_id" not in json.dumps(snapshot.model_dump(), ensure_ascii=False, default=str)
        finally:
            await engine.dispose()

    # 方法作用：未知身份的上下文被拒绝且零查询。
    # Args: monkeypatch - pytest 补丁工具。
    # Returns: 无返回值。
    async def test_anonymous_context_is_rejected(self, monkeypatch) -> None:
        """user_id=0 的上下文不得触发任何采样。"""
        # Arrange
        datasource = SimpleNamespace(name="probe-sqlite", dialect="sqlite")
        _patch_registry(monkeypatch, datasource)
        calls = _patch_executor(monkeypatch, lambda sql: _execution(sql, []))
        profiler = DataProfiler()

        # Act
        snapshot = await profiler.profile(
            _context(datasource, user_id=0),
            {"t_workshop": ["id"]},
        )

        # Assert
        assert snapshot.query_count == 0
        assert snapshot.errors == ["forbidden"]
        assert calls == []

    # 方法作用：授权或权限存储不可用时失败关闭。
    # Args: monkeypatch - pytest 补丁工具。
    # Returns: 无返回值。
    async def test_policy_failure_fails_closed(self, monkeypatch) -> None:
        """权限解析异常不得降级为无过滤采样。"""
        # Arrange
        datasource = SimpleNamespace(name="probe-sqlite", dialect="sqlite")
        _patch_registry(monkeypatch, datasource, list_all_error=RuntimeError("permission store down"))
        calls = _patch_executor(monkeypatch, lambda sql: _execution(sql, []))
        profiler = DataProfiler()

        # Act
        snapshot = await profiler.profile(_context(datasource), {"t_workshop": ["id"]})

        # Assert
        assert snapshot.query_count == 0
        assert snapshot.errors == ["policy_unavailable"]
        assert calls == []

    # 方法作用：数据源不在本次授权结果中时拒绝执行。
    # Args: monkeypatch - pytest 补丁工具。
    # Returns: 无返回值。
    async def test_unauthorized_datasource_is_rejected(self, monkeypatch) -> None:
        """registry 摘要里没有该数据源时直接拒绝。"""
        # Arrange
        datasource = SimpleNamespace(name="probe-sqlite", dialect="sqlite")
        registry = SimpleNamespace(
            resolve=AsyncMock(return_value=datasource),
            list_all=AsyncMock(return_value=[{"name": "another-ds", "dialect": "mysql"}]),
        )
        monkeypatch.setattr("src.datasource.registry.get_registry", lambda: registry)
        calls = _patch_executor(monkeypatch, lambda sql: _execution(sql, []))
        profiler = DataProfiler()

        # Act
        snapshot = await profiler.profile(_context(datasource), {"t_workshop": ["id"]})

        # Assert
        assert snapshot.query_count == 0
        assert snapshot.errors == ["policy_unavailable"]
        assert calls == []

    # 方法作用：只对受控且可超时的方言执行，同步驱动明确降级。
    # Args: monkeypatch - pytest 补丁工具。
    # Returns: 无返回值。
    async def test_unsupported_dialect_and_sync_driver_degrade(self, monkeypatch) -> None:
        """Oracle 与同步引擎都不进入执行通道。"""
        # Arrange
        _patch_settings(monkeypatch, sql_profiling_sample_rows=100)
        calls = _patch_executor(monkeypatch, lambda sql: _execution(sql, []))
        profiler = DataProfiler()

        oracle = SimpleNamespace(name="oracle-ds", dialect="oracle", engine=None, connector=None)
        _patch_registry(monkeypatch, oracle)

        # Act
        oracle_snapshot = await profiler.profile(_context(oracle), {"t_workshop": ["id"]})

        # Assert
        assert oracle_snapshot.errors == ["unsupported_dialect"]
        assert oracle_snapshot.query_count == 0
        assert calls == []

        # Arrange：同步 SQLAlchemy 引擎（pysqlite 驱动不可取消）
        sync_engine = sa.create_engine("sqlite:///:memory:")
        sync_datasource = SimpleNamespace(
            name="sync-sqlite", dialect="sqlite", engine=sync_engine, connector=None,
        )
        _patch_registry(monkeypatch, sync_datasource)

        try:
            # Act
            sync_snapshot = await profiler.profile(_context(sync_datasource), {"t_workshop": ["id"]})

            # Assert
            assert sync_snapshot.errors == ["synchronous_driver"]
            assert sync_snapshot.query_count == 0
            assert calls == []
        finally:
            sync_engine.dispose()

    # 方法作用：执行失败的错误码不回显原始异常或 SQL。
    # Args: monkeypatch - pytest 补丁工具。
    # Returns: 无返回值。
    async def test_execution_error_is_scrubbed(self, monkeypatch) -> None:
        """失败只保留脱敏代码，快照不得携带 SQL 原文。"""
        # Arrange
        datasource = SimpleNamespace(name="probe-sqlite", dialect="sqlite")
        _patch_registry(monkeypatch, datasource)
        _patch_settings(monkeypatch, sql_profiling_sample_rows=100)

        def handler(sql):
            return SQLExecutionResult(
                success=False, sql=sql, dialect="sqlite",
                error="SELECT secret_token FROM t_secret WHERE id=1",
                error_type="execution",
                details=[{"message": "near \"secret\": syntax error"}],
            )

        _patch_executor(monkeypatch, handler)
        profiler = DataProfiler()

        # Act
        snapshot = await profiler.profile(_context(datasource), {"t_workshop": ["id"]})

        # Assert
        assert snapshot.errors == ["execution_error"]
        payload = json.dumps(snapshot.model_dump(), ensure_ascii=False, default=str)
        assert "secret_token" not in payload
        assert "t_secret" not in payload


class TestProfilingFailureObservability:
    """覆盖 29.1/29.2 失败边界可观测性：执行器漏出的异常保留一次脱敏堆栈。

    统一执行器已处理并返回 success=False 的降级分支不重复堆栈；只有真正漏出
    执行器的异常才在本边界记录，且异常正文中的 SQL/数据/凭证不得进入日志。
    """

    # 方法作用：高频正常查询不产生任何成功日志。
    # Args: self - 测试实例；monkeypatch - pytest 补丁工具。
    # Returns: 断言通过时无返回值。
    async def test_normal_calls_emit_no_success_logs(self, monkeypatch) -> None:
        """连续成功执行不得新增日志事件，尤其不能新增逐层成功 INFO。"""
        # Arrange
        from structlog.testing import capture_logs

        datasource = SimpleNamespace(name="probe-sqlite", dialect="sqlite")
        _patch_executor(monkeypatch, lambda sql: _execution(sql, [{"id": 1}]))
        profiler = DataProfiler()
        context = _context(datasource)

        # Act
        with capture_logs() as events:
            for _ in range(5):
                rows, truncated, code = await profiler._run_bounded_query(  # noqa: SLF001 - 直接验证有界查询边界
                    context, "SELECT id FROM t_workshop", 10, 1.0,
                )

        # Assert
        assert events == []
        assert (rows, truncated, code) == ([{"id": 1}], False, "")

    # 方法作用：执行器漏出的异常保留堆栈与 error_type，模拟秘密不入日志。
    # Args: self - 测试实例；monkeypatch - pytest 补丁工具。
    # Returns: 断言通过时无返回值。
    async def test_leaked_exception_keeps_stack_without_secret(self, monkeypatch) -> None:
        """堆栈必须可排障，异常正文中的模拟凭证不得进入任何日志字段。"""
        # Arrange
        from structlog.testing import capture_logs

        secret = "SECRET-模拟凭证-abc123"
        datasource = SimpleNamespace(name="probe-sqlite", dialect="sqlite")

        # 方法作用：模拟统一执行器自身抛错（而非返回 success=False）的真实漏出。
        # Args: 与 validate_and_execute_sql 契约一致的调用参数。
        # Returns: 永不返回，直接抛出含模拟秘密的异常。
        async def leaking(sql, datasource_name, dialect="", *, explain=True, max_rows=None, tenant_id=None):
            """抛出携带模拟秘密的运行时异常，用于验证日志脱敏。"""
            raise RuntimeError(f"执行器未接住: {secret}")

        monkeypatch.setattr(profiling_module, "validate_and_execute_sql", leaking)
        profiler = DataProfiler()
        context = _context(datasource)

        # Act
        with capture_logs() as events:
            rows, truncated, code = await profiler._run_bounded_query(  # noqa: SLF001 - 直接验证有界查询边界
                context, "SELECT id FROM t_workshop", 10, 1.0,
            )

        # Assert
        assert (rows, truncated, code) == ([], False, "execution_error")
        assert len(events) == 1
        event = events[0]
        assert event["event"] == "数据画像查询异常"
        assert event["error_type"] == "RuntimeError"
        stack_text = "".join(traceback.format_exception(*event["exc_info"]))
        # 原始调用帧与固定中文文案都在，异常类型同时以结构化字段落盘。
        assert "leaking" in stack_text
        assert "数据画像查询边界捕获未处理异常" in stack_text
        assert secret not in stack_text
        assert secret not in repr(events)

    # 方法作用：单次失败只留一条堆栈，后续调用恢复且不重复记录。
    # Args: self - 测试实例；monkeypatch - pytest 补丁工具。
    # Returns: 断言通过时无返回值。
    async def test_repeated_failures_and_recovery_log_single_stack(self, monkeypatch) -> None:
        """一次失败对应一条堆栈；健康调用继续成功且不产生新日志。"""
        # Arrange
        from structlog.testing import capture_logs

        datasource = SimpleNamespace(name="probe-sqlite", dialect="sqlite")
        remaining_failures = ["boom"]

        # 方法作用：首次调用抛错，之后恢复为成功结果。
        # Args: 与 validate_and_execute_sql 契约一致的调用参数。
        # Returns: 成功时的 SQLExecutionResult。
        async def flaky(sql, datasource_name, dialect="", *, explain=True, max_rows=None, tenant_id=None):
            """仅第一次抛出异常，后续调用返回成功结果以验证恢复。"""
            if remaining_failures:
                remaining_failures.pop()
                raise RuntimeError("首次失败")
            return _execution(sql, [{"id": 1}])

        monkeypatch.setattr(profiling_module, "validate_and_execute_sql", flaky)
        profiler = DataProfiler()
        context = _context(datasource)

        # Act
        with capture_logs() as events:
            first = await profiler._run_bounded_query(  # noqa: SLF001 - 直接验证有界查询边界
                context, "SELECT id FROM t_workshop", 10, 1.0,
            )
            second = await profiler._run_bounded_query(  # noqa: SLF001 - 直接验证有界查询边界
                context, "SELECT id FROM t_workshop", 10, 1.0,
            )
            third = await profiler._run_bounded_query(  # noqa: SLF001 - 直接验证有界查询边界
                context, "SELECT id FROM t_workshop", 10, 1.0,
            )

        # Assert
        assert first == ([], False, "execution_error")
        assert second == ([{"id": 1}], False, "")
        assert third == ([{"id": 1}], False, "")
        assert len(events) == 1
        stack_text = "".join(traceback.format_exception(*events[0]["exc_info"]))
        assert stack_text.count("数据画像查询边界捕获未处理异常") == 1


class TestProfilingBudgetAndCache:
    """覆盖 29.2 的查询/耗时预算、缓存隔离与取消。"""

    # 方法作用：预取查询数受上限约束，超出部分返回明确预算码。
    # Args: monkeypatch - pytest 补丁工具。
    # Returns: 无返回值。
    async def test_query_budget_limits_tables(self, monkeypatch) -> None:
        """sql_profiling_max_queries 决定真实查询上限。"""
        # Arrange
        datasource = SimpleNamespace(name="probe-sqlite", dialect="sqlite")
        _patch_registry(monkeypatch, datasource)
        _patch_settings(
            monkeypatch,
            sql_profiling_sample_rows=100,
            sql_profiling_max_queries=1,
            sql_profiling_max_tables=6,
        )
        calls = _patch_executor(monkeypatch, lambda sql: _execution(sql, [{"id": 1}]))
        profiler = DataProfiler()

        # Act
        snapshot = await profiler.profile(_context(datasource), {
            "t_workshop": ["id"], "t_factory": ["factory_id"],
        })

        # Assert
        assert len(calls) == 1
        assert snapshot.query_count == 1
        assert "query_budget" in snapshot.errors

    # 方法作用：总耗时预算耗尽后停止后续表并给出明确代码。
    # Args: monkeypatch - pytest 补丁工具。
    # Returns: 无返回值。
    async def test_total_time_budget_stops_prefetch(self, monkeypatch) -> None:
        """sql_profiling_total_seconds 覆盖全部预取。"""
        # Arrange
        datasource = SimpleNamespace(name="probe-sqlite", dialect="sqlite")
        _patch_registry(monkeypatch, datasource)
        _patch_settings(
            monkeypatch,
            sql_profiling_sample_rows=100,
            sql_profiling_total_seconds=0.06,
            sql_profiling_timeout_seconds=1.0,
        )
        clock = {"now": 0.0}
        monkeypatch.setattr(profiling_module, "_now", lambda: clock["now"])
        calls: list[str] = []

        async def slow(sql, datasource_name, dialect="", *, explain=True, max_rows=None, tenant_id=None):
            calls.append(sql)
            clock["now"] += 0.04
            return _execution(sql, [{"id": 1}])

        monkeypatch.setattr(profiling_module, "validate_and_execute_sql", slow)
        profiler = DataProfiler()

        # Act
        snapshot = await profiler.profile(_context(datasource), {
            "t_workshop": ["id"], "t_factory": ["factory_id"], "t_constant": ["flag"],
        })

        # Assert
        assert len(calls) == 2
        assert snapshot.query_count == 2
        assert "total_timeout" in snapshot.errors

    # 方法作用：单次查询超时返回脱敏超时码而不是抛出。
    # Args: monkeypatch - pytest 补丁工具。
    # Returns: 无返回值。
    async def test_single_query_timeout_degrades(self, monkeypatch) -> None:
        """单次超时按表降级，不影响其余表的统计。"""
        # Arrange
        datasource = SimpleNamespace(name="probe-sqlite", dialect="sqlite")
        _patch_registry(monkeypatch, datasource)
        _patch_settings(
            monkeypatch,
            sql_profiling_sample_rows=100,
            sql_profiling_timeout_seconds=0.02,
            sql_profiling_total_seconds=5.0,
        )

        async def hanging(sql, datasource_name, dialect="", *, explain=True, max_rows=None, tenant_id=None):
            await asyncio.sleep(5)
            return _execution(sql, [])

        monkeypatch.setattr(profiling_module, "validate_and_execute_sql", hanging)
        profiler = DataProfiler()

        # Act
        snapshot = await profiler.profile(_context(datasource), {"t_workshop": ["id"]})

        # Assert
        assert snapshot.errors == ["timeout"]
        assert snapshot.tables[0].error == "timeout"

    # 方法作用：缓存命中零查询，且返回副本不污染缓存。
    # Args: monkeypatch - pytest 补丁工具。
    # Returns: 无返回值。
    async def test_cache_hit_zero_query_and_returns_copy(self, monkeypatch) -> None:
        """同一上下文重复调用只执行一次查询。"""
        # Arrange
        datasource = SimpleNamespace(name="probe-sqlite", dialect="sqlite")
        _patch_registry(monkeypatch, datasource)
        _patch_settings(monkeypatch, sql_profiling_sample_rows=100)
        calls = _patch_executor(monkeypatch, lambda sql: _execution(sql, [{"id": 1}, {"id": 2}]))
        profiler = DataProfiler()
        context = _context(datasource, schema_fingerprint="fp-1")

        # Act
        first = await profiler.profile(context, {"t_workshop": ["id"]})
        first.tables[0].columns[0].name = "tampered"
        second = await profiler.profile(context, {"t_workshop": ["id"]})

        # Assert
        assert len(calls) == 1
        assert second.tables[0].columns[0].name == "id"
        assert second.query_count == 0

    # 方法作用：缓存按租户/用户/行策略/列集合隔离。
    # Args: monkeypatch - pytest 补丁工具。
    # Returns: 无返回值。
    async def test_cache_isolation_by_identity_and_policy(self, monkeypatch) -> None:
        """不同身份、不同行策略、不同列集合都不得复用画像。"""
        # Arrange
        datasource = SimpleNamespace(name="probe-sqlite", dialect="sqlite")
        _patch_registry(monkeypatch, datasource)
        _patch_settings(monkeypatch, sql_profiling_sample_rows=100)
        calls = _patch_executor(monkeypatch, lambda sql: _execution(sql, [{"id": 1}]))
        profiler = DataProfiler()

        # Act
        await profiler.profile(_context(datasource, schema_fingerprint="fp"), {"t_workshop": ["id"]})
        # 用户8 必须切换到自己的真实身份通道，服务入口不接受跨用户上下文。
        with use_identity_context(RequestIdentity(tenant_id=3, user_id=8, role="analyst")):
            await profiler.profile(
                _context(datasource, user_id=8, schema_fingerprint="fp"),
                {"t_workshop": ["id"]},
            )
        await profiler.profile(
            _context(datasource, schema_fingerprint="fp", row_filter_sql="factory_id = 1"),
            {"t_workshop": ["id"]},
        )
        await profiler.profile(_context(datasource, schema_fingerprint="fp"), {"t_workshop": ["id", "region"]})

        # Assert
        assert len(calls) == 4

    # 方法作用：TTL 到期后重新采样。
    # Args: monkeypatch - pytest 补丁工具。
    # Returns: 无返回值。
    async def test_cache_ttl_expiry(self, monkeypatch) -> None:
        """sql_profiling_ttl_seconds 决定缓存有效期。"""
        # Arrange
        datasource = SimpleNamespace(name="probe-sqlite", dialect="sqlite")
        _patch_registry(monkeypatch, datasource)
        _patch_settings(monkeypatch, sql_profiling_sample_rows=100, sql_profiling_ttl_seconds=10)
        calls = _patch_executor(monkeypatch, lambda sql: _execution(sql, [{"id": 1}]))
        clock = {"now": 0.0}
        monkeypatch.setattr(profiling_module, "_now", lambda: clock["now"])
        profiler = DataProfiler()
        context = _context(datasource, schema_fingerprint="fp")

        # Act
        await profiler.profile(context, {"t_workshop": ["id"]})
        clock["now"] = 5.0
        await profiler.profile(context, {"t_workshop": ["id"]})
        clock["now"] = 11.0
        await profiler.profile(context, {"t_workshop": ["id"]})

        # Assert
        assert len(calls) == 2

    # 方法作用：失败结果不持久缓存，下一次调用重新采样。
    # Args: monkeypatch - pytest 补丁工具。
    # Returns: 无返回值。
    async def test_failure_is_not_cached(self, monkeypatch) -> None:
        """错误画像不得被当成正常数据复用。"""
        # Arrange
        datasource = SimpleNamespace(name="probe-sqlite", dialect="sqlite")
        _patch_registry(monkeypatch, datasource)
        _patch_settings(monkeypatch, sql_profiling_sample_rows=100)
        calls = _patch_executor(
            monkeypatch,
            lambda sql: SQLExecutionResult(success=False, sql=sql, dialect="sqlite", error_type="execution"),
        )
        profiler = DataProfiler()
        context = _context(datasource, schema_fingerprint="fp")

        # Act
        await profiler.profile(context, {"t_workshop": ["id"]})
        await profiler.profile(context, {"t_workshop": ["id"]})

        # Assert
        assert len(calls) == 2

    # 方法作用：同键并发只发起一次真实采样，两侧拿到一致结果。
    # Args: monkeypatch - pytest 补丁工具。
    # Returns: 无返回值。
    async def test_concurrent_same_key_single_flight(self, monkeypatch) -> None:
        """并发填充必须合并，不能重复扫描。"""
        # Arrange
        datasource = SimpleNamespace(name="probe-sqlite", dialect="sqlite")
        _patch_registry(monkeypatch, datasource)
        _patch_settings(monkeypatch, sql_profiling_sample_rows=100)
        calls: list[str] = []

        async def slow(sql, datasource_name, dialect="", *, explain=True, max_rows=None, tenant_id=None):
            calls.append(sql)
            await asyncio.sleep(0.05)
            return _execution(sql, [{"id": 1}, {"id": 2}])

        monkeypatch.setattr(profiling_module, "validate_and_execute_sql", slow)
        profiler = DataProfiler()
        context = _context(datasource, schema_fingerprint="fp")

        # Act
        first, second = await asyncio.gather(
            profiler.profile(context, {"t_workshop": ["id"]}),
            profiler.profile(context, {"t_workshop": ["id"]}),
        )

        # Assert
        assert len(calls) == 1
        assert first.tables[0].sampled_rows == second.tables[0].sampled_rows == 2
        assert first is not second

    # 方法作用：取消原样传播且不写缓存。
    # Args: monkeypatch - pytest 补丁工具。
    # Returns: 无返回值。
    async def test_cancellation_propagates_without_cache(self, monkeypatch) -> None:
        """取消不是超时，必须向上传播且不污染缓存。"""
        # Arrange
        datasource = SimpleNamespace(name="probe-sqlite", dialect="sqlite")
        _patch_registry(monkeypatch, datasource)
        _patch_settings(
            monkeypatch,
            sql_profiling_sample_rows=100,
            sql_profiling_timeout_seconds=5.0,
            sql_profiling_total_seconds=10.0,
        )
        calls: list[str] = []

        async def hanging(sql, datasource_name, dialect="", *, explain=True, max_rows=None, tenant_id=None):
            calls.append(sql)
            await asyncio.sleep(5)
            return _execution(sql, [])

        monkeypatch.setattr(profiling_module, "validate_and_execute_sql", hanging)
        profiler = DataProfiler()
        context = _context(datasource, schema_fingerprint="fp")

        # Act
        task = asyncio.create_task(profiler.profile(context, {"t_workshop": ["id"]}))
        await asyncio.sleep(0.02)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

        # Assert：缓存无残留（成功路径的缓存查询数为 0，此处只能确认没有把取消当成完成）
        assert len(calls) == 1
        cached = profiler._cache  # noqa: SLF001 - 断言缓存未被写入
        assert cached == {}

    # 方法作用：禁用开关时零查询且返回明确状态。
    # Args: monkeypatch - pytest 补丁工具。
    # Returns: 无返回值。
    async def test_disabled_profiling_is_zero_query(self, monkeypatch) -> None:
        """关闭总开关必须带来零新增数据库查询。"""
        # Arrange
        datasource = SimpleNamespace(name="probe-sqlite", dialect="sqlite")
        registry = _patch_registry(monkeypatch, datasource)
        _patch_settings(monkeypatch, sql_profiling_enabled=False)
        calls = _patch_executor(monkeypatch, lambda sql: _execution(sql, []))
        profiler = DataProfiler()

        # Act
        snapshot = await profiler.profile(_context(datasource), {"t_workshop": ["id"]})

        # Assert
        assert snapshot.query_count == 0
        assert snapshot.errors == ["disabled"]
        assert calls == []
        registry.list_all.assert_not_awaited()


# 方法作用：构造执行期最新权限解析替身，模拟已持久化的权限收紧或加严。
# Args: datasource - 被测数据源；allowed - 最新授权列；row_filter - 最新行过滤片段。
# Returns: 可直接替换 resolve_datasource_access 的协程函数。
def _live_access(datasource, *, allowed: list[str] | None = None, row_filter: str = ""):
    async def current_access(*args, **kwargs):
        return {datasource.name: {
            "allowed_columns": list(allowed or []),
            "row_filter_sql": row_filter,
        }}

    return current_access


class TestProfilingPolicyIntersection:
    """覆盖 29.2 的空权限交集拒绝、权限收紧缓存隔离与行策略 AND 合并（spec/55 §10）。"""

    # 方法作用：快照与最新列权限互不相交时，画像与探查都必须拒绝且零 SQL。
    # Args: monkeypatch - pytest 补丁工具。
    # Returns: 无返回值。
    async def test_empty_column_intersection_rejects_both_entries(self, monkeypatch) -> None:
        """快照只许 id、当前只许 customer：空交集不是"没有列限制"，不能执行查询。"""
        # Arrange
        datasource = SimpleNamespace(name="probe-sqlite", dialect="sqlite")
        _patch_registry(monkeypatch, datasource)
        _patch_settings(monkeypatch, sql_profiling_sample_rows=100)
        calls = _patch_executor(monkeypatch, lambda sql: _execution(sql, [{"id": 1}]))
        monkeypatch.setattr(
            "src.security.permission_check.resolve_datasource_access",
            _live_access(datasource, allowed=["customer"]),
        )
        profiler = DataProfiler()
        context = _context(datasource, allowed_columns=["id"])

        # Act
        snapshot = await profiler.profile(context, {"t_workshop": ["id"]})
        probe = await profiler.probe(context, ProbeRequest(
            operation="table_sample", table="t_workshop", columns=["id"], limit=5,
        ))

        # Assert
        assert snapshot.query_count == 0
        assert snapshot.errors == ["column_not_allowed"]
        assert not probe.success
        assert probe.error_code == "column_not_allowed"
        assert calls == []

    # 方法作用：执行期权限收紧后缓存命中也不能复用旧画像。
    # Args: monkeypatch - pytest 补丁工具。
    # Returns: 无返回值。
    async def test_tightened_live_policy_invalidates_cached_profile(self, monkeypatch) -> None:
        """先按宽权限采样，撤权后同一上下文不得命中原缓存，越权列零查询。"""
        # Arrange
        datasource = SimpleNamespace(name="probe-sqlite", dialect="sqlite")
        _patch_registry(monkeypatch, datasource)
        _patch_settings(monkeypatch, sql_profiling_sample_rows=100)
        calls = _patch_executor(monkeypatch, lambda sql: _execution(sql, [{"id": 1}, {"id": 2}]))
        live = {"allowed": ["id", "workshop_name"]}

        # 每次解析都读取最新持久化权限，模拟执行期已发生的撤权。
        # Args: args/kwargs - 权限解析调用参数。
        # Returns: 当前最新列权限的授权结果。
        async def current_access(*args, **kwargs):
            return {datasource.name: {
                "allowed_columns": list(live["allowed"]), "row_filter_sql": "",
            }}

        monkeypatch.setattr("src.security.permission_check.resolve_datasource_access", current_access)
        profiler = DataProfiler()
        context = _context(
            datasource, schema_fingerprint="fp", allowed_columns=["id", "workshop_name"],
        )

        # Act
        first = await profiler.profile(context, {"t_workshop": ["id", "workshop_name"]})
        live["allowed"] = ["id"]
        narrowed = await profiler.profile(context, {"t_workshop": ["workshop_name"]})

        # Assert：第一次正常采样；撤权后拒绝且没有新增查询，也没有返回旧快照
        assert first.errors == []
        assert len(calls) == 1
        assert narrowed.query_count == 0
        assert narrowed.tables[0].error == "column_not_allowed"

    # 方法作用：快照与最新行过滤同时存在时必须 AND 合并，窄侧不能被宽侧覆盖。
    # Args: monkeypatch - pytest 补丁工具。
    # Returns: 无返回值。
    async def test_row_filters_from_both_sides_are_anded(self, monkeypatch) -> None:
        """两个行策略都在 WHERE 中生效，不允许丢弃任一侧。"""
        # Arrange
        datasource = SimpleNamespace(name="probe-sqlite", dialect="sqlite")
        _patch_registry(monkeypatch, datasource)
        _patch_settings(monkeypatch, sql_profiling_sample_rows=100)
        calls = _patch_executor(monkeypatch, lambda sql: _execution(sql, [{"id": 3}]))
        monkeypatch.setattr(
            "src.security.permission_check.resolve_datasource_access",
            _live_access(datasource, row_filter="id > 2"),
        )
        profiler = DataProfiler()

        # Act
        await profiler.profile(
            _context(datasource, row_filter_sql="factory_id = 1"), {"t_workshop": ["id"]},
        )

        # Assert：SQL 为标识符加引号的 AND 合并形态
        assert len(calls) == 1
        assert '"factory_id" = 1' in calls[0]
        assert '"id" > 2' in calls[0]
        assert " AND " in calls[0].upper()


# 方法作用：构造带敏感列与普通列的真实 SQLite 数据源，验收输出脱敏边界。
# Args: 无。
# Returns: (数据源配置, 引擎)；调用方负责 dispose。
async def _masking_db() -> tuple[DataSourceConfig, object]:
    engine = create_async_engine("sqlite+aiosqlite:///:memory:", poolclass=sa.pool.StaticPool)
    async with engine.begin() as connection:
        await connection.execute(sa.text(
            "CREATE TABLE t_contact ("
            "id INTEGER PRIMARY KEY, mobile TEXT, full_name TEXT, signup_date TEXT)"
        ))
        await connection.execute(sa.text(
            "INSERT INTO t_contact (id, mobile, full_name, signup_date) VALUES "
            "(1,'13800001111','张三','2024-01-05'),(2,'13899991111','李四','2024-06-30')"
        ))
    datasource = DataSourceConfig(
        name="probe-sqlite", dialect="sqlite", mode="embedded",
        database=":memory:", engine=engine,
    )
    datasource.connector = SQLiteConnector(datasource).attach_engine(engine)
    datasource.schema = SchemaSnapshot(tables=[
        TableSchema(name="t_contact", columns=[
            ColumnInfo("id", "INTEGER", is_primary_key=True),
            ColumnInfo("mobile", "TEXT"),
            ColumnInfo("full_name", "TEXT"),
            ColumnInfo("signup_date", "TEXT"),
        ], row_count_estimate=2),
    ])
    return datasource, engine


# 方法作用：构造与脱敏样本库一致的授权表结构映射。
# Args: 无。
# Returns: 表名到 TableSchema 的映射。
def _masking_tables() -> dict:
    return {"t_contact": TableSchema(name="t_contact", columns=[
        ColumnInfo("id", "INTEGER", is_primary_key=True),
        ColumnInfo("mobile", "TEXT"),
        ColumnInfo("full_name", "TEXT"),
        ColumnInfo("signup_date", "TEXT"),
    ])}


class TestProfilingMasking:
    """覆盖 29.1/29.3 输出脱敏：先统计后脱敏，画像与探查都不是脱敏旁路（spec/55 §10）。"""

    # 方法作用：画像先按原值统计，再按原列名脱敏输出值。
    # Args: monkeypatch - pytest 补丁工具。
    # Returns: 无返回值。
    async def test_profile_masks_outputs_but_keeps_true_statistics(self, monkeypatch) -> None:
        """两个手机号掩码相同，但 distinct_count 仍为按原值统计的 2。"""
        # Arrange
        datasource, engine = await _masking_db()
        _patch_registry(monkeypatch, datasource)
        _patch_settings(monkeypatch, sql_profiling_sample_rows=100)
        profiler = DataProfiler()

        try:
            # Act
            snapshot = await profiler.profile(
                _context(
                    datasource, _masking_tables(), allowed_columns=["mobile", "full_name"],
                ),
                {"t_contact": ["mobile", "full_name"]},
            )

            # Assert：敏感列输出脱敏且基数不变，普通列保持原值
            assert snapshot.errors == []
            columns = {column.name: column for column in snapshot.tables[0].columns}
            mobile = columns["mobile"]
            assert mobile.distinct_count == 2
            assert mobile.top_value_masked is True
            assert "****" in str(mobile.top_value)
            assert all("****" in str(item["value"]) for item in mobile.top_values)
            name = columns["full_name"]
            assert name.top_value_masked is False
            assert "张三" in [item["value"] for item in name.top_values]
            payload = snapshot.model_dump_json()
            assert "13800001111" not in payload
            assert "13899991111" not in payload
        finally:
            await engine.dispose()

    # 方法作用：探查各原语输出在统计完成后按原列名脱敏，脱敏值不再声称精确匹配。
    # Args: monkeypatch - pytest 补丁工具。
    # Returns: 无返回值。
    async def test_probe_masks_cells_and_drops_masked_exact_claims(self, monkeypatch) -> None:
        """table_sample/value_domain/entity_lookup 输出脱敏，期间无原值外泄。"""
        # Arrange
        datasource, engine = await _masking_db()
        _patch_registry(monkeypatch, datasource)
        _patch_settings(monkeypatch, sql_profiling_sample_rows=100, sql_probe_limit=50)
        profiler = DataProfiler()
        context = _context(
            datasource, _masking_tables(),
            allowed_columns=["mobile", "full_name", "signup_date"],
        )

        try:
            # Act
            sample = await profiler.probe(context, ProbeRequest(
                operation="table_sample", table="t_contact", columns=["mobile", "full_name"],
                limit=10,
            ))
            domain = await profiler.probe(context, ProbeRequest(
                operation="value_domain", table="t_contact", column="mobile",
            ))
            lookup = await profiler.probe(context, ProbeRequest(
                operation="entity_lookup", table="t_contact", column="mobile",
                pattern="13800001111",
            ))
            span = await profiler.probe(context, ProbeRequest(
                operation="time_range", table="t_contact", column="signup_date",
            ))

            # Assert：敏感列脱敏；脱敏值 exact 必须为 False；普通列不受影响
            assert sample.success and domain.success and lookup.success and span.success
            assert all("****" in row["mobile"] for row in sample.rows)
            assert {row["full_name"] for row in sample.rows} == {"张三", "李四"}
            assert all("****" in str(row["value"]) for row in domain.rows)
            assert lookup.rows[0]["exact"] is False
            assert "****" in str(lookup.rows[0]["value"])
            assert span.rows[0]["compatible"] is True
            assert span.rows[0]["min"] == "2024-01-05"
            payload = "".join(
                result.model_dump_json() for result in (sample, domain, lookup, span)
            )
            assert "13800001111" not in payload
            assert "13899991111" not in payload
        finally:
            await engine.dispose()
