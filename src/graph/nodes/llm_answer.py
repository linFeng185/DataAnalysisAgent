"""llm_direct_answer Node — 跳过 SQL 流水线，知识库 + LLM 直接回答。

触发条件: classify_intent 返回 metadata 或 chat 意图。
不查数据库，纯 LLM 调用。LLM 不可用时用规则回退。
"""

from __future__ import annotations

import time

from src.graph.state import AnalysisState
from src.logging_config import get_logger

logger = get_logger(__name__)


async def llm_direct_answer_node(state: AnalysisState) -> dict:
    """LLM 直接回答节点。

    用于以下场景:
    - metadata: "数据库有哪些表" / "DATE_FORMAT 怎么用"
    - chat: "你好" / "你能做什么"

    从 state 中读取 long_term_memories_text 作为知识库上下文。

    Args:
        state: AnalysisState

    Returns: {"final_response": dict, "analysis_result": dict, "chart_config": dict}
    """
    _start = time.monotonic()
    intent = state.get("intent", "chat")
    query = state.get("user_query", "")
    knowledge_text = state.get("long_term_memories_text", "") or ""
    logger.info("节点开始", node="llm_direct_answer", intent=intent, query=query[:60])

    # 组装上下文：知识库参考 + 对话历史
    parts = []
    if knowledge_text:
        parts.append(f"## 知识库参考\n{knowledge_text[:2000]}")
    history = state.get("conversation_history", []) or []
    if history:
        ctx = "\n".join(
            f"Q: {t.get('user_query','')}\nA: {t.get('analysis_summary','')[:200]}"
            for t in history[-3:] if t.get("user_query"))
        if ctx:
            parts.append(f"## 历史对话\n{ctx}")
    context = "\n\n".join(parts) if parts else ""

    # 按意图构建 Prompt
    if intent == "metadata":
        prompt = (
            f"用户询问数据库结构信息。{context}\n\n"
            f"用户问题: {query}\n\n"
            "请根据知识库参考信息回答。如果知识库中有相关表结构或字段信息，列出具体内容。如果没有找到，告知用户。"
        )
    else:
        prompt = (
            f"用户进行闲聊或咨询。{context}\n\n"
            f"用户问题: {query}\n\n请简洁友好地回答。如果用户询问系统功能，告知能做什么。"
        )

    # LLM 调用
    try:
        from src.llm.client import is_llm_available, get_llm
        if is_llm_available():
            llm = get_llm(temperature=0, reasoning=False)
            from langchain_core.messages import SystemMessage, HumanMessage
            resp = await llm.ainvoke([
                SystemMessage(content="你是数据分析助手。简洁用中文回答，不编造。"),
                HumanMessage(content=prompt),
            ])
            answer = resp.content.strip() if resp.content else ""
        else:
            answer = _fallback_answer(intent, query, knowledge_text)
    except Exception as e:
        logger.warning("LLM 直接回答失败，使用回退", error=str(e))
        answer = _fallback_answer(intent, query, knowledge_text)

    elapsed = round((time.monotonic() - _start) * 1000)
    logger.info("节点完成", node="llm_direct_answer", elapsed_ms=elapsed, answer_len=len(answer))

    # 输出结构与 build_response 兼容
    return {
        "final_response": {
            "success": True,
            "source": "llm_direct",  # 前端据此区分展示
            "user_query": query,
            "sql": "",
            "data": [],
            "analysis": {"summary": answer, "insights": [],
                         "recommended_chart_type": "table"},
            "chart": {"type": "table", "option": {}},
            "activated_skills": state.get("activated_skills", []),
            "activated_knowledge": knowledge_text[:200] if knowledge_text else "",
        },
        "analysis_result": {"summary": answer, "insights": []},
        "chart_config": {"type": "table", "option": {}},
    }


def _fallback_answer(intent: str, query: str, knowledge: str) -> str:
    """LLM 不可用时的规则回退回答。

    Args:
        intent: 意图类型
        query: 用户问题
        knowledge: 知识库上下文

    Returns: 回退文本
    """
    if intent == "metadata":
        if knowledge:
            return f"根据知识库记录:\n{knowledge[:500]}"
        return "未找到相关数据库结构信息。请上传数据库文档到知识库，或使用 /schema 查看已注册的表结构。"
    return "你好！我是数据分析助手，可以帮你用自然语言查询和分析数据库。请选择一个数据源并输入你的问题。"
