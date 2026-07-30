"""DeepSeek V4 Pro/Flash 适配器与官方思考协议。"""

from __future__ import annotations

from src.llm.adapters.base import ModelAdapter, StreamChunk, SupportedFeatures
from src.logging_config import get_logger

logger = get_logger(__name__)


class _DeepSeekV4Adapter(ModelAdapter):
    """两个 DeepSeek V4 模型共享的思考、工具调用和流式解析协议。"""

    provider = "openai"
    default_base_url = "https://api.deepseek.com"
    supported_features = SupportedFeatures(
        streaming=True, reasoning=True, reasoning_content_in_response=True,
        function_calling=True, json_mode=True, max_tokens_limit=8192,
        context_window=1_000_000, vision=False,
        reasoning_efforts=("high", "max"), default_reasoning_effort="high",
        reasoning_ignores_sampling=True)

    # 方法作用：把统一思考开关和深度转换为 DeepSeek 官方请求参数。
    # Args: reasoning - 是否开启思考模式；reasoning_effort - 统一推理深度。
    # Returns: reasoning_effort 与 extra_body.thinking 参数。
    def get_chat_openai_kwargs(
        self,
        *,
        reasoning: bool = True,
        reasoning_effort: str | None = None,
    ) -> dict:
        if not reasoning:
            return {"extra_body": {"thinking": {"type": "disabled"}}}
        return {
            "reasoning_effort": self.normalize_reasoning_effort(reasoning_effort),
            "extra_body": {"thinking": {"type": "enabled"}},
        }

    # 方法作用：按 DeepSeek 兼容规则归一化 low/medium/xhigh 深度别名。
    # Args: reasoning_effort - 用户或模型默认推理深度。
    # Returns: DeepSeek 实际生效的 high 或 max。
    def normalize_reasoning_effort(self, reasoning_effort: str | None) -> str:
        effort = str(reasoning_effort or "high").lower()
        if effort in {"low", "medium", "high"}:
            return "high"
        if effort in {"xhigh", "max"}:
            return "max"
        raise ValueError(f"DeepSeek 不支持推理深度: {effort}")

    # 方法作用：兼容提取 DeepSeek 流式 delta 中的 reasoning_content。
    # Args: chunk - LangChain 消息块或生成块。
    # Returns: 同时包含正文和推理内容的统一 StreamChunk。
    def parse_stream_chunk(self, chunk) -> StreamChunk:
        result = super().parse_stream_chunk(chunk)
        if not result.reasoning_content:
            try:
                target = chunk
                if hasattr(chunk, "message") and not hasattr(chunk, "additional_kwargs"):
                    target = chunk.message
                if hasattr(target, "response_metadata") and isinstance(target.response_metadata, dict):
                    choices = target.response_metadata.get("choices", [])
                    if choices and isinstance(choices, list) and len(choices) > 0:
                        delta = choices[0].get("delta", {}) if isinstance(choices[0], dict) else getattr(choices[0], "delta", {})
                        rc = delta.get("reasoning_content", "") if isinstance(delta, dict) else getattr(delta, "reasoning_content", "")
                        if rc:
                            result.reasoning_content = rc if isinstance(rc, str) else str(rc)
            except Exception as exc:
                logger.debug(
                    "DeepSeek 推理流兼容解析跳过",
                    chunk_type=type(chunk).__name__,
                    error=str(exc),
                    exc_info=True,
                )
        return result


class DeepSeekV4ProAdapter(_DeepSeekV4Adapter):
    """DeepSeek V4 Pro 模型适配器。"""


class DeepSeekV4FlashAdapter(_DeepSeekV4Adapter):
    """DeepSeek V4 Flash 模型适配器。"""
