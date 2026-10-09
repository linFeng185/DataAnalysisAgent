"""覆盖 28.3：工具探索与最终公开回答的真实增量分离。"""

from types import SimpleNamespace

from langchain_core.messages import AIMessage, AIMessageChunk, HumanMessage


class TestPublicAnswerStream:
    """最终回答必须 astream，而不是完整 ainvoke 后拆分。"""

    # 验证公开模式使用逐块模型调用，SQL 调用保持兼容。
    # Args: self - 测试实例；monkeypatch - pytest 补丁工具。
    # Returns: 无返回值。
    async def test_public_answer_uses_stream_and_restores_scope(self, monkeypatch) -> None:
        """公开模式仅影响分析/直接回答，调用完成恢复 ContextVar。"""
        import src.llm.knowledge_loop as loop

        # Arrange
        chunks = []

        # 模拟真正逐块返回的模型调用。
        # Args: messages - 模型消息；config - 调用配置。
        # Returns: 异步模型分片。
        async def stream(messages, config=None):
            assert config["metadata"]["public_answer"] is True
            for text in ['{"summary":"', "增长", '"}']:
                chunks.append(text)
                yield AIMessageChunk(content=text)

        # 普通调用用于确认公开模式没有泄漏到 SQL 任务。
        # Args: messages - 模型消息；config - 调用配置。
        # Returns: 完整 SQL 模型响应。
        async def invoke(messages, config=None):
            return AIMessage(content="SELECT 1")

        model = SimpleNamespace(astream=stream, ainvoke=invoke)
        prepared = SimpleNamespace(
            task="analyze_result", model=model, config={}, messages=[HumanMessage(content="分析")]
        )
        monkeypatch.setattr(loop, "build_knowledge_tools_for_call", lambda prepared=None: [])
        # Act
        with loop.use_public_answer_stream():
            response = await loop.invoke_with_knowledge(prepared)
        prepared.task = "generate_sql"
        ordinary = await loop.invoke_with_knowledge(prepared)
        # Assert
        assert len(chunks) == 3
        assert response.content == '{"summary":"增长"}'
        assert ordinary.content == "SELECT 1"

    # 验证知识检索只参与受隔离的探索回合，最终回答仍是真正的无工具流。
    # Args: self - 实例；monkeypatch - 检索边界补丁。
    # Returns: 无返回值。
    async def test_tool_exploration_is_tagged_and_final_stream_has_no_tools(
        self, monkeypatch
    ) -> None:
        """公开回答携带检索证据，但不拼入工具探索的中间正文。"""
        from langchain_core.messages import ToolMessage

        import src.llm.knowledge_loop as loop
        from tests.test_llm.test_knowledge_loop import _bind_loop, _FakeToolModel, _prepared

        # Arrange
        model = _FakeToolModel(
            [
                AIMessage(
                    content="内部检索说明",
                    tool_calls=[
                        {
                            "name": "search_knowledge",
                            "args": {"query": "销售口径"},
                            "id": "lookup-1",
                        }
                    ],
                ),
                AIMessage(content="探索已结束"),
                [AIMessageChunk(content='{"summary":"'), AIMessageChunk(content='销售增长"}')],
            ]
        )
        prepared = _prepared(model)
        prepared.task = "analyze_result"
        prepared.prompt_id = "analysis.result"

        # Act
        with _bind_loop(monkeypatch, message_budget_chars=16000), loop.use_public_answer_stream():
            response = await loop.invoke_with_knowledge(prepared)

        # Assert
        assert response.content == '{"summary":"销售增长"}'
        assert len(model.calls) == 3
        assert all(loop.TOOL_ROUND_TAG in call["config"]["tags"] for call in model.calls[:2])
        final = model.calls[-1]
        assert final["tools"] == []
        assert loop.TOOL_ROUND_TAG not in final["config"]["tags"]
        assert final["config"]["metadata"]["public_answer"] is True
        evidence = [message for message in final["messages"] if isinstance(message, ToolMessage)]
        assert len(evidence) == 1
        assert "GMV" in evidence[0].content
