"""Anthropic Provider，实现统一非流式、流式和 ChatModel 工厂接口。"""

from __future__ import annotations

from typing import Any

from src.config import get_settings
from src.llm.adapters.base import SupportedFeatures
from src.llm.provider import LLMProvider, LLMResponse, LLMStreamChunk
from src.llm.provider_registry import register_provider
from src.logging_config import get_logger


logger = get_logger(__name__)


@register_provider(
    "anthropic",
    api_key_setting="anthropic_api_key",
    base_url_setting="anthropic_base_url",
    default_model="claude-sonnet-4-6",
)
class AnthropicProvider(LLMProvider):
    """通过 langchain-anthropic 调用 Claude 模型。"""

    # 方法作用：保存 Anthropic 模型连接配置和能力声明。
    # Args: model_id - 模型标识；base_url - 可选兼容服务地址；api_key - Anthropic API Key。
    # Returns: 无返回值。
    def __init__(self, model_id: str, base_url: str, api_key: str) -> None:
        logger.debug("AnthropicProvider.__init__ 入口", model_id=model_id, has_key=bool(api_key))
        self._model_id = model_id
        self._base_url = base_url
        self._api_key = api_key
        from src.llm.model_registry import get_model_registry

        info = get_model_registry().get(model_id)
        self._capabilities = info.capabilities if info else SupportedFeatures(
            streaming=True,
            reasoning=True,
            function_calling=True,
            json_mode=True,
            max_tokens_limit=8192,
            context_window=200000,
            vision=True,
        )
        logger.info("AnthropicProvider.__init__ 完成", model_id=model_id)

    @property
    def capabilities(self) -> SupportedFeatures:
        """返回 Claude 模型能力声明。

        Args:
            无。

        Returns:
            SupportedFeatures 能力对象。
        """
        logger.debug("AnthropicProvider.capabilities 入口", model_id=self._model_id)
        logger.info("AnthropicProvider.capabilities 完成", model_id=self._model_id)
        return self._capabilities

    # 方法作用：创建 LangChain ChatAnthropic 实例。
    # Args: temperature - 温度；max_tokens - 输出上限；stream - 是否流式；reasoning - 是否允许推理模式。
    # Returns: 配置完成的 ChatAnthropic 实例。
    def get_chat_model(
        self,
        temperature: float | None = None,
        max_tokens: int | None = None,
        stream: bool = True,
        reasoning: bool = True,
    ):
        """使用统一参数创建 ChatAnthropic，不发起网络请求。"""
        del reasoning
        settings = get_settings()
        logger.debug(
            "Anthropic ChatModel 创建入口",
            model_id=self._model_id,
            stream=stream,
        )
        from langchain_anthropic import ChatAnthropic

        kwargs: dict[str, Any] = {
            "model": self._model_id,
            "temperature": temperature if temperature is not None else settings.llm_temperature,
            "max_tokens": max_tokens or settings.llm_max_tokens,
            "api_key": self._api_key or None,
            "timeout": settings.llm_timeout,
            "streaming": stream,
        }
        if self._base_url:
            kwargs["base_url"] = self._base_url
        model = ChatAnthropic(**kwargs)
        logger.info("Anthropic ChatModel 创建完成", model_id=self._model_id, stream=stream)
        return model

    # 方法作用：调用 Claude 并返回统一完整响应。
    # Args: messages - 字典消息列表；temperature - 温度；max_tokens - 输出上限；stream - 兼容参数。
    # Returns: LLMResponse 标准响应。
    async def agenerate(
        self,
        messages: list[dict],
        temperature: float | None = None,
        max_tokens: int | None = None,
        stream: bool = False,
    ) -> LLMResponse:
        """执行一次非流式 Anthropic 调用。"""
        logger.debug("AnthropicProvider.agenerate 入口", model_id=self._model_id, messages=len(messages))
        try:
            model = self.get_chat_model(temperature, max_tokens, stream=False)
            response = await model.ainvoke([self._to_lc_msg(message) for message in messages])
            content, reasoning = self._extract_content(response)
            result = LLMResponse(content=content, reasoning=reasoning)
        except Exception as exc:
            logger.error("AnthropicProvider.agenerate 失败", error=str(exc), exc_info=True)
            raise
        logger.info("AnthropicProvider.agenerate 完成", model_id=self._model_id, content_chars=len(content))
        return result

    # 方法作用：流式调用 Claude 并转换为统一 Chunk。
    # Args: messages - 字典消息列表；temperature - 温度；max_tokens - 输出上限。
    # Returns: 异步生成 LLMStreamChunk。
    async def astream(
        self,
        messages: list[dict],
        temperature: float | None = None,
        max_tokens: int | None = None,
    ):
        """逐块产出 Anthropic 文本和推理内容。"""
        logger.debug("AnthropicProvider.astream 入口", model_id=self._model_id, messages=len(messages))
        try:
            model = self.get_chat_model(temperature, max_tokens, stream=True)
            async for chunk in model.astream([self._to_lc_msg(message) for message in messages]):
                content, reasoning = self._extract_content(chunk)
                yield LLMStreamChunk(content=content, reasoning=reasoning)
        except Exception as exc:
            logger.error("AnthropicProvider.astream 失败", error=str(exc), exc_info=True)
            raise
        logger.info("AnthropicProvider.astream 完成", model_id=self._model_id)

    # 方法作用：从 Anthropic 文本块列表中提取正文和 thinking 内容。
    # Args: response - LangChain Anthropic 消息或 Chunk。
    # Returns: 正文和推理文本二元组。
    @staticmethod
    def _extract_content(response: Any) -> tuple[str, str]:
        """兼容字符串内容和 Anthropic content blocks。"""
        logger.debug("Anthropic 内容提取入口", response_type=type(response).__name__)
        raw_content = getattr(response, "content", "") or ""
        if isinstance(raw_content, str):
            result = (raw_content, "")
        else:
            text_parts: list[str] = []
            reasoning_parts: list[str] = []
            for block in raw_content if isinstance(raw_content, list) else []:
                if isinstance(block, dict):
                    block_type = str(block.get("type", ""))
                    value = str(block.get("text") or block.get("thinking") or "")
                else:
                    block_type = str(getattr(block, "type", ""))
                    value = str(getattr(block, "text", "") or getattr(block, "thinking", "") or "")
                if block_type in {"thinking", "reasoning"}:
                    reasoning_parts.append(value)
                elif value:
                    text_parts.append(value)
            result = ("".join(text_parts), "".join(reasoning_parts))
        logger.info(
            "Anthropic 内容提取完成",
            content_chars=len(result[0]),
            reasoning_chars=len(result[1]),
        )
        return result
