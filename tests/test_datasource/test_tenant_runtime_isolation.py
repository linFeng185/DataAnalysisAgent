"""数据源运行时租户隔离回归测试。"""

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest


class TestDatasourceTenantRuntimeIsolation:
    """覆盖同名数据源的租户复合键。"""

    def test_same_name_isolated_by_tenant(self) -> None:
        """两个租户注册同名数据源时不能覆盖彼此配置。"""
        # Arrange
        from src.datasource.providers.external import (
            DataSourceCreateRequest,
            ExternalDataSourceProvider,
        )

        provider = ExternalDataSourceProvider()
        first = DataSourceCreateRequest(name="sales", dialect="mysql", host="tenant-a")
        second = DataSourceCreateRequest(name="sales", dialect="mysql", host="tenant-b")

        # Act
        asyncio.run(provider.register(first, tenant_id=11, owner_user_id=101))
        asyncio.run(provider.register(second, tenant_id=22, owner_user_id=202))

        # Assert
        assert asyncio.run(provider.lookup("sales", tenant_id=11)).host == "tenant-a"
        assert asyncio.run(provider.lookup("sales", tenant_id=22)).host == "tenant-b"

    # 方法作用：验证更新当前租户配置不会删除其他租户的同名数据源。
    # Args: self - pytest 测试类实例。
    # Returns: 无返回值，断言失败时由 pytest 报告。
    async def test_update_same_name_preserves_other_tenant(self) -> None:
        """租户 11 更新 sales 时，租户 22 的 sales 必须保持原配置。"""
        # Arrange
        from src.api.schemas import DataSourceCreateRequest, DataSourceUpdateRequest
        from src.datasource.providers.external import ExternalDataSourceProvider

        provider = ExternalDataSourceProvider()
        await provider.register(
            DataSourceCreateRequest(name="sales", dialect="mysql", host="tenant-a"),
            tenant_id=11,
            owner_user_id=101,
        )
        other_tenant_source = await provider.register(
            DataSourceCreateRequest(name="sales", dialect="mysql", host="tenant-b"),
            tenant_id=22,
            owner_user_id=202,
        )
        provider.probe_request = AsyncMock(return_value=True)
        provider.persist = AsyncMock()
        request = DataSourceUpdateRequest(
            dialect="mysql",
            host="tenant-a-new",
            password="new-secret",
        )

        # Act
        updated = await provider.update(
            "sales",
            request,
            engine_factory=AsyncMock(),
            tenant_id=11,
            owner_user_id=101,
        )

        # Assert
        assert updated.host == "tenant-a-new"
        assert await provider.lookup("sales", tenant_id=11) is updated
        assert await provider.lookup("sales", tenant_id=22) is other_tenant_source

    # 方法作用：验证迁移期旧字符串缓存键不能绕过运行时租户复合键。
    # Args: self - pytest 测试类实例；monkeypatch - pytest 补丁工具。
    # Returns: 无返回值，断言失败时由 pytest 报告。
    async def test_legacy_string_cache_key_cannot_cross_tenant(self, monkeypatch) -> None:
        """租户 22 不得命中属于租户 11 的历史 `sales` 缓存。"""
        # Arrange
        from src.datasource.config import DataSourceConfig
        from src.datasource.registry import DataSourceRegistry
        from src.exceptions import DataSourceNotFoundError

        registry = DataSourceRegistry()
        legacy_config = DataSourceConfig(
            name="sales",
            mode="external",
            dialect="postgres",
            tenant_id=11,
        )
        registry._cache["sales"] = legacy_config  # type: ignore[index]  # noqa: SLF001
        monkeypatch.setattr(registry, "_isolation_enabled", lambda: True)

        # Act / Assert
        with pytest.raises(DataSourceNotFoundError):
            await registry.resolve("sales", tenant_id=22)

    # 方法作用：验证 Registry 能为系统后台任务枚举所有已注册数据源租户。
    # Args: self - pytest 测试类实例；monkeypatch - pytest 补丁工具。
    # Returns: 无返回值，断言失败时由 pytest 报告。
    async def test_registry_lists_datasource_tenant_ids(self, monkeypatch) -> None:
        """后台任务应看到有数据源的全部租户，而非当前请求租户。"""
        # Arrange
        from src.datasource.config import DataSourceConfig
        from src.datasource.registry import DataSourceRegistry

        provider = SimpleNamespace(list_tenant_ids=AsyncMock(return_value=[11, 22]))
        registry = DataSourceRegistry()
        registry.register_provider("external", provider)
        registry.register_config(DataSourceConfig(
            name="warehouse",
            mode="external",
            dialect="postgres",
            tenant_id=33,
        ))

        # Act
        tenant_ids = await registry.list_tenant_ids()

        # Assert
        assert tenant_ids == [11, 22, 33]
        provider.list_tenant_ids.assert_awaited_once()
