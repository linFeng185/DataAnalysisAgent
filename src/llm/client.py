"""10.1 LLM 客户端工厂 — 通过模型适配器统一管理各模型差异。"""

from __future__ import annotations

from langchain_core.language_models import BaseChatModel

from src.config import get_settings
from src.llm.adapters.registry import get_adapter
from src.logging_config import get_logger

logger = get_logger(__name__)


def get_openai_llm(model: str | None = None, temperature: float | None = None, max_tokens: int | None = None, reasoning: bool = True) -> BaseChatModel:
    """10.1.1 ChatOpenAI 工厂 — 通过适配器注入模型特有参数。

    reasoning=False 时剥离 reasoning_effort/extra_body 参数，
    用于 SQL 生成等结构化任务，可显著降低首 token 延迟。
    """
    s = get_settings()
    model_name = model or s.llm_model
    adapter = get_adapter(model_name)
    base = s.openai_base_url or adapter.get_default_base_url()
    sf = adapter.supported_features

    logger.info("LLM 初始化", model=model_name, base_url=base, has_key=bool(s.openai_api_key),
                reasoning=sf.reasoning and reasoning, streaming=sf.streaming)

    from src.llm.reasoning_chat_openai import ReasoningChatOpenAI
    adapter_kwargs = adapter.get_chat_openai_kwargs()

    if not reasoning:
        adapter_kwargs.pop("reasoning_effort", None)
        adapter_kwargs.pop("extra_body", None)

    return ReasoningChatOpenAI(
        model=model_name,
        temperature=temperature if temperature is not None else s.llm_temperature,
        max_tokens=max_tokens or s.llm_max_tokens,
        api_key=s.openai_api_key or None,
        base_url=base or None,
        timeout=s.llm_timeout,
        streaming=True,  # 始终开启，ainvoke 内部静默消费，astream_events 输出事件
        **adapter_kwargs,
    )


def get_anthropic_llm(model: str | None = None, temperature: float | None = None, max_tokens: int | None = None) -> BaseChatModel:
    """10.1.2 ChatAnthropic 工厂。"""
    s = get_settings()
    from langchain_anthropic import ChatAnthropic
    return ChatAnthropic(
        model=model or "claude-sonnet-4-6",
        temperature=temperature if temperature is not None else s.llm_temperature,
        max_tokens=max_tokens or s.llm_max_tokens,
        api_key=s.anthropic_api_key or None,
        timeout=s.llm_timeout,
        streaming=True,
    )


def get_llm(provider: str | None = None, model: str | None = None, temperature: float | None = None, reasoning: bool = True) -> BaseChatModel:
    """10.1.3 路由器。"""
    s = get_settings()
    provider = provider or s.llm_provider
    if provider == "anthropic":
        return get_anthropic_llm(model=model, temperature=temperature)
    return get_openai_llm(model=model, temperature=temperature, reasoning=reasoning)


def get_cheap_llm() -> BaseChatModel:
    """10.1.4 低成本 LLM。"""
    return get_openai_llm(model=get_settings().cheap_llm_model, temperature=0, max_tokens=1024)


def is_llm_available() -> bool:
    """API Key 是否已配置。"""
    s = get_settings()
    if s.llm_provider == "anthropic":
        ok = bool(s.anthropic_api_key)
        if not ok:
            logger.warning("LLM 不可用: ANTHROPIC_API_KEY 未设置")
    else:
        ok = bool(s.openai_api_key)
        if not ok:
            logger.warning("LLM 不可用: OPENAI_API_KEY 未设置, 将使用模板回退")
    return ok
