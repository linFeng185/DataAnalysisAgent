"""平台超级管理员、强制认证和后台路由回归测试。"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest
from fastapi import HTTPException


logger = logging.getLogger(__name__)


class TestPlatformIdentity:
    """覆盖功能 21.2.1、21.2.2 和 21.2.4。"""

    # 方法作用：验证单租户模式仍然要求登录，仅身份探测允许匿名。
    # Args: self - pytest 测试类实例。
    # Returns: 无返回值，断言失败时由 pytest 报告。
    def test_single_tenant_still_requires_authentication(self) -> None:
        """multi_tenant 只能控制隔离，不能关闭认证。"""
        logger.debug("test_single_tenant_still_requires_authentication 入口")
        from src.security.tenant_policy import TenantPolicy

        policy = TenantPolicy(multi_tenant=False)

        assert policy.requires_authentication() is True
        assert policy.requires_authentication(is_probe=True) is False
        logger.info("test_single_tenant_still_requires_authentication 完成")

    # 方法作用：验证超级管理员权限同时要求固定用户编号和角色。
    # Args: self - pytest 测试类实例；monkeypatch - 身份补丁。
    # Returns: 无返回值，断言失败时由 pytest 报告。
    def test_super_admin_requires_reserved_user_id(self, monkeypatch) -> None:
        """普通账号即使伪造 super_admin 角色也不能获得平台权限。"""
        logger.debug("test_super_admin_requires_reserved_user_id 入口")
        import src.api.auth as auth

        monkeypatch.setattr(auth, "get_current_role", lambda: "super_admin")
        monkeypatch.setattr(auth, "get_current_user_id", lambda: 2)

        with pytest.raises(HTTPException, match="超级管理员"):
            auth.require_super_admin()
        logger.info("test_super_admin_requires_reserved_user_id 完成")

    # 方法作用：验证关闭公开注册时在数据库操作前拒绝请求。
    # Args: self - pytest 测试类实例；monkeypatch - 配置补丁。
    # Returns: 无返回值，断言失败时由 pytest 报告。
    async def test_registration_switch_blocks_public_registration(self, monkeypatch) -> None:
        """registration_enabled=false 时不能通过公开端点创建账号。"""
        logger.debug("test_registration_switch_blocks_public_registration 入口")
        import src.api.auth as auth

        monkeypatch.setattr(
            auth,
            "get_settings",
            lambda: SimpleNamespace(registration_enabled=False),
        )
        request = auth.RegisterRequest(
            username="blocked-user",
            password="StrongPassword123!",
        )

        with pytest.raises(HTTPException, match="注册") as exc_info:
            await auth.register(request, response=SimpleNamespace(), request=None)

        assert exc_info.value.status_code == 403
        logger.info("test_registration_switch_blocks_public_registration 完成")

    # 方法作用：验证认证状态端点向前端返回注册开关且始终要求登录。
    # Args: self - pytest 测试类实例；monkeypatch - 配置和身份补丁。
    # Returns: 无返回值，断言失败时由 pytest 报告。
    async def test_auth_probe_exposes_registration_switch(self, monkeypatch) -> None:
        """登录页应由服务端开关决定是否显示注册入口。"""
        logger.debug("test_auth_probe_exposes_registration_switch 入口")
        import src.api.auth as auth

        monkeypatch.setattr(auth, "get_current_user_id", lambda: 0)
        monkeypatch.setattr(auth, "get_current_tenant_id", lambda: 1)
        monkeypatch.setattr(auth, "get_current_role", lambda: "anonymous")
        monkeypatch.setattr(
            auth,
            "get_settings",
            lambda: SimpleNamespace(registration_enabled=True),
        )

        result = await auth.current_user()

        assert result["auth_required"] is True
        assert result["registration_enabled"] is True
        logger.info("test_auth_probe_exposes_registration_switch 完成")

    # 方法作用：验证达到失败阈值后数据库写入账号锁定时间。
    # Args: self - pytest 测试类实例；monkeypatch - 密码和数据库补丁。
    # Returns: 无返回值，断言失败时由 pytest 报告。
    async def test_login_failure_persists_account_lock(self, monkeypatch) -> None:
        """单进程限流之外必须有跨进程共享的账号锁定。"""
        logger.debug("test_login_failure_persists_account_lock 入口")
        import src.api.auth as auth

        connection = MagicMock()
        locked_until = datetime(2026, 7, 24, 12, 30, tzinfo=timezone.utc)
        connection.fetchrow = AsyncMock(side_effect=[
            {
                "id": 7, "tenant_id": 2, "role": "analyst", "password_hash": "hash",
                "is_active": True, "failed_login_attempts": 0, "locked_until": None,
            },
            {"failed_login_attempts": 1, "locked_until": locked_until},
        ])
        connection.execute = AsyncMock(return_value="UPDATE 1")
        acquire = MagicMock()
        acquire.__aenter__ = AsyncMock(return_value=connection)
        acquire.__aexit__ = AsyncMock(return_value=False)
        pool = MagicMock()
        pool.acquire.return_value = acquire
        monkeypatch.setattr(auth, "get_pg_pool", AsyncMock(return_value=pool))
        monkeypatch.setattr(auth, "_verify_password", lambda password, password_hash: False)
        monkeypatch.setattr(
            auth,
            "get_settings",
            lambda: SimpleNamespace(
                login_max_per_hour=20,
                login_lockout_threshold=1,
                login_lockout_minutes=15,
            ),
        )
        auth._login_limits.clear()  # noqa: SLF001

        with pytest.raises(HTTPException) as exc_info:
            await auth.login(
                auth.LoginRequest(username="alice", password="wrong-pass"),
                response=SimpleNamespace(),
            )

        assert exc_info.value.status_code == 401
        update_args = connection.fetchrow.await_args_list[1].args
        assert "failed_login_attempts=failed_login_attempts+1" in update_args[0]
        assert update_args[1] == 1
        assert update_args[2] is not None
        assert update_args[3] == 7
        connection.execute.assert_not_awaited()
        logger.info("test_login_failure_persists_account_lock 完成")


class TestPlatformAdminRoutes:
    """覆盖功能 21.3.1-21.3.3 的路由契约。"""

    # 方法作用：验证平台管理路由全部挂载到统一 API Router。
    # Args: self - pytest 测试类实例。
    # Returns: 无返回值，断言失败时由 pytest 报告。
    def test_admin_routes_are_registered(self) -> None:
        """租户、用户和配置摘要端点缺一不可。"""
        logger.debug("test_admin_routes_are_registered 入口")
        from src.api.routes import router
        from src.api.routes.admin import router as admin_router

        included_routers = [
            route.original_router
            for route in router.routes
            if hasattr(route, "original_router")
        ]
        admin_included = any(candidate is admin_router for candidate in included_routers)
        routes = {
            (method, route.path)
            for route in admin_router.routes
            for method in (getattr(route, "methods", None) or set())
        }
        logger.info(
            "平台管理路由注册探针",
            extra={
                "included": admin_included,
                "route_count": len(routes),
            },
        )

        assert admin_included is True
        assert ("GET", "/admin/tenants") in routes
        assert ("POST", "/admin/tenants") in routes
        assert ("GET", "/admin/users") in routes
        assert ("POST", "/admin/users") in routes
        assert ("GET", "/admin/config") in routes
        logger.info("test_admin_routes_are_registered 完成")

    # 方法作用：验证迁移保护固定超级管理员并补充登录锁定字段。
    # Args: self - pytest 测试类实例。
    # Returns: 无返回值，断言失败时由 pytest 报告。
    def test_platform_admin_migration_contract(self) -> None:
        """数据库层必须阻止 id=1 被降权并持久化爆破状态。"""
        logger.debug("test_platform_admin_migration_contract 入口")
        from pathlib import Path

        sql = Path("migrations/006_platform_admin.sql").read_text(encoding="utf-8")

        assert "failed_login_attempts" in sql
        assert "locked_until" in sql
        assert "id = 1 AND role = 'super_admin'" in sql
        assert "id <> 1 AND role <> 'super_admin'" in sql
        assert "datasource_configs" in sql
        logger.info("test_platform_admin_migration_contract 完成")

    # 方法作用：验证租户与首个管理员在同一事务中创建。
    # Args: self - pytest 测试类实例；monkeypatch - 身份、密码和数据库补丁。
    # Returns: 无返回值，断言失败时由 pytest 报告。
    async def test_create_tenant_creates_tenant_admin_atomically(self, monkeypatch) -> None:
        """租户创建不能留下没有管理员的半成品。"""
        logger.debug("test_create_tenant_creates_tenant_admin_atomically 入口")
        import src.api.auth as auth
        import src.memory.pg_pool as pg_pool
        from src.api.routes.admin import TenantCreateRequest, create_tenant

        connection = MagicMock()
        connection.fetchval = AsyncMock(side_effect=[None, 2, 8])
        transaction = MagicMock()
        transaction.__aenter__ = AsyncMock(return_value=None)
        transaction.__aexit__ = AsyncMock(return_value=False)
        connection.transaction = MagicMock(return_value=transaction)
        acquire = MagicMock()
        acquire.__aenter__ = AsyncMock(return_value=connection)
        acquire.__aexit__ = AsyncMock(return_value=False)
        pool = MagicMock()
        pool.acquire.return_value = acquire
        monkeypatch.setattr(auth, "require_super_admin", lambda: None)
        monkeypatch.setattr(auth, "_hash_password", lambda password: "bcrypt-hash")
        monkeypatch.setattr(pg_pool, "get_pg_pool", AsyncMock(return_value=pool))

        result = await create_tenant(TenantCreateRequest(
            name="Acme", admin_username="acme-admin", admin_password="StrongPass123!",
        ))

        assert result["tenant"]["id"] == 2
        assert result["admin"]["id"] == 8
        assert result["admin"]["role"] == "tenant_admin"
        assert transaction.__aenter__.await_count == 1
        logger.info("test_create_tenant_creates_tenant_admin_atomically 完成")

    # 方法作用：验证后台不能修改固定超级管理员的角色或状态。
    # Args: self - pytest 测试类实例；monkeypatch - 平台身份补丁。
    # Returns: 无返回值，断言失败时由 pytest 报告。
    async def test_update_user_protects_reserved_admin(self, monkeypatch) -> None:
        """固定账号保护必须在数据库调用前生效。"""
        logger.debug("test_update_user_protects_reserved_admin 入口")
        import src.api.auth as auth
        from src.api.routes.admin import UserUpdateRequest, update_user

        monkeypatch.setattr(auth, "require_super_admin", lambda: None)

        with pytest.raises(HTTPException) as exc_info:
            await update_user(1, UserUpdateRequest(role="viewer", is_active=False))

        assert exc_info.value.status_code == 403
        logger.info("test_update_user_protects_reserved_admin 完成")


class TestSuperAdminBootstrap:
    """覆盖固定超级管理员启动初始化的幂等与安全冲突路径。"""

    # 方法作用：验证已存在且有效的 id=1 超管不会被启动密码覆盖。
    # Args: self - pytest 测试类实例；monkeypatch - PG 池补丁。
    # Returns: 无返回值，断言失败时由 pytest 报告。
    async def test_existing_super_admin_is_left_unchanged(self, monkeypatch) -> None:
        """启动配置只能初始化缺失账号，不能静默重置密码。"""
        logger.debug("test_existing_super_admin_is_left_unchanged 入口")
        import src.memory.pg_pool as pg_pool
        from src.bootstrap import _ensure_super_admin

        connection = MagicMock()
        connection.fetchrow = AsyncMock(return_value={
            "id": 1, "username": "admin", "role": "super_admin", "is_active": True,
        })
        connection.execute = AsyncMock()
        transaction = MagicMock()
        transaction.__aenter__ = AsyncMock(return_value=None)
        transaction.__aexit__ = AsyncMock(return_value=False)
        connection.transaction = MagicMock(return_value=transaction)
        acquire = MagicMock()
        acquire.__aenter__ = AsyncMock(return_value=connection)
        acquire.__aexit__ = AsyncMock(return_value=False)
        pool = MagicMock()
        pool.acquire.return_value = acquire
        monkeypatch.setattr(pg_pool, "get_pg_pool", AsyncMock(return_value=pool))

        await _ensure_super_admin(SimpleNamespace(
            super_admin_username="admin", super_admin_password="",
        ))

        connection.execute.assert_not_awaited()
        logger.info("test_existing_super_admin_is_left_unchanged 完成")
