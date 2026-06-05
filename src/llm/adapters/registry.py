"""适配器注册表 — model name → 适配器自动匹配。"""

from __future__ import annotations

from src.llm.adapters.base import ModelAdapter

_registry: dict[str, ModelAdapter] = {}


def register(pattern: str, adapter: ModelAdapter) -> None:
    """注册适配器，pattern 为模型名匹配关键字（小写）。"""
    _registry[pattern] = adapter


def get_adapter(model_name: str) -> ModelAdapter:
    """根据模型名匹配适配器，未匹配时返回 OpenAIAdapter。"""
    name_lower = model_name.lower()
    for pattern, adapter in _registry.items():
        if pattern in name_lower:
            return adapter
    from src.llm.adapters.openai_adapter import OpenAIAdapter
    return OpenAIAdapter()


def list_registered() -> dict[str, dict]:
    """列出所有已注册的适配器及其能力，用于调试。"""
    result = {}
    for pattern, a in _registry.items():
        sf = a.supported_features
        result[pattern] = {
            "provider": a.provider,
            "base_url": a.default_base_url,
            "reasoning": sf.reasoning,
            "streaming": sf.streaming,
            "function_calling": sf.function_calling,
        }
    return result


# 初始化注册
from src.llm.adapters.deepseek import DeepSeekV4ProAdapter
from src.llm.adapters.openai_adapter import OpenAIAdapter

register("deepseek", DeepSeekV4ProAdapter())
register("gpt", OpenAIAdapter())
