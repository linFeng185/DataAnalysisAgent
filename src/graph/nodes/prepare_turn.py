"""prepare_turn Node：在保留会话历史的前提下清理上一轮瞬态状态。"""

from __future__ import annotations

from copy import deepcopy

from src.graph.state import AnalysisState
from src.logging_config import get_logger


logger = get_logger(__name__)


# 方法作用：把已完成轮次的 SQL、结果和分析固化为可校验的跨轮快照。
# Args: state - 已完成上一轮或正在构建响应的 LangGraph 状态。
# Returns: 只包含跨轮分析所需字段的普通字典。
def build_turn_snapshot(state: AnalysisState) -> dict:
    """构建不包含 Schema 对象和错误状态的轻量结果快照。"""
    logger.debug(
        "构建轮次结果快照入口",
        datasource=state.get("datasource", ""),
        rows=len(state.get("query_result_sample", []) or []),
        has_sql=bool(state.get("generated_sql")),
    )
    history = state.get("conversation_history", []) or []
    last_turn = history[-1] if history and isinstance(history[-1], dict) else {}
    source_query = last_turn.get("user_query", "") or state.get("user_query", "")
    source_datasource = last_turn.get("datasource", "") or state.get("datasource", "")
    multi_source_results = state.get("multi_source_results", []) or []
    success = not bool(
        state.get("execution_error")
        or state.get("validation_errors")
        or state.get("explain_errors")
    )
    result_available = success and bool(
        state.get("generated_sql")
        or state.get("query_result_sample")
        or any(item.get("success") for item in multi_source_results if isinstance(item, dict))
    )
    snapshot = {
        "source_query": source_query,
        "source_intent": state.get("intent", ""),
        "datasource": source_datasource,
        "selected_datasources": deepcopy(state.get("selected_datasources", []) or []),
        "generated_sql": state.get("generated_sql", "") or "",
        "query_result_sample": deepcopy(state.get("query_result_sample", []) or []),
        "query_result_full_count": int(state.get("query_result_full_count", 0) or 0),
        "query_result_truncated": bool(state.get("query_result_truncated", False)),
        "query_result_statistics": deepcopy(state.get("query_result_statistics", {}) or {}),
        "analysis_result": deepcopy(state.get("analysis_result", {}) or {}),
        "chart_config": deepcopy(state.get("chart_config", {}) or {}),
        "multi_source_results": deepcopy(multi_source_results),
        "result_available": result_available,
    }
    logger.info(
        "构建轮次结果快照完成",
        datasource=source_datasource,
        rows=len(snapshot["query_result_sample"]),
        result_available=result_available,
    )
    return snapshot


# 方法作用：清空 checkpoint 恢复的上一轮执行产物，防止新问题读取旧结果或旧错误。
# Args: state - 合并当前 API 输入和 checkpoint 后的 LangGraph 状态。
# Returns: 仅包含需要重置的轮次级字段，不覆盖 conversation_history 和 messages。
async def prepare_turn_node(state: AnalysisState) -> dict:
    """初始化当前轮次的瞬态状态，同时保留跨轮对话记忆。"""
    logger.debug(
        "轮次状态初始化入口",
        query=(state.get("user_query", "") or "")[:80],
        datasource=state.get("datasource", ""),
        previous_sql=bool(state.get("generated_sql")),
        previous_error=bool(state.get("execution_error")),
        previous_rows=len(state.get("query_result_sample", []) or []),
    )
    logger.info(
        "轮次清理边界输入",
        has_previous_sql=bool(state.get("generated_sql")),
        previous_rows=len(state.get("query_result_sample", []) or []),
        previous_full_count=state.get("query_result_full_count", 0),
        has_previous_analysis=bool(state.get("analysis_result")),
        history_turns=len(state.get("conversation_history", []) or []),
    )
    # 兼容部署前已经存在、尚未写入快照的 checkpoint。
    previous_snapshot = deepcopy(state.get("previous_turn_snapshot", {}) or {})
    if not previous_snapshot and (
        state.get("generated_sql")
        or state.get("query_result_sample")
        or state.get("multi_source_results")
    ):
        previous_snapshot = build_turn_snapshot(state)
        logger.info("旧 checkpoint 结果已迁移为轮次快照")

    result = {
        "intent": "",
        "activated_skills": [],
        "skill_prompt_override": "",
        "skill_tools": [],
        "multi_source_results": [],
        "previous_turn_snapshot": previous_snapshot,
        "previous_result_restored": False,
        "dialect": "",
        "resolved_schema": None,
        "relevant_tables": [],
        "few_shot_examples": [],
        "business_rules_text": "",
        "enum_dictionary": {},
        "long_term_memories_text": "",
        "needs_decompose": False,
        "decompose_steps": [],
        "generated_sql": "",
        "needs_time_range": False,
        "time_range_explanation": "",
        "sql_reasoning_content": "",
        "retry_count": 0,
        "sql_valid": False,
        "validation_errors": [],
        "validation_warnings": [],
        "transpiled_sql": "",
        "explain_errors": [],
        "execution_error": "",
        "execution_error_type": "",
        "execution_retry_count": 0,
        "query_result_sample": [],
        "query_result_full_count": 0,
        "query_result_truncated": False,
        "query_result_statistics": {},
        "analysis_result": {},
        "chart_config": {},
        "mcp_agent_output": "",
        "final_response": {},
    }
    logger.info(
        "轮次状态初始化完成",
        cleared_fields=len(result),
        history_turns=len(state.get("conversation_history", []) or []),
        output_rows=len(result["query_result_sample"]),
        output_has_sql=bool(result["generated_sql"]),
    )
    return result
