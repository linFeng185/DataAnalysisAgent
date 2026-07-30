"""平台模型目录与租户 LLM 命名连接 API 测试。"""

import json
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock


# 方法作用：构造支持 async with acquire 的数据库连接池替身。
# Args: connection - 需要由连接池返回的连接替身。
# Returns: 可供管理 API 使用的连接池替身。
def _fake_pool(connection: MagicMock) -> MagicMock:
    acquire = MagicMock()
    acquire.__aenter__ = AsyncMock(return_value=connection)
    acquire.__aexit__ = AsyncMock(return_value=False)
    pool = MagicMock()
    pool.acquire.return_value = acquire
    return pool


class TestPlatformLLMCatalog:
    """覆盖功能 10.1.9 的平台厂商与模型目录。"""

    # 方法作用：验证超级管理员可新增 OpenAI-compatible 厂商而无需新增业务分支。
    # Args: self - pytest 测试类实例；monkeypatch - pytest 补丁工具。
    # Returns: 无返回值，断言失败时由 pytest 报告。
    async def test_super_admin_creates_openai_compatible_provider(
        self,
        monkeypatch,
    ) -> None:
        """新厂商只声明协议、展示名和默认地址。"""
        # Arrange
        import src.api.auth as auth
        import src.api.routes.llm_admin as llm_admin

        connection = MagicMock()
        connection.fetchrow = AsyncMock(return_value={
            "id": 5,
            "code": "moonshot",
            "display_name": "Moonshot",
            "protocol": "openai_compatible",
            "default_base_url": "https://api.moonshot.cn/v1",
            "is_active": True,
        })
        monkeypatch.setattr(auth, "require_super_admin", lambda: None)
        monkeypatch.setattr(
            llm_admin,
            "get_pg_pool",
            AsyncMock(return_value=_fake_pool(connection)),
        )

        # Act
        result = await llm_admin.create_provider_catalog_entry(
            llm_admin.ProviderCatalogCreateRequest(
                code="moonshot",
                display_name="Moonshot",
                protocol="openai_compatible",
                default_base_url="https://api.moonshot.cn/v1",
            ),
        )

        # Assert
        assert result["protocol"] == "openai_compatible"
        assert "api_key" not in result

    # 方法作用：验证模型能力以能力对象本身写入 JSONB，而不是再次嵌套 capabilities。
    # Args: self - pytest 测试类实例；monkeypatch - pytest 补丁工具。
    # Returns: 无返回值，断言失败时由 pytest 报告。
    async def test_model_capabilities_are_serialized_without_extra_wrapper(
        self,
        monkeypatch,
    ) -> None:
        """数据库 capabilities 字段应直接保存 streaming/context_window 等键。"""
        # Arrange
        import src.api.auth as auth
        import src.api.routes.llm_admin as llm_admin

        connection = MagicMock()
        connection.fetchrow = AsyncMock(return_value={
            "id": 21,
            "provider_id": 5,
            "model_id": "model-a",
            "display_name": "Model A",
            "capabilities": {"streaming": True, "context_window": 32000},
            "is_active": True,
        })
        monkeypatch.setattr(auth, "require_super_admin", lambda: None)
        monkeypatch.setattr(
            llm_admin,
            "get_pg_pool",
            AsyncMock(return_value=_fake_pool(connection)),
        )

        # Act
        await llm_admin.create_model_catalog_entry(
            5,
            llm_admin.ModelCatalogCreateRequest(
                model_id="model-a",
                display_name="Model A",
                capabilities={"streaming": True, "context_window": 32000},
            ),
        )

        # Assert
        stored = json.loads(connection.fetchrow.await_args.args[-1])
        assert stored == {"streaming": True, "context_window": 32000}


class TestTenantLLMConnections:
    """覆盖功能 10.1.10 的租户命名连接。"""

    # 方法作用：验证租户管理员创建连接时使用当前租户并加密 API Key。
    # Args: self - pytest 测试类实例；monkeypatch - pytest 补丁工具。
    # Returns: 无返回值，断言失败时由 pytest 报告。
    async def test_tenant_admin_creates_encrypted_named_connection(
        self,
        monkeypatch,
    ) -> None:
        """响应只返回 api_key_configured，不能返回明文或密文。"""
        # Arrange
        import src.api.auth as auth
        import src.api.routes.llm_admin as llm_admin

        connection = MagicMock()
        connection.fetchrow = AsyncMock(side_effect=[
            {"id": 3, "protocol": "openai_compatible", "is_active": True},
            {
                "id": 11,
                "tenant_id": 7,
                "provider_id": 3,
                "name": "deepseek-primary",
                "base_url": "https://deepseek.example/v1",
                "is_active": True,
            },
        ])
        transaction = MagicMock()
        transaction.__aenter__ = AsyncMock(return_value=None)
        transaction.__aexit__ = AsyncMock(return_value=False)
        connection.transaction.return_value = transaction
        connection.execute = AsyncMock(return_value="INSERT 0 1")
        monkeypatch.setattr(auth, "require_tenant_user_admin", lambda: None)
        monkeypatch.setattr(auth, "get_current_tenant_id", lambda: 7)
        monkeypatch.setattr(
            llm_admin,
            "get_pg_pool",
            AsyncMock(return_value=_fake_pool(connection)),
        )
        monkeypatch.setattr(
            llm_admin,
            "CredentialManager",
            lambda: SimpleNamespace(encrypt=lambda value: "encrypted-key"),
        )

        # Act
        result = await llm_admin.create_tenant_connection(
            llm_admin.TenantConnectionCreateRequest(
                provider_id=3,
                name="deepseek-primary",
                base_url="https://deepseek.example/v1",
                api_key="tenant-secret",
                model_catalog_ids=[31],
            ),
        )

        # Assert
        insert_args = connection.fetchrow.await_args_list[1].args
        assert insert_args[-1] == "encrypted-key"
        assert 7 in insert_args
        assert result["api_key_configured"] is True
        assert "api_key" not in result
        assert "encrypted_api_key" not in result

    # 方法作用：验证连接更新时空 API Key 沿用原凭证且不触发加密。
    # Args: self - pytest 测试类实例；monkeypatch - pytest 补丁工具。
    # Returns: 无返回值，断言失败时由 pytest 报告。
    async def test_empty_api_key_keeps_existing_credential(self, monkeypatch) -> None:
        """管理员只改名称时不能清空或重写已经保存的 API Key。"""
        # Arrange
        import src.api.auth as auth
        import src.api.routes.llm_admin as llm_admin

        connection = MagicMock()
        connection.fetchrow = AsyncMock(return_value={
            "id": 11,
            "tenant_id": 7,
            "provider_id": 3,
            "name": "renamed",
            "base_url": "https://deepseek.example/v1",
            "is_active": True,
            "encrypted_api_key": "existing-encrypted-key",
        })
        transaction = MagicMock()
        transaction.__aenter__ = AsyncMock(return_value=None)
        transaction.__aexit__ = AsyncMock(return_value=False)
        connection.transaction.return_value = transaction
        encrypt = MagicMock(side_effect=AssertionError("空 API Key 不应重新加密"))
        monkeypatch.setattr(auth, "require_tenant_user_admin", lambda: None)
        monkeypatch.setattr(auth, "get_current_tenant_id", lambda: 7)
        monkeypatch.setattr(
            llm_admin,
            "get_pg_pool",
            AsyncMock(return_value=_fake_pool(connection)),
        )
        monkeypatch.setattr(
            llm_admin,
            "CredentialManager",
            lambda: SimpleNamespace(encrypt=encrypt),
        )

        # Act
        result = await llm_admin.update_tenant_connection(
            11,
            llm_admin.TenantConnectionUpdateRequest(name="renamed", api_key=""),
        )

        # Assert
        assert connection.fetchrow.await_args.args[3] is None
        assert result["api_key_configured"] is True
        encrypt.assert_not_called()

    # 方法作用：验证默认模型只能设置为当前租户连接已经启用的模型。
    # Args: self - pytest 测试类实例；monkeypatch - pytest 补丁工具。
    # Returns: 无返回值，断言失败时由 pytest 报告。
    async def test_tenant_admin_sets_default_model_for_current_tenant(
        self,
        monkeypatch,
    ) -> None:
        """默认写入必须同时携带 tenant_id、connection_id 和 model_catalog_id。"""
        # Arrange
        import src.api.auth as auth
        import src.api.routes.llm_admin as llm_admin

        connection = MagicMock()
        connection.fetchval = AsyncMock(return_value=1)
        connection.execute = AsyncMock(return_value="INSERT 0 1")
        monkeypatch.setattr(auth, "require_tenant_user_admin", lambda: None)
        monkeypatch.setattr(auth, "get_current_tenant_id", lambda: 7)
        monkeypatch.setattr(
            llm_admin,
            "get_pg_pool",
            AsyncMock(return_value=_fake_pool(connection)),
        )

        # Act
        result = await llm_admin.set_tenant_llm_default(
            llm_admin.TenantLLMDefaultRequest(
                connection_id=11,
                model_catalog_id=21,
            ),
        )

        # Assert
        assert connection.fetchval.await_args.args[1:] == (11, 7, 21)
        assert connection.execute.await_args.args[1:] == (7, 11, 21)
        assert result == {"connection_id": 11, "model_catalog_id": 21}
