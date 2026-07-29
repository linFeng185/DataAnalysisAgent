"""Schema 缓存刷新服务启动接入测试。"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest


@pytest.mark.asyncio
class TestCacheRefresherBootstrap:
    """覆盖功能 6.4.1 的应用生命周期接入。"""

    # 方法作用：验证启用时创建刷新服务并注册到 AppContext。
    # Args: self - pytest 测试类实例；monkeypatch - pytest 补丁工具。
    # Returns: 无返回值，断言失败时由 pytest 报告。
    async def test_start_registers_cache_refresh_service(self, monkeypatch) -> None:
        """服务必须在启动阶段注册关闭器，不能创建失管后台任务。"""
        # Arrange
        from src import app_context, bootstrap
        import src.datasource.registry as registry_module
        import src.knowledge.cache_refresher as refresher_module
        import src.knowledge.schema_manager as schema_module

        context = SimpleNamespace(set_resource=MagicMock())
        registry = SimpleNamespace()
        manager = SimpleNamespace()
        refresher = SimpleNamespace()
        service = SimpleNamespace(start=AsyncMock(), close=AsyncMock())
        monkeypatch.setattr(app_context, "get_app_context", lambda: context)
        monkeypatch.setattr(registry_module, "get_registry", lambda: registry)
        monkeypatch.setattr(schema_module, "SchemaManager", MagicMock(return_value=manager))
        refresher_factory = MagicMock(return_value=refresher)
        service_factory = MagicMock(return_value=service)
        monkeypatch.setattr(refresher_module, "CacheRefresher", refresher_factory)
        monkeypatch.setattr(refresher_module, "CacheRefreshService", service_factory)
        settings = SimpleNamespace(
            schema_cache_refresh_enabled=True,
            schema_cache_refresh_interval_seconds=3600,
            datasource_cache_backend="local",
            redis_url="redis://localhost:6379/0",
        )

        # Act
        await bootstrap._start_schema_cache_refresh(settings)

        # Assert
        refresher_factory.assert_called_once_with(
            schema_manager=manager,
            redis_client=None,
        )
        service_factory.assert_called_once_with(refresher, registry, 3600)
        service.start.assert_awaited_once()
        assert context.set_resource.call_args.args[:2] == (
            "schema_cache_refresh_service",
            service,
        )
