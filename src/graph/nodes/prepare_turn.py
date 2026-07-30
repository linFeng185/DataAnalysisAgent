"""prepare_turn Node：在保留会话历史的前提下清理上一轮瞬态状态。"""

from __future__ import annotations

from copy import deepcopy

from src.graph.context import build_context_groups, read_contexts
from src.graph.state import AnalysisState
from src.logging_config import get_logger

logger = get_logger(__name__)


# 方法作用：把已完成轮次的来源和 SQL 固化为可校验的轻量跨轮索引。
# Args: state - 已完成上一轮或正在构建响应的 LangGraph 状态。
# Returns: 只包含跨轮分析所需字段的普通字典。
def build_turn_snapshot(state: AnalysisState) -> dict:
    """构建不复制结果、分析、图表和 Schema 对象的轻量索引。"""
    contexts = read_contexts(state)
    logger.debug(
        "构建轮次结果快照入口",
        datasource=contexts.routing.datasource,
        rows=len(state.get("query_result_sample", []) or []),
        has_sql=bool(state.get("generated_sql")),
    )
    history = state.get("conversation_history", []) or []
    last_turn = history[-1] if history and isinstance(history[-1], dict) else {}
    source_query = last_turn.get("user_query", "") or contexts.request.user_query
    source_datasource = last_turn.get("datasource", "") or contexts.routing.datasource
    multi_source_results = state.get("multi_source_results", []) or []
    final_response = state.get("final_response", {}) or {}
    if isinstance(final_response, dict) and "success" in final_response:
        success = bool(final_response.get("success"))
    else:
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
        "source_intent": contexts.routing.intent,
        "datasource": source_datasource,
        "selected_datasources": deepcopy(contexts.routing.selected_datasources),
        "generated_sql": state.get("generated_sql", "") or "",
        "result_available": result_available,
    }
    logger.info(
        "构建轮次结果快照完成",
        datasource=source_datasource,
        selected_sources=len(snapshot["selected_datasources"]),
        result_available=result_available,
    )
    return snapshot


# 方法作用：清空 checkpoint 恢复的上一轮执行产物，防止新问题读取旧结果或旧错误。
# Args: state - 合并当前 API 输入和 checkpoint 后的 LangGraph 状态。
# Returns: 仅包含需要重置的轮次级字段，不覆盖 conversation_history 和 messages。
async def prepare_turn_node(state: AnalysisState) -> dict:
    """初始化当前轮次的瞬态状态，同时保留跨轮对话记忆。"""
    contexts = read_contexts(state)
    logger.debug(
        "轮次状态初始化入口",
        query=contexts.request.user_query[:80],
        datasource=contexts.routing.datasource,
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

    user_preferences: dict = {}
    long_term_memories_text = ""
    tenant_id = contexts.request.tenant_id
    user_id = contexts.request.user_id
    if tenant_id > 0 and user_id > 0:
        try:
            from src.memory.long_term_store import get_long_term_memory_store
            from src.security.tenant_policy import RequestIdentity

            identity = RequestIdentity(
                tenant_id=tenant_id,
                user_id=user_id,
                role=contexts.request.user_role or "analyst",
            )
            memory_store = await get_long_term_memory_store()
            user_preferences = await memory_store.get_preferences(identity=identity)
            memories = await memory_store.search(
                contexts.request.user_query,
                top_k=5,
                identity=identity,
            )
            memory_sections = [str(item.content).strip() for item in memories if item.content]
            if user_preferences:
                preferences_text = "\n".join(
                    f"{key}: {value}" for key, value in sorted(user_preferences.items())
                )
                memory_sections.insert(0, f"用户偏好:\n{preferences_text}")
            long_term_memories_text = "\n\n---\n\n".join(memory_sections)
            logger.info(
                "长期记忆加载边界完成",
                tenant_id=tenant_id,
                user_id=user_id,
                preference_count=len(user_preferences),
                memory_count=len(memories),
                memory_chars=len(long_term_memories_text),
            )
        except Exception as exc:
            logger.error(
                "长期记忆加载失败，降级为空上下文",
                tenant_id=tenant_id,
                user_id=user_id,
                error=str(exc),
                exc_info=True,
            )

    result = {
        "intent": "",
        "task_plan": {},
        "activated_skills": [],
        "activated_skill_ids": [],
        "skill_candidate_ids": [],
        "skill_activation_stage": "",
        "skill_prompt_override": "",
        "skill_tools": [],
        "skill_tool_budget": 0,
        "skill_tool_calls": 0,
        "multi_source_results": [],
        "multi_source_result": {},
        "previous_turn_snapshot": previous_snapshot,
        "previous_result_restored": False,
        "dialect": "",
        "resolved_schema": None,
        "relevant_tables": [],
        "few_shot_examples": [],
        "business_rules_text": "",
        "enum_dictionary": {},
        "user_preferences": user_preferences,
        "long_term_memories_text": long_term_memories_text,
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
        "sql_explain_checked": False,
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
    result.update(build_context_groups({**state, **result}))
    # 升级后首次请求顺手移除旧 checkpoint 中重复保存的富结果。
    history_items = list(state.get("conversation_history", []) or [])
    if len(history_items) >= 50:
        from src.memory.session_archive import compact_turn_history

        history_items = await compact_turn_history(history_items)
    compact_history = []
    history_compacted = False
    for item in history_items:
        if not isinstance(item, dict):
            compact_history.append(item)
            continue
        compact_item = dict(item)
        history_compacted = "final_result" in compact_item or history_compacted
        compact_item.pop("final_result", None)
        compact_history.append(compact_item)
    if history_compacted or history_items != list(state.get("conversation_history", []) or []):
        result["conversation_history"] = compact_history
        logger.info("旧 checkpoint 对话富结果已移除", history_turns=len(compact_history))
    logger.info(
        "轮次状态初始化完成",
        cleared_fields=len(result),
        history_turns=len(state.get("conversation_history", []) or []),
        output_rows=len(result["query_result_sample"]),
        output_has_sql=bool(result["generated_sql"]),
    )
    return result
