"""OpenAI 标准适配器。"""

from __future__ import annotations

from src.llm.adapters.base import ModelAdapter, SupportedFeatures


class OpenAIAdapter(ModelAdapter):
    provider = "openai"
    default_base_url = "https://api.openai.com/v1"
    supported_features = SupportedFeatures(
        streaming=True,
        reasoning=True,
        reasoning_content_in_response=False,
        function_calling=True,
        json_mode=True,
    )

    # 方法作用：保持标准 OpenAI 模型不注入厂商私有推理参数。
    # Args: reasoning - 是否开启推理；reasoning_effort - 可选推理深度。
    # Returns: 空参数字典。
    def get_chat_openai_kwargs(
        self,
        *,
        reasoning: bool = True,
        reasoning_effort: str | None = None,
    ) -> dict:
        del reasoning, reasoning_effort
        return {}
