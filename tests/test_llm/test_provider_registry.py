"""LLM Provider 注册表与 Anthropic 实现回归测试。"""

from __future__ import annotations

import logging
from types import SimpleNamespace

logger = logging.getLogger(__name__)


class TestProviderRegistry:
    """覆盖功能 19.11、19.19：Provider 注册、创建和 Claude 路由。"""

    # 方法作用：验证模型注册表中的 Claude 可以创建 AnthropicProvider。
    # Args: self - pytest 测试类实例；monkeypatch - pytest 补丁工具。
    # Returns: 无返回值，断言失败时由 pytest 报告。
    def test_claude_model_creates_anthropic_provider(self, monkeypatch) -> None:
        """Claude 不能再因 Provider 硬编码而返回不支持错误。"""
        logger.debug("test_claude_model_creates_anthropic_provider 入口")
        import src.llm.client as client_module
        from src.llm.provider_anthropic import AnthropicProvider

        settings = SimpleNamespace(
            anthropic_api_key="anthropic-test-key",
            anthropic_base_url="",
            openai_api_key="openai-test-key",
            openai_base_url="",
            llm_model="claude-sonnet-4-6",
        )
        monkeypatch.setattr(client_module, "get_settings", lambda: settings)

        provider = client_module.get_provider("claude-sonnet-4-6")

        assert isinstance(provider, AnthropicProvider)
        logger.info("test_claude_model_creates_anthropic_provider 完成")

    # 方法作用：验证 Provider 装饰器支持注册自定义实现。
    # Args: self - pytest 测试类实例。
    # Returns: 无返回值，断言失败时由 pytest 报告。
    def test_custom_provider_can_be_registered(self) -> None:
        """扩展 Provider 时不应修改 client.py 条件分支。"""
        logger.debug("test_custom_provider_can_be_registered 入口")
        from src.llm.provider_registry import (
            create_provider,
            register_provider,
            unregister_provider,
        )

        class FakeProvider:
            """测试用 Provider。"""

            # 方法作用：保存注册表传入的标准构造参数。
            # Args: model_id - 模型标识；base_url - 服务地址；api_key - 测试密钥。
            # Returns: 无返回值。
            def __init__(self, model_id: str, base_url: str, api_key: str) -> None:
                logger.debug("FakeProvider.__init__ 入口", extra={"model_id": model_id})
                self.model_id = model_id
                self.base_url = base_url
                self.api_key = api_key
                logger.info("FakeProvider.__init__ 完成")

        try:
            register_provider("fake")(FakeProvider)
            provider = create_provider("fake", "fake-model", "http://local", "key")
            assert provider.model_id == "fake-model"
        finally:
            unregister_provider("fake")
        logger.info("test_custom_provider_can_be_registered 完成")

    # 方法作用：验证未知 Provider 产生明确异常。
    # Args: self - pytest 测试类实例；pytest - 通过局部导入使用异常断言。
    # Returns: 无返回值，断言失败时由 pytest 报告。
    def test_unknown_provider_is_rejected(self) -> None:
        """异常输入不能静默回退到 OpenAI。"""
        logger.debug("test_unknown_provider_is_rejected 入口")
        import pytest

        from src.llm.provider_registry import create_provider

        with pytest.raises(ValueError, match="不支持的 Provider"):
            create_provider("missing", "model", "", "")
        logger.info("test_unknown_provider_is_rejected 完成")

    # 方法作用：验证 OpenAI-compatible 协议别名在默认模型查询中同样生效。
    # Args: self - pytest 测试类实例。
    # Returns: 无返回值，断言失败时由 pytest 报告。
    def test_openai_compatible_alias_uses_openai_registration(self) -> None:
        """注册、创建和默认模型读取必须共享同一协议规范化规则。"""
        from src.llm.provider_registry import get_default_model

        assert get_default_model("openai_compatible") == get_default_model("openai")

    # 方法作用：验证 Provider Context 缓存使用凭证摘要区分连接实例。
    # Args: self - pytest 测试类实例；monkeypatch - pytest 补丁工具。
    # Returns: 无返回值，断言失败时由 pytest 报告。
    def test_provider_cache_fingerprint_distinguishes_api_keys(self, monkeypatch) -> None:
        """同模型和地址的不同 Key 不得错误复用认证客户端。"""
        logger.debug("test_provider_cache_fingerprint_distinguishes_api_keys 入口")
        import src.llm.client as client_module
        import src.llm.provider_registry as registry_module
        from src.app_context import AppContext, use_app_context

        created: list[object] = []

        # 方法作用：为每组连接参数返回可比较的独立 Provider 对象。
        # Args: name - Provider 名；model_id - 模型；base_url - 地址；api_key - 凭证。
        # Returns: 新建测试 Provider 对象。
        def create_provider(name: str, model_id: str, base_url: str, api_key: str) -> object:
            logger.debug("测试 Provider 工厂入口", extra={"provider": name, "model": model_id})
            provider = object()
            created.append(provider)
            logger.info("测试 Provider 工厂完成", extra={"has_key": bool(api_key)})
            return provider

        monkeypatch.setattr(registry_module, "create_provider", create_provider)
        settings = SimpleNamespace(multi_tenant=False, llm_model="model")
        with use_app_context(AppContext(settings)):
            first = client_module.get_provider(
                "model", provider_name="openai", base_url="http://llm", api_key="key-one",
            )
            second = client_module.get_provider(
                "model", provider_name="openai", base_url="http://llm", api_key="key-two",
            )
            reused = client_module.get_provider(
                "model", provider_name="openai", base_url="http://llm", api_key="key-one",
            )

        assert first is reused
        assert first is not second
        assert len(created) == 2
        logger.info("test_provider_cache_fingerprint_distinguishes_api_keys 完成")
