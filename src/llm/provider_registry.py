"""LLM Provider 注册表，统一模型工厂和配置字段解析。"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from src.llm.provider import LLMProvider
from src.logging_config import get_logger


logger = get_logger(__name__)


@dataclass(frozen=True)
class ProviderRegistration:
    """Provider 实现及其配置字段声明。"""

    name: str
    provider_class: type[LLMProvider]
    api_key_setting: str
    base_url_setting: str
    default_model: str


_registry: dict[str, ProviderRegistration] = {}
_defaults_loaded = False


# 方法作用：以装饰器形式注册 Provider 实现和配置元数据。
# Args: name - Provider 名称；api_key_setting - Settings 密钥字段；base_url_setting - Settings 地址字段；default_model - 默认模型。
# Returns: 接收 Provider 类并完成注册的装饰器。
def register_provider(
    name: str,
    *,
    api_key_setting: str = "",
    base_url_setting: str = "",
    default_model: str = "",
):
    """注册 Provider，重复名称由最新显式注册覆盖。"""
    normalized = name.strip().lower()
    logger.debug("注册 Provider 装饰器入口", provider=normalized)

    # 方法作用：把 Provider 类写入模块级注册表。
    # Args: provider_class - LLMProvider 实现类。
    # Returns: 原 Provider 类，供装饰器保持类定义不变。
    def decorator(provider_class: type[LLMProvider]) -> type[LLMProvider]:
        logger.debug(
            "Provider 类注册入口",
            provider=normalized,
            provider_class=provider_class.__name__,
        )
        if not normalized:
            logger.error("Provider 类注册失败", reason="名称为空")
            raise ValueError("Provider 名称不能为空")
        _registry[normalized] = ProviderRegistration(
            name=normalized,
            provider_class=provider_class,
            api_key_setting=api_key_setting,
            base_url_setting=base_url_setting,
            default_model=default_model,
        )
        logger.info(
            "Provider 类注册完成",
            provider=normalized,
            provider_class=provider_class.__name__,
        )
        return provider_class

    logger.info("注册 Provider 装饰器完成", provider=normalized)
    return decorator


# 方法作用：加载项目内置 Provider 模块以触发装饰器注册。
# Args: 无。
# Returns: 无返回值。
def _load_default_providers() -> None:
    """延迟导入内置实现，避免 provider.py 与注册表产生循环依赖。"""
    global _defaults_loaded
    logger.debug("加载默认 Provider 入口", already_loaded=_defaults_loaded)
    if _defaults_loaded:
        logger.info("加载默认 Provider 跳过", reason="已加载")
        return
    _defaults_loaded = True
    try:
        import src.llm.provider_anthropic  # noqa: F401
        import src.llm.provider_openai  # noqa: F401
    except Exception as exc:
        _defaults_loaded = False
        logger.error("加载默认 Provider 失败", error=str(exc), exc_info=True)
        raise
    logger.info("加载默认 Provider 完成", providers=sorted(_registry))


# 方法作用：按名称创建 Provider 实例。
# Args: name - Provider 名称；model_id - 模型标识；base_url - 服务地址；api_key - API 密钥。
# Returns: 注册的 Provider 实例。
def create_provider(
    name: str,
    model_id: str,
    base_url: str,
    api_key: str,
) -> LLMProvider:
    """从注册表创建 Provider，不对具体厂商做条件分支。"""
    _load_default_providers()
    normalized = name.strip().lower()
    logger.debug("创建 Provider 入口", provider=normalized, model_id=model_id)
    registration = _registry.get(normalized)
    if registration is None:
        logger.error("创建 Provider 失败", provider=normalized, reason="实现未注册")
        raise ValueError(f"不支持的 Provider: {normalized}")
    provider = registration.provider_class(model_id, base_url, api_key)
    logger.info("创建 Provider 完成", provider=normalized, model_id=model_id)
    return provider


# 方法作用：从 Settings 声明字段解析凭证并创建 Provider。
# Args: name - Provider 名称；model_id - 模型标识；settings - Settings 或测试配置。
# Returns: 已验证凭证的 Provider 实例。
def create_provider_from_settings(
    name: str,
    model_id: str,
    settings: Any,
) -> LLMProvider:
    """配置字段由注册元数据驱动，client.py 不再硬编码厂商。"""
    _load_default_providers()
    normalized = name.strip().lower()
    logger.debug("按配置创建 Provider 入口", provider=normalized, model_id=model_id)
    registration = _registry.get(normalized)
    if registration is None:
        logger.error("按配置创建 Provider 失败", provider=normalized, reason="实现未注册")
        raise ValueError(f"不支持的 Provider: {normalized}")
    api_key = str(getattr(settings, registration.api_key_setting, "") or "")
    base_url = str(getattr(settings, registration.base_url_setting, "") or "")
    if not api_key:
        logger.error("按配置创建 Provider 失败", provider=normalized, reason="API Key 缺失")
        raise ValueError(f"模型 {model_id} 需要 {registration.api_key_setting.upper()}")
    provider = create_provider(normalized, model_id, base_url, api_key)
    logger.info("按配置创建 Provider 完成", provider=normalized, model_id=model_id)
    return provider


# 方法作用：判断指定 Provider 的凭证是否已配置。
# Args: name - Provider 名称；settings - Settings 或测试配置。
# Returns: API Key 非空返回 True。
def provider_has_credentials(name: str, settings: Any) -> bool:
    """只检查配置，不触发网络请求或创建模型客户端。"""
    _load_default_providers()
    normalized = name.strip().lower()
    logger.debug("Provider 凭证检查入口", provider=normalized)
    registration = _registry.get(normalized)
    available = bool(
        registration
        and getattr(settings, registration.api_key_setting, "")
    )
    logger.info("Provider 凭证检查完成", provider=normalized, available=available)
    return available


# 方法作用：返回 Provider 声明的默认模型。
# Args: name - Provider 名称。
# Returns: 默认模型标识，未注册时抛出 ValueError。
def get_default_model(name: str) -> str:
    """为显式切换 Provider 但未指定模型的调用提供默认值。"""
    _load_default_providers()
    normalized = name.strip().lower()
    logger.debug("获取 Provider 默认模型入口", provider=normalized)
    registration = _registry.get(normalized)
    if registration is None:
        logger.error("获取 Provider 默认模型失败", provider=normalized)
        raise ValueError(f"不支持的 Provider: {normalized}")
    logger.info(
        "获取 Provider 默认模型完成",
        provider=normalized,
        model_id=registration.default_model,
    )
    return registration.default_model


# 方法作用：移除注册项，供测试和插件卸载使用。
# Args: name - Provider 名称。
# Returns: 存在并移除时返回 True。
def unregister_provider(name: str) -> bool:
    """显式移除 Provider，不影响其他注册项。"""
    normalized = name.strip().lower()
    logger.debug("注销 Provider 入口", provider=normalized)
    removed = _registry.pop(normalized, None) is not None
    logger.info("注销 Provider 完成", provider=normalized, removed=removed)
    return removed


# 方法作用：列出当前已注册 Provider 名称。
# Args: 无。
# Returns: 排序后的 Provider 名称列表。
def list_providers() -> list[str]:
    """返回可供管理 API 或诊断使用的稳定名称列表。"""
    _load_default_providers()
    result = sorted(_registry)
    logger.info("列出 Provider 完成", count=len(result), providers=result)
    return result
