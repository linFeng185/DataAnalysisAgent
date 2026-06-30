"""4.10 build_response Node — 组装最终响应。"""

from __future__ import annotations

import time

from src.graph.state import AnalysisState
from src.logging_config import get_logger

logger = get_logger(__name__)


async def build_response_node(state: AnalysisState) -> dict:
    _start = time.monotonic()
    logger.info("节点开始", node="build_response")
    if state.get("validation_errors"):
        logger.info("节点完成", node="build_response", elapsed_ms=round((time.monotonic() - _start) * 1000))
        return {"final_response": {
            "success": False,
            "error_code": "VALIDATION_FAILED",
            "error_message": str(state["validation_errors"]),
            "user_query": state.get("user_query", ""),
        }}
    result = {
        "success": True,
        "session_id": "",
        "user_query": state.get("user_query", ""),
        "sql": state.get("generated_sql", ""),
        "data": state.get("query_result_sample", []),
        "analysis": state.get("analysis_result", {}),
        "chart": state.get("chart_config", {}),
    }
    reasoning = state.get("sql_reasoning_content", "")
    if reasoning:
        result["sql_reasoning_content"] = reasoning

    # 附加激活的技能和知识库信息
    result["activated_skills"] = state.get("activated_skills", []) or []
    result["activated_knowledge"] = state.get("long_term_memories_text", "") or ""

    # 7.2.2 记录本轮对话 — 优先从 messages 复原（保证持久化），回退到 conversation_history
    history = list(state.get("conversation_history", []) or [])
    # 如果 conversation_history 为空，从 messages 字段复原
    if not history:
        msgs = state.get("messages", []) or []
        for msg in msgs:
            if hasattr(msg, 'content') and msg.content:
                role = 'user' if msg.__class__.__name__ == 'HumanMessage' else 'assistant'
                history.append({
                    "turn_id": len(history) + 1,
                    "user_query": msg.content if role == 'user' else '',
                    "generated_sql": '',
                    "execution_success": True,
                    "chart_type": '',
                    "analysis_summary": msg.content if role == 'assistant' else '',
                })
        if history:
            logger.info("对话历史从 messages 复原", turns=len(history))
    analysis = state.get("analysis_result", {}) or {}
    query = state.get("user_query", "")
    sql = state.get("generated_sql", "")
    analysis_summary = analysis.get("summary", "")
    already_recorded = any(
        (isinstance(t, dict) and t.get("user_query") == query) or
        (hasattr(t, "user_query") and t.user_query == query)
        for t in history
    )
    turn_entry = {
        "turn_id": len(history) + 1,
        "user_query": query,
        "generated_sql": sql,
        "execution_success": not state.get("execution_error"),
        "analysis_summary": analysis_summary,
        "chart_type": analysis.get("recommended_chart_type") or state.get("chart_config", {}).get("type", ""),
    }
    new_messages = []
    if not already_recorded:
        history.append(turn_entry)
        logger.info("对话历史已追加", turns=len(history), query=query[:60])
        # 同时写入 messages 字段（使用 add_messages reducer，checkpointer 保证持久化）
        from langchain_core.messages import HumanMessage, AIMessage
        new_messages = [
            HumanMessage(content=query),
            AIMessage(content=f"SQL: {sql}\n结论: {analysis_summary}" if analysis_summary else f"SQL: {sql}"),
        ]

    # 写入查询历史（前端历史页面使用）
    if not already_recorded and query.strip():
        try:
            from src.memory.history_store import get_history_store
            row_count = len(state.get("query_result_sample", []) or [])
            get_history_store().add(
                user_query=query, datasource=state.get("datasource", ""),
                session_id=state.get("session_id", "") or "",
                generated_sql=sql, success=not state.get("execution_error"),
                row_count=row_count,
            )
        except Exception:
            pass

    logger.info("节点完成", node="build_response", elapsed_ms=round((time.monotonic() - _start) * 1000))
    return {"final_response": result, "conversation_history": history, "messages": new_messages}
