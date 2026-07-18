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
    schema_text = _format_schema_context(state.get("relevant_tables", []) or [])
    logger.info("节点开始", node="llm_direct_answer", intent=intent, query=query[:60])
    logger.info(
        "直接回答上下文到达",
        intent=intent,
        table_count=len(state.get("relevant_tables", []) or []),
        schema_chars=len(schema_text),
        knowledge_chars=len(knowledge_text),
        history_turns=len(state.get("conversation_history", []) or []),
    )

    # 组装上下文：知识库参考 + 对话历史
    parts = []
    if schema_text:
        parts.append(f"## 当前数据源 Schema\n{schema_text}")
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
        from src.llm.client import get_task_llm, is_task_llm_available
        if is_task_llm_available("direct_answer"):
            llm = get_task_llm("direct_answer", temperature=0, reasoning=False)
            from langchain_core.messages import SystemMessage, HumanMessage
            resp = await llm.ainvoke([
                SystemMessage(content="你是数据分析助手。简洁用中文回答，不编造。"),
                HumanMessage(content=prompt),
            ])
            answer = resp.content.strip() if resp.content else ""
        else:
            answer = _fallback_answer(intent, query, knowledge_text, schema_text)
    except Exception as e:
        logger.warning("LLM 直接回答失败，使用回退", error=str(e))
        answer = _fallback_answer(intent, query, knowledge_text, schema_text)

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


def _fallback_answer(intent: str, query: str, knowledge: str, schema_text: str = "") -> str:
    """LLM 不可用时的规则回退回答。

    Args:
        intent: 意图类型
        query: 用户问题
        knowledge: 知识库上下文
        schema_text: 当前数据源的确定性 Schema 摘要

    Returns: 回退文本
    """
    if intent == "metadata":
        if schema_text:
            return f"当前数据源结构:\n{schema_text}"
        if knowledge:
            return f"根据知识库记录:\n{knowledge[:500]}"
        return "未找到相关数据库结构信息。请上传数据库文档到知识库，或使用 /schema 查看已注册的表结构。"
    return "你好！我是数据分析助手，可以帮你用自然语言查询和分析数据库。请选择一个数据源并输入你的问题。"


# 方法作用：把 retrieve_schema 的结构化表信息转换为直接回答可用的确定性摘要。
# Args: tables - 当前数据源相关表列表。
# Returns: 包含表名、说明和字段的紧凑文本，无表时返回空字符串。
def _format_schema_context(tables: list[dict]) -> str:
    """格式化 metadata 回答使用的真实 Schema，不依赖知识库是否命中。"""
    logger.debug("格式化 metadata Schema 入口", table_count=len(tables))
    lines: list[str] = []
    for table in tables[:30]:
        name = str(table.get("name", "") or "").strip()
        if not name:
            continue
        description = str(table.get("description", "") or "").strip()
        columns = table.get("columns", []) or []
        column_text = ", ".join(
            f"{column.get('name', '')} ({column.get('type', '')})"
            for column in columns[:50]
            if column.get("name")
        )
        header = f"- {name}" + (f": {description}" if description else "")
        lines.append(header)
        if column_text:
            lines.append(f"  字段: {column_text}")
    result = "\n".join(lines)
    logger.info("格式化 metadata Schema 完成", table_count=len(tables), chars=len(result))
    return result
