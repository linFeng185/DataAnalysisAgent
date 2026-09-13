"""真实报表关闭时连接池泄漏回归。"""

from types import SimpleNamespace
from unittest.mock import AsyncMock

from src.app_context import create_app_context, use_app_context_async
from src.config import Settings
from src.datasource.registry import get_registry


class TestRegistryShutdown:
    """覆盖 20.x AppContext 对数据源连接的生命周期管理。"""

    # 方法作用：验证应用关闭等待已解析连接器及缓存失效的待释放任务。
    # Args: self - 测试实例。
    # Returns: 无返回值。
    async def test_context_closes_registry_connections(self) -> None:
        """事件循环结束前必须释放 aiomysql 等异步连接，不能留给 __del__。"""
        # Arrange
        context = create_app_context(Settings(_env_file=None))
        connector = SimpleNamespace(close=AsyncMock())
        stale = SimpleNamespace(close=AsyncMock())
        config = SimpleNamespace(connector=connector, engine=None)

        # Act
        async with use_app_context_async(context):
            registry = get_registry()
            registry._cache[(3, "mysql")] = config
            registry._release_runtime_config(SimpleNamespace(connector=stale, engine=None), "stale")
            await context.close()
            await context.close()

        # Assert
        connector.close.assert_awaited_once()
        stale.close.assert_awaited_once()
        assert not registry._cache
        assert not registry._pending_release_tasks
