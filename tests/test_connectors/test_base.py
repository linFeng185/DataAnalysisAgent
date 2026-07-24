"""连接器模块测试 — URL 构建、工厂、格式化、超时、EXPLAIN。"""

from __future__ import annotations

import pytest
from sqlalchemy.engine import make_url

from src.datasource.config import DataSourceConfig


def _ds(dialect: str, **kw) -> DataSourceConfig:
    return DataSourceConfig(name=f"t_{dialect}", dialect=dialect, mode="embedded", **kw)


# ================================================================
# URL 构建 (3.1.2)
# ================================================================

class TestConnectionURL:

    # 方法作用：验证开发环境创建异步引擎时也不会开启 SQLAlchemy SQL 回显。
    # Args: self - pytest 测试类实例；monkeypatch - pytest 补丁工具。
    # Returns: 无返回值，断言失败时由 pytest 报告。
    @pytest.mark.asyncio
    async def test_async_engine_disables_sql_echo(self, monkeypatch):
        """SQL 文本不得因开发模式被重复写入应用日志。"""
        # Arrange
        from src.connectors import base as base_module
        from src.connectors.mysql import MySQLConnector

        captured: dict[str, object] = {}
        expected_engine = object()

        # 方法作用：记录通用连接器传给 SQLAlchemy 的引擎参数。
        # Args: url - SQLAlchemy URL；kwargs - 引擎关键字参数。
        # Returns: 测试用引擎对象。
        def fake_create_async_engine(url, **kwargs):
            captured["url"] = url
            captured["kwargs"] = kwargs
            return expected_engine

        monkeypatch.setattr(base_module, "create_async_engine", fake_create_async_engine)
        connector = MySQLConnector(_ds("mysql"))

        # Act
        result = await connector.create_engine()

        # Assert
        assert result is expected_engine
        assert captured["kwargs"]["echo"] is False

    def test_clickhouse(self):
        from src.connectors.clickhouse import ClickHouseConnector
        ds = _ds("clickhouse", host="ch.local", port=9000, database="analytics", username="r", password="s")
        url = ClickHouseConnector(ds)._build_url()  # noqa: SLF001
        assert str(url) == "clickhouse+asynch://r:***@ch.local:9000/analytics"
        assert url.password == "s"

    def test_mysql(self):
        from src.connectors.mysql import MySQLConnector
        ds = _ds("mysql", host="db1", port=3306, database="ecom", username="ro", password="p")
        url = MySQLConnector(ds)._build_url()  # noqa: SLF001
        assert str(url) == "mysql+aiomysql://ro:***@db1:3306/ecom?charset=utf8mb4"
        assert url.password == "p"

    def test_postgres(self):
        from src.connectors.postgres import PostgreSQLConnector
        ds = _ds("postgres", host="pg", port=5432, database="analytics", username="ro", password="p")
        url = PostgreSQLConnector(ds)._build_url()  # noqa: SLF001
        assert str(url) == "postgresql+asyncpg://ro:***@pg:5432/analytics"
        assert url.password == "p"

    # 方法作用：验证特殊字符密码通过 SQLAlchemy URL 结构化保存且 repr 脱敏。
    # Args: self - pytest 测试类实例。
    # Returns: 无返回值，断言失败时由 pytest 报告。
    def test_special_character_password_is_safe(self):
        """密码中的 @、/、: 不得改变 URL 主机与数据库解析结果。"""
        from src.connectors.mysql import MySQLConnector

        password = "p@ss/w:rd"
        ds = _ds(
            "mysql", host="db.local", port=3306, database="sales",
            username="reader", password=password,
        )
        url = MySQLConnector(ds)._build_url()  # noqa: SLF001
        parsed = make_url(url)

        assert parsed.password == password
        assert parsed.host == "db.local"
        assert password not in repr(url)

    @pytest.mark.asyncio
    async def test_oracle_create_engine_is_async_and_uses_service_name(self, monkeypatch):
        """Oracle 首次执行创建引擎时必须可 await，并使用 service_name 连接服务。"""
        # Arrange
        from src.connectors import oracle as oracle_module
        from src.connectors.oracle import OracleConnector

        ds = _ds(
            "oracle",
            host="oracle.local",
            port=1521,
            database="XEPDB1",
            username="reader",
            password="secret",
        )
        captured: dict[str, object] = {}

        def fake_create_engine(url, **kwargs):
            captured["url"] = url
            captured["kwargs"] = kwargs
            return object()

        monkeypatch.setattr(oracle_module.sa, "create_engine", fake_create_engine)
        connector = OracleConnector(ds)

        # Act
        engine = await connector.create_engine()

        # Assert
        assert engine is connector.engine
        assert "service_name=XEPDB1" in str(captured["url"])

    @pytest.mark.asyncio
    async def test_oracle_execute_returns_dict_rows(self):
        """Oracle execute 应在线程池中运行并返回 list[dict]。"""
        # Arrange
        from src.connectors.oracle import OracleConnector

        class Row:
            _mapping = {"id": 1}

        class Connection:
            def __enter__(self):
                return self

            def __exit__(self, exc_type, exc, tb):
                return False

            def execute(self, statement, params):
                assert str(statement) == "SELECT 1"
                assert params == {}
                return type("Result", (), {"fetchall": lambda self: [Row()]})()

        class Engine:
            def connect(self):
                return Connection()

        connector = OracleConnector(_ds("oracle"))
        connector._engine = Engine()  # noqa: SLF001

        # Act
        result = await connector.execute("SELECT 1")

        # Assert
        assert result == [{"id": 1}]

    @pytest.mark.asyncio
    async def test_oracle_health_check_uses_dual(self):
        """Oracle health_check 必须使用 DUAL 探针。"""
        # Arrange
        from unittest.mock import AsyncMock
        from src.connectors.oracle import OracleConnector

        connector = OracleConnector(_ds("oracle"))
        connector.execute = AsyncMock(return_value=[])  # type: ignore[method-assign]

        # Act
        result = await connector.health_check()

        # Assert
        assert result is True
        connector.execute.assert_awaited_once_with("SELECT 1 FROM DUAL")

    @pytest.mark.asyncio
    async def test_oracle_explain_reports_failure(self):
        """Oracle explain 失败时返回语义错误摘要。"""
        # Arrange
        from unittest.mock import AsyncMock
        from src.connectors.oracle import OracleConnector

        connector = OracleConnector(_ds("oracle"))
        connector.execute = AsyncMock(side_effect=RuntimeError("ORA-00933"))  # type: ignore[method-assign]

        # Act
        result = await connector.explain("SELECT 1")

        # Assert
        assert result["valid"] is False
        assert result["errors"][0]["type"] == "semantic_error"
        connector.execute.assert_awaited_once_with("EXPLAIN PLAN FOR SELECT 1")

    @pytest.mark.asyncio
    async def test_oracle_close_disposes_sync_engine(self):
        """Oracle close 应在线程池中释放同步引擎并清空引用。"""
        # Arrange
        from src.connectors.oracle import OracleConnector

        class Engine:
            disposed = False

            def dispose(self):
                self.disposed = True

        connector = OracleConnector(_ds("oracle"))
        engine = Engine()
        connector._engine = engine  # noqa: SLF001

        # Act
        await connector.close()

        # Assert
        assert engine.disposed is True
        assert connector.engine is None

    @pytest.mark.asyncio
    async def test_clickhouse_connector_uses_clickhouse_connect_client(self, monkeypatch):
        """ClickHouseConnector 首次执行应使用已安装的 clickhouse-connect 客户端。"""
        # Arrange
        from src.connectors.clickhouse import ClickHouseConnector

        class QueryResult:
            column_names = ["value"]
            result_rows = [(1,)]

        class FakeClient:
            def __init__(self):
                self.queries: list[str] = []

            def query(self, sql, parameters=None, settings=None):
                self.queries.append((sql, parameters, settings))
                return QueryResult()

            def close(self):
                return None

        client = FakeClient()
        import socket
        from unittest.mock import MagicMock

        import clickhouse_connect
        monkeypatch.setattr(clickhouse_connect, "get_client", lambda **kwargs: client)
        monkeypatch.setattr(
            socket,
            "getaddrinfo",
            lambda *args, **kwargs: [
                (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("93.184.216.34", 8123)),
            ],
        )
        monkeypatch.setattr(socket, "create_connection", MagicMock(return_value=MagicMock()))
        connector = ClickHouseConnector(_ds(
            "clickhouse", host="ch.local", port=9000,
            database="default", username="reader", password="secret",
        ))

        # Act
        await connector.create_engine()
        result = await connector.execute("SELECT 1")

        # Assert
        assert result == [{"value": 1}]
        assert client.queries[0][0] == "SELECT 1"
        assert client.queries[0][2]["max_execution_time"] > 0

    # 方法作用：验证 ClickHouse 命名参数替换不会破坏共享前缀的长参数名。
    # Args: self - pytest 测试类实例。
    # Returns: 无返回值，断言失败时由 pytest 报告。
    def test_clickhouse_parameter_binding_respects_identifier_boundaries(self) -> None:
        """`:date` 与 `:date_end` 必须分别绑定，不能产生 `{date}_end`。"""
        # Arrange
        from src.connectors.clickhouse import ClickHouseConnector

        # Act
        sql, params = ClickHouseConnector._bind_parameters(  # noqa: SLF001
            "SELECT :date AS start_date, :date_end AS end_date",
            {"date": "2026-01-01", "date_end": "2026-01-31"},
        )

        # Assert
        assert sql == "SELECT {date:String} AS start_date, {date_end:String} AS end_date"
        assert params == {"date": "2026-01-01", "date_end": "2026-01-31"}


# ================================================================
# 工厂 (3.1)
# ================================================================

class TestConnectorFactory:

    @pytest.mark.parametrize("dialect,cls_name", [
        ("clickhouse", "ClickHouseConnector"),
        ("mysql", "MySQLConnector"),
        ("postgres", "PostgreSQLConnector"),
    ])
    def test_factory(self, dialect, cls_name):
        from src.connectors.base import create_connector
        c = create_connector(_ds(dialect))
        assert type(c).__name__ == cls_name

    def test_unsupported(self):
        from src.connectors.base import create_connector
        with pytest.raises(ValueError, match="不支持的方言"):
            create_connector(_ds("xxx"))


# ================================================================
# 结果格式化 (3.1.4)
# ================================================================

class TestRowsToDictList:

    def test_convert(self):
        from src.connectors.base import ConnectorBase
        class R:
            _mapping = {"a": 1}
        assert ConnectorBase.rows_to_dict_list([R()]) == [{"a": 1}]

    def test_empty(self):
        from src.connectors.base import ConnectorBase
        assert ConnectorBase.rows_to_dict_list([]) == []

    # 方法作用：验证数据库 NaN/Infinity 在批量转换时安全降级为空值。
    # Args: self - pytest 测试类实例。
    # Returns: 无返回值，断言失败时由 pytest 报告。
    def test_non_finite_float_values_become_none(self) -> None:
        """非有限浮点不得进入 Decimal 和后续 JSON 序列化链路。"""
        # Arrange
        from src.connectors.base import ConnectorBase

        class Row:
            _mapping = {"nan": float("nan"), "positive_inf": float("inf"), "value": 1.25}

        # Act
        result = ConnectorBase.rows_to_dict_list([Row()])

        # Assert
        assert result[0]["nan"] is None
        assert result[0]["positive_inf"] is None
        assert str(result[0]["value"]) == "1.25"


# ================================================================
# 超时 SQL (3.1.3)
# ================================================================

class TestTimeoutSQL:

    def test_clickhouse(self):
        from src.connectors.clickhouse import ClickHouseConnector
        s = ClickHouseConnector(_ds("clickhouse"))._get_timeout()  # noqa: SLF001
        assert s and "max_execution_time" in s

    def test_mysql(self):
        from src.connectors.mysql import MySQLConnector
        s = MySQLConnector(_ds("mysql"))._get_timeout()  # noqa: SLF001
        assert s and "max_execution_time" in s

    def test_postgres(self):
        from src.connectors.postgres import PostgreSQLConnector
        s = PostgreSQLConnector(_ds("postgres"))._get_timeout()  # noqa: SLF001
        assert s and "statement_timeout" in s


# ================================================================
# EXPLAIN (3.4.2)
# ================================================================

class TestEXPLAIN:

    def test_clickhouse_skipped(self, monkeypatch):
        """explain_skip_dialects 配置生效。"""
        import asyncio
        from src.app_context import AppContext, use_app_context
        from src.config import Settings
        from src.connectors.clickhouse import ClickHouseConnector
        del monkeypatch
        settings = Settings(explain_skip_dialects=["clickhouse"])
        with use_app_context(AppContext(settings)):
            result = asyncio.run(
                ClickHouseConnector(_ds("clickhouse")).explain("SELECT 1")
            )
        assert result["valid"] is True


class TestClickHousePartition:
    """3.2.5 分区键。"""

    def test_no_db_graceful(self):
        import asyncio
        from src.connectors.clickhouse import ClickHouseConnector
        r = asyncio.run(ClickHouseConnector(_ds("clickhouse")).get_partition_key("t"))
        assert r == ""
