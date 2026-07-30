"""租户 LLM 配置解析与统一调用测试。"""

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock


# 方法作用：构造支持 async with acquire 的数据库连接池替身。
# Args: connection - 需要由连接池返回的连接替身。
# Returns: 可供配置解析器使用的连接池替身。
def _fake_pool(connection: MagicMock) -> MagicMock:
    acquire = MagicMock()
    acquire.__aenter__ = AsyncMock(return_value=connection)
    acquire.__aexit__ = AsyncMock(return_value=False)
    pool = MagicMock()
    pool.acquire.return_value = acquire
    return pool


class TestTenantLLMSelection:
    """覆盖功能 10.1.11-10.1.12 的请求级选择与 Provider 解析。"""

    # 方法作用：验证请求级选择在上下文退出后精确恢复。
    # Args: self - pytest 测试类实例。
    # Returns: 无返回值，断言失败时由 pytest 报告。
    def test_selection_context_restores_previous_value(self) -> None:
        """并发请求不能复用其他租户的连接选择。"""
        # Arrange
        from src.llm.tenant_config import (
            TenantLLMSelection,
            get_current_tenant_llm_selection,
            use_tenant_llm_selection,
        )

        selection = TenantLLMSelection(
            tenant_id=7,
            connection_id=11,
            connection_name="openai-primary",
            provider_code="openai",
            protocol="openai_compatible",
            model_catalog_id=21,
            model_id="gpt-4o",
            base_url="https://llm.example/v1",
            api_key="secret",
        )

        # Act / Assert
        assert get_current_tenant_llm_selection() is None
        with use_tenant_llm_selection(selection):
            assert get_current_tenant_llm_selection() is selection
        assert get_current_tenant_llm_selection() is None

    # 方法作用：验证数据库解析只读取当前租户连接并解密 API Key。
    # Args: self - pytest 测试类实例；monkeypatch - pytest 补丁工具。
    # Returns: 无返回值，断言失败时由 pytest 报告。
    async def test_resolver_filters_tenant_and_decrypts_key(self, monkeypatch) -> None:
        """显式连接和模型必须同时属于 tenant_id=7。"""
        # Arrange
        import src.llm.tenant_config as tenant_config

        connection = MagicMock()
        connection.fetchrow = AsyncMock(return_value={
            "connection_id": 11,
            "connection_name": "openai-primary",
            "provider_code": "openai",
            "protocol": "openai_compatible",
            "model_catalog_id": 21,
            "model_id": "gpt-4o",
            "capabilities": {"reasoning": True, "reasoning_efforts": ["high", "max"]},
            "base_url": "https://llm.example/v1",
            "encrypted_api_key": "encrypted-value",
        })
        monkeypatch.setattr(
            tenant_config,
            "get_pg_pool",
            AsyncMock(return_value=_fake_pool(connection)),
        )
        monkeypatch.setattr(
            tenant_config,
            "CredentialManager",
            lambda: SimpleNamespace(decrypt=lambda value: "decrypted-key"),
        )

        # Act
        selection = await tenant_config.resolve_tenant_llm_selection(
            tenant_id=7,
            connection_id=11,
            model_id="gpt-4o",
        )

        # Assert
        query_args = connection.fetchrow.await_args.args
        assert "c.tenant_id=$1" in query_args[0]
        assert query_args[1:] == (7, 11, "gpt-4o")
        assert selection.api_key == "decrypted-key"
        assert "encrypted_api_key" not in selection.to_public_dict()
        assert "api_key" not in selection.to_public_dict()

    # 方法作用：验证租户模型能力允许时解析并归一化对话推理偏好。
    # Args: self - pytest 测试类实例；monkeypatch - pytest 补丁工具。
    # Returns: 无返回值，断言失败时由 pytest 报告。
    async def test_resolver_accepts_supported_reasoning_preference(self, monkeypatch) -> None:
        """用户选择 max 时应写入请求级选择且不进入持久化状态。"""
        # Arrange
        import src.llm.tenant_config as tenant_config

        connection = MagicMock()
        connection.fetchrow = AsyncMock(return_value={
            "connection_id": 11,
            "connection_name": "deepseek-primary",
            "provider_code": "deepseek",
            "protocol": "openai_compatible",
            "model_catalog_id": 22,
            "model_id": "deepseek-v4-pro",
            "capabilities": '{"reasoning":true,"reasoning_efforts":["high","max"],"reasoning_default_effort":"high"}',
            "base_url": "https://api.deepseek.com",
            "encrypted_api_key": "encrypted-value",
        })
        monkeypatch.setattr(
            tenant_config,
            "get_pg_pool",
            AsyncMock(return_value=_fake_pool(connection)),
        )
        monkeypatch.setattr(
            tenant_config,
            "CredentialManager",
            lambda: SimpleNamespace(decrypt=lambda value: "decrypted-key"),
        )

        # Act
        selection = await tenant_config.resolve_tenant_llm_selection(
            tenant_id=7,
            connection_id=11,
            model_id="deepseek-v4-pro",
            reasoning_enabled=True,
            reasoning_effort="max",
        )

        # Assert
        assert selection.reasoning_enabled is True
        assert selection.reasoning_effort == "max"
        assert selection.capabilities["reasoning"] is True
        assert selection.to_public_dict()["reasoning_effort"] == "max"

    # 方法作用：验证不支持推理的模型拒绝用户开启思考模式。
    # Args: self - pytest 测试类实例；monkeypatch - pytest 补丁工具。
    # Returns: 无返回值，断言失败时由 pytest 报告。
    async def test_resolver_rejects_reasoning_for_unsupported_model(self, monkeypatch) -> None:
        """模型能力是服务端授权依据，不能只依赖前端禁用控件。"""
        # Arrange
        import pytest

        import src.llm.tenant_config as tenant_config

        connection = MagicMock()
        connection.fetchrow = AsyncMock(return_value={
            "connection_id": 11,
            "connection_name": "plain",
            "provider_code": "custom",
            "protocol": "openai_compatible",
            "model_catalog_id": 23,
            "model_id": "plain-model",
            "capabilities": {"reasoning": False},
            "base_url": "https://llm.example/v1",
            "encrypted_api_key": "encrypted-value",
        })
        monkeypatch.setattr(
            tenant_config,
            "get_pg_pool",
            AsyncMock(return_value=_fake_pool(connection)),
        )

        # Act / Assert
        with pytest.raises(ValueError, match="不支持推理"):
            await tenant_config.resolve_tenant_llm_selection(
                tenant_id=7,
                connection_id=11,
                model_id="plain-model",
                reasoning_enabled=True,
                reasoning_effort="high",
            )

    # 方法作用：验证跨租户连接无法被显式对话选择解析。
    # Args: self - pytest 测试类实例；monkeypatch - pytest 补丁工具。
    # Returns: 无返回值，断言失败时由 pytest 报告。
    async def test_resolver_rejects_cross_tenant_connection(self, monkeypatch) -> None:
        """查询无当前 tenant_id 匹配记录时必须失败关闭，不能回退默认连接。"""
        # Arrange
        import pytest

        import src.llm.tenant_config as tenant_config

        connection = MagicMock()
        connection.fetchrow = AsyncMock(return_value=None)
        monkeypatch.setattr(
            tenant_config,
            "get_pg_pool",
            AsyncMock(return_value=_fake_pool(connection)),
        )

        # Act / Assert
        with pytest.raises(LookupError, match="未配置可用"):
            await tenant_config.resolve_tenant_llm_selection(
                tenant_id=7,
                connection_id=99,
                model_id="gpt-4o",
            )
        query_args = connection.fetchrow.await_args.args
        assert "c.tenant_id=$1" in query_args[0]
        assert query_args[1:] == (7, 99, "gpt-4o")

    # 方法作用：验证统一任务模型工厂使用当前租户选择而非全局 Settings。
    # Args: self - pytest 测试类实例；monkeypatch - pytest 补丁工具。
    # Returns: 无返回值，断言失败时由 pytest 报告。
    def test_task_llm_uses_current_tenant_selection(self, monkeypatch) -> None:
        """对话选择 openai_compatible 连接后所有任务必须进入统一 Provider 工厂。"""
        # Arrange
        import src.llm.client as client
        import src.observability as observability
        from src.llm.tenant_config import TenantLLMSelection, use_tenant_llm_selection

        chat_model = object()
        provider = SimpleNamespace(get_chat_model=MagicMock(return_value=chat_model))
        get_provider = MagicMock(return_value=provider)
        monkeypatch.setattr(client, "get_provider", get_provider)
        monkeypatch.setattr(observability, "attach_llm_metrics", lambda model, task: model)
        selection = TenantLLMSelection(
            tenant_id=7,
            connection_id=11,
            connection_name="deepseek-a",
            provider_code="deepseek",
            protocol="openai_compatible",
            model_catalog_id=22,
            model_id="deepseek-v4-pro",
            base_url="https://api.deepseek.com",
            api_key="tenant-key",
            capabilities={"reasoning": True, "reasoning_efforts": ["high", "max"]},
            reasoning_enabled=True,
            reasoning_effort="max",
        )

        # Act
        with use_tenant_llm_selection(selection):
            result = client.get_task_llm("direct_answer", reasoning=None)

        # Assert
        assert result is chat_model
        assert get_provider.call_args.kwargs == {
            "model_id": "deepseek-v4-pro",
            "provider_name": "openai_compatible",
            "base_url": "https://api.deepseek.com",
            "api_key": "tenant-key",
        }
        assert provider.get_chat_model.call_args.kwargs["reasoning"] is True
        assert provider.get_chat_model.call_args.kwargs["reasoning_effort"] == "max"


class TestTenantModelOptions:
    """覆盖功能 10.1.11 的租户可用模型响应。"""

    # 方法作用：验证模型列表按连接分组并返回租户默认选择。
    # Args: self - pytest 测试类实例；monkeypatch - pytest 补丁工具。
    # Returns: 无返回值，断言失败时由 pytest 报告。
    async def test_list_options_returns_connections_and_default(self, monkeypatch) -> None:
        """同一厂商的两个命名连接必须作为不同选择返回。"""
        # Arrange
        import src.llm.tenant_config as tenant_config

        connection = MagicMock()
        connection.fetch = AsyncMock(return_value=[
            {
                "connection_id": 11,
                "connection_name": "primary",
                "provider_code": "openai",
                "model_catalog_id": 21,
                "model_id": "gpt-4o",
                "display_name": "GPT-4o",
                "capabilities": {},
            },
            {
                "connection_id": 12,
                "connection_name": "backup",
                "provider_code": "openai",
                "model_catalog_id": 21,
                "model_id": "gpt-4o",
                "display_name": "GPT-4o",
                "capabilities": {},
            },
        ])
        connection.fetchrow = AsyncMock(return_value={
            "connection_id": 12,
            "model_catalog_id": 21,
            "model_id": "gpt-4o",
        })
        monkeypatch.setattr(
            tenant_config,
            "get_pg_pool",
            AsyncMock(return_value=_fake_pool(connection)),
        )

        # Act
        result = await tenant_config.list_tenant_model_options(tenant_id=7)

        # Assert
        assert [item["connection_id"] for item in result["models"]] == [11, 12]
        assert result["default"] == {"connection_id": 12, "model_id": "gpt-4o"}
