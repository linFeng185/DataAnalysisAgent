"""LLM Provider 单元测试。"""

import logging

import pytest
from src.llm.adapters.base import SupportedFeatures
from src.llm.provider import LLMProvider, LLMResponse, LLMStreamChunk


logger = logging.getLogger(__name__)


class FakeLLMProvider(LLMProvider):
    def __init__(self, responses=None):
        self.responses = responses or ["Mock response"]
        self._idx = 0
        self._caps = SupportedFeatures(context_window=128000, vision=False)

    @property
    def capabilities(self):
        return self._caps

    async def agenerate(self, messages, temperature=None, max_tokens=None, stream=False):
        r = self.responses[self._idx % len(self.responses)]
        self._idx += 1
        return LLMResponse(content=r, reasoning="mock reasoning")

    async def astream(self, messages, temperature=None, max_tokens=None):
        for r in self.responses:
            yield LLMStreamChunk(content=r, reasoning="")

    # 方法作用：返回测试用 ChatModel 占位对象以满足 Provider 契约。
    # Args: temperature - 温度；max_tokens - 最大输出；stream - 是否流式；reasoning - 是否推理。
    # Returns: 不执行真实模型调用的测试占位对象。
    def get_chat_model(
        self,
        temperature=None,
        max_tokens=None,
        stream=True,
        reasoning=True,
    ):
        logger.debug(
            "FakeLLMProvider.get_chat_model 入口",
            extra={"stream": stream, "reasoning": reasoning},
        )
        result = object()
        logger.info("FakeLLMProvider.get_chat_model 完成")
        return result


class TestLLMProvider:
    @pytest.fixture
    def p(self):
        return FakeLLMProvider(["A", "B"])

    @pytest.mark.asyncio
    async def test_agenerate(self, p):
        r = await p.agenerate([{"role": "user", "content": "hi"}])
        assert r.content == "A"
        assert r.reasoning

    @pytest.mark.asyncio
    async def test_capabilities(self, p):
        assert p.capabilities.context_window == 128000

    @pytest.mark.asyncio
    async def test_astream(self, p):
        chunks = [c async for c in p.astream([{}])]
        assert len(chunks) == 2
        assert chunks[0].content == "A"
