"""统一结构化 Prompt 调用入口测试。"""

from __future__ import annotations

from types import SimpleNamespace


class TestStructuredInvocation:
    """覆盖功能 10.2.10、17.3.5 的统一调用和追踪元数据边界。"""

    async def test_invoke_structured_uses_registered_contract(self, monkeypatch) -> None:
        """调用入口必须使用注册 Prompt，并返回目标 Pydantic 模型。"""
        # Arrange
        import src.llm.invocation as invocation
        from src.llm.output_contracts import TextAnswerOutput
        from src.llm.prompt_budget import PromptSection

        captured: dict[str, object] = {}

        class FakeModel:
            async def ainvoke(self, messages):
                captured["messages"] = messages
                return SimpleNamespace(content='{"answer":"已完成"}')

        monkeypatch.setattr(invocation, "get_task_llm", lambda *args, **kwargs: FakeModel())

        # Act
        result = await invocation.invoke_structured(
            "direct.answer",
            [PromptSection("query", "请回答订单问题", priority=100)],
            output_model=TextAnswerOutput,
            task="direct_answer",
        )

        # Assert
        assert isinstance(result, TextAnswerOutput)
        assert result.answer == "已完成"
        assert len(captured["messages"]) == 2
        assert "订单问题" in captured["messages"][1].content

    def test_prepare_invocation_preserves_parent_trace_metadata(self) -> None:
        """统一调用应继承父级配置并附加 Prompt、任务和能力标签。"""
        # Arrange
        from src.llm.invocation import prepare_invocation
        from src.llm.prompt_budget import PromptSection

        captured: dict[str, object] = {}

        class ConfigurableFakeModel:
            def with_config(self, config):
                captured["config"] = config
                return self

        # Act
        prepared = prepare_invocation(
            "direct.answer",
            [PromptSection("query", "库存是多少", priority=100)],
            config={
                "metadata": {"request_id": "request-1"},
                "tags": ["parent"],
            },
            metadata={"capability": "direct_answer", "datasource": "inventory"},
            model=ConfigurableFakeModel(),
        )

        # Assert
        metadata = prepared.config["metadata"]
        assert metadata == {
            "request_id": "request-1",
            "prompt_id": "direct.answer",
            "prompt_version": "1.0.0",
            "llm_task": "direct_answer",
            "capability": "direct_answer",
            "datasource": "inventory",
        }
        assert prepared.config["tags"] == [
            "parent",
            "prompt:direct.answer",
            "prompt-version:1.0.0",
            "task:direct_answer",
        ]
        assert captured["config"] == prepared.config
