"""LLM Provider 抽象接口 — 统一 LLM 调用入口。

所有 LLM 调用方通过此接口访问模型，不直接依赖 LangChain 或特定实现。
新增模型只需实现 LLMProvider 子类并注册到 ModelRegistry。
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass

from src.llm.adapters.base import SupportedFeatures


@dataclass
class LLMResponse:
    """非流式调用的完整响应。

    usage: {"prompt_tokens": N, "completion_tokens": M}，仅部分 Provider 提供
    """
    content: str
    reasoning: str = ""
    finish_reason: str = "stop"
    usage: dict | None = None


@dataclass
class LLMStreamChunk:
    """流式调用的单个 token 块。

    reasoning: 思考链内容（DeepSeek/Claude reasoning 模式）
    """
    content: str = ""
    reasoning: str = ""


class LLMProvider(ABC):
    """LLM Provider 抽象接口。

    每个 Provider 负责一个模型家族（OpenAI/Anthropic/vLLM），
    管理连接参数、模型能力和响应解析。

    子类: OpenAIProvider, AnthropicProvider, VLLMProvider
    """

    @property
    @abstractmethod
    def capabilities(self) -> SupportedFeatures:
        """返回模型能力声明（reasoning/vision/context_window 等）。

        Returns: SupportedFeatures 实例
        """
        ...

    @abstractmethod
    async def agenerate(
        self, messages: list[dict],
        temperature: float | None = None,
        max_tokens: int | None = None,
        stream: bool = False,
    ) -> LLMResponse:
        """非流式调用 LLM，返回完整响应。

        Args:
            messages: [{"role": "system", "content": "..."}, {"role": "user", "content": "..."}]
            temperature: 温度参数，None 则使用全局配置
            max_tokens: 最大输出 Token 数
            stream: 是否流式（agenerate 设为 False）

        Returns: LLMResponse 包含完整文本和思考链
        """
        ...

    @abstractmethod
    async def astream(
        self, messages: list[dict],
        temperature: float | None = None,
        max_tokens: int | None = None,
    ):
        """流式调用 LLM，逐个 yield LLMStreamChunk。

        Args:
            messages: 同 agenerate
            temperature: 同 agenerate
            max_tokens: 同 agenerate

        Yields: LLMStreamChunk（content + reasoning）
        """
        ...

    @abstractmethod
    def get_chat_model(
        self,
        temperature: float | None = None,
        max_tokens: int | None = None,
        stream: bool = True,
        reasoning: bool = True,
    ):
        """创建供 LangGraph/LangChain 组合使用的 ChatModel。

        Args:
            temperature: 温度参数。
            max_tokens: 最大输出 Token 数。
            stream: 是否启用流式。
            reasoning: 是否启用模型推理模式。

        Returns:
            LangChain BaseChatModel 实例。
        """
        ...

    @staticmethod
    def _to_lc_msg(m: dict):
        """将 dict 格式消息转为 LangChain Message 对象。

        供子类在内部使用——外部调用方只传 dict，内部适配 LangChain。

        Args:
            m: {"role": "user", "content": "hello"}

        Returns: SystemMessage / HumanMessage / AIMessage
        """
        from langchain_core.messages import SystemMessage, HumanMessage, AIMessage
        r, c = m.get("role", "user"), m.get("content", "")
        if r == "system": return SystemMessage(content=c)
        if r == "assistant": return AIMessage(content=c)
        return HumanMessage(content=c)
