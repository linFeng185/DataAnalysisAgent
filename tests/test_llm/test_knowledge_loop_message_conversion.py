"""工具续轮消息转换回归：真实 langchain_openai 序列化必须保留 tool_calls。"""

from __future__ import annotations

from langchain_core.messages import AIMessage, AIMessageChunk

from src.llm.knowledge_loop import _as_assistant_message


class TestAssistantMessageConversion:
    """覆盖 27.1.5：分片合并结果必须能作为下一轮 assistant 消息被 provider 接受。"""

    # 方法作用：经真实 provider 请求体构造后，tool_calls 与 reasoning_content 必须同时保留。
    # Args: 无
    # Returns: 无返回值，断言失败由 pytest 报告
    def test_converted_message_keeps_tool_calls_in_openai_payload(self) -> None:
        """经真实 provider 请求体构造后，tool_calls 与 reasoning_content 必须同时保留。"""
        # Arrange
        from langchain_openai.chat_models.base import _convert_message_to_dict

        from src.llm.reasoning_chat_openai import ReasoningChatOpenAI

        chunk = AIMessageChunk(
            content="",
            additional_kwargs={"reasoning_content": "synthetic-protocol-marker"},
            tool_calls=[{
                "name": "search_knowledge",
                "args": {"query": "status"},
                "id": "call-review-1",
            }],
        )

        # Act
        message = _as_assistant_message(chunk)
        payload = _convert_message_to_dict(message)
        # 项目实际使用的 provider 子类：只用本地构造验证请求体，不发起任何网络请求。
        provider = ReasoningChatOpenAI(
            model="gpt-4o-mini", api_key="synthetic-key", base_url="http://127.0.0.1:1",
        )
        outbound = provider._get_request_payload([message])["messages"][0]

        # Assert
        assert isinstance(message, AIMessage)
        assert not isinstance(message, AIMessageChunk)
        assert payload["role"] == "assistant"
        assert payload["tool_calls"][0]["id"] == "call-review-1"
        assert payload["tool_calls"][0]["function"]["name"] == "search_knowledge"
        assert "status" in outbound["tool_calls"][0]["function"]["arguments"]
        assert outbound["tool_calls"][0]["id"] == "call-review-1"
        assert outbound["reasoning_content"] == "synthetic-protocol-marker"

    # 方法作用：无工具调用时转换结果仍是普通 assistant 文本，不引入多余字段。
    # Args: 无
    # Returns: 无返回值，断言失败由 pytest 报告
    def test_converted_message_survives_plain_openai_text_round(self) -> None:
        """无工具调用时转换结果仍是普通 assistant 文本，不引入多余字段。"""
        # Arrange
        from langchain_openai.chat_models.base import _convert_message_to_dict

        chunk = AIMessageChunk(content="SELECT 1")

        # Act
        payload = _convert_message_to_dict(_as_assistant_message(chunk))

        # Assert
        assert payload == {"role": "assistant", "content": "SELECT 1"}

    # 方法作用：已是 AIMessage 的响应不应被再次包装，避免丢失自定义字段。
    # Args: 无
    # Returns: 无返回值，断言失败由 pytest 报告
    def test_existing_ai_message_is_not_rewrapped(self) -> None:
        """已是 AIMessage 的响应不应被再次包装，避免丢失自定义字段。"""
        # Arrange
        original = AIMessage(content="SELECT 1", additional_kwargs={"custom": "keep"})

        # Act
        converted = _as_assistant_message(original)

        # Assert
        assert converted is original
