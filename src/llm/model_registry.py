"""模型注册表 — 管理所有可用模型及其能力声明。

前端 GET /models 从此读取列表。
get_by_capability() 支持按 vision/context_window 筛选。
"""

from __future__ import annotations

from dataclasses import dataclass

from src.llm.adapters.base import SupportedFeatures
from src.logging_config import get_logger


logger = get_logger(__name__)


@dataclass
class ModelInfo:
    """单个模型的元数据。

    cost_per_1k_tokens: 可选，用于后续成本追踪
    """
    model_id: str
    provider: str
    display_name: str
    capabilities: SupportedFeatures
    cost_per_1k_tokens: float = 0.0


class ModelRegistry:
    """模型注册表——列出所有可用模型及其能力。

    内置模型在 _register_defaults() 中注册。
    支持按能力筛选（vision=True, context_window>=100000）。
    """

    def __init__(self):
        """初始化注册表。"""
        self._models: dict[str, ModelInfo] = {}

    def register(self, info: ModelInfo):
        """注册一个模型。

        Args:
            info: ModelInfo 实例
        """
        self._models[info.model_id] = info

    def list_all(self) -> list[ModelInfo]:
        """列出所有已注册模型。

        Returns: ModelInfo 列表
        """
        return list(self._models.values())

    def get(self, model_id: str) -> ModelInfo | None:
        """按 ID 查找模型。

        Args:
            model_id: 模型标识符如 "deepseek-v4-pro"

        Returns: 匹配的 ModelInfo，不存在返回 None
        """
        return self._models.get(model_id)

    def get_by_capability(self, **caps) -> list[ModelInfo]:
        """按能力筛选模型。

        布尔字段用 == 比较，数值字段用 >= 比较。

        Args:
            vision=True, context_window=100000 等

        Returns: 满足条件的 ModelInfo 列表

        Example:
            registry.get_by_capability(vision=True, context_window=100000)
            → [ModelInfo("gpt-4o"), ModelInfo("claude-sonnet-4-6")]
        """
        return [
            m for m in self._models.values()
            if all(
                getattr(m.capabilities, k) == v if isinstance(v, bool)
                else getattr(m.capabilities, k) >= v
                for k, v in caps.items()
            )
        ]


# 方法作用：从当前 AppContext 获取模型注册表并按需注册内置模型。
# Args: 无。
# Returns: 当前应用独享的 ModelRegistry 实例。
def get_model_registry() -> ModelRegistry:
    """获取当前应用的 ModelRegistry，首次调用时注册内置模型。

    Returns: ModelRegistry 实例
    """
    from src.app_context import get_app_context

    logger.debug("获取 ModelRegistry 入口")
    result = get_app_context().get_or_create(
        "model_registry",
        _create_model_registry,
    )
    logger.info("获取 ModelRegistry 完成")
    return result


# 方法作用：创建已注册内置模型的 ModelRegistry。
# Args: 无。
# Returns: 完成默认模型注册的 ModelRegistry 实例。
def _create_model_registry() -> ModelRegistry:
    logger.debug("创建 ModelRegistry 入口")
    result = ModelRegistry()
    _register_defaults(result)
    logger.info("创建 ModelRegistry 完成")
    return result


def _register_defaults(r: ModelRegistry):
    """注册内置模型。"""
    r.register(ModelInfo("deepseek-v4-flash", "openai", "DeepSeek V4 Flash",
        SupportedFeatures(streaming=True, reasoning=False, function_calling=False,
                          json_mode=True, max_tokens_limit=8192,
                          context_window=1_000_000, vision=False)))
    r.register(ModelInfo("deepseek-v4-pro", "openai", "DeepSeek V4 Pro",
        SupportedFeatures(streaming=True, reasoning=True, reasoning_content_in_response=True,
                          function_calling=False, json_mode=True, max_tokens_limit=8192,
                          context_window=1_000_000, vision=False)))
    r.register(ModelInfo("gpt-4o", "openai", "GPT-4o",
        SupportedFeatures(streaming=True, reasoning=True, function_calling=True,
                          json_mode=True, max_tokens_limit=16384,
                          context_window=128000, vision=True)))
    r.register(ModelInfo("claude-sonnet-4-6", "anthropic", "Claude Sonnet 4.6",
        SupportedFeatures(streaming=True, reasoning=True, function_calling=True,
                          json_mode=True, max_tokens_limit=8192,
                          context_window=200000, vision=True)))
