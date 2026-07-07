"""LLM Provider 单元测试。"""

import pytest
from src.llm.adapters.base import SupportedFeatures
from src.llm.provider import LLMProvider, LLMResponse, LLMStreamChunk


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
