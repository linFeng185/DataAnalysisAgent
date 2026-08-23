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
        registry.invalidate.assert_called_once_with("sales", tenant_id=3)

    # 方法作用：验证 Schema 显式名称必须属于当前租户的可见数据源快照。
    # Args: self - pytest 测试类实例；monkeypatch - 身份与 Registry 补丁。
    # Returns: 无返回值，断言失败时由 pytest 报告。
    async def test_schema_rejects_datasource_outside_visible_snapshot(self, monkeypatch) -> None:
        """不可见或不存在的数据源名称不得绕过 Schema ACL 边界。"""
        # Arrange
        from fastapi import HTTPException

        import src.api.auth as auth
        import src.api.routes as routes
        from src.api.routes.schema import _resolve_datasource_name

        identity = SimpleNamespace(tenant_id=3, user_id=7, role="analyst")
        registry = SimpleNamespace(list_all=AsyncMock(return_value=[{
            "name": "sales", "dialect": "postgres",
        }]))
        monkeypatch.setattr(auth, "get_current_identity", lambda: identity)
        monkeypatch.setattr(routes, "_registry", lambda: registry)

        # Act / Assert
        with pytest.raises(HTTPException) as caught:
            await _resolve_datasource_name("hidden")

        assert caught.value.status_code == 404
        registry.list_all.assert_awaited_once_with(tenant_id=3)

    # 方法作用：验证 Schema 数据源解析显式注入当前 AppContext 的集中租户策略。
    # Args: self - pytest 测试类实例；monkeypatch - 身份、Registry 与权限补丁。
    # Returns: 无返回值，断言失败时由 pytest 报告。
    async def test_schema_injects_central_tenant_policy(self, monkeypatch) -> None:
        """Schema ACL 不得因漏传策略回落到单租户 trusted 模式。"""
        # Arrange
        import src.api.auth as auth
        import src.api.routes as routes
        import src.app_context as app_context_module
        import src.security.permission_check as permission_module
        from src.api.routes.schema import _resolve_datasource_name

        identity = SimpleNamespace(tenant_id=3, user_id=7, role="analyst")
        registry = SimpleNamespace(list_all=AsyncMock(return_value=[{
            "name": "sales", "dialect": "postgres",
        }]))
        resolver = AsyncMock(return_value={"sales": {"name": "sales"}})
        policy = object()
        monkeypatch.setattr(auth, "get_current_identity", lambda: identity)
        monkeypatch.setattr(routes, "_registry", lambda: registry)
        monkeypatch.setattr(permission_module, "resolve_datasource_access", resolver)
        monkeypatch.setattr(app_context_module, "get_tenant_policy", lambda: policy)

        # Act
        result = await _resolve_datasource_name("sales")

        # Assert
        assert result == "sales"
        assert resolver.await_args.kwargs["tenant_policy"] is policy

    # 方法作用：验证 Schema Registry 内部 TypeError 不会触发无租户重试。
    # Args: self - pytest 测试类实例；monkeypatch - pytest 补丁工具。
    # Returns: 无返回值，断言失败时由 pytest 报告。
    async def test_schema_does_not_retry_internal_type_error(self, monkeypatch) -> None:
        """租户列表读取失败时必须保留原异常且只调用一次。"""
        # Arrange
        import src.api.auth as auth
        import src.api.routes as routes
        from src.api.routes.schema import _resolve_datasource_name

        identity = SimpleNamespace(tenant_id=3, user_id=7, role="analyst")
        calls = 0

        async def _list_all(*, tenant_id: int | None = None):
            nonlocal calls
            calls += 1
            assert tenant_id == 3
            raise TypeError("registry body failure")

        registry = SimpleNamespace(list_all=_list_all)
        monkeypatch.setattr(auth, "get_current_identity", lambda: identity)
        monkeypatch.setattr(routes, "_registry", lambda: registry)

        # Act / Assert
        with pytest.raises(TypeError, match="registry body failure"):
            await _resolve_datasource_name("sales")
        assert calls == 1
