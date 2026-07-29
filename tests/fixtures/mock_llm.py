"""不访问网络的顺序响应 Fake LLM 工厂。"""

from __future__ import annotations

from collections.abc import Sequence

from langchain_core.language_models.fake_chat_models import FakeListChatModel
from langchain_core.messages import AIMessage


# 方法作用：把字符串或 AIMessage 序列转换为支持流式调用的 FakeListChatModel。
# Args: responses - 按调用顺序返回的响应。
# Returns: 不访问真实模型的 LangChain ChatModel。
def make_fake_llm(
    responses: Sequence[str | AIMessage],
) -> FakeListChatModel:
    normalized = [
        response.content if isinstance(response, AIMessage) else str(response)
        for response in responses
    ]
    if not normalized:
        raise ValueError("Fake LLM 至少需要一个响应")
    return FakeListChatModel(responses=normalized)
