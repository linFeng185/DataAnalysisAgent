"""数据库连接器注册表与运行时委托测试。"""

from __future__ import annotations

import logging
from unittest.mock import AsyncMock

import pytest

from src.datasource.config import DataSourceConfig


logger = logging.getLogger(__name__)


class TestConnectorRegistry:
    """覆盖功能 3.1.1~3.1.3：方言注册和 Registry 引擎委托。"""

    # 方法作用：构造连接器测试使用的最小数据源配置。
    # Args: self - pytest 测试类实例；dialect - 数据库方言。
    # Returns: DataSourceConfig 测试对象。
    def _datasource(self, dialect: str) -> DataSourceConfig:
        """生成不触发真实数据库连接的配置。"""
        logger.debug("_datasource 入口", extra={"dialect": dialect})
        result = DataSourceConfig(name="test", mode="external", dialect=dialect)
        logger.info("_datasource 完成", extra={"dialect": dialect})
        return result

    # 方法作用：验证新方言可以通过装饰器注册并由工厂创建。
    # Args: self - pytest 测试类实例。
    # Returns: 无返回值，断言失败时由 pytest 报告。
    def test_custom_connector_can_be_registered(self) -> None:
        """新增方言不应修改 create_connector 条件分支。"""
        logger.debug("test_custom_connector_can_be_registered 入口")
        from src.connectors.registry import (
            create_connector,
            register_connector,
            unregister_connector,
        )

        class FakeConnector:
            """测试用连接器。"""

            # 方法作用：保存数据源配置。
            # Args: config - 测试数据源配置。
            # Returns: 无返回值。
            def __init__(self, config: DataSourceConfig) -> None:
                logger.debug("FakeConnector.__init__ 入口")
                self.config = config
                logger.info("FakeConnector.__init__ 完成")

        try:
            register_connector("fake")(FakeConnector)
            connector = create_connector(self._datasource("fake"))
            assert isinstance(connector, FakeConnector)
        finally:
            unregister_connector("fake")
        logger.info("test_custom_connector_can_be_registered 完成")

    # 方法作用：验证 DataSourceRegistry 委托连接器创建运行时引擎。
    # Args: self - pytest 测试类实例；monkeypatch - pytest 补丁工具。
    # Returns: 无返回值，断言失败时由 pytest 报告。
    @pytest.mark.asyncio
    async def test_datasource_registry_delegates_engine_creation(self, monkeypatch) -> None:
        """生产链路不能继续维护第二套方言引擎工厂。"""
        logger.debug("test_datasource_registry_delegates_engine_creation 入口")
        import src.connectors.registry as connector_registry
        from src.datasource.registry import DataSourceRegistry

        expected_engine = object()
        connector = AsyncMock()
        connector.create_engine.return_value = expected_engine
        create = lambda config: connector
        monkeypatch.setattr(connector_registry, "create_connector", create)

        result = await DataSourceRegistry()._create_engine(self._datasource("postgres"))

        assert result is expected_engine
        connector.create_engine.assert_awaited_once()
        logger.info("test_datasource_registry_delegates_engine_creation 完成")
