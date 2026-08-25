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

_TABLE_HINTS: dict[str, tuple[str, ...]] = {
    "user": ("用户", "user", "会员", "vip_level", "用户数", "用户数量"),
    "customer": ("客户", "customer"),
    "order": ("订单", "order", "下单", "交易"),
    "product": ("商品", "产品", "product"),
    "employee": ("员工", "雇员", "employee"),
    "department": ("部门", "department"),
}


# 方法作用：规范化补查问题用于当前请求内去重。
# Args: question - 模型提出的自然语言补查问题。
# Returns: 去除空白并转小写后的稳定文本。
def _normalize_question(question: str) -> str:
    """问题去重只影响执行预算，不改变交给 SQL 模型的原文。"""
    return re.sub(r"\s+", "", str(question or "")).lower()


# 方法作用：从 SQL 中提取真实表名，供补查问题按表归并。
# Args: sql - 待读取的只读 SQL；dialect - 可选 SQL 方言。
# Returns: 去重后的表名列表；解析失败时返回正则回退结果。
def _extract_sql_table_names(sql: str, dialect: str = "") -> list[str]:
    """优先使用 sqlglot 区分表引用与列名，解析失败时仅保守读取 FROM/JOIN。"""
    text = str(sql or "").strip()
    if not text:
        return []
    names: list[str] = []
    try:
        import sqlglot
        from sqlglot import exp

        statements = sqlglot.parse(text, read=dialect or None)
        for statement in statements:
            for table in statement.find_all(exp.Table):
                name = str(table.name or "").strip()
                if name and name.lower() not in {item.lower() for item in names}:
                    names.append(name)
        if names:
            return names
    except Exception as exc:
        logger.debug("补查 SQL 表名解析回退", error=str(exc)[:160])
    for match in re.finditer(
        r"\b(?:from|join)\s+([`\"\[]?[\w.-]+[`\"\]]?)",
        text,
        flags=re.IGNORECASE,
    ):
        name = match.group(1).strip("`\"[]")
        base = name.rsplit(".", 1)[-1]
        if base and base.lower() not in {item.lower() for item in names}:
            names.append(base)
    return names


# 方法作用：从相关 Schema 中提取表名和字段名映射。
# Args: relevant_tables - retrieve_schema 输出的表结构字典列表。
# Returns: 小写表名到小写字段名集合的映射。
def _schema_table_columns(relevant_tables: list[dict] | None) -> dict[str, set[str]]:
    """兼容字典和轻量对象，避免补查选择器依赖具体 Schema 实现。"""
    result: dict[str, set[str]] = {}
    for table in relevant_tables or []:
        if isinstance(table, dict):
            table_name = str(table.get("name", "") or "").strip()
            columns = table.get("columns", []) or []
        else:
            table_name = str(getattr(table, "name", "") or "").strip()
            columns = getattr(table, "columns", []) or []
        if not table_name:
            continue
        column_names: set[str] = set()
        for column in columns:
            if isinstance(column, dict):
                name = str(column.get("name", "") or "").strip()
            else:
                name = str(getattr(column, "name", "") or "").strip()
            if name:
                column_names.add(name.lower())
        result[table_name.lower()] = column_names
    return result


# 方法作用：判断一个补查问题最可能涉及的授权数据表。
# Args: question - 待补查自然语言问题；table_columns - Schema 表字段映射。
# Returns: 可确认的表名集合；无法确认时返回空集合。
def _infer_question_tables(
    question: str,
    table_columns: dict[str, set[str]],
) -> set[str]:
    """只在有字段、表名或明确业务词证据时归属表，防止跨表误合并。"""
    text = str(question or "").lower()
    if not text:
        return set()
    matches: set[str] = set()
    for table_name, columns in table_columns.items():
        base = table_name.rsplit(".", 1)[-1]
        if re.search(rf"(?<![\w]){re.escape(base)}(?![\w])", text):
            matches.add(table_name)
            continue
        if any(
            re.search(rf"(?<![\w]){re.escape(column)}(?![\w])", text)
            for column in columns
        ):
            matches.add(table_name)
            continue
        stem = re.sub(r"s$", "", base)
        hints = _TABLE_HINTS.get(stem, ())
        if any(hint in text for hint in hints):
            matches.add(table_name)
    return matches


# 方法作用：构造本轮唯一的补查任务并合并同表问题。
# Args: analysis - 当前分析结果；evidence - 已执行补查记录；relevant_tables - 当前授权表结构；primary_sql - 主查询 SQL。
# Returns: question/source_questions/table_names 三字段补查任务；无新问题时 question 为空。
def build_supplemental_request(
    analysis: dict,
    evidence: list[dict],
    relevant_tables: list[dict] | None = None,
    primary_sql: str = "",
) -> dict[str, Any]:
    """每轮只返回一个问题；同表缺口要求 SQL 一次返回全部指标或分组结果。"""
    table_columns = _schema_table_columns(relevant_tables)
    primary_tables = {
        name.lower() for name in _extract_sql_table_names(primary_sql)
    }
    for item in evidence or []:
        if isinstance(item, dict):
            primary_tables.update(
                name.lower()
                for name in _extract_sql_table_names(str(item.get("sql", "") or ""))
            )
    known_tables = set(table_columns) | primary_tables
    for table_name in known_tables:
        table_columns.setdefault(table_name, set())

    executed: set[str] = set()
    for item in evidence or []:
        if not isinstance(item, dict):
            continue
        values = [item.get("question", ""), *(item.get("source_questions", []) or [])]
        executed.update(
            normalized
            for normalized in (_normalize_question(str(value or "")) for value in values)
            if normalized
        )

    candidates: list[tuple[str, set[str]]] = []
    for value in analysis.get("supplemental_questions", []) or []:
        question = str(value or "").strip()
        normalized = _normalize_question(question)
        if not normalized or normalized in executed:
            continue
        tables = _infer_question_tables(question, table_columns)
        candidates.append((question, tables))
    if not candidates:
        return {"question": "", "source_questions": [], "table_names": []}

    first_question, first_tables = candidates[0]
    selected = [first_question]
    if first_tables:
        for question, tables in candidates[1:]:
            if tables and tables == first_tables:
                selected.append(question)
    table_names = sorted(first_tables)
    if len(selected) == 1:
        merged_question = first_question
    else:
        table_label = ", ".join(table_names) if table_names else "同一数据表"
        details = "；".join(
            f"{index}. {question}" for index, question in enumerate(selected, 1)
        )
        merged_question = (
            f"请针对同一张表（{table_label}）使用一条 SQL，一次性返回以下补查所需的全部结果，"
            f"保留每个问题要求的分组维度和指标，不要拆分成多次查询：{details}。"
        )
    logger.info(
        "自主补查任务构造完成",
        candidate_count=len(candidates),
        selected_count=len(selected),
        table_names=table_names,
        merged=len(selected) > 1,
    )
    return {
        "question": merged_question,
        "source_questions": selected,
        "table_names": table_names,
    }


# 方法作用：选择尚未执行过的第一个补查任务。
# Args: analysis - 当前分析结果；evidence - 已执行补查记录；relevant_tables - 当前授权表结构；primary_sql - 主查询 SQL。
# Returns: 待执行问题；没有新问题时返回空字符串。
def select_supplemental_question(
    analysis: dict,
    evidence: list[dict],
    relevant_tables: list[dict] | None = None,
    primary_sql: str = "",
) -> str:
    """保持旧的字符串接口，同时复用同表问题合并和来源问题去重。"""
    return str(
        build_supplemental_request(
            analysis,
            evidence,
            relevant_tables=relevant_tables,
            primary_sql=primary_sql,
        ).get("question", "")
        or ""
    )


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
    request = build_supplemental_request(
        analysis,
        evidence,
        relevant_tables=state.get("relevant_tables", []) or [],
        primary_sql=str(state.get("generated_sql", "") or ""),
    )
    question = str(request.get("question", "") or "")
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
    source_questions = list(request.get("source_questions", []) or [])
    if len(source_questions) > 1:
        record = {**record, "source_questions": source_questions}
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
