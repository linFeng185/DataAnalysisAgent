"""功能27.1.3/27.1.5的独立审核：流式中间内容与首次绑定预算。"""

from __future__ import annotations

import json

from langchain_core.messages import AIMessage, AIMessageChunk

from tests.test_llm.test_knowledge_loop import (
    _bind_loop,
    _FakeToolModel,
    _prepared,
    _tool_round_chunks,
)


class TestKnowledgeLoopReview:
    """在开发者已有Fake协议上验证未被基础用例覆盖的边界。"""

    # 验证首轮工具调用前的正文不会被当作最终正文输出。
    # Args: monkeypatch - 测试替换工具。
    # Returns: 无，断言只产出最终正文且首轮回调可被SSE隔离。
    async def test_first_tool_round_never_yields_intermediate_content(self, monkeypatch) -> None:
        """模型先输出一句说明再请求工具时，说明不能泄漏到SQL或SSE。"""
        import src.llm.knowledge_loop as loop

        # Arrange：完整首轮包含正文与后续工具分片。
        first_round = [AIMessageChunk(content="中间说明不得出现在最终SQL中")]
        first_round.extend(_tool_round_chunks("search_knowledge", "review-call", ['{"query":"status"}']))
        model = _FakeToolModel([
            lambda: first_round,
            lambda: [AIMessageChunk(content="SELECT 1")],
        ])

        # Act：直接消费真正的流式工具助手。
        with _bind_loop(monkeypatch):
            chunks = [chunk async for chunk in loop.stream_with_knowledge(_prepared(model))]

        # Assert：即使消费方没有撤回功能，也看不到中间正文。
        text = "".join(str(getattr(chunk, "content", "") or "") for chunk in chunks)
        assert text == "SELECT 1"
        assert loop.TOOL_ROUND_TAG in model.calls[0]["config"].get("tags", [])

    # 验证模型首次调用前就检查工具schema预算。
    # Args: monkeypatch - 测试替换工具。
    # Returns: 无，断言每次实际调用的消息和工具都处于预算内。
    async def test_initial_call_obeys_message_and_tool_schema_budget(self, monkeypatch) -> None:
        """预算放不下schema时应不挂工具，而非在第一次调用之后才裁剪。"""
        import src.llm.knowledge_loop as loop

        # Arrange：原始问题很小，但工具schema无法放入400字符预算。
        model = _FakeToolModel([AIMessage(content="无需检索")])

        # Act：显式给出受限预算。
        with _bind_loop(monkeypatch, message_budget_chars=400):
            await loop.invoke_with_knowledge(_prepared(model))

        # Assert：在发送请求前遵守相同的总消息预算。
        assert len(model.calls) == 1
        assert model.calls[0]["tools"] == []

    # 验证复杂SQL提示词优先预留工具空间，而不是填满后静默禁用检索。
    # Args: monkeypatch - 测试替换工具。
    # Returns: 无，断言大Schema场景仍能提供工具且总预算受限。
    async def test_large_sql_context_reserves_space_for_knowledge_tool(self, monkeypatch) -> None:
        """工具最有价值的大Schema场景不能因原Prompt填满而永久不可用。"""
        import src.llm.knowledge_loop as loop
        from src.llm.invocation import prepare_invocation
        from src.llm.prompt_budget import PromptSection
        from src.llm.prompts import get_prompt_definition

        # Arrange：低优先级Schema文本足以占满原有预算。
        model = _FakeToolModel([
            AIMessage(content="", tool_calls=[{
                "name": "search_knowledge", "args": {"query": "订单口径"}, "id": "review-large-context",
            }]),
            AIMessage(content="已完成"),
        ])
        sections = [
            PromptSection("query", "统计订单状态", priority=100, min_chars=30),
            PromptSection("schema", "orders(status, amount);\n" * 5000, priority=80, min_chars=100),
        ]

        # Act：走真实Prompt预算与实际工具入口，不使用手写短消息绕过预算器。
        with _bind_loop(monkeypatch):
            prepared = prepare_invocation(
                "sql.generate", sections, model=model,
                system_values={"dialect": "postgres", "skill_instructions": ""},
            )
            await loop.invoke_with_knowledge(prepared)

        # Assert：在既有SQL总预算内预留工具空间，而非悄悄关掉能力。
        assert model.calls[0]["tools"]
        schema_chars = loop._tool_schema_chars(model.calls[0]["tools"])
        assert loop._message_chars(model.calls[0]["messages"], schema_chars) <= (
            get_prompt_definition("sql.generate").context_budget
        )
        replies = [message for message in model.calls[-1]["messages"] if getattr(message, "type", "") == "tool"]
        assert replies
        payload = json.loads(replies[0].content)
        assert payload["evidence"], "预留空间必须让实际检索证据进入续轮，不能只容纳工具schema"
        assert "GMV" in payload["evidence"][0]["content"]

    # 验证流式工具调用转换为assistant消息后仍保留供应商所需的推理协议字段。
    # Args: 无。
    # Returns: 无，断言工具调用与reasoning_content同时保留。
    def test_stream_assistant_conversion_preserves_provider_reasoning_fields(self) -> None:
        """部分模型续轮要求原assistant的reasoning_content，转换不能只保留正文和调用。"""
        import src.llm.knowledge_loop as loop

        # Arrange：合成协议数据，不包含任何真实模型推理。
        chunk = AIMessageChunk(
            content="",
            additional_kwargs={"reasoning_content": "synthetic-protocol-marker"},
            tool_calls=[{"name": "search_knowledge", "args": {"query": "status"}, "id": "review-call"}],
        )

        # Act：执行流式续轮使用的消息转换。
        message = loop._as_assistant_message(chunk)

        # Assert：provider续轮需要的字段与原工具ID均不丢失。
        assert message.additional_kwargs.get("reasoning_content") == "synthetic-protocol-marker"
        assert message.tool_calls[0]["id"] == "review-call"

    # 验证供应商解析为invalid_tool_calls的坏JSON同样获得匹配工具回复。
    # Args: monkeypatch - 测试替换工具。
    # Returns: 无，断言坏JSON调用被明确拒绝后仍能继续生成。
    async def test_provider_invalid_tool_call_still_receives_matching_reply(self, monkeypatch) -> None:
        """不能把仅含invalid_tool_calls的空正文当作已完成回答交给JSON解析器。"""
        import src.llm.knowledge_loop as loop

        # Arrange：这是LangChain对供应商损坏参数的真实消息表示。
        malformed = AIMessage(
            content="",
            invalid_tool_calls=[{
                "name": "search_knowledge", "args": '{"query":',
                "id": "review-invalid", "error": "synthetic invalid JSON",
            }],
        )
        model = _FakeToolModel([malformed, AIMessage(content="已说明上下文不足")])

        # Act：让循环处理损坏参数，不访问真实模型或向量服务。
        with _bind_loop(monkeypatch):
            result = await loop.invoke_with_knowledge(_prepared(model))

        # Assert：调用ID闭合，返回最终回答而不是中间空消息。
        assert result.content == "已说明上下文不足"
        replies = [message for message in model.calls[1]["messages"] if getattr(message, "type", "") == "tool"]
        assert len(replies) == 1
        assert replies[0].tool_call_id == "review-invalid"
        assert '"invalid_arguments"' in replies[0].content
