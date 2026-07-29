"""4.10 build_response Node — 组装最终响应。"""

from __future__ import annotations

import time

from src.graph.state import AnalysisState
from src.graph.outcome import (
    is_successful_response,
    public_error_message,
    sanitize_public_output,
    status_from_response,
)
from src.logging_config import get_logger

logger = get_logger(__name__)


# 方法作用：限制公开知识文本长度并标记截断。
# Args: value - 待输出值；max_chars - 最大字符数。
# Returns: 未超限原文本或带省略号的截断文本。
def _bounded_text(value: object, max_chars: int = 500) -> str:
    """限制可返回给客户端和历史存储的知识文本，避免原文泄漏和响应膨胀。"""
    text = str(value or "")
    if len(text) <= max_chars:
        return text
    return text[:max_chars] + "..."


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
    multi_source_results = state.get("multi_source_results", []) or []
    successful_sources = [item for item in multi_source_results if item.get("success")]

    existing_response = state.get("final_response", {}) or {}
    is_direct_response = (
        existing_response.get("source") in {"llm_direct", "mcp_agent"}
        and existing_response.get("user_query", "") == state.get("user_query", "")
    )

    if is_direct_response:
        final_result = dict(existing_response)
        if final_result.get("success") is False:
            final_result.setdefault(
                "error_code",
                "MCP_AGENT_FAILED" if final_result.get("source") == "mcp_agent" else "TASK_FAILED",
            )
            final_result["error_message"] = public_error_message(
                str(final_result["error_code"]),
                fallback="直接回答失败",
            )
            final_result["status"] = "failed"
        else:
            final_result.setdefault("status", "success")
        logger.info("保留直接响应", source=final_result.get("source", ""))
    elif is_time_prompt:
        explanation = state.get("time_range_explanation") or "请指定查询的时间范围（最近一周/一月/一年/两年/三年/五年）"
        logger.info("提示用户指定时间范围", explanation=explanation[:100])
        final_result = {
            "success": True, "status": "needs_input", "source": "prompt", "needs_time_range": True,
            "user_query": state.get("user_query", ""), "sql": "", "data": [],
            "sql_statements": [],
            "analysis": {"summary": explanation, "insights": [], "recommended_chart_type": "table"},
            "chart": {"type": "table", "option": {}},
        }
    elif multi_source_results and not successful_sources:
        final_result = {
            "success": False,
            "status": "failed",
            "source": "multi_source_query",
            "error_code": "MULTI_SOURCE_FAILED",
            "error_message": public_error_message("MULTI_SOURCE_FAILED"),
            "user_query": state.get("user_query", ""),
            "sql": "",
            "sql_statements": [],
            "data": [],
            "analysis": state.get("analysis_result", {}) or {},
            "chart": {"type": "table", "option": {}},
        }
    elif state.get("validation_errors"):
        final_result = {
            "success": False, "status": "failed", "source": "sql_query", "error_code": "VALIDATION_FAILED",
            "error_message": public_error_message("VALIDATION_FAILED"),
            "user_query": state.get("user_query", ""), "sql": "",
            "sql_statements": [], "data": [], "analysis": {}, "chart": {},
        }
    elif state.get("explain_errors"):
        final_result = {
            "success": False, "status": "failed", "source": "sql_query", "error_code": "EXPLAIN_FAILED",
            "error_message": public_error_message("EXPLAIN_FAILED"),
            "user_query": state.get("user_query", ""), "sql": "",
            "sql_statements": [], "data": [], "analysis": {}, "chart": {},
        }
    else:
        exec_error = state.get("execution_error", "")
        success = not bool(exec_error)
        final_result = {
            "success": success, "status": "success" if success else "failed",
            "source": "sql_query", "session_id": "",
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
            final_result["error_message"] = public_error_message("SQL_EXECUTION_FAILED")

    if multi_source_results and successful_sources:
        final_result["source"] = "multi_source_query"
        if len(successful_sources) < len(multi_source_results):
            final_result["status"] = "partial"
            final_result["error_code"] = "MULTI_SOURCE_PARTIAL"
            final_result["error_message"] = public_error_message("MULTI_SOURCE_PARTIAL")

    final_result.setdefault("sql_statements", sql_statements)
    if not final_result.get("sql") and sql_statements:
        final_result["sql"] = sql

    # 附加技能与知识库
    final_result["activated_skills"] = state.get("activated_skills", []) or []
    final_result["activated_knowledge"] = _bounded_text(
        state.get("long_term_memories_text", "") or "",
    )
    final_result = sanitize_public_output(final_result)

    # 追加对话历史（所有路径共用，含时间提示路径）
    history = []
    for item in state.get("conversation_history", []) or []:
        if isinstance(item, dict):
            compact_item = dict(item)
            compact_item.pop("final_result", None)
            history.append(compact_item)
        else:
            history.append(item)
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
            "execution_success": is_successful_response(final_result),
            "analysis_summary": analysis_summary,
            "chart_type": analysis.get("recommended_chart_type") or state.get("chart_config", {}).get("type", ""),
        }
        history.append(turn_entry)
        logger.info("对话历史已追加", turns=len(history), query=query[:60])
        from langchain_core.messages import HumanMessage, AIMessage
        answer_prefix = "查询结论" if final_result.get("source") == "sql_query" else "回答"
        new_messages = [
            HumanMessage(content=query),
            AIMessage(content=f"{answer_prefix}: {analysis_summary}" if analysis_summary else answer_prefix),
        ]

    # 写入查询历史
    if query.strip():
        try:
            from src.memory.history_store import get_history_store
            row_count = len(state.get("query_result_sample", []) or [])
            await get_history_store().add(
                user_query=query, datasource=state.get("datasource", ""),
                session_id=state.get("session_id", "") or "",
                generated_sql=gen_sql, success=is_successful_response(final_result),
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

    # 仅学习当前身份下成功执行的 SQL，避免把失败语句和安全短路结果污染模板库。
    tenant_id = int(state.get("tenant_id", 0) or 0)
    user_id = int(state.get("user_id", 0) or 0)
    if (
        query.strip()
        and gen_sql.strip()
        and tenant_id > 0
        and user_id > 0
        and is_successful_response(final_result)
        and final_result.get("source") == "sql_query"
    ):
        try:
            from src.memory.long_term_store import get_long_term_memory_store
            from src.security.tenant_policy import RequestIdentity

            identity = RequestIdentity(
                tenant_id=tenant_id,
                user_id=user_id,
                role=str(state.get("user_role", "analyst") or "analyst"),
            )
            memory_store = await get_long_term_memory_store()
            await memory_store.save_sql_template(
                query,
                gen_sql,
                str(state.get("dialect", "") or "unknown"),
                identity=identity,
                visibility="private",
            )
            logger.info(
                "成功 SQL 长期记忆写入完成",
                tenant_id=tenant_id,
                user_id=user_id,
                dialect=state.get("dialect", "") or "unknown",
            )
        except Exception as exc:
            logger.error(
                "成功 SQL 长期记忆写入失败",
                tenant_id=tenant_id,
                user_id=user_id,
                error=str(exc),
                exc_info=True,
            )

    logger.info("节点完成", node="build_response", elapsed_ms=round((time.monotonic() - _start) * 1000))
    from src.graph.nodes.prepare_turn import build_turn_snapshot

    snapshot_state = dict(state)
    snapshot_state["conversation_history"] = history
    previous_turn_snapshot = build_turn_snapshot(snapshot_state)
    logger.info(
        "统一结果状态完成",
        status=status_from_response(final_result),
        success=is_successful_response(final_result),
        source=final_result.get("source", ""),
    )
    return {
        "final_response": final_result,
        "conversation_history": history,
        "messages": new_messages,
        "previous_turn_snapshot": previous_turn_snapshot,
    }
