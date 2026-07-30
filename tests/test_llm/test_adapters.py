"""模型适配器单元测试 — 覆盖 base / deepseek / openai / registry。"""

from __future__ import annotations

from src.llm.adapters.base import SupportedFeatures
from src.llm.adapters.deepseek import DeepSeekV4FlashAdapter, DeepSeekV4ProAdapter
from src.llm.adapters.openai_adapter import OpenAIAdapter
from src.llm.adapters.registry import get_adapter, list_registered


class TestSupportedFeatures:
    def test_default_features(self):
        sf = SupportedFeatures()
        assert sf.streaming is True
        assert sf.reasoning is False
        assert sf.function_calling is True

    def test_deepseek_features(self):
        sf = DeepSeekV4ProAdapter().supported_features
        assert sf.reasoning is True
        assert sf.reasoning_content_in_response is True

    def test_openai_features(self):
        sf = OpenAIAdapter().supported_features
        assert sf.reasoning is True
        assert sf.reasoning_content_in_response is False


class TestDeepSeekAdapter:
    # 方法作用：验证 DeepSeek 思考开关和标准深度被转换为官方请求参数。
    # Args: self - pytest 测试类实例。
    # Returns: 无返回值，断言失败时由 pytest 报告。
    def test_get_chat_openai_kwargs(self):
        adapter = DeepSeekV4ProAdapter()
        kwargs = adapter.get_chat_openai_kwargs(reasoning=True, reasoning_effort="max")
        assert "reasoning_effort" in kwargs
        assert kwargs["reasoning_effort"] == "max"
        assert "extra_body" in kwargs
        assert kwargs["extra_body"]["thinking"]["type"] == "enabled"

    # 方法作用：验证 DeepSeek 兼容深度别名和关闭思考模式的协议映射。
    # Args: self - pytest 测试类实例。
    # Returns: 无返回值，断言失败时由 pytest 报告。
    def test_reasoning_effort_aliases_and_disabled_mode(self):
        """low/medium 应归一为 high，xhigh 归一为 max，关闭时显式发送 disabled。"""
        # Arrange
        adapter = DeepSeekV4FlashAdapter()

        # Act / Assert
        assert adapter.get_chat_openai_kwargs(
            reasoning=True,
            reasoning_effort="medium",
        )["reasoning_effort"] == "high"
        assert adapter.get_chat_openai_kwargs(
            reasoning=True,
            reasoning_effort="xhigh",
        )["reasoning_effort"] == "max"
        disabled = adapter.get_chat_openai_kwargs(reasoning=False)
        assert disabled == {"extra_body": {"thinking": {"type": "disabled"}}}

    def test_default_base_url(self):
        assert DeepSeekV4ProAdapter().default_base_url == "https://api.deepseek.com"

    def test_parse_response_with_reasoning(self):
        adapter = DeepSeekV4ProAdapter()

        class MockMsg:
            content = "回答内容"
            additional_kwargs = {"reasoning_content": "这是推理过程"}
            tool_calls = None

        result = adapter.parse_response(MockMsg())
        assert result.content == "回答内容"
        assert "推理过程" in result.reasoning_content

    def test_parse_response_without_reasoning(self):
        adapter = DeepSeekV4ProAdapter()

        class MockMsg:
            content = "普通回答"
            additional_kwargs = {}
            tool_calls = None

        result = adapter.parse_response(MockMsg())
        assert result.content == "普通回答"
        assert result.reasoning_content == ""

    def test_parse_stream_chunk_with_reasoning(self):
        adapter = DeepSeekV4ProAdapter()

        class MockChunk:
            content = "token"
            additional_kwargs = {"reasoning_content": "思考中..."}

        result = adapter.parse_stream_chunk(MockChunk())
        assert result.content == "token"
        assert result.reasoning_content == "思考中..."

    def test_parse_stream_chunk_delta_path(self):
        adapter = DeepSeekV4ProAdapter()

        class MockChunk:
            content = ""
            additional_kwargs = {}
            response_metadata = {
                "choices": [{"delta": {"reasoning_content": "delta推理"}}]
            }

        result = adapter.parse_stream_chunk(MockChunk())
        assert result.reasoning_content == "delta推理"


class TestOpenAIAdapter:
    def test_get_chat_openai_kwargs_empty(self):
        assert OpenAIAdapter().get_chat_openai_kwargs() == {}

    def test_default_base_url(self):
        assert "openai.com" in OpenAIAdapter().default_base_url

    def test_parse_basic_response(self):
        adapter = OpenAIAdapter()

        class MockMsg:
            content = "GPT 回答"
            additional_kwargs = {}
            tool_calls = None

        result = adapter.parse_response(MockMsg())
        assert result.content == "GPT 回答"
        assert result.reasoning_content == ""


class TestRegistry:
    # 方法作用：验证两个 DeepSeek V4 模型分别命中明确适配器。
    # Args: self - pytest 测试类实例。
    # Returns: 无返回值，断言失败时由 pytest 报告。
    def test_deepseek_match(self):
        assert isinstance(get_adapter("deepseek-v4-pro"), DeepSeekV4ProAdapter)
        assert isinstance(get_adapter("deepseek-v4-flash"), DeepSeekV4FlashAdapter)

    def test_gpt_match(self):
        adapter = get_adapter("gpt-4o")
        assert isinstance(adapter, OpenAIAdapter)

    def test_unknown_model_fallback(self):
        adapter = get_adapter("claude-unknown")
        assert isinstance(adapter, OpenAIAdapter)

    def test_list_registered(self):
        items = list_registered()
        assert "deepseek-v4-pro" in items
        assert "deepseek-v4-flash" in items
        assert "gpt" in items
        assert items["deepseek-v4-pro"]["reasoning"] is True
        assert items["deepseek-v4-flash"]["streaming"] is True


class TestReasoningChatOpenAI:
    """覆盖 DeepSeek 非流式响应和工具调用上下文中的 reasoning_content。"""

    # 方法作用：验证非流式响应中的 reasoning_content 不被 LangChain 丢弃。
    # Args: self - pytest 测试类实例。
    # Returns: 无返回值，断言失败时由 pytest 报告。
    def test_non_stream_response_keeps_reasoning_content(self) -> None:
        """官方响应字段必须保存在 AIMessage.additional_kwargs。"""
        # Arrange
        from src.llm.reasoning_chat_openai import ReasoningChatOpenAI

        model = ReasoningChatOpenAI(model="deepseek-v4-pro", api_key="test-key")

        # Act
        result = model._create_chat_result({
            "choices": [{
                "index": 0,
                "finish_reason": "stop",
                "message": {
                    "role": "assistant",
                    "content": "最终回答",
                    "reasoning_content": "内部推理",
                },
            }],
            "model": "deepseek-v4-pro",
        })

        # Assert
        message = result.generations[0].message
        assert message.additional_kwargs["reasoning_content"] == "内部推理"

    # 方法作用：验证工具调用后的 assistant 推理字段会在下一请求中完整回传。
    # Args: self - pytest 测试类实例。
    # Returns: 无返回值，断言失败时由 pytest 报告。
    def test_request_payload_returns_reasoning_content_for_tool_chain(self) -> None:
        """DeepSeek 工具调用链缺失 reasoning_content 会触发上游 400。"""
        # Arrange
        from langchain_core.messages import AIMessage, HumanMessage

        from src.llm.reasoning_chat_openai import ReasoningChatOpenAI

        model = ReasoningChatOpenAI(model="deepseek-v4-pro", api_key="test-key")
        messages = [
            HumanMessage(content="天气如何"),
            AIMessage(content="", additional_kwargs={"reasoning_content": "先查天气"}),
        ]

        # Act
        payload = model._get_request_payload(messages)

        # Assert
        assert payload["messages"][1]["reasoning_content"] == "先查天气"


class TestBaseAdapter:
    def test_direct_reasoning_attr(self):
        class MockMsg:
            content = "test"
            additional_kwargs = {}
            reasoning_content = "直接属性推理"
            tool_calls = None

        adapter = OpenAIAdapter()
        result = adapter.parse_response(MockMsg())
        assert result.reasoning_content == "直接属性推理"

    def test_parse_stream_chunk_empty(self):
        class MockChunk:
            content = None
            additional_kwargs = {}

        adapter = OpenAIAdapter()
        result = adapter.parse_stream_chunk(MockChunk())
        assert result.content == ""
        assert result.reasoning_content == ""

    def test_parse_response_with_tool_calls(self):
        class MockMsg:
            content = ""
            additional_kwargs = {}
            tool_calls = [{"name": "get_weather", "args": {"city": "杭州"}}]

        adapter = OpenAIAdapter()
        result = adapter.parse_response(MockMsg())
        assert len(result.tool_calls) == 1
        assert result.tool_calls[0]["name"] == "get_weather"
