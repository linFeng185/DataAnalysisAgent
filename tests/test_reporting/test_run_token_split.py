"""LLM token 用量拆分的取值口径测试（复现 2026-09-16 事故：87 次调用 token 全 0）。"""

from __future__ import annotations

from types import SimpleNamespace


class TestExtractTokenUsage:
    """覆盖 llm_output 与消息 usage_metadata 两条来源。"""

    # 方法作用：验证标准 llm_output.token_usage 优先取用。
    def test_reads_llm_output_usage(self) -> None:
        # Arrange
        from src.observability import _extract_token_usage

        response = SimpleNamespace(llm_output={"token_usage": {"prompt_tokens": 120, "completion_tokens": 30}})

        # Act
        prompt, completion = _extract_token_usage(response)

        # Assert
        assert (prompt, completion) == (120, 30)

    # 方法作用：验证 llm_output 为空时改读消息上的 usage_metadata。
    def test_falls_back_to_message_usage_metadata(self) -> None:
        """OpenAI 兼容端点把用量挂在消息上，llm_output 常常是空的。"""
        # Arrange
        from src.observability import _extract_token_usage

        message = SimpleNamespace(usage_metadata={"input_tokens": 2048, "output_tokens": 512})
        generation = SimpleNamespace(message=message, generation_info=None)
        response = SimpleNamespace(llm_output={}, generations=[[generation]])

        # Act
        prompt, completion = _extract_token_usage(response)

        # Assert
        assert (prompt, completion) == (2048, 512)

    # 方法作用：验证只有 generation_info.usage 时也能读到。
    def test_reads_generation_info_usage(self) -> None:
        # Arrange
        from src.observability import _extract_token_usage

        generation = SimpleNamespace(message=None, generation_info={"usage": {"prompt_tokens": 7, "completion_tokens": 3}})
        response = SimpleNamespace(llm_output=None, generations=[[generation]])

        # Act / Assert
        assert _extract_token_usage(response) == (7, 3)

    # 方法作用：验证两条来源都没有时返回 0 而不是抛错。
    def test_missing_usage_returns_zero(self) -> None:
        # Arrange
        from src.observability import _extract_token_usage

        # Act / Assert
        assert _extract_token_usage(SimpleNamespace()) == (0, 0)


class TestAttachLlmMetricsDoesNotMutate:
    """覆盖 2026-09-16 内存事故：共享缓存模型被无限追加回调。"""

    # 方法作用：验证挂载回调不会就地修改被复用的模型对象。
    def test_returns_copy_and_keeps_original_clean(self) -> None:
        """Provider 缓存 ChatModel 并跨次复用，就地 append 会让回调无限增长。"""
        # Arrange
        from pydantic import BaseModel

        from src.observability import attach_llm_metrics

        class _FakeModel(BaseModel):
            callbacks: list = []

        original = _FakeModel()

        # Act
        first = attach_llm_metrics(original, "report_unit")
        second = attach_llm_metrics(original, "report_unit")

        # Assert：原件保持干净，两次挂载各自独立，不互相累积。
        assert original.callbacks == []
        assert len(first.callbacks) == 1
        assert len(second.callbacks) == 1
        assert first is not original

    # 方法作用：验证多次挂载到同一模型不会出现回调翻倍。
    def test_repeated_attach_does_not_accumulate_on_source(self) -> None:
        # Arrange
        from pydantic import BaseModel

        from src.observability import attach_llm_metrics

        class _FakeModel(BaseModel):
            callbacks: list = []

        original = _FakeModel()

        # Act
        for _ in range(20):
            attach_llm_metrics(original, "report_unit")

        # Assert
        assert len(original.callbacks) == 0
