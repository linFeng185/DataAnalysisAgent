"""10.1 LLM 客户端工厂 — 通过模型适配器统一管理各模型差异。"""

from __future__ import annotations

import hashlib
from typing import TYPE_CHECKING

from langchain_core.language_models import BaseChatModel

from src.config import get_settings
from src.llm.adapters.registry import get_adapter
from src.logging_config import get_logger

if TYPE_CHECKING:
    from src.llm.provider import LLMProvider

logger = get_logger(__name__)


# 方法作用：通过统一 Provider 入口创建 OpenAI-compatible ChatModel。
# Args: model - 模型名；temperature - 温度；max_tokens - 输出上限；reasoning - 是否启用推理；base_url/api_key/timeout - 可选连接覆盖。
# Returns: Provider 创建的 BaseChatModel。
def get_openai_llm(
    model: str | None = None,
    temperature: float | None = None,
    max_tokens: int | None = None,
    reasoning: bool = True,
    *,
    base_url: str | None = None,
    api_key: str | None = None,
    timeout: int | None = None,
) -> BaseChatModel:
    """10.1.1 ChatOpenAI 工厂 — 通过适配器注入模型特有参数。

    reasoning=False 时剥离 reasoning_effort/extra_body 参数，
    用于 SQL 生成等结构化任务，可显著降低首 token 延迟。
    """
    s = get_settings()
    model_name = model or s.llm_model
    adapter = get_adapter(model_name)
    base = base_url if base_url is not None else (s.openai_base_url or adapter.get_default_base_url())
    resolved_api_key = api_key if api_key is not None else s.openai_api_key
    resolved_timeout = timeout if timeout is not None else s.llm_timeout
    sf = adapter.supported_features

    logger.info("LLM 初始化", model=model_name, base_url=base, has_key=bool(resolved_api_key),
                reasoning=sf.reasoning and reasoning, streaming=sf.streaming)

    provider_instance = get_provider(
        model_id=model_name,
        provider_name="openai",
        base_url=base,
        api_key=resolved_api_key,
    )
    result = provider_instance.get_chat_model(
        temperature=temperature if temperature is not None else s.llm_temperature,
        max_tokens=max_tokens or s.llm_max_tokens,
        stream=True,
        reasoning=reasoning,
        timeout=resolved_timeout,
    )
    logger.info("OpenAI-compatible LLM 创建完成", model=model_name)
    return result


# 方法作用：通过统一 Provider 入口创建 Anthropic ChatModel。
# Args: model - 模型名；temperature - 温度；max_tokens - 输出上限。
# Returns: Provider 创建的 BaseChatModel。
def get_anthropic_llm(model: str | None = None, temperature: float | None = None, max_tokens: int | None = None) -> BaseChatModel:
    """10.1.2 ChatAnthropic 工厂。"""
    from src.llm.provider_registry import get_default_model

    model_name = model or get_default_model("anthropic")
    provider = get_provider(model_id=model_name, provider_name="anthropic")
    result = provider.get_chat_model(
        temperature=temperature,
        max_tokens=max_tokens,
        stream=True,
    )
    logger.info("Anthropic LLM 创建完成", model=model_name)
    return result


# 方法作用：兼容旧调用并通过统一 Provider 入口创建 ChatModel。
# Args: provider - Provider 名称；model - 模型名；temperature - 温度；reasoning - 是否推理；max_tokens - 输出上限。
# Returns: Provider 创建的 BaseChatModel。
def get_llm(
    provider: str | None = None,
    model: str | None = None,
    temperature: float | None = None,
    reasoning: bool = True,
    max_tokens: int | None = None,
) -> BaseChatModel:
    """10.1.3 路由器。"""
    s = get_settings()
    provider_name = provider or s.llm_provider
    from src.llm.provider_registry import get_default_model

    if model:
        model_name = model
    elif provider_name == s.llm_provider:
        model_name = s.llm_model
    else:
        model_name = get_default_model(provider_name)
    provider_instance = get_provider(model_id=model_name, provider_name=provider_name)
    result = provider_instance.get_chat_model(
        temperature=temperature,
        stream=True,
        reasoning=reasoning,
        max_tokens=max_tokens,
    )
    logger.info("兼容 LLM 路由完成", provider=provider_name, model=model_name)
    return result


# 方法作用：通过统一兼容路由创建低成本模型。
# Args: 无。
# Returns: 已配置的低成本 BaseChatModel。
def get_cheap_llm() -> BaseChatModel:
    """10.1.4 低成本 LLM。"""
    settings = get_settings()
    logger.debug(
        "创建低成本 LLM 入口",
        provider=settings.llm_provider,
        model=settings.cheap_llm_model,
    )
    result = get_llm(
        provider=settings.llm_provider,
        model=settings.cheap_llm_model,
        temperature=0,
        reasoning=False,
        max_tokens=1024,
    )
    logger.info(
        "创建低成本 LLM 完成",
        provider=settings.llm_provider,
        model=settings.cheap_llm_model,
    )
    return result


# 方法作用：判断配置的远程模型是否具备调用凭证。
# Args: settings - Settings 或具有同名字段的测试配置。
# Returns: 远程 Provider 可用返回 True，否则返回 False。
def _is_remote_available(settings) -> bool:
    """检查远程配置模型可用性，不触发任何网络请求。"""
    provider = getattr(settings, "llm_provider", "openai")
    from src.llm.provider_registry import provider_has_credentials

    available = provider_has_credentials(provider, settings)
    logger.debug("远程 LLM 可用性检查", provider=provider, available=available)
    return available


# 方法作用：判断本地 OpenAI-compatible 模型是否已完整配置。
# Args: settings - Settings 或具有同名字段的测试配置。
# Returns: 本地模型名称和地址均存在时返回 True。
def _is_local_available(settings) -> bool:
    """检查本地模型配置完整性，不要求真实 API Key。"""
    available = bool(
        getattr(settings, "local_llm_model", "")
        and getattr(settings, "local_llm_base_url", "")
    )
    logger.debug("本地 LLM 可用性检查", available=available)
    return available


# 方法作用：按节点任务和配置决定使用本地模型、远程模型或确定性回退。
# Args: task - 节点任务标识；settings - 可选受控配置，默认读取项目 Settings。
# Returns: local/remote/none 三种目标之一。
def resolve_llm_task_target(task: str, settings=None) -> str:
    """解析节点级模型目标，避免轻量任务默认等待慢速远程模型。"""
    settings = settings or get_settings()
    normalized_task = (task or "").strip().lower()
    remote_tasks = {
        item.strip().lower()
        for item in str(getattr(settings, "llm_remote_tasks", "generate_sql")).split(",")
        if item.strip()
    }
    local_available = _is_local_available(settings)
    remote_available = _is_remote_available(settings)
    if normalized_task in remote_tasks:
        target = "remote" if remote_available else ("local" if local_available else "none")
    elif local_available:
        target = "local"
    elif getattr(settings, "llm_allow_remote_fallback", False) and remote_available:
        target = "remote"
    else:
        target = "none"
    logger.info(
        "LLM 任务路由完成",
        task=normalized_task,
        target=target,
        remote_authorized=normalized_task in remote_tasks,
    )
    return target


# 方法作用：判断指定节点任务是否存在被策略授权的可用模型。
# Args: task - 节点任务标识。
# Returns: 路由目标不是 none 时返回 True。
def is_task_llm_available(task: str) -> bool:
    """检查节点任务模型可用性。"""
    logger.debug("LLM 任务可用性检查入口", task=task)
    available = resolve_llm_task_target(task) != "none"
    logger.info("LLM 任务可用性检查完成", task=task, available=available)
    return available


# 方法作用：根据节点任务创建本地或远程 ChatModel 实例。
# Args: task - 节点任务标识；temperature - 温度；reasoning - 是否启用模型推理模式；max_tokens - 输出上限。
# Returns: 已按任务策略配置的 BaseChatModel。
def get_task_llm(
    task: str,
    temperature: float | None = None,
    reasoning: bool = False,
    max_tokens: int | None = None,
) -> BaseChatModel:
    """创建节点级模型，未授权任何模型时抛出明确异常。"""
    settings = get_settings()
    target = resolve_llm_task_target(task, settings=settings)
    logger.debug("创建任务 LLM 入口", task=task, target=target, reasoning=reasoning)
    if target == "local":
        provider = get_provider(
            model_id=settings.local_llm_model,
            provider_name="openai",
            base_url=settings.local_llm_base_url,
            api_key=settings.local_llm_api_key or "local",
        )
        model = provider.get_chat_model(
            temperature=temperature,
            max_tokens=max_tokens,
            stream=True,
            reasoning=reasoning,
            timeout=settings.local_llm_timeout,
        )
    elif target == "remote":
        model = get_llm(
            temperature=temperature,
            reasoning=reasoning,
        )
    else:
        logger.error("创建任务 LLM 失败", task=task, reason="没有授权的可用模型")
        raise RuntimeError(f"任务 {task} 没有授权的可用模型")
    logger.info("创建任务 LLM 完成", task=task, target=target)
    return model


# 方法作用：解析模型所属 Provider，并在当前 AppContext 内复用实例。
# Args: model_id - 模型标识；provider_name - 可选显式 Provider；base_url/api_key - 可选连接覆盖。
# Returns: 当前 Context 持有的 LLMProvider。
def get_provider(
    model_id: str | None = None,
    *,
    provider_name: str | None = None,
    base_url: str | None = None,
    api_key: str | None = None,
) -> "LLMProvider":
    s = get_settings()
    mid = model_id or s.llm_model
    logger.info(
        "Provider 路由边界输入",
        model_id=mid,
        explicit_provider=provider_name or "",
        explicit_connection=base_url is not None or api_key is not None,
    )
    resolved_provider = (provider_name or "").strip().lower()
    if not resolved_provider:
        from src.llm.model_registry import get_model_registry

        info = get_model_registry().get(mid)
        if not info:
            logger.error("Provider 路由失败", model_id=mid, reason="模型未注册")
            raise ValueError(f"未知模型: {mid}")
        resolved_provider = info.provider
    logger.info("Provider 模型解析完成", model_id=mid, provider=resolved_provider)

    from src.app_context import get_app_context
    from src.llm.provider_registry import create_provider, create_provider_from_settings

    connection_fingerprint = hashlib.sha256(
        (
            f"base_explicit={base_url is not None}:{base_url or ''}\0"
            f"key_explicit={api_key is not None}:{api_key or ''}"
        ).encode("utf-8")
    ).hexdigest()[:12]
    resource_name = f"llm_provider:{resolved_provider}:{mid}:{connection_fingerprint}"

    # 方法作用：按显式连接覆盖或 Settings 注册元数据创建 Provider。
    # Args: 无。
    # Returns: 新建 LLMProvider。
    def factory() -> "LLMProvider":
        logger.debug("Provider 资源工厂入口", provider=resolved_provider, model_id=mid)
        if base_url is not None or api_key is not None:
            result = create_provider(
                resolved_provider,
                mid,
                base_url or "",
                api_key or "",
            )
        else:
            result = create_provider_from_settings(resolved_provider, mid, s)
        logger.info("Provider 资源工厂完成", provider=resolved_provider, model_id=mid)
        return result

    provider = get_app_context().get_or_create(resource_name, factory)
    logger.info("Provider 路由完成", model_id=mid, provider=resolved_provider)
    return provider


def is_llm_available() -> bool:
    """API Key 是否已配置。"""
    s = get_settings()
    from src.llm.provider_registry import provider_has_credentials

    ok = provider_has_credentials(s.llm_provider, s)
    if not ok:
        logger.warning("LLM 不可用，将使用模板回退", provider=s.llm_provider)
    return ok
