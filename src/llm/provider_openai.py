"""OpenAI 兼容 Provider — 适配现有 ReasoningChatOpenAI + ModelAdapter 体系。

逻辑要点:
- 流式调用每次创建新 ChatOpenAI 实例（防并发状态污染）
- 非流式调用复用缓存实例（节省内存）
- 通过 ModelAdapter 注入模型特有参数（DeepSeek reasoning_effort 等）
"""

from __future__ import annotations

import time

from src.config import get_settings
from src.llm.adapters.registry import get_adapter
from src.llm.provider import LLMProvider, LLMResponse, LLMStreamChunk
from src.logging_config import get_logger

logger = get_logger(__name__)


class OpenAIProvider(LLMProvider):
    """OpenAI 兼容协议的 LLM Provider。

    适配 DeepSeek / OpenAI / 任何兼容 OpenAI API 的服务。
    通过 ModelAdapter 处理模型间的参数差异。

    Args:
        model_id: 模型标识符，对应 ModelRegistry 中的 model_id
        base_url: API 地址
        api_key: API 密钥
    """

    def __init__(self, model_id: str, base_url: str, api_key: str):
        """初始化 Provider。

        Args:
            model_id: 模型 ID
            base_url: API base URL
            api_key: API Key
        """
        self._model_id = model_id
        self._base_url = base_url
        self._api_key = api_key
        self._adapter = get_adapter(model_id)
        self._capabilities = self._adapter.supported_features
        self._cached_llm = None  # 非流式实例缓存

    @property
    def capabilities(self):
        """返回模型能力声明。

        Returns: SupportedFeatures 实例
        """
        return self._capabilities

    async def agenerate(self, messages, temperature=None, max_tokens=None, stream=False) -> LLMResponse:
        """非流式调用 LLM。

        Args:
            messages: [{"role": "system", "content": "..."}, ...]
            temperature: 温度参数
            max_tokens: 最大输出 Token
            stream: 是否流式（agenerate 时传 False）

        Returns: LLMResponse
        """
        s = get_settings()
        llm = self._get_llm(
            temperature if temperature is not None else s.llm_temperature,
            max_tokens or s.llm_max_tokens, stream)
        lc_msgs = [self._to_lc_msg(m) for m in messages]
        _start = time.monotonic()
        resp = await llm.ainvoke(lc_msgs)
        logger.debug("LLM 调用完成", model=self._model_id,
                     elapsed_ms=round((time.monotonic() - _start) * 1000))
        return LLMResponse(content=resp.content or "",
                           reasoning=self._adapter.parse_response(resp).reasoning_content)

    async def astream(self, messages, temperature=None, max_tokens=None):
        """流式调用 LLM，逐个 yield LLMStreamChunk。

        Args:
            messages: 同 agenerate
            temperature: 温度参数
            max_tokens: 最大输出 Token

        Yields: LLMStreamChunk
        """
        s = get_settings()
        llm = self._get_llm(
            temperature if temperature is not None else s.llm_temperature,
            max_tokens or s.llm_max_tokens, True)
        lc_msgs = [self._to_lc_msg(m) for m in messages]
        async for chunk in llm.astream(lc_msgs):
            p = self._adapter.parse_stream_chunk(chunk)
            yield LLMStreamChunk(content=p.content, reasoning=p.reasoning_content)

    def _get_llm(self, temperature: float, max_tokens: int, stream: bool):
        """获取 ChatOpenAI 实例。

        流式调用每次创建新实例（streaming 属性会改变实例行为），
        非流式调用复用缓存实例（节省内存和连接池）。

        Args:
            temperature: 温度
            max_tokens: 最大输出 Token
            stream: 是否流式

        Returns: ReasoningChatOpenAI 实例
        """
        from src.llm.reasoning_chat_openai import ReasoningChatOpenAI
        if stream:
            # 流式不缓存——每次创建新实例，防止并发覆盖 streaming 属性
            return ReasoningChatOpenAI(
                model=self._model_id, temperature=temperature, max_tokens=max_tokens,
                api_key=self._api_key or None, base_url=self._base_url or None,
                timeout=get_settings().llm_timeout, streaming=True,
                **(self._adapter.get_chat_openai_kwargs()))
        # 非流式复用实例
        if self._cached_llm is None:
            self._cached_llm = ReasoningChatOpenAI(
                model=self._model_id, api_key=self._api_key or None,
                base_url=self._base_url or None, timeout=get_settings().llm_timeout,
                streaming=False, **(self._adapter.get_chat_openai_kwargs()))
        self._cached_llm.temperature = temperature
        self._cached_llm.max_tokens = max_tokens
        return self._cached_llm
