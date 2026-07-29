"""数据源生命周期 API 契约测试。"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from src.api.schemas import DataSourceCreateRequest, DataSourceUpdateRequest


@pytest.mark.asyncio
class TestDatasourceLifecycleApi:
    """覆盖功能 18.3.4、18.3.5 的管理 API。"""

    # 方法作用：验证连接测试接口不注册或持久化临时数据源。
    # Args: self - pytest 测试类实例；monkeypatch - pytest 补丁工具。
    # Returns: 无返回值，断言失败时由 pytest 报告。
    async def test_connection_test_returns_probe_result_without_persistence(self, monkeypatch) -> None:
        """临时探测应只返回连通性结果。"""
        # Arrange
        import src.api.auth as auth
        import src.api.routes as routes

        provider = SimpleNamespace(probe_request=AsyncMock(return_value=True))
        registry = SimpleNamespace(
            get_provider=lambda name: provider if name == "external" else None,
            _create_engine=AsyncMock(),
        )
        monkeypatch.setattr(auth, "require_tenant_admin", lambda: None)
        monkeypatch.setattr(routes, "_registry", lambda: registry)
        request = DataSourceCreateRequest(
            name="temporary",
            dialect="postgres",
            password="secret",
        )

        # Act
        result = await routes.test_datasource_connection(request)

        # Assert
        assert result == {"success": True, "message": "连接成功"}
        provider.probe_request.assert_awaited_once_with(request, registry._create_engine)

    # 方法作用：验证编辑接口更新当前租户数据源并清除 Registry 缓存。
    # Args: self - pytest 测试类实例；monkeypatch - pytest 补丁工具。
    # Returns: 无返回值，断言失败时由 pytest 报告。
    async def test_update_datasource_replaces_owned_configuration(self, monkeypatch) -> None:
        """租户管理员应能编辑自己租户的外挂数据源。"""
        # Arrange
        import src.api.auth as auth
        import src.api.routes as routes

        existing = SimpleNamespace(tenant_id=3)
        updated = SimpleNamespace(
            name="sales", dialect="postgres", version="16", mode="external",
            host="new-db", port=5432, database="sales", username="readonly",
            description="新版只读库",
        )
        provider = SimpleNamespace(
            lookup=AsyncMock(return_value=existing),
            update=AsyncMock(return_value=updated),
        )
        registry = SimpleNamespace(
            get_provider=lambda name: provider if name == "external" else None,
            _create_engine=AsyncMock(),
            invalidate=MagicMock(return_value=True),
        )
        monkeypatch.setattr(auth, "require_tenant_admin", lambda: None)
        monkeypatch.setattr(auth, "get_current_tenant_id", lambda: 3)
        monkeypatch.setattr(auth, "get_current_user_id", lambda: 7)
        monkeypatch.setattr(auth, "is_platform_super_admin", lambda: False)
        monkeypatch.setattr(routes, "_registry", lambda: registry)
        request = DataSourceUpdateRequest(
            dialect="postgres",
            version="16",
            host="new-db",
            port=5432,
            database="sales",
            username="readonly",
            description="新版只读库",
        )

        # Act
        result = await routes.update_datasource("sales", request)

        # Assert
        assert result.host == "new-db"
        provider.update.assert_awaited_once_with(
            "sales",
            request,
            engine_factory=registry._create_engine,
            tenant_id=3,
            owner_user_id=7,
        )
        registry.invalidate.assert_called_once_with("sales")
