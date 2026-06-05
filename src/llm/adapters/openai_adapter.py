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

    def get_chat_openai_kwargs(self) -> dict:
        return {}
