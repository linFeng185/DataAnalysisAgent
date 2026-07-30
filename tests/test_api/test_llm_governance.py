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
            "capability_schema": {"fields": []},
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
                capability_schema={"fields": []},
            ),
        )

        # Assert
        assert result["protocol"] == "openai_compatible"
        assert "api_key" not in result
        assert json.loads(connection.fetchrow.await_args.args[-1]) == {"fields": []}

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
        connection.fetchval = AsyncMock(return_value={
            "fields": [
                {"key": "streaming", "label": "流式输出", "type": "boolean"},
                {
                    "key": "context_window",
                    "label": "上下文窗口",
                    "type": "integer",
                },
            ],
        })
        monkeypatch.setattr(auth, "require_super_admin", lambda: None)
        monkeypatch.setattr(
            llm_admin,
            "get_pg_pool",
            AsyncMock(return_value=_fake_pool(connection)),
        )

        # Act
        result = await llm_admin.create_model_catalog_entry(
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
        assert result["id"] == 21
        assert result["capabilities"] == stored

    # 方法作用：验证数据库返回的 JSONB 字符串会在 API 边界恢复为能力对象。
    # Args: self - pytest 测试类实例；monkeypatch - pytest 补丁工具。
    # Returns: 无返回值，断言失败时由 pytest 报告。
    async def test_model_list_normalizes_jsonb_string_capabilities(self, monkeypatch) -> None:
        """前端动态表单必须收到对象，不能收到再次编码的 JSON 字符串。"""
        # Arrange
        import src.api.auth as auth
        import src.api.routes.llm_admin as llm_admin

        connection = MagicMock()
        connection.fetch = AsyncMock(return_value=[{
            "id": 21,
            "provider_id": 5,
            "model_id": "model-a",
            "display_name": "Model A",
            "capabilities": '{"reasoning":true,"reasoning_efforts":["high"]}',
            "is_active": True,
        }])
        monkeypatch.setattr(auth, "require_tenant_admin", lambda: None)
        monkeypatch.setattr(
            llm_admin,
            "get_pg_pool",
            AsyncMock(return_value=_fake_pool(connection)),
        )

        # Act
        result = await llm_admin.list_model_catalog(5)

        # Assert
        assert result["models"][0]["capabilities"] == {
            "reasoning": True,
            "reasoning_efforts": ["high"],
        }

    # 方法作用：验证超级管理员可以物理删除未被租户连接引用的模型。
    # Args: self - pytest 测试类实例；monkeypatch - pytest 补丁工具。
    # Returns: 无返回值，断言失败时由 pytest 报告。
    async def test_super_admin_deletes_unused_model(self, monkeypatch) -> None:
        """删除成功后目录记录必须真正消失，而不是只写 is_active=false。"""
        # Arrange
        import src.api.auth as auth
        import src.api.routes.llm_admin as llm_admin

        connection = MagicMock()
        connection.execute = AsyncMock(return_value="DELETE 1")
        monkeypatch.setattr(auth, "require_super_admin", lambda: None)
        monkeypatch.setattr(
            llm_admin,
            "get_pg_pool",
            AsyncMock(return_value=_fake_pool(connection)),
        )

        # Act
        result = await llm_admin.delete_model_catalog_entry(21)

        # Assert
        assert result == {"status": "ok", "model_catalog_id": 21}
        assert "DELETE FROM llm_model_catalog" in connection.execute.await_args.args[0]

    # 方法作用：验证被租户连接引用的模型不会被物理删除。
    # Args: self - pytest 测试类实例；monkeypatch - pytest 补丁工具。
    # Returns: 无返回值，断言失败时由 pytest 报告。
    async def test_referenced_model_delete_returns_conflict(self, monkeypatch) -> None:
        """平台删除不能静默破坏租户连接和默认模型。"""
        # Arrange
        from fastapi import HTTPException

        import src.api.auth as auth
        import src.api.routes.llm_admin as llm_admin

        class ForeignKeyViolationError(Exception):
            """模拟 asyncpg 外键冲突。"""

        connection = MagicMock()
        connection.execute = AsyncMock(side_effect=ForeignKeyViolationError("referenced"))
        monkeypatch.setattr(auth, "require_super_admin", lambda: None)
        monkeypatch.setattr(
            llm_admin,
            "get_pg_pool",
            AsyncMock(return_value=_fake_pool(connection)),
        )

        # Act / Assert
        try:
            await llm_admin.delete_model_catalog_entry(21)
        except HTTPException as exc:
            assert exc.status_code == 409
            assert "租户连接" in exc.detail
        else:
            raise AssertionError("被引用模型应返回 409")

    # 方法作用：验证超级管理员删除无租户连接的厂商及其模型目录。
    # Args: self - pytest 测试类实例；monkeypatch - pytest 补丁工具。
    # Returns: 无返回值，断言失败时由 pytest 报告。
    async def test_super_admin_deletes_unused_provider_and_models(self, monkeypatch) -> None:
        """厂商无租户连接时应在同一事务删除子模型和厂商记录。"""
        # Arrange
        import src.api.auth as auth
        import src.api.routes.llm_admin as llm_admin

        connection = MagicMock()
        connection.fetchval = AsyncMock(return_value=0)
        connection.execute = AsyncMock(side_effect=["DELETE 2", "DELETE 1"])
        transaction = MagicMock()
        transaction.__aenter__ = AsyncMock(return_value=None)
        transaction.__aexit__ = AsyncMock(return_value=False)
        connection.transaction.return_value = transaction
        monkeypatch.setattr(auth, "require_super_admin", lambda: None)
        monkeypatch.setattr(
            llm_admin,
            "get_pg_pool",
            AsyncMock(return_value=_fake_pool(connection)),
        )

        # Act
        result = await llm_admin.delete_provider_catalog_entry(5)

        # Assert
        assert result == {"status": "ok", "provider_id": 5}
        statements = [call.args[0] for call in connection.execute.await_args_list]
        assert "DELETE FROM llm_model_catalog" in statements[0]
        assert "DELETE FROM llm_provider_catalog" in statements[1]

    # 方法作用：验证仍被租户连接引用的厂商不能被物理删除。
    # Args: self - pytest 测试类实例；monkeypatch - pytest 补丁工具。
    # Returns: 无返回值，断言失败时由 pytest 报告。
    async def test_referenced_provider_delete_returns_conflict(self, monkeypatch) -> None:
        """删除厂商前必须检查租户连接，且冲突时不能触碰模型目录。"""
        # Arrange
        from fastapi import HTTPException

        import src.api.auth as auth
        import src.api.routes.llm_admin as llm_admin

        connection = MagicMock()
        connection.fetchval = AsyncMock(return_value=1)
        connection.execute = AsyncMock()
        transaction = MagicMock()
        transaction.__aenter__ = AsyncMock(return_value=None)
        transaction.__aexit__ = AsyncMock(return_value=False)
        connection.transaction.return_value = transaction
        monkeypatch.setattr(auth, "require_super_admin", lambda: None)
        monkeypatch.setattr(
            llm_admin,
            "get_pg_pool",
            AsyncMock(return_value=_fake_pool(connection)),
        )

        # Act / Assert
        try:
            await llm_admin.delete_provider_catalog_entry(5)
        except HTTPException as exc:
            assert exc.status_code == 409
            assert "租户连接" in exc.detail
        else:
            raise AssertionError("被租户连接引用的厂商应返回 409")
        connection.execute.assert_not_awaited()

    # 方法作用：验证检查后并发出现外键引用时仍返回业务冲突而不是 500。
    # Args: self - pytest 测试类实例；monkeypatch - pytest 补丁工具。
    # Returns: 无返回值，断言失败时由 pytest 报告。
    async def test_provider_delete_maps_foreign_key_race_to_conflict(self, monkeypatch) -> None:
        """引用检查与删除之间的竞态必须由数据库外键兜底并转换为 409。"""
        # Arrange
        from fastapi import HTTPException

        import src.api.auth as auth
        import src.api.routes.llm_admin as llm_admin

        class ForeignKeyViolationError(Exception):
            """模拟 asyncpg 外键冲突。"""

        connection = MagicMock()
        connection.fetchval = AsyncMock(return_value=0)
        connection.execute = AsyncMock(side_effect=ForeignKeyViolationError("referenced"))
        transaction = MagicMock()
        transaction.__aenter__ = AsyncMock(return_value=None)
        transaction.__aexit__ = AsyncMock(return_value=False)
        connection.transaction.return_value = transaction
        monkeypatch.setattr(auth, "require_super_admin", lambda: None)
        monkeypatch.setattr(
            llm_admin,
            "get_pg_pool",
            AsyncMock(return_value=_fake_pool(connection)),
        )

        # Act / Assert
        try:
            await llm_admin.delete_provider_catalog_entry(5)
        except HTTPException as exc:
            assert exc.status_code == 409
            assert "租户连接" in exc.detail
        else:
            raise AssertionError("并发外键引用应返回 409")


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
