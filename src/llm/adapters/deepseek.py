"""DeepSeek V4 Pro 适配器 — 思考模式 + reasoning_effort。"""

from __future__ import annotations

from src.llm.adapters.base import ModelAdapter, ParsedResponse, StreamChunk, SupportedFeatures


class DeepSeekV4ProAdapter(ModelAdapter):
    provider = "openai"
    default_base_url = "https://api.deepseek.com"
    supported_features = SupportedFeatures(
        streaming=True,
        reasoning=True,
        reasoning_content_in_response=True,
        function_calling=True,
        json_mode=True,
    )

    def get_chat_openai_kwargs(self) -> dict:
        return {
            "reasoning_effort": "high",
            "extra_body": {"thinking": {"type": "enabled"}},
        }

    def parse_stream_chunk(self, chunk) -> StreamChunk:
        result = super().parse_stream_chunk(chunk)
        if not result.reasoning_content:
            try:
                if hasattr(chunk, "response_metadata") and isinstance(chunk.response_metadata, dict):
                    choices = chunk.response_metadata.get("choices", [])
                    if choices and isinstance(choices, list) and len(choices) > 0:
                        delta = choices[0].get("delta", {}) if isinstance(choices[0], dict) else getattr(choices[0], "delta", {})
                        rc = delta.get("reasoning_content", "") if isinstance(delta, dict) else getattr(delta, "reasoning_content", "")
                        if rc:
                            result.reasoning_content = rc if isinstance(rc, str) else str(rc)
            except Exception:
                pass
        return result
