"""Cookie 认证与请求身份上下文安全测试。"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest
from starlette.requests import Request
from starlette.responses import Response


class TestCookieAuthentication:
    """覆盖 Cookie 登录、登出和 Bearer 兼容。"""

    async def test_login_sets_httponly_cookie_without_returning_token(self, monkeypatch):
        """登录应把 JWT 写入 HttpOnly Cookie，响应体不得暴露令牌。"""
        # Arrange
        from passlib.hash import bcrypt

        import src.api.auth as auth

        connection = MagicMock()
        connection.fetchrow = AsyncMock(return_value={
            "id": 7,
            "tenant_id": 3,
            "role": "analyst",
            "password_hash": "test-hash",
        })
        acquire = MagicMock()
        acquire.__aenter__ = AsyncMock(return_value=connection)
        acquire.__aexit__ = AsyncMock(return_value=None)
        pool = MagicMock()
        pool.acquire.return_value = acquire
        monkeypatch.setattr(bcrypt, "verify", lambda password, password_hash: True)
        monkeypatch.setattr(auth, "get_pg_pool", AsyncMock(return_value=pool))
        monkeypatch.setattr(auth, "get_settings", lambda: SimpleNamespace(
            database_url="postgresql+asyncpg://test",
            jwt_secret="x" * 32,
            jwt_access_token_expire_hours=24,
            env="test",
        ))
        monkeypatch.setattr(auth, "_secret_cache", None)
        response = Response()

        # Act
        body = await auth.login(
            auth.LoginRequest(username="alice", password="secret123"),
            response,
        )

        # Assert
        cookie = response.headers.get("set-cookie", "")
        assert "access_token=" in cookie
        assert "HttpOnly" in cookie
        assert "SameSite=lax" in cookie
        assert "access_token" not in body

    async def test_register_sets_httponly_cookie_without_returning_token(self, monkeypatch):
        """注册成功后应直接建立 Cookie 会话且不暴露 JWT。"""
        # Arrange
        from passlib.hash import bcrypt

        import src.api.auth as auth

        connection = MagicMock()
        connection.fetchval = AsyncMock(return_value=8)
        transaction = MagicMock()
        transaction.__aenter__ = AsyncMock(return_value=None)
        transaction.__aexit__ = AsyncMock(return_value=None)
        connection.transaction.return_value = transaction
        acquire = MagicMock()
        acquire.__aenter__ = AsyncMock(return_value=connection)
        acquire.__aexit__ = AsyncMock(return_value=None)
        pool = MagicMock()
        pool.acquire.return_value = acquire
        monkeypatch.setattr(bcrypt, "hash", lambda password: "test-hash")
        monkeypatch.setattr(auth, "get_pg_pool", AsyncMock(return_value=pool))
        monkeypatch.setattr(auth, "get_settings", lambda: SimpleNamespace(
            database_url="postgresql+asyncpg://test",
            multi_tenant=False,
            jwt_secret="x" * 32,
            jwt_access_token_expire_hours=24,
            env="test",
        ))
        monkeypatch.setattr(auth, "_secret_cache", None)
        response = Response()

        # Act
        body = await auth.register(
            auth.RegisterRequest(username="bob", password="secret123"),
            response,
        )

        # Assert
        assert "access_token=" in response.headers.get("set-cookie", "")
        assert "HttpOnly" in response.headers.get("set-cookie", "")
        assert "access_token" not in body

    async def test_logout_clears_access_cookie(self):
        """登出应立即清除服务端认证 Cookie。"""
        # Arrange
        import src.api.auth as auth

        response = Response()

        # Act
        body = await auth.logout(response)

        # Assert
        assert body == {"status": "ok"}
        cookie = response.headers.get("set-cookie", "")
        assert "access_token=" in cookie
        assert "Max-Age=0" in cookie


class TestAuthContextIsolation:
    """覆盖请求身份注入与 ContextVar 清理。"""

    async def test_cookie_authenticates_and_context_is_reset(self, monkeypatch):
        """Cookie 身份仅在当前请求内可见，请求结束后恢复默认值。"""
        # Arrange
        import src.api.auth as auth

        settings = SimpleNamespace(
            multi_tenant=True,
            admin_api_key="",
            jwt_secret="y" * 32,
            jwt_access_token_expire_hours=24,
            env="test",
        )
        monkeypatch.setattr(auth, "get_settings", lambda: settings)
        monkeypatch.setattr(auth, "_secret_cache", None)
        token = auth.create_access_token(9, 4, "viewer")
        scope = {
            "type": "http",
            "http_version": "1.1",
            "method": "GET",
            "scheme": "http",
            "path": "/api/v1/history",
            "raw_path": b"/api/v1/history",
            "query_string": b"",
            "headers": [(b"cookie", f"access_token={token}".encode("ascii"))],
            "client": ("127.0.0.1", 1234),
            "server": ("test", 80),
        }
        request = Request(scope)
        middleware = auth.AuthMiddleware(AsyncMock())
        seen: dict[str, int] = {}

        async def call_next(_: Request) -> Response:
            """记录中间件注入的请求身份。"""
            seen["user_id"] = auth.get_current_user_id()
            seen["tenant_id"] = auth.get_current_tenant_id()
            return Response("ok")

        # Act
        response = await middleware.dispatch(request, call_next)

        # Assert
        assert response.status_code == 200
        assert seen == {"user_id": 9, "tenant_id": 4}
        assert auth.get_current_user_id() == 0
        assert auth.get_current_tenant_id() == 1

    async def test_missing_token_returns_401_in_multi_tenant_mode(self, monkeypatch):
        """多租户模式缺少令牌时应返回 401 响应而非抛出中间件异常。"""
        # Arrange
        import src.api.auth as auth

        monkeypatch.setattr(auth, "get_settings", lambda: SimpleNamespace(
            multi_tenant=True,
            admin_api_key="",
        ))
        scope = {
            "type": "http",
            "http_version": "1.1",
            "method": "GET",
            "scheme": "http",
            "path": "/api/v1/history",
            "raw_path": b"/api/v1/history",
            "query_string": b"",
            "headers": [],
            "client": ("127.0.0.1", 1234),
            "server": ("test", 80),
        }
        middleware = auth.AuthMiddleware(AsyncMock())

        # Act
        response = await middleware.dispatch(Request(scope), AsyncMock(return_value=Response("ok")))

        # Assert
        assert response.status_code == 401

    async def test_auth_probe_is_available_without_token(self, monkeypatch):
        """多租户未登录时允许身份探测端点返回认证开关。"""
        # Arrange
        import src.api.auth as auth

        monkeypatch.setattr(auth, "get_settings", lambda: SimpleNamespace(
            multi_tenant=True,
            admin_api_key="",
        ))
        scope = {
            "type": "http",
            "http_version": "1.1",
            "method": "GET",
            "scheme": "http",
            "path": "/api/v1/auth/me",
            "raw_path": b"/api/v1/auth/me",
            "query_string": b"",
            "headers": [],
            "client": ("127.0.0.1", 1234),
            "server": ("test", 80),
        }
        middleware = auth.AuthMiddleware(AsyncMock())

        async def call_next(_: Request) -> Response:
            """返回身份探测结果。"""
            return Response("probe")

        # Act
        response = await middleware.dispatch(Request(scope), call_next)

        # Assert
        assert response.status_code == 200

    # 方法作用：验证 ADMIN_API_KEY 不再阻断普通业务 POST 请求。
    # Args: self - pytest 测试类实例；monkeypatch - pytest 补丁工具；path - 业务路径。
    # Returns: 无返回值，断言失败时由 pytest 报告。
    @pytest.mark.parametrize("path", ["/api/v1/chat", "/api/v1/knowledge/docs/upload"])
    async def test_admin_api_key_does_not_gate_business_posts(self, monkeypatch, path: str):
        """已认证用户调用聊天和个人资源写入时不需要平台管理 Key。"""
        # Arrange
        import src.api.auth as auth

        settings = SimpleNamespace(
            multi_tenant=True,
            admin_api_key="a" * 32,
            jwt_secret="z" * 32,
            jwt_access_token_expire_hours=24,
            env="test",
        )
        monkeypatch.setattr(auth, "get_settings", lambda: settings)
        monkeypatch.setattr(auth, "_secret_cache", None)
        token = auth.create_access_token(9, 4, "analyst")
        scope = {
            "type": "http", "http_version": "1.1", "method": "POST",
            "scheme": "http", "path": path, "raw_path": path.encode("ascii"),
            "query_string": b"", "headers": [(b"authorization", f"Bearer {token}".encode("ascii"))],
            "client": ("127.0.0.1", 1234), "server": ("test", 80),
        }

        # Act
        response = await auth.AuthMiddleware(AsyncMock()).dispatch(
            Request(scope), AsyncMock(return_value=Response("ok")),
        )

        # Assert
        assert response.status_code == 200

    # 方法作用：验证平台管理写端点仍然强制 ADMIN_API_KEY。
    # Args: self - pytest 测试类实例；monkeypatch - pytest 补丁工具。
    # Returns: 无返回值，断言失败时由 pytest 报告。
    async def test_admin_api_key_still_gates_platform_management(self, monkeypatch):
        """数据源注册属于平台管理操作，只有 JWT 仍不足以放行。"""
        # Arrange
        import src.api.auth as auth

        settings = SimpleNamespace(
            multi_tenant=True,
            admin_api_key="a" * 32,
            jwt_secret="z" * 32,
            jwt_access_token_expire_hours=24,
            env="test",
        )
        monkeypatch.setattr(auth, "get_settings", lambda: settings)
        monkeypatch.setattr(auth, "_secret_cache", None)
        token = auth.create_access_token(9, 4, "super_admin")
        path = "/api/v1/datasources"
        base_scope = {
            "type": "http", "http_version": "1.1", "method": "POST",
            "scheme": "http", "path": path, "raw_path": path.encode("ascii"),
            "query_string": b"", "client": ("127.0.0.1", 1234), "server": ("test", 80),
        }
        middleware = auth.AuthMiddleware(AsyncMock())

        # Act
        denied_scope = {
            **base_scope,
            "headers": [(b"authorization", f"Bearer {token}".encode("ascii"))],
        }
        allowed_scope = {
            **base_scope,
            "headers": [
                (b"authorization", f"Bearer {token}".encode("ascii")),
                (b"x-admin-key", settings.admin_api_key.encode("ascii")),
            ],
        }
        denied = await middleware.dispatch(
            Request(denied_scope), AsyncMock(return_value=Response("ok")),
        )
        allowed = await middleware.dispatch(
            Request(allowed_scope), AsyncMock(return_value=Response("ok")),
        )

        # Assert
        assert denied.status_code == 401
        assert allowed.status_code == 200

    async def test_current_user_reports_auth_requirement(self, monkeypatch):
        """身份查询应返回当前上下文及服务端认证开关。"""
        # Arrange
        import src.api.auth as auth

        monkeypatch.setattr(auth, "get_settings", lambda: SimpleNamespace(multi_tenant=True))
        user_token = auth._current_user_id.set(9)  # noqa: SLF001
        tenant_token = auth._current_tenant_id.set(4)  # noqa: SLF001
        role_token = auth._current_role.set("viewer")  # noqa: SLF001

        try:
            # Act
            result = await auth.current_user()
        finally:
            auth._current_user_id.reset(user_token)  # noqa: SLF001
            auth._current_tenant_id.reset(tenant_token)  # noqa: SLF001
            auth._current_role.reset(role_token)  # noqa: SLF001

        # Assert
        assert result == {
            "authenticated": True,
            "auth_required": True,
            "user_id": 9,
            "tenant_id": 4,
            "role": "viewer",
        }


class TestAdministratorBoundaries:
    """覆盖平台超级管理员与租户管理员的授权边界。"""

    # 验证平台治理端点只允许超级管理员。
    # Args: self - pytest 测试类实例；monkeypatch - pytest 补丁工具。
    # Returns: 无返回值，断言失败时由 pytest 报告。
    def test_require_super_admin_rejects_tenant_admin(self, monkeypatch) -> None:
        """租户管理员不能获得平台级全局治理权限。"""
        # Arrange
        import src.api.auth as auth

        monkeypatch.setattr(auth, "get_current_role", lambda: "tenant_admin")

        # Act / Assert
        with pytest.raises(Exception) as caught:
            auth.require_super_admin()
        assert getattr(caught.value, "status_code", None) == 403

    # 验证租户管理端点同时允许租户管理员和超级管理员。
    # Args: self - pytest 测试类实例；monkeypatch - pytest 补丁工具。
    # Returns: 无返回值，断言失败时由 pytest 报告。
    @pytest.mark.parametrize("role", ["tenant_admin", "super_admin"])
    def test_require_tenant_admin_accepts_both_admin_roles(self, monkeypatch, role: str) -> None:
        """超级管理员可以覆盖租户管理权限，但租户管理员不反向覆盖平台权限。"""
        # Arrange
        import src.api.auth as auth

        monkeypatch.setattr(auth, "get_current_role", lambda: role)

        # Act / Assert
        assert auth.require_tenant_admin() is None
