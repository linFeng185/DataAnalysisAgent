"""模型适配器基类 — 统一各模型参数差异与响应解析。"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field


@dataclass
class SupportedFeatures:
    """模型能力声明，用于客户端自适应。"""
    streaming: bool = True
    reasoning: bool = False
    reasoning_content_in_response: bool = False
    function_calling: bool = True
    json_mode: bool = True
    max_tokens_limit: int = 16384
    context_window: int = 128000
    vision: bool = False
    default_temperature: float = 0.0


@dataclass
class ParsedResponse:
    content: str = ""
    reasoning_content: str = ""
    tool_calls: list[dict] = field(default_factory=list)


@dataclass
class StreamChunk:
    reasoning_content: str = ""
    content: str = ""


class ModelAdapter(ABC):
    """模型适配器基类。

    每个子类负责：
    - get_chat_openai_kwargs(): 返回 ChatOpenAI 构造时的额外参数
    - parse_response(): 从完整响应中提取 content 和 reasoning_content
    - parse_stream_chunk(): 从流式 chunk 中提取 content 和 reasoning_content
    """

    provider: str = "openai"
    default_base_url: str = ""
    supported_features: SupportedFeatures = SupportedFeatures()

    def get_chat_openai_kwargs(self) -> dict:
        """返回 ChatOpenAI 构造函数的额外参数。

        子类重写此方法来注入模型特有参数，如 reasoning_effort、model_kwargs 等。
        streaming 参数由 client.py 统一设置，不在此返回。
        """
        return {}

    def get_default_base_url(self) -> str:
        return self.default_base_url

    # ---- 响应解析 ----

    def parse_response(self, raw) -> ParsedResponse:
        """从 LLM 完整响应中提取内容和推理链。"""
        content = ""
        if hasattr(raw, "content") and raw.content:
            content = raw.content if isinstance(raw.content, str) else str(raw.content)

        reasoning = self._extract_reasoning(raw)

        tool_calls = []
        if hasattr(raw, "tool_calls") and raw.tool_calls:
            tool_calls = [
                tc if isinstance(tc, dict) else {"name": getattr(tc, "name", ""), "args": getattr(tc, "args", {})}
                for tc in raw.tool_calls
            ]

        return ParsedResponse(content=content, reasoning_content=reasoning, tool_calls=tool_calls)

    def parse_stream_chunk(self, chunk) -> StreamChunk:
        """从流式 chunk 中提取内容和推理链。

        支持 AIMessageChunk 和 ChatGenerationChunk 两种输入。
        """
        reasoning = self._extract_reasoning(chunk)
        content = ""
        if hasattr(chunk, "content") and chunk.content:
            content = chunk.content if isinstance(chunk.content, str) else str(chunk.content)
        elif hasattr(chunk, "text") and chunk.text:
            # ChatGenerationChunk 通过 .text 属性暴露内容
            content = chunk.text if isinstance(chunk.text, str) else str(chunk.text)
        return StreamChunk(reasoning_content=reasoning, content=content)

    def _extract_reasoning(self, obj) -> str:
        """从 LangChain message/chunk 中提取 reasoning_content，兼容多版本。

        支持两种输入：
        - AIMessageChunk（直接含 additional_kwargs / reasoning_content 属性）
        - ChatGenerationChunk（reasoning_content 在 .message 子对象上）
        """
        target = obj
        if hasattr(obj, "message") and not hasattr(obj, "additional_kwargs"):
            target = obj.message

        if hasattr(target, "additional_kwargs") and isinstance(target.additional_kwargs, dict):
            r = target.additional_kwargs.get("reasoning_content", "")
            if r:
                return r if isinstance(r, str) else str(r)

        if hasattr(target, "response_metadata") and isinstance(target.response_metadata, dict):
            r = target.response_metadata.get("reasoning_content", "")
            if r:
                return r if isinstance(r, str) else str(r)

        if hasattr(target, "reasoning_content") and target.reasoning_content:
            rc = target.reasoning_content
            return rc if isinstance(rc, str) else str(rc)

        return ""
