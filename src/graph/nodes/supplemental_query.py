"""证据不足时执行受限补查，并把脱敏结果交回分析节点。"""

from __future__ import annotations

import re
import time
from typing import Any

from langchain_core.runnables import RunnableConfig

from src.graph.context import read_contexts
from src.graph.state import AnalysisState
from src.logging_config import get_logger

logger = get_logger(__name__)


# 方法作用：规范化补查问题用于当前请求内去重。
# Args: question - 模型提出的自然语言补查问题。
# Returns: 去除空白并转小写后的稳定文本。
def _normalize_question(question: str) -> str:
    """问题去重只影响执行预算，不改变交给 SQL 模型的原文。"""
    return re.sub(r"\s+", "", str(question or "")).lower()


# 方法作用：选择尚未执行过的第一个补查问题。
# Args: analysis - 当前分析结果；evidence - 已执行补查记录。
# Returns: 待执行问题；没有新问题时返回空字符串。
def select_supplemental_question(analysis: dict, evidence: list[dict]) -> str:
    """每轮只执行一个问题，避免模型一次扩张成无界查询集合。"""
    executed = {
        _normalize_question(str(item.get("question", "") or ""))
        for item in evidence
        if isinstance(item, dict) and item.get("question")
    }
    for value in analysis.get("supplemental_questions", []) or []:
        question = str(value or "").strip()
        normalized = _normalize_question(question)
        if normalized and normalized not in executed:
            return question
    return ""


# 方法作用：构造只包含本轮可信身份、权限和补查问题的隔离 SQL 状态。
# Args: state - 主查询状态；question - 当前补查问题。
# Returns: 可交给 Schema、SQL、安全校验和执行节点的状态。
def _build_supplemental_state(state: AnalysisState, question: str) -> AnalysisState:
    """显式复制白名单字段，避免把主查询错误或结果误带入补查执行。"""
    contexts = read_contexts(state)
    request_context = contexts.request.model_copy(update={"user_query": question}).model_dump()
    task_plan = {
        "intent": "query",
        "operation": "query",
        "capability": "sql_analysis",
        "needs_time_range": False,
    }
    routing_context = contexts.routing.model_copy(update={
        "intent": "query",
        "collect_attribution_rows": contexts.routing.intent == "attribution",
        "task_plan": task_plan,
        "selected_datasources": [contexts.routing.datasource]
        if contexts.routing.datasource else [],
    }).model_dump()
    return {
        "user_query": question,
        "request_context": request_context,
        "session_id": contexts.request.session_id,
        "tenant_id": contexts.request.tenant_id,
        "user_id": contexts.request.user_id,
        "user_role": contexts.request.user_role,
        "request_rate_limit_checked": contexts.request.request_rate_limit_checked,
        "datasource": contexts.routing.datasource,
        "selected_datasources": [contexts.routing.datasource]
        if contexts.routing.datasource else [],
        "intent": "query",
        "task_plan": task_plan,
        "routing_context": routing_context,
        "datasource_access": contexts.permission.datasource_access,
        "allowed_columns": contexts.permission.allowed_columns,
        "row_filter_sql": contexts.permission.row_filter_sql,
        "enabled_skill_ids": contexts.permission.enabled_skill_ids,
        "permission_context": contexts.permission.model_dump(),
        "dialect": contexts.execution.dialect,
        "execution_context": contexts.execution.model_copy(update={
            "retry_count": 0,
            "execution_retry_count": 0,
            "sql_valid": False,
            "sql_explain_checked": False,
            "validation_error_count": 0,
            "explain_error_count": 0,
            "execution_error_type": "",
            "row_count": 0,
            "truncated": False,
        }).model_dump(),
        "resolved_schema": None,
        "relevant_tables": [],
        "few_shot_examples": [],
        "business_rules_text": "",
        "enum_dictionary": {},
        "long_term_memories_text": str(state.get("long_term_memories_text", "") or ""),
        "conversation_history": [],
        "messages": [],
        "skill_prompt_override": str(state.get("skill_prompt_override", "") or ""),
        "generated_sql": "",
        "retry_count": 0,
        "sql_valid": False,
        "validation_errors": [],
        "validation_warnings": [],
        "explain_errors": [],
        "sql_explain_checked": False,
        "execution_error": "",
        "execution_error_type": "",
        "execution_retry_count": 0,
        "query_result_sample": [],
        "query_result_full_count": 0,
        "query_result_truncated": False,
    }


# 方法作用：合并节点状态增量并返回新状态。
# Args: state - 当前隔离状态；update - 下游节点返回增量。
# Returns: 合并后的普通字典。
def _merge(state: AnalysisState, update: dict[str, Any]) -> AnalysisState:
    return {**state, **update}


# 方法作用：在同一调用追踪下执行一次完整的补查 SQL 安全链。
# Args: state - 主状态；question - 补查问题；config - 父级 LangGraph 运行配置。
# Returns: 包含成功、SQL、脱敏数据、行数和错误的补查记录。
async def _execute_supplemental_question(
    state: AnalysisState,
    question: str,
    config: RunnableConfig,
) -> dict[str, Any]:
    from src.graph.nodes.execute_sql import execute_sql_node
    from src.graph.nodes.generate_sql import generate_sql_node
    from src.graph.nodes.layer3_validate import layer3_validate_node
    from src.graph.nodes.layer4_explain import layer4_explain_node
    from src.graph.nodes.retrieve_schema import retrieve_schema_node

    work = _build_supplemental_state(state, question)
    nested_config = dict(config or {})
    metadata = dict(nested_config.get("metadata", {}) or {})
    metadata.update({"node": "supplemental_query", "supplemental": True})
    nested_config["metadata"] = metadata
    try:
        work = _merge(work, await retrieve_schema_node(work))
        from src.config import get_settings

        attempts = max(1, min(2, int(get_settings().max_retry_count)))
        last_error = ""
        for attempt in range(1, attempts + 1):
            logger.info(
                "自主补查 SQL 尝试",
                question=question[:100],
                attempt=attempt,
                table_count=len(work.get("relevant_tables", []) or []),
            )
            work = _merge(work, await generate_sql_node(work, nested_config))
            sql = str(work.get("generated_sql", "") or "").strip()
            if not sql:
                last_error = str(
                    work.get("input_explanation")
                    or work.get("execution_error")
                    or "补查无法生成 SQL"
                )
                break
            work = _merge(work, await layer3_validate_node(work))
            if work.get("validation_errors"):
                last_error = "; ".join(
                    str(item.get("message", "") or "")
                    for item in work.get("validation_errors", [])
                    if isinstance(item, dict)
                ) or "补查 SQL 校验失败"
                continue
            work = _merge(work, await layer4_explain_node(work))
            if work.get("explain_errors"):
                last_error = "; ".join(
                    str(item.get("message", "") or "")
                    for item in work.get("explain_errors", [])
                    if isinstance(item, dict)
                ) or "补查 SQL 预执行失败"
                continue
            work = _merge(work, await execute_sql_node(work))
            if not work.get("execution_error"):
                rows = list(
                    work.get("attribution_candidate_rows", [])
                    or work.get("query_result_sample", [])
                    or [],
                )
                row_count = int(work.get("query_result_full_count", len(rows)) or 0)
                complete = (
                    not bool(work.get("query_result_truncated", False))
                    and row_count <= len(rows)
                )
                return {
                    "question": question,
                    "success": True,
                    "sql": str(work.get("generated_sql", "") or ""),
                    "dialect": str(work.get("dialect", "") or ""),
                    "row_count": row_count,
                    "complete": complete,
                    "data": rows,
                    "error": "",
                }
            last_error = str(work.get("execution_error", "") or "补查 SQL 执行失败")
            if str(work.get("execution_error_type", "") or "") in {
                "configuration", "security", "rate_limit", "llm_unavailable",
            }:
                break
        return {
            "question": question,
            "success": False,
            "sql": str(work.get("generated_sql", "") or ""),
            "dialect": str(work.get("dialect", "") or ""),
            "row_count": 0,
            "complete": False,
            "data": [],
            "error": last_error[:300] or "补查未获得可用证据",
        }
    except Exception as exc:
        logger.error("自主补查异常，保留首次查询结果", error=str(exc), exc_info=True)
        return {
            "question": question,
            "success": False,
            "sql": str(work.get("generated_sql", "") or ""),
            "dialect": str(work.get("dialect", "") or ""),
            "row_count": 0,
            "complete": False,
            "data": [],
            "error": str(exc)[:300],
        }


# 方法作用：执行一轮自主补查并把证据写回主状态。
# Args: state - 已完成一次分析且标记 supplementable 的状态；config - 父级运行配置。
# Returns: 补查轮次、累计证据和清空后的分析结果。
async def supplemental_query_node(
    state: AnalysisState,
    config: RunnableConfig,
) -> dict[str, Any]:
    """补查失败保持主查询数据不变，下一次分析负责明确降低结论强度。"""
    started_at = time.monotonic()
    evidence = list(state.get("supplemental_evidence", []) or [])
    analysis = dict(state.get("analysis_result", {}) or {})
    question = select_supplemental_question(analysis, evidence)
    from src.config import get_settings

    max_rounds = int(getattr(get_settings(), "max_supplemental_query_rounds", 2) or 0)
    current_round = int(state.get("supplemental_query_round", 0) or 0)
    if not question or current_round >= max_rounds:
        logger.info(
            "自主补查跳过",
            reason="无新问题" if not question else "轮次预算耗尽",
            current_round=current_round,
            max_rounds=max_rounds,
        )
        return {
            "supplemental_query_round": max_rounds,
            "supplemental_evidence": evidence,
        }

    logger.info(
        "自主补查开始",
        round=current_round + 1,
        max_rounds=max_rounds,
        question=question[:120],
    )
    record = await _execute_supplemental_question(state, question, config)
    evidence.append(record)
    logger.info(
        "自主补查完成",
        round=current_round + 1,
        success=record["success"],
        row_count=record["row_count"],
        elapsed_ms=round((time.monotonic() - started_at) * 1000),
    )
    return {
        "supplemental_query_round": current_round + 1,
        "supplemental_evidence": evidence,
        "analysis_result": {},
    }
