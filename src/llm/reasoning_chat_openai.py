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
from langchain_core.outputs import ChatGenerationChunk, ChatResult
from langchain_openai import ChatOpenAI


class ReasoningChatOpenAI(ChatOpenAI):
    """
    ChatOpenAI 子类，将 API 返回的 reasoning_content 保留到 additional_kwargs 中。

    使用方式与 ChatOpenAI 完全相同，额外处理了流式 chunk 中的 thinking 内容。
    """

    # 方法作用：在非流式 DeepSeek 响应中保留 reasoning_content。
    # Args: response - OpenAI 字典或 BaseModel 响应；generation_info - 可选生成元数据。
    # Returns: reasoning_content 已写入 AIMessage.additional_kwargs 的 ChatResult。
    def _create_chat_result(
        self,
        response: dict | Any,
        generation_info: dict | None = None,
    ) -> ChatResult:
        result = super()._create_chat_result(response, generation_info)
        response_dict = response if isinstance(response, dict) else response.model_dump()
        choices = response_dict.get("choices", []) if isinstance(response_dict, dict) else []
        for generation, choice in zip(result.generations, choices, strict=False):
            message = choice.get("message", {}) if isinstance(choice, dict) else {}
            reasoning = message.get("reasoning_content", "") if isinstance(message, dict) else ""
            if reasoning:
                generation.message.additional_kwargs["reasoning_content"] = str(reasoning)
        return result

    # 方法作用：在 DeepSeek 工具调用后把 assistant reasoning_content 回传给 API。
    # Args: input_ - LangChain 消息输入；stop - 停止词；kwargs - 其他调用参数。
    # Returns: 已补齐 reasoning_content 的 OpenAI 请求负载。
    def _get_request_payload(
        self,
        input_: Any,
        *,
        stop: list[str] | None = None,
        **kwargs: Any,
    ) -> dict:
        payload = super()._get_request_payload(input_, stop=stop, **kwargs)
        source_messages = self._convert_input(input_).to_messages()
        outbound_messages = payload.get("messages", [])
        for source, outbound in zip(source_messages, outbound_messages, strict=False):
            additional = getattr(source, "additional_kwargs", {}) or {}
            reasoning = additional.get("reasoning_content", "")
            if reasoning and isinstance(outbound, dict) and outbound.get("role") == "assistant":
                outbound["reasoning_content"] = str(reasoning)
        return payload

    # 方法作用：把流式原始 delta 的 reasoning_content 注入 LangChain 消息块。
    # Args: chunk - 原始响应块；default_chunk_class - 默认消息块类型；base_generation_info - 基础生成元数据。
    # Returns: 保留推理字段的生成块；上游无有效块时返回 None。
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
