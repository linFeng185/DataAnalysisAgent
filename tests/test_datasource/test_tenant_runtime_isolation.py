"""数据源运行时租户隔离回归测试。"""

import asyncio
from unittest.mock import AsyncMock


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
