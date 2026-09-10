"""ExternalDataSourceProvider 测试 — 2.3.1~6。"""

from __future__ import annotations

import asyncio


class TestApplicationOnlyLoading:
    """2.3.6 配置只由应用管理，不保留 YAML 发现接口。"""

    # 方法作用：确认构造 Provider 不读取任何外部配置。
    # Args: self - 测试实例。
    # Returns: 无返回值。
    def test_no_yaml_loader(self) -> None:
        """空 Provider 不会因本地文件存在而隐式注册连接。"""
        # Arrange
        from src.datasource.providers.external import ExternalDataSourceProvider
        # Act
        provider = ExternalDataSourceProvider()
        # Assert
        assert not provider._sources
        assert not hasattr(provider, "load_yaml")
        assert not hasattr(provider, "from_yaml")


class TestDynamicRegistration:
    """2.3.2-2.3.3 注册/移除。"""

    def test_register(self):
        from src.datasource.providers.external import (
            DataSourceCreateRequest,
            ExternalDataSourceProvider,
        )

        req = DataSourceCreateRequest(name="new_db", dialect="mysql", host="10.0.3.1", port=3306, database="analytics", username="reader", password="p@ss", description="新接入")
        provider = ExternalDataSourceProvider()
        ds = asyncio.run(provider.register(req))
        assert ds.name == "new_db"
        assert ds.password != "p@ss"

    def test_unregister(self):
        from src.datasource.providers.external import (
            DataSourceCreateRequest,
            ExternalDataSourceProvider,
        )

        req = DataSourceCreateRequest(name="tmp", dialect="mysql", host="localhost", database="tmp", username="root", password="x")
        provider = ExternalDataSourceProvider()
        asyncio.run(provider.register(req))
        assert len(asyncio.run(provider.list_all())) == 1
        asyncio.run(provider.unregister("tmp"))
        assert len(asyncio.run(provider.list_all())) == 0

    def test_unregister_nonexistent(self):
        from src.datasource.providers.external import ExternalDataSourceProvider
        asyncio.run(ExternalDataSourceProvider().unregister("x"))  # 不抛异常


class TestExternalProviderInterface:
    """Provider 基础接口。"""

    def test_lookup_found_and_not_found(self):
        from src.datasource.providers.external import (
            DataSourceCreateRequest,
            ExternalDataSourceProvider,
        )

        req = DataSourceCreateRequest(name="lk", dialect="clickhouse", host="localhost", database="db", username="u", password="p")
        provider = ExternalDataSourceProvider()
        asyncio.run(provider.register(req))

        assert asyncio.run(provider.lookup("lk")) is not None
        assert asyncio.run(provider.lookup("nope")) is None

    def test_list_all_empty(self):
        from src.datasource.providers.external import ExternalDataSourceProvider
        assert asyncio.run(ExternalDataSourceProvider().list_all()) == []

    def test_oracle_connection_probe_uses_dual(self):
        """Oracle 21c 连通性探针必须带 FROM DUAL，避免 SELECT 1 语法错误。"""
        # Arrange
        from src.datasource.config import DataSourceConfig
        from src.datasource.providers.external import ExternalDataSourceProvider

        class FakeConnection:
            def __init__(self):
                self.statements: list[str] = []

            def execute(self, statement, params=None):
                sql = str(statement)
                self.statements.append(sql)
                if sql.strip().upper() == "SELECT 1":
                    raise RuntimeError("ORA-00923: FROM keyword not found where expected")
                return object()

        class ConnectionContext:
            def __init__(self, connection):
                self.connection = connection

            def __enter__(self):
                return self.connection

            def __exit__(self, exc_type, exc, tb):
                return False

        class FakeEngine:
            def __init__(self, connection):
                self.connection = connection

            def connect(self):
                return ConnectionContext(self.connection)

        connection = FakeConnection()
        config = DataSourceConfig(
            name="oracle_test", dialect="oracle", mode="external", engine=FakeEngine(connection)
        )

        # Act
        result = asyncio.run(ExternalDataSourceProvider().test_connection(config))

        # Assert
        assert result is True
        assert connection.statements == ["SELECT 1 FROM DUAL"]
