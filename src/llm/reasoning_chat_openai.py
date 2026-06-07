"""
自定义 ChatOpenAI 子类 — 修复 langchain-openai 丢弃 reasoning_content 的问题。

langchain-openai 1.x 的 _convert_delta_to_message_chunk 只提取已知字段
(id/role/content/function_call/tool_calls)，reasoning_content 被忽略。
这导致 DeepSeek 等模型的思考过程在流式输出中不可见。

本模块通过重写 _convert_chunk_to_generation_chunk 将 reasoning_content
注入 additional_kwargs，使 downstream 的提取逻辑能正常工作。
"""

from __future__ import annotations

from typing import Any

from langchain_core.messages import AIMessageChunk
from langchain_core.outputs import ChatGenerationChunk
from langchain_openai import ChatOpenAI


class ReasoningChatOpenAI(ChatOpenAI):
    """
    ChatOpenAI 子类，将 API 返回的 reasoning_content 保留到 additional_kwargs 中。

    使用方式与 ChatOpenAI 完全相同，额外处理了流式 chunk 中的 thinking 内容。
    """

    def _convert_chunk_to_generation_chunk(
        self,
        chunk: dict,
        default_chunk_class: type,
        base_generation_info: dict | None,
    ) -> ChatGenerationChunk | None:
        generation_chunk = super()._convert_chunk_to_generation_chunk(
            chunk, default_chunk_class, base_generation_info,
        )
        if generation_chunk is None:
            return None

        reasoning = _extract_raw_delta_reasoning(chunk)
        if reasoning and isinstance(generation_chunk.message, AIMessageChunk):
            generation_chunk.message.additional_kwargs["reasoning_content"] = reasoning

        return generation_chunk


def _extract_raw_delta_reasoning(chunk: dict) -> str:
    """从原始 API chunk dict 中提取 reasoning_content。"""
    choices = (
        chunk.get("choices", [])
        or chunk.get("chunk", {}).get("choices", [])
    )
    if not choices:
        return ""
    delta = choices[0].get("delta", {}) or {}
    rc = delta.get("reasoning_content", "")
    return rc if isinstance(rc, str) else str(rc) if rc else ""
