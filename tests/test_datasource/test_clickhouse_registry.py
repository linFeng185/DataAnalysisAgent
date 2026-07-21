"""ClickHouse Registry 客户端适配回归测试。"""

from __future__ import annotations

import logging

import sqlalchemy as sa
import pytest


logger = logging.getLogger(__name__)


class TestClickHouseRegistryAdapter:
    """覆盖 clickhouse-connect 适配层。"""

    @pytest.mark.asyncio
    async def test_build_engine_without_sqlalchemy_clickhouse_dialect(self, monkeypatch):
        """Registry 应使用已声明的 clickhouse-connect，而不是缺失的 SQLAlchemy 方言。"""
        # Arrange
        import socket
        from unittest.mock import MagicMock
        from src.datasource.config import DataSourceConfig
        from src.datasource.registry import DataSourceRegistry

        class QueryResult:
            column_names = ["ok"]
            result_rows = [(1,)]

        class FakeClient:
            def __init__(self):
                self.queries: list[str] = []

            def query(self, sql, parameters=None):
                self.queries.append(sql)
                return QueryResult()

            def close(self):
                return None

        client = FakeClient()
        client_options: dict[str, object] = {}
        import clickhouse_connect

        def fake_get_client(**kwargs):
            client_options.update(kwargs)
            return client

        monkeypatch.setattr(clickhouse_connect, "get_client", fake_get_client)
        tcp_connection = MagicMock()
        tcp_probe = MagicMock(return_value=tcp_connection)
        monkeypatch.setattr(socket, "create_connection", tcp_probe)
        config = DataSourceConfig(
            name="clickhouse_test",
            dialect="clickhouse",
            mode="external",
            host="ch.local",
            port=9000,
            database="default",
            username="reader",
            password="secret",
            extra_params={"connect_timeout": 5, "query_retries": 0},
        )
        registry = DataSourceRegistry()

        # Act
        engine = await registry._create_engine(config)  # noqa: SLF001
        with engine.connect() as connection:
            result = connection.execute(sa.text("SELECT 1"))
            rows = result.fetchall()

        # Assert
        assert rows[0]._mapping == {"ok": 1}
        assert client.queries == ["SELECT 1"]
        assert client_options["port"] == 8123
        assert client_options["connect_timeout"] == 5
        assert client_options["query_retries"] == 0
        tcp_probe.assert_called_once_with(("ch.local", 8123), timeout=5)

    # 方法作用：验证 Registry 内部调用也拒绝未实现方言。
    # Args: self - pytest 测试类实例；monkeypatch - pytest 补丁工具。
    # Returns: 无返回值，断言失败时由 pytest 报告。
    async def test_unimplemented_dialect_never_falls_back_to_postgres(self, monkeypatch):
        """YAML 或内部配置绕过 API 时也不得创建 PostgreSQL engine。"""
        # Arrange
        from unittest.mock import MagicMock

        from src.datasource.config import DataSourceConfig
        from src.datasource.registry import DataSourceRegistry

        config = DataSourceConfig(name="warehouse", dialect="hive", mode="external")

        # Act / Assert
        with pytest.raises(ConnectionError, match="hive"):
            await DataSourceRegistry()._create_engine(config)  # noqa: SLF001

    @pytest.mark.asyncio
    async def test_clickhouse_async_context_supports_schema_introspection(self, monkeypatch):
        """ClickHouse 连接适配器应同时支持 Provider 的 async with 访问方式。"""
        # Arrange
        import socket
        from unittest.mock import MagicMock
        from src.datasource.config import DataSourceConfig
        from src.datasource.registry import DataSourceRegistry
        import clickhouse_connect

        class QueryResult:
            column_names = ["name"]
            result_rows = [("orders",)]

        class FakeClient:
            def query(self, sql, parameters=None):
                return QueryResult()

            def close(self):
                return None

        monkeypatch.setattr(clickhouse_connect, "get_client", MagicMock(return_value=FakeClient()))
        monkeypatch.setattr(socket, "create_connection", MagicMock(return_value=MagicMock()))
        config = DataSourceConfig(
            name="clickhouse_test", dialect="clickhouse", mode="external",
            host="ch.local", port=8123, database="default",
        )
        engine = await DataSourceRegistry()._create_engine(config)  # noqa: SLF001

        # Act
        async with engine.connect() as connection:
            result = await connection.execute(sa.text("SELECT name FROM system.tables"))
            rows = result.fetchall()

        # Assert
        assert rows[0]._mapping["name"] == "orders"

    # 验证测试数据源指向实际承载测试数据的 analytics 库。
    # Args: self - pytest 测试类实例。
    # Returns: 无返回值，断言失败时由 pytest 报告。
    def test_clickhouse_test_config_uses_analytics_database(self):
        """clickhouse_test 必须连接含十万行测试数据的 analytics 数据库。"""
        logger.debug("test_clickhouse_test_config_uses_analytics_database 入口")
        try:
            # Arrange / Act：只解析配置，不建立真实连接。
            import yaml

            with open("config/datasources.yaml", encoding="utf-8") as config_file:
                config = yaml.safe_load(config_file)
            datasource = config["datasources"]["clickhouse_test"]

            # Assert
            assert datasource["database"] == "analytics"
            logger.info(
                "test_clickhouse_test_config_uses_analytics_database 完成",
                extra={"database": datasource["database"]},
            )
        except Exception as exc:
            logger.error(
                "test_clickhouse_test_config_uses_analytics_database 异常: %s",
                exc,
                exc_info=True,
            )
            raise
