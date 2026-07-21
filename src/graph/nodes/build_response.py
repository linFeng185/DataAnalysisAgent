"""4.10 build_response Node — 组装最终响应。"""

from __future__ import annotations

import time
from copy import deepcopy

from src.graph.state import AnalysisState
from src.logging_config import get_logger

logger = get_logger(__name__)


# 收集单源或多源执行完成后的最终 SQL 列表。
# Args: state - LangGraph 当前分析状态。
# Returns: 包含 datasource、dialect、sql 的 SQL 展示条目。
def _build_sql_statements(state: AnalysisState) -> list[dict]:
    """多源优先读取 worker 最终 SQL，单源读取 execute_sql 写回 SQL。"""
    logger.debug(
        "最终 SQL 列表构建入口",
        multi_source_count=len(state.get("multi_source_results", []) or []),
    )
    try:
        statements = [
            {
                "datasource": str(result.get("datasource", "")),
                "dialect": str(result.get("dialect", "")),
                "sql": str(result.get("sql", "") or "").strip(),
            }
            for result in (state.get("multi_source_results", []) or [])
            if result.get("success") and str(result.get("sql", "") or "").strip()
        ]
        if not statements:
            sql = str(state.get("generated_sql", "") or "").strip()
            if sql:
                statements = [{
                    "datasource": str(state.get("datasource", "")),
                    "dialect": str(state.get("dialect", "")),
                    "sql": sql,
                }]
        logger.info("最终 SQL 列表构建完成", statement_count=len(statements))
        return statements
    except Exception as exc:
        logger.error("最终 SQL 列表构建失败", error=str(exc), exc_info=True)
        return []


# 将 SQL 列表转换为兼容旧客户端的顶层 SQL 文本。
# Args: statements - 最终 SQL 展示条目。
# Returns: 单源原始 SQL或带数据源注释的多源 SQL 合集。
def _format_sql_statements(statements: list[dict]) -> str:
    """保留单源展示格式，多源使用注释分隔不同数据库语句。"""
    logger.debug("最终 SQL 文本格式化入口", statement_count=len(statements))
    try:
        if not statements:
            logger.info("最终 SQL 文本格式化完成", chars=0)
            return ""
        if len(statements) == 1:
            result = str(statements[0].get("sql", "") or "")
        else:
            result = "\n\n".join(
                f"-- datasource: {item.get('datasource', '')} ({item.get('dialect', '')})\n"
                f"{item.get('sql', '')}"
                for item in statements
            )
        logger.info("最终 SQL 文本格式化完成", chars=len(result))
        return result
    except Exception as exc:
        logger.error("最终 SQL 文本格式化失败", error=str(exc), exc_info=True)
        return ""


async def build_response_node(state: AnalysisState) -> dict:
    """组装统一最终响应并保存本轮对话历史。

    Args:
        state: LangGraph 当前分析状态。

    Returns:
        包含最终响应、对话历史和新增消息的状态增量。
    """
    _start = time.monotonic()
    logger.debug(
        "构建响应入口",
        node="build_response",
        truncated=bool(state.get("query_result_truncated", False)),
    )

    sql_statements = _build_sql_statements(state)
    sql = _format_sql_statements(sql_statements)
    logger.info(
        "构建响应状态到达",
        has_generated_sql=bool(sql),
        needs_time_range=bool(state.get("needs_time_range", False)),
        multi_source_count=len(state.get("multi_source_results", []) or []),
        data_rows=len(state.get("query_result_sample", []) or []),
        has_analysis=bool(state.get("analysis_result")),
        has_execution_error=bool(state.get("execution_error")),
        validation_error_count=len(state.get("validation_errors", []) or []),
    )
    is_time_prompt = (
        bool(state.get("needs_time_range", False))
        and not state.get("multi_source_results")
        and sql == ""
        and not state.get("execution_error")
        and not state.get("validation_errors")
    )

    existing_response = state.get("final_response", {}) or {}
    is_direct_response = (
        existing_response.get("source") in {"llm_direct", "mcp_agent"}
        and existing_response.get("user_query", "") == state.get("user_query", "")
    )

    if is_direct_response:
        final_result = dict(existing_response)
        logger.info("保留直接响应", source=final_result.get("source", ""))
    elif is_time_prompt:
        explanation = state.get("time_range_explanation") or "请指定查询的时间范围（最近一周/一月/一年/两年/三年/五年）"
        logger.info("提示用户指定时间范围", explanation=explanation[:100])
        final_result = {
            "success": True, "source": "prompt", "needs_time_range": True,
            "user_query": state.get("user_query", ""), "sql": "", "data": [],
            "sql_statements": [],
            "analysis": {"summary": explanation, "insights": [], "recommended_chart_type": "table"},
            "chart": {"type": "table", "option": {}},
        }
    elif state.get("validation_errors"):
        final_result = {
            "success": False, "source": "sql_query", "error_code": "VALIDATION_FAILED",
            "error_message": str(state["validation_errors"]),
            "user_query": state.get("user_query", ""), "sql": "",
            "sql_statements": [], "data": [], "analysis": {}, "chart": {},
        }
    else:
        exec_error = state.get("execution_error", "")
        final_result = {
            "success": not exec_error, "source": "sql_query", "session_id": "",
            "user_query": state.get("user_query", ""),
            "sql": sql,
            "sql_statements": sql_statements,
            "data": state.get("query_result_sample", []),
            "row_count": state.get("query_result_full_count", 0),
            "truncated": bool(state.get("query_result_truncated", False)),
            "analysis": state.get("analysis_result", {}),
            "chart": state.get("chart_config", {}),
        }
        if exec_error:
            final_result["error_code"] = "SQL_EXECUTION_FAILED"
            final_result["error_message"] = exec_error
        reasoning = state.get("sql_reasoning_content", "")
        if reasoning:
            final_result["sql_reasoning_content"] = reasoning

    final_result.setdefault("sql_statements", sql_statements)
    if not final_result.get("sql") and sql_statements:
        final_result["sql"] = sql

    # 附加技能与知识库
    final_result["activated_skills"] = state.get("activated_skills", []) or []
    final_result["activated_knowledge"] = state.get("long_term_memories_text", "") or ""

    # 追加对话历史（所有路径共用，含时间提示路径）
    history = list(state.get("conversation_history", []) or [])
    if not history:
        msgs = state.get("messages", []) or []
        for msg in msgs:
            if hasattr(msg, 'content') and msg.content:
                role = 'user' if msg.__class__.__name__ == 'HumanMessage' else 'assistant'
                history.append({
                    "turn_id": len(history) + 1,
                    "user_query": msg.content if role == 'user' else '',
                    "generated_sql": '', "execution_success": True,
                    "chart_type": '', "analysis_summary": msg.content if role == 'assistant' else '',
                })
        if history:
            logger.info("对话历史从 messages 复原", turns=len(history))
    analysis = final_result.get("analysis", {}) or state.get("analysis_result", {}) or {}
    query = state.get("user_query", "")
    gen_sql = str(final_result.get("sql", "") or "")
    analysis_summary = analysis.get("summary", "")
    new_messages = []
    if query.strip():
        turn_entry = {
            "turn_id": len(history) + 1, "user_query": query,
            "generated_sql": gen_sql,
            "execution_success": not state.get("execution_error"),
            "analysis_summary": analysis_summary,
            "chart_type": analysis.get("recommended_chart_type") or state.get("chart_config", {}).get("type", ""),
            "final_result": deepcopy(final_result),
        }
        history.append(turn_entry)
        logger.info("对话历史已追加", turns=len(history), query=query[:60])
        from langchain_core.messages import HumanMessage, AIMessage
        new_messages = [
            HumanMessage(content=query),
            AIMessage(content=f"SQL: {gen_sql}\n结论: {analysis_summary}" if analysis_summary else f"SQL: {gen_sql}"),
        ]

    # 写入查询历史
    if query.strip():
        try:
            from src.memory.history_store import get_history_store
            row_count = len(state.get("query_result_sample", []) or [])
            get_history_store().add(
                user_query=query, datasource=state.get("datasource", ""),
                session_id=state.get("session_id", "") or "",
                generated_sql=gen_sql, success=not state.get("execution_error"),
                row_count=row_count,
                final_result=final_result,
            )
        except Exception as exc:
            logger.error(
                "查询历史写入调度失败",
                session_id=state.get("session_id", "") or "",
                error=str(exc),
                exc_info=True,
            )

    logger.info("节点完成", node="build_response", elapsed_ms=round((time.monotonic() - _start) * 1000))
    from src.graph.nodes.prepare_turn import build_turn_snapshot

    snapshot_state = dict(state)
    snapshot_state["conversation_history"] = history
    previous_turn_snapshot = build_turn_snapshot(snapshot_state)
    return {
        "final_response": final_result,
        "conversation_history": history,
        "messages": new_messages,
        "previous_turn_snapshot": previous_turn_snapshot,
    }
