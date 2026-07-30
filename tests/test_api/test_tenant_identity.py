"""租户编码登录与租户用户自治测试。"""

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest
from fastapi import HTTPException, Response


# 方法作用：构造支持 async with acquire 的数据库连接池替身。
# Args: connection - 需要由连接池返回的连接替身。
# Returns: 可供业务代码使用的连接池替身。
def _fake_pool(connection: MagicMock) -> MagicMock:
    acquire = MagicMock()
    acquire.__aenter__ = AsyncMock(return_value=connection)
    acquire.__aexit__ = AsyncMock(return_value=False)
    pool = MagicMock()
    pool.acquire.return_value = acquire
    return pool


class TestTenantIdentity:
    """覆盖功能 21.2.5 的租户编码登录与注册阻断。"""

    # 方法作用：验证登录按规范化租户编码和大小写敏感用户名精确查询。
    # Args: self - pytest 测试类实例；monkeypatch - pytest 补丁工具。
    # Returns: 无返回值，断言失败时由 pytest 报告。
    async def test_login_uses_tenant_code_and_case_sensitive_username(
        self,
        monkeypatch,
    ) -> None:
        """Acme/Alice 登录必须查询 acme 租户下精确大小写的 Alice。"""
        # Arrange
        import src.api.auth as auth

        connection = MagicMock()
        connection.fetchrow = AsyncMock(return_value={
            "id": 7,
            "tenant_id": 3,
            "tenant_code": "acme",
            "role": "analyst",
            "password_hash": "hash",
            "is_active": True,
            "failed_login_attempts": 0,
            "locked_until": None,
            "tenant_active": True,
        })
        connection.execute = AsyncMock(return_value="UPDATE 1")
        monkeypatch.setattr(auth, "get_pg_pool", AsyncMock(return_value=_fake_pool(connection)))
        monkeypatch.setattr(auth, "_verify_password", lambda password, password_hash: True)
        monkeypatch.setattr(
            auth,
            "get_settings",
            lambda: SimpleNamespace(
                login_max_per_hour=20,
                login_lockout_threshold=5,
                login_lockout_minutes=15,
                jwt_access_token_expire_hours=24,
                jwt_secret="tenant-login-test-secret-value",
                env="test",
            ),
        )
        monkeypatch.setattr(auth, "_secret_cache", None)
        auth._login_limits.clear()  # noqa: SLF001
        response = Response()

        # Act
        body = await auth.login(
            auth.LoginRequest(tenant_code="Acme", username="Alice", password="secret123"),
            response,
        )

        # Assert
        query_args = connection.fetchrow.await_args.args
        assert "LOWER(t.code)=LOWER($1)" in query_args[0]
        assert "u.username=$2" in query_args[0]
        assert query_args[1:] == ("acme", "Alice")
        assert body["tenant_code"] == "acme"

    # 方法作用：验证公开注册即使旧配置开启也固定拒绝。
    # Args: self - pytest 测试类实例；monkeypatch - pytest 补丁工具。
    # Returns: 无返回值，断言失败时由 pytest 报告。
    async def test_public_registration_is_always_blocked(self, monkeypatch) -> None:
        """遗留 registration_enabled=true 不能重新开放公开注册。"""
        # Arrange
        import src.api.auth as auth

        get_pool = AsyncMock()
        monkeypatch.setattr(auth, "get_pg_pool", get_pool)
        monkeypatch.setattr(
            auth,
            "get_settings",
            lambda: SimpleNamespace(registration_enabled=True),
        )

        # Act / Assert
        with pytest.raises(HTTPException) as caught:
            await auth.register(
                auth.RegisterRequest(username="Alice", password="secret123"),
                Response(),
            )
        assert caught.value.status_code == 403
        get_pool.assert_not_awaited()

    # 方法作用：验证迁移建立租户编码和租户内大小写敏感用户名唯一约束。
    # Args: self - pytest 测试类实例。
    # Returns: 无返回值，断言失败时由 pytest 报告。
    def test_tenant_identity_migration_contract(self) -> None:
        """迁移必须移除全局用户名索引并保护不可变 tenant_code。"""
        # Arrange
        from pathlib import Path

        migration = Path("migrations/013_tenant_identity_llm.sql")

        # Act
        sql = migration.read_text(encoding="utf-8")

        # Assert
        assert "ADD COLUMN IF NOT EXISTS code" in sql
        assert "uq_tenants_code" in sql
        assert "prevent_tenant_code_change" in sql
        assert "DROP INDEX IF EXISTS uq_users_username_global" in sql
        assert "UNIQUE (tenant_id, username)" in sql


class TestTenantUserAdministration:
    """覆盖功能 21.3.6 的当前租户用户自治。"""

    # 方法作用：验证租户管理员可创建当前租户的另一个租户管理员。
    # Args: self - pytest 测试类实例；monkeypatch - pytest 补丁工具。
    # Returns: 无返回值，断言失败时由 pytest 报告。
    async def test_tenant_admin_creates_admin_in_current_tenant(
        self,
        monkeypatch,
    ) -> None:
        """请求不得携带或覆盖 tenant_id，归属必须来自认证上下文。"""
        # Arrange
        import src.api.auth as auth
        import src.memory.pg_pool as pg_pool
        from src.api.routes.admin import UserCreateRequest, create_user

        connection = MagicMock()
        connection.fetchrow = AsyncMock(return_value={
            "id": 12,
            "username": "TeamAdmin",
            "tenant_id": 7,
            "role": "tenant_admin",
            "is_active": True,
            "created_at": None,
        })
        monkeypatch.setattr(auth, "require_tenant_user_admin", lambda: None)
        monkeypatch.setattr(auth, "get_current_tenant_id", lambda: 7)
        monkeypatch.setattr(auth, "_hash_password", lambda password: "bcrypt-hash")
        monkeypatch.setattr(pg_pool, "get_pg_pool", AsyncMock(return_value=_fake_pool(connection)))

        # Act
        result = await create_user(UserCreateRequest(
            username="TeamAdmin",
            password="StrongPass123!",
            role="tenant_admin",
        ))

        # Assert
        insert_args = connection.fetchrow.await_args.args
        assert "tenant_id" in insert_args[0]
        assert insert_args[-1] == 7
        assert result["tenant_id"] == 7
        assert result["role"] == "tenant_admin"

    # 方法作用：验证租户用户更新同时按用户 ID 和当前租户 ID 过滤。
    # Args: self - pytest 测试类实例；monkeypatch - pytest 补丁工具。
    # Returns: 无返回值，断言失败时由 pytest 报告。
    async def test_update_user_filters_current_tenant(self, monkeypatch) -> None:
        """其他租户的相同 user_id 不得被当前租户管理员修改。"""
        # Arrange
        import src.api.auth as auth
        import src.memory.pg_pool as pg_pool
        from src.api.routes.admin import UserUpdateRequest, update_user

        connection = MagicMock()
        connection.fetchrow = AsyncMock(side_effect=[
            {"role": "analyst", "is_active": True},
            None,
        ])
        monkeypatch.setattr(auth, "require_tenant_user_admin", lambda: None)
        monkeypatch.setattr(auth, "get_current_tenant_id", lambda: 7)
        monkeypatch.setattr(pg_pool, "get_pg_pool", AsyncMock(return_value=_fake_pool(connection)))

        # Act / Assert
        with pytest.raises(HTTPException) as caught:
            await update_user(99, UserUpdateRequest(role="viewer"))
        assert caught.value.status_code == 404
        update_args = connection.fetchrow.await_args.args
        assert "tenant_id=$4" in update_args[0]
        assert update_args[-1] == 7

    # 方法作用：验证超级管理员不能进入租户用户管理边界。
    # Args: self - pytest 测试类实例；monkeypatch - pytest 补丁工具。
    # Returns: 无返回值，断言失败时由 pytest 报告。
    def test_super_admin_cannot_manage_tenant_users(self, monkeypatch) -> None:
        """平台超级管理员只管理租户生命周期和模型目录。"""
        # Arrange
        import src.api.auth as auth

        monkeypatch.setattr(auth, "get_current_role", lambda: "super_admin")

        # Act / Assert
        with pytest.raises(HTTPException) as caught:
            auth.require_tenant_user_admin()
        assert caught.value.status_code == 403
