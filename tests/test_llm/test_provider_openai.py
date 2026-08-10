"""OpenAI-compatible Provider 传输参数测试。"""

from __future__ import annotations

from types import SimpleNamespace


class TestOpenAIProviderTransport:
    """覆盖流式首块超时的独立配置边界。"""

    # 方法作用：验证流式模型使用独立首块超时而非完整请求超时。
    # Args: self - pytest 测试类实例；monkeypatch - pytest 补丁工具。
    # Returns: 无返回值，断言失败时由 pytest 报告。
    def test_stream_model_uses_configured_chunk_timeout(self, monkeypatch) -> None:
        """首块僵死应在较短窗口触发回退，不能占满完整请求超时。"""
        # Arrange
        import src.llm.provider_openai as provider_module

        settings = SimpleNamespace(
            llm_temperature=0.0,
            llm_max_tokens=256,
            llm_timeout=60,
            llm_stream_chunk_timeout=7,
        )
        monkeypatch.setattr(provider_module, "get_settings", lambda: settings)
        provider = provider_module.OpenAIProvider(
            "deepseek-v4-flash",
            "https://api.deepseek.com",
            "test-key",
        )

        # Act
        model = provider.get_chat_model(stream=True, reasoning=False)

        # Assert
        assert model.streaming is True
        assert model.stream_chunk_timeout == 7.0
