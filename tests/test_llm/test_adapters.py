"""模型适配器单元测试 — 覆盖 base / deepseek / openai / registry。"""

from __future__ import annotations

import pytest

from src.llm.adapters.base import SupportedFeatures
from src.llm.adapters.deepseek import DeepSeekV4ProAdapter
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
    def test_get_chat_openai_kwargs(self):
        adapter = DeepSeekV4ProAdapter()
        kwargs = adapter.get_chat_openai_kwargs()
        assert "reasoning_effort" in kwargs
        assert kwargs["reasoning_effort"] == "high"
        assert "extra_body" in kwargs
        assert kwargs["extra_body"]["thinking"]["type"] == "enabled"

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
    def test_deepseek_match(self):
        adapter = get_adapter("deepseek-v4-pro")
        assert isinstance(adapter, DeepSeekV4ProAdapter)

    def test_gpt_match(self):
        adapter = get_adapter("gpt-4o")
        assert isinstance(adapter, OpenAIAdapter)

    def test_unknown_model_fallback(self):
        adapter = get_adapter("claude-unknown")
        assert isinstance(adapter, OpenAIAdapter)

    def test_list_registered(self):
        items = list_registered()
        assert "deepseek" in items
        assert "gpt" in items
        assert items["deepseek"]["reasoning"] is True
        assert items["deepseek"]["streaming"] is True


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
