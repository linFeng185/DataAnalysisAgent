"""4.8 analyze_result Node — 统计引擎 + LLM 洞察。"""

from __future__ import annotations

import asyncio
import json
import time
from datetime import date, datetime
from decimal import Decimal
from itertools import zip_longest
from typing import Any

from langchain_core.messages import HumanMessage
from pydantic import ValidationError

from src.graph.state import AnalysisState
from src.llm.prompt_budget import PromptSection
from src.logging_config import get_logger
from src.tools import processors as _processors  # noqa: F401
from src.tools.analyzer import (
    _extract,
    _find_numeric,
    compute_concentration,
    compute_statistics,
    compute_trend,
    detect_outliers_zscore,
)


def _json_default(obj):
    """处理 date/datetime/Decimal，Decimal 保持精确。"""
    if isinstance(obj, (datetime, date)):
        return obj.isoformat()
    if isinstance(obj, Decimal):
        normalized = obj.normalize()
        __, __, exp = normalized.as_tuple()
        if exp >= 0:
            return int(normalized)
        return float(normalized) if abs(exp) <= 12 else str(normalized)
    raise TypeError(f"Type {type(obj)} not serializable")

logger = get_logger(__name__)


# 方法作用：延迟检查指定 LLM 任务是否可用，避免工作流装配时加载模型依赖。
# Args: task - LLM 任务标识。
# Returns: 指定任务存在可用模型时返回 True。
def _is_task_llm_available(task: str) -> bool:
    from src.llm.client import is_task_llm_available

    return is_task_llm_available(task)


# 方法作用：延迟创建指定任务模型并保留测试可替换的模块级边界。
# Args: task - LLM 任务标识；kwargs - 模型创建参数。
# Returns: 任务对应的 ChatModel。
def _get_task_llm(task: str, **kwargs: Any) -> Any:
    from src.llm.client import get_task_llm

    return get_task_llm(task, **kwargs)


# 方法作用：兼容旧测试和扩展点，同时检查分析任务的模型可用性。
# Args: 无。
# Returns: 本地分析模型或显式远程兜底可用时返回 True。
def is_llm_available() -> bool:
    """返回结果分析任务的模型可用状态。"""
    logger.debug("分析任务模型可用性入口")
    available = _is_task_llm_available("analyze_result")
    logger.debug("分析任务模型可用性完成", available=available)
    return available


# 方法作用：兼容旧 Mock 接口并创建结果分析任务模型。
# Args: temperature - 分析文本生成温度；max_tokens - 输出 token 上限。
# Returns: 按 analyze_result 任务策略创建的 ChatModel。
def get_llm(temperature: float = 0.3, max_tokens: int | None = None):
    """创建结果分析模型，默认使用快速本地模型。"""
    logger.debug(
        "创建分析任务模型入口",
        temperature=temperature,
        max_tokens=max_tokens,
    )
    kwargs: dict[str, Any] = {"temperature": temperature, "reasoning": None}
    if max_tokens is not None:
        kwargs["max_tokens"] = max_tokens
    model = _get_task_llm("analyze_result", **kwargs)
    logger.debug("创建分析任务模型完成")
    return model


# 方法作用：创建带输出上限的分析模型并兼容旧测试替身。
# Args: max_tokens - 分析 JSON 的最大输出 token 数。
# Returns: 可执行结果分析的 ChatModel。
def _get_analysis_llm(max_tokens: int = 2048) -> Any:
    try:
        return get_llm(temperature=0.3, max_tokens=max_tokens)
    except TypeError as exc:
        # 旧扩展点可能只接受 temperature，保留显式兼容而不吞掉其他类型错误。
        if "max_tokens" not in str(exc):
            raise
        logger.warning("分析模型工厂不支持 max_tokens，回退兼容调用")
        return get_llm(temperature=0.3)


async def analyze_result_node(state: AnalysisState) -> dict:
    """描述统计 + 趋势 + 异常 + 占比 → LLM 解读 (有 API Key) 或规则摘要。"""
    from src.graph.context import read_contexts

    contexts = read_contexts(state)
    _start = time.monotonic()
    logger.debug("节点开始", node="analyze_result")
    rows: list[dict] = state.get("query_result_sample", [])
    intent = contexts.routing.intent or "query"
    sql = state.get("generated_sql", "")
    logger.debug(
        "分析节点边界输入",
        intent=intent,
        data_rows=len(rows),
        has_sql=bool(sql),
        history_turns=len(state.get("conversation_history", []) or []),
    )

    if contexts.routing.task_plan.get("operation") == "indicator_catalog":
        from src.graph.indicator_catalog import (
            build_indicator_catalog_plan,
            build_indicator_catalog_summary,
        )

        catalog_plan = build_indicator_catalog_plan(state.get("relevant_tables", []) or [])
        summary = build_indicator_catalog_summary(catalog_plan, rows)
        logger.info(
            "行式指标目录分析完成",
            table=catalog_plan.selected_table,
            result_count=len(rows),
        )
        return {"analysis_result": {
            "summary": summary,
            "insights": [],
            "recommended_chart_type": "table",
            "follow_up_questions": [],
            "evidence_status": "sufficient",
            "evidence_gaps": [],
            "supplemental_questions": [],
            "statistics": {},
        }}

    stats = compute_statistics(rows)
    schema_context = _format_analysis_schema(state.get("relevant_tables", []) or [])
    supplemental_evidence_text = _format_supplemental_evidence(
        state.get("supplemental_evidence", []) or [],
    )
    if intent == "attribution":
        from src.graph.attribution import resolve_touchpoint_attribution

        attribution_resolution = await resolve_touchpoint_attribution(
            state,
            user_query=contexts.request.user_query,
            relevant_tables=state.get("relevant_tables", []) or [],
        )
        if attribution_resolution is not None:
            attribution_rows = attribution_resolution.analysis_rows
            attribution_stats = compute_statistics(attribution_rows) if attribution_rows else stats
            result = dict(attribution_resolution.analysis)
            business_context = "\n\n".join(
                part for part in (
                    state.get("business_rules_text", "") or "",
                    state.get("long_term_memories_text", "") or "",
                ) if part
            )
            logger.info(
                "触点归因分析分支入口",
                applied=attribution_resolution.applied,
                model=(result.get("attribution", {}) or {}).get("selected_model", ""),
                evidence_status=result.get("evidence_status", ""),
                attribution_rows=len(attribution_rows),
            )
            if attribution_resolution.applied and is_llm_available():
                interpreted = await _llm_analyze(
                    attribution_rows,
                    sql,
                    attribution_stats,
                    attribution_resolution.deterministic_findings,
                    "",
                    "",
                    state.get("conversation_history", []),
                    user_query=contexts.request.user_query,
                    result_full_count=len(attribution_rows),
                    result_truncated=False,
                    business_context=business_context,
                    schema_context=schema_context,
                    supplemental_evidence=supplemental_evidence_text,
                    chart_type="bar",
                    intent=intent,
                )
                for field in (
                    "related_metrics",
                    "business_impacts",
                    "possible_causes",
                    "recommended_actions",
                    "follow_up_questions",
                ):
                    result[field] = list(interpreted.get(field, []) or [])
                result["data_quality"] = _merge_text_lists(
                    result.get("data_quality", []),
                    interpreted.get("data_quality", []),
                )
                result["limitations"] = _merge_text_lists(
                    result.get("limitations", []),
                    interpreted.get("limitations", []),
                )
            elif attribution_resolution.applied:
                rule_context = _build_rule_business_analysis(
                    attribution_rows,
                    attribution_stats,
                    contexts.request.user_query,
                )
                for field in (
                    "related_metrics",
                    "business_impacts",
                    "possible_causes",
                    "recommended_actions",
                ):
                    result[field] = list(rule_context.get(field, []) or [])
            result["statistics"] = attribution_stats
            result = _attach_supplemental_trace(result, state)
            result = _attach_skill_outputs(
                result,
                attribution_rows,
                state.get("activated_skills", []) or [],
            )
            result, skill_calls = await _execute_post_analysis_skill_tools(
                result,
                attribution_rows,
                state,
            )
            logger.info(
                "触点归因分析分支完成",
                applied=attribution_resolution.applied,
                evidence_status=result.get("evidence_status", ""),
                result_count=len((result.get("attribution", {}) or {}).get("results", []) or []),
            )
            return {
                "analysis_result": result,
                "skill_tool_calls": skill_calls,
                "attribution_selection": attribution_resolution.selection,
            }
    if not rows:
        if is_llm_available():
            result = await _llm_analyze(
                [],
                sql,
                stats,
                "当前查询未返回数据",
                "",
                "",
                state.get("conversation_history", []),
                user_query=contexts.request.user_query,
                result_full_count=0,
                result_truncated=False,
                business_context="\n\n".join(
                    part for part in (
                        state.get("business_rules_text", "") or "",
                        state.get("long_term_memories_text", "") or "",
                    ) if part
                ),
                schema_context=schema_context,
                supplemental_evidence=supplemental_evidence_text,
                chart_type="table",
                intent=intent,
            )
            result["statistics"] = stats
            result = _attach_supplemental_trace(result, state)
            logger.info(
                "无数据证据评估完成",
                evidence_status=result.get("evidence_status", ""),
                gap_count=len(result.get("evidence_gaps", []) or []),
            )
            return {"analysis_result": result}
        logger.debug("节点完成", node="analyze_result", elapsed_ms=round((time.monotonic() - _start) * 1000))
        return {"analysis_result": {
            "summary": "无数据可供分析",
            "insights": [],
            "recommended_chart_type": "table",
            "follow_up_questions": [],
            "evidence_status": "needs_user_input",
            "evidence_gaps": ["当前查询没有返回可分析记录"],
            "supplemental_questions": [],
            "statistics": stats,
        }}

    # 数据处理器（脚本精确计算，LLM 仅润色）
    processor_result = None
    try:
        from src.tools.data_processor import get_processor
        proc = get_processor(intent, query=contexts.request.user_query)
        if proc:
            nc = _find_numeric(rows)
            params = _build_processor_params(rows, proc.name, nc)
            logger.debug("处理器参数准备完成", processor=proc.name, params=params)
            processor_result = await asyncio.to_thread(proc.process, rows, params)
            logger.info("处理器完成", processor=proc.name, intent=intent,
                        confidence=processor_result.confidence)
    except Exception as exc:
        from src.failure_policy import FailureDomain, fallback_allowed

        if not fallback_allowed(FailureDomain.DATA_PROCESSOR):
            raise
        logger.error("处理器失败，回退 LLM", error=str(exc), exc_info=True)

    # 处理器负责确定性计算，完整分析模型负责业务关联、影响和行动建议。
    if processor_result and processor_result.data:
        analysis_available = is_llm_available()
        business_context = "\n\n".join(
            part for part in (
                state.get("business_rules_text", "") or "",
                state.get("long_term_memories_text", "") or "",
            ) if part
        )
        logger.debug(
            "分析分支选择边界",
            branch="processor_then_business_analysis",
            processor=getattr(proc, "name", "unknown"),
            processor_insight_count=len(processor_result.insights),
            analysis_available=analysis_available,
            business_context_chars=len(business_context),
        )
        result = {
            "summary": processor_result.summary,
            "insights": processor_result.insights,
            "recommended_chart_type": processor_result.chart_type,
            "follow_up_questions": [],
            "processor_name": getattr(proc, "name", "unknown"),
            "statistics": stats,
        }
        result = _attach_skill_outputs(result, rows, state.get("activated_skills", []) or [])
        deterministic_findings = "\n".join((
            f"处理器摘要：{processor_result.summary}",
            *(f"处理器洞察：{item}" for item in processor_result.insights),
        ))
        if analysis_available:
            result = await _llm_analyze(
                rows,
                sql,
                stats,
                deterministic_findings,
                "",
                "",
                state.get("conversation_history", []),
                user_query=contexts.request.user_query,
                result_full_count=contexts.execution.row_count or len(rows),
                result_truncated=contexts.execution.truncated,
                business_context=business_context,
                schema_context=schema_context,
                supplemental_evidence=supplemental_evidence_text,
                chart_type=processor_result.chart_type,
                intent=intent,
            )
            result["processor_name"] = getattr(proc, "name", "unknown")
            result["statistics"] = stats
        else:
            rule_context = _build_rule_business_analysis(
                rows,
                stats,
                contexts.request.user_query,
            )
            logger.debug(
                "处理器与规则输出合并边界",
                processor_chart_type=processor_result.chart_type,
                rule_chart_type=rule_context.get("recommended_chart_type", ""),
                is_rule_chart_override="recommended_chart_type" in rule_context,
            )
            result.update(rule_context)
        result = _attach_supplemental_trace(result, state)
        result, skill_calls = await _execute_post_analysis_skill_tools(result, rows, state)
        logger.debug(
            "处理器分析输出边界",
            summary_chars=len(str(result.get("summary", "") or "")),
            insight_count=len(result.get("insights", []) or []),
            impact_count=len(result.get("business_impacts", []) or []),
            cause_count=len(result.get("possible_causes", []) or []),
            action_count=len(result.get("recommended_actions", []) or []),
            related_metric_count=len(result.get("related_metrics", []) or []),
        )
        logger.debug("节点完成", node="analyze_result", processor=result.get("processor_name"),
                    elapsed_ms=round((time.monotonic() - _start) * 1000))
        return {"analysis_result": result, "skill_tool_calls": skill_calls}

    # 回退：旧统计路径（无匹配处理器时）
    numeric_cols = stats.get("numeric_columns", [])

    trend_info = ""
    if intent in ("trend", "aggregation"):
        time_col = _find_time_col(rows)
        if time_col and numeric_cols:
            t = compute_trend(rows, time_col, numeric_cols[0])
            trend_info = f"趋势: {_trend_label(t['trend'])}, 环比: {t['change_pct']}%"

    outlier_info = ""
    if numeric_cols and len(rows) >= 8:
        vals = _extract(rows, numeric_cols[0])
        outliers = detect_outliers_zscore(vals)
        if outliers:
            outlier_info = f"检测到 {len(outliers)} 个异常值"

    conc_info = ""
    cat_col = _find_category_col(rows)
    if cat_col and numeric_cols and len(rows) > 1:
        grouped = _group_by(rows, cat_col, numeric_cols[0])
        vals = [v for _, v in grouped]
        c = compute_concentration(vals, min(5, len(vals)))
        if c.get("top_concentration", 0) > 0:
            conc_info = f"Top{c['top_n']} 集中度: {c['top_concentration']}%"

    chart_type = _recommend_chart_type(rows, numeric_cols)

    if is_llm_available():
        history = state.get("conversation_history", [])
        business_context = "\n\n".join(
            part for part in (
                state.get("business_rules_text", "") or "",
                state.get("long_term_memories_text", "") or "",
            ) if part
        )
        result = await _llm_analyze(
            rows,
            sql,
            stats,
            trend_info,
            outlier_info,
            conc_info,
            history,
            user_query=contexts.request.user_query,
            result_full_count=contexts.execution.row_count or len(rows),
            result_truncated=contexts.execution.truncated,
            business_context=business_context,
            schema_context=schema_context,
            supplemental_evidence=supplemental_evidence_text,
            chart_type=chart_type,
            intent=intent,
        )
    else:
        result = _rule_analyze(
            rows,
            stats,
            trend_info,
            outlier_info,
            conc_info,
            chart_type,
            intent,
            contexts.request.user_query,
        )

    result["statistics"] = stats
    result = _attach_supplemental_trace(result, state)
    result = _attach_skill_outputs(result, rows, state.get("activated_skills", []) or [])
    result, skill_calls = await _execute_post_analysis_skill_tools(result, rows, state)
    logger.debug("节点完成", node="analyze_result", elapsed_ms=round((time.monotonic() - _start) * 1000))
    return {"analysis_result": result, "skill_tool_calls": skill_calls}


async def _llm_polish(summary: str, insights: list[str], data_sample: str) -> dict | None:
    """LLM 对处理器输出做自然语言润色，不参与数值计算。"""
    try:
        llm = _get_task_llm("polish_result", temperature=0.3, reasoning=None)
        from src.llm.invocation import invoke_structured
        from src.llm.output_contracts import PolishOutput

        parsed = await invoke_structured(
            "analysis.polish",
            [
                PromptSection(
                    "summary",
                    f"## 分析摘要\n{summary}",
                    priority=100,
                    min_chars=500,
                    max_chars=1400,
                ),
                PromptSection(
                    "insights",
                    "## 关键洞察\n" + "\n".join(f"- {item}" for item in insights[:5]),
                    priority=90,
                    min_chars=300,
                    max_chars=900,
                ),
                PromptSection(
                    "data_sample",
                    f"## 数据样本\n{data_sample}",
                    priority=50,
                    max_chars=1000,
                ),
            ],
            output_model=PolishOutput,
            model=llm,
        )
        return parsed.model_dump()
    except Exception as exc:
        logger.error("LLM 润色异常", error=str(exc), exc_info=True)
        return None


# 方法作用：按来源分别均匀采样并交错展示，避免字符裁剪只保留首个来源。
# Args: rows - 已授权的查询结果样本。
# Returns: 各来源均有代表行的紧凑 JSON；单源保持原顺序。
def _to_compact(rows: list[dict]) -> str:
    """全量数据转紧凑 JSON，超限按来源均匀抽取。"""
    if not rows:
        return "[]"
    from src.config import get_settings
    limit = get_settings().analysis_data_max_chars
    text = json.dumps(rows, ensure_ascii=False, default=_json_default,
                      separators=(",", ":"))
    groups: dict[str, list[dict]] = {}
    for row in rows:
        groups.setdefault(str(row.get("_datasource", "")), []).append(row)
    max_rows = len(rows) if len(text) <= limit else max(10, int(limit / (len(text) / len(rows))))
    per_source = max(1, max_rows // len(groups))
    batches = []
    for source_rows in groups.values():
        if len(text) <= limit or len(source_rows) <= per_source:
            batches.append(source_rows)
        else:
            indices = [index * (len(source_rows) - 1) // max(1, per_source - 1) for index in range(per_source)]
            batches.append([source_rows[index] for index in indices])
    sampled = [row for group in zip_longest(*batches) for row in group if row is not None]
    return json.dumps(sampled, ensure_ascii=False, default=_json_default,
                      separators=(",", ":"))


# 方法作用：确定性汇总各来源样本时间边界，避免模型将可见头部误当全量。
# Args: rows - 进入分析器的已授权样本。
# Returns: 仅描述样本范围和来源行数的说明，不推断数据库完整覆盖。
def _format_sample_scope(rows: list[dict]) -> str:
    groups: dict[str, list[dict]] = {}
    for row in rows:
        groups.setdefault(str(row.get("_datasource", "当前来源")), []).append(row)
    parts = ["以下边界按进入分析器的样本确定性计算，不代表数据库完整范围；模型可见代表行更少不能改变这些边界。"]
    for source, source_rows in groups.items():
        boundaries = []
        for column in dict.fromkeys(key for row in source_rows for key in row):
            if not any(token in column.lower() for token in ("date", "time", "day", "month", "year", "日期", "年月")):
                continue
            values = []
            for row in source_rows:
                value = row.get(column)
                text = value.isoformat() if isinstance(value, (date, datetime)) else str(value or "")
                try:
                    datetime.fromisoformat(f"{text}-01" if len(text) == 7 else text)
                except ValueError:
                    continue
                values.append(text)
            if values:
                boundaries.append(f"{column}={min(values)} 至 {max(values)}")
        parts.append(f"{source}：{len(source_rows)} 行样本；" + "；".join(boundaries[:4]))
    return "\n".join(parts)


# 方法作用：把当前授权 Schema 压缩成证据充分性评估可用的字段摘要。
# Args: tables - retrieve_schema 返回的真实表结构。
# Returns: 不超过 6000 字符的紧凑 JSON。
def _format_analysis_schema(tables: list[dict]) -> str:
    """只保留表名、说明和字段名注释，避免把完整 Schema 重复投喂。"""
    compact = [
        {
            "table": str(table.get("name", "") or ""),
            "description": str(table.get("description", "") or ""),
            "columns": [
                {
                    "name": str(column.get("name", "") or ""),
                    "comment": str(column.get("comment", "") or ""),
                }
                for column in (table.get("columns", []) or [])[:40]
                if isinstance(column, dict) and column.get("name")
            ],
        }
        for table in tables[:30]
        if isinstance(table, dict) and table.get("name")
    ]
    return json.dumps(compact, ensure_ascii=False, separators=(",", ":"))[:6000]


# 方法作用：把补查结果转换成重新分析可引用的脱敏证据文本。
# Args: evidence - supplemental_query 节点累计的补查记录。
# Returns: 包含问题、SQL、行数、数据或失败原因的紧凑 JSON。
def _format_supplemental_evidence(evidence: list[dict]) -> str:
    """补查数据已经过执行节点脱敏，此处仍限制每项最多 50 行和总字符数。"""
    compact = [
        {
            "question": str(item.get("question", "") or ""),
            "source_questions": list(item.get("source_questions", []) or []),
            "success": bool(item.get("success")),
            "sql": str(item.get("sql", "") or ""),
            "row_count": int(item.get("row_count", 0) or 0),
            "complete": bool(item.get("complete", False)),
            "data": list(item.get("data", []) or [])[:50] if item.get("success") else [],
            "error": str(item.get("error", "") or "")[:300],
        }
        for item in evidence[-3:]
        if isinstance(item, dict)
    ]
    return json.dumps(
        compact,
        ensure_ascii=False,
        default=_json_default,
        separators=(",", ":"),
    )[:8000]


# 方法作用：给公开分析结果附加不含数据明细的自主补查轨迹。
# Args: result - 当前分析结果；state - 当前图状态。
# Returns: 包含轮次和补查摘要的新字典。
def _attach_supplemental_trace(result: dict, state: AnalysisState) -> dict:
    """公开轨迹说明系统为什么补查以及补查是否成功，不重复泄露结果行。"""
    evidence = state.get("supplemental_evidence", []) or []
    if not evidence:
        return result
    enriched = dict(result)
    enriched["supplemental_query_round"] = int(
        state.get("supplemental_query_round", 0) or 0,
    )
    enriched["supplemental_evidence"] = [
        {
            "question": str(item.get("question", "") or ""),
            "source_questions": list(item.get("source_questions", []) or []),
            "success": bool(item.get("success")),
            "row_count": int(item.get("row_count", 0) or 0),
            "error": str(item.get("error", "") or "")[:300],
        }
        for item in evidence
        if isinstance(item, dict)
    ]
    return enriched


# 方法作用：稳定合并规则与模型的文本列表并去重。
# Args: first - 高优先级规则列表；second - 补充解释列表。
# Returns: 保持首次出现顺序的非空字符串列表。
def _merge_text_lists(first: list[Any], second: list[Any]) -> list[str]:
    """确定性限制和质量说明始终排在模型补充内容之前。"""
    return list(dict.fromkeys(
        str(item).strip()
        for item in (*list(first or []), *list(second or []))
        if str(item).strip()
    ))


# 方法作用：为已激活且有确定性实现的 Skill 写入真实分析产物。
# Args: result - 基础分析结果；rows - 查询结果样本；activated_skills - 当前激活 Skill 名称。
# Returns: 附加质量报告和质量说明后的分析结果。
def _attach_skill_outputs(
    result: dict,
    rows: list[dict],
    activated_skills: list[str],
) -> dict:
    """只声明真正执行过的 Skill 输出，避免“已激活但未运行”的假状态。"""
    logger.debug(
        "附加 Skill 分析产物入口",
        activated_skills=activated_skills,
        row_count=len(rows),
    )
    if "data-quality-check" not in activated_skills:
        logger.debug("附加 Skill 分析产物跳过", reason="未激活数据质量 Skill")
        return result

    columns = sorted({str(column) for row in rows for column in row})
    null_rates = {
        column: round(
            sum(1 for row in rows if row.get(column) is None) / len(rows),
            4,
        )
        for column in columns
    } if rows else {}
    serialized_rows = [
        json.dumps(row, ensure_ascii=False, sort_keys=True, default=_json_default)
        for row in rows
    ]
    duplicate_row_count = len(serialized_rows) - len(set(serialized_rows))
    outliers: dict[str, list[dict]] = {}
    for column in _find_numeric(rows):
        detected = detect_outliers_zscore(_extract(rows, column))
        if detected:
            outliers[column] = detected
    quality_report = {
        "row_count": len(rows),
        "null_rates": null_rates,
        "duplicate_row_count": duplicate_row_count,
        "outliers": outliers,
    }
    enriched = dict(result)
    enriched["quality_report"] = quality_report
    quality_notes = list(enriched.get("data_quality", []) or [])
    high_null_columns = [column for column, rate in null_rates.items() if rate > 0.1]
    if high_null_columns:
        quality_notes.append(f"高空值率字段: {', '.join(high_null_columns)}")
    if duplicate_row_count:
        quality_notes.append(f"检测到 {duplicate_row_count} 行重复记录")
    if outliers:
        quality_notes.append(f"检测到异常值字段: {', '.join(sorted(outliers))}")
    enriched["data_quality"] = quality_notes
    logger.info(
        "附加 Skill 分析产物完成",
        null_column_count=len(high_null_columns),
        duplicate_row_count=duplicate_row_count,
        outlier_column_count=len(outliers),
    )
    return enriched


# 方法作用：按显式适配器和请求预算执行分析后 Skill 工具。
# Args: result - 基础分析结果；rows - 查询样本；state - Skill 工具与预算状态。
# Returns: 附加真实工具产物的分析结果和累计调用次数。
async def _execute_post_analysis_skill_tools(
    result: dict,
    rows: list[dict],
    state: AnalysisState,
) -> tuple[dict, int]:
    """按显式参数适配器执行分析后 Skill 工具，并严格遵守请求级预算。"""
    from src.graph.skill_activation import resolve_active_skill_tools

    # 工具对象只在实际消费节点恢复，避免进入 LangGraph checkpoint 序列化边界。
    tools = resolve_active_skill_tools(state)
    used = int(state.get("skill_tool_calls", 0) or 0)
    budget = int(state.get("skill_tool_budget", 0) or 0)
    limit = max(0, budget)
    if not tools:
        logger.debug("分析后 Skill 工具跳过", reason="无已授权工具")
        return result, used

    enriched = dict(result)
    tool_outputs = dict(enriched.get("skill_outputs", {}) or {})
    columns = sorted({str(column) for row in rows for column in row})
    numeric_columns = _find_numeric(rows)

    # 方法作用：在共享预算内执行单个已授权 Skill 工具。
    # Args: tool - 目标工具；payload - 工具结构化输入。
    # Returns: 工具输出；超预算返回 None，执行异常返回脱敏错误。
    async def invoke(tool, payload: dict) -> object | None:
        nonlocal used
        if used >= limit:
            logger.warning("分析后 Skill 工具预算耗尽", tool=tool.name, used=used, limit=limit)
            return None
        used += 1
        logger.info("分析后 Skill 工具调用", tool=tool.name, call_count=used, limit=limit)
        try:
            return await tool.ainvoke(payload)
        except Exception as exc:
            logger.error(
                "分析后 Skill 工具失败",
                tool=tool.name,
                error=str(exc),
                exc_info=True,
            )
            return {"error": "工具执行失败"}

    for tool in tools:
        name = str(getattr(tool, "name", "") or "")
        if not name:
            continue
        outputs: list[object] = []
        if name == "render_report":
            from src.graph.context import read_contexts
            from src.graph.reporting import build_report_metrics

            query = read_contexts(state).request.user_query
            template = "monthly_report" if "月报" in query else "weekly_report"
            metrics = build_report_metrics(rows)
            rendered = await invoke(tool, {
                "template": template,
                "data": {
                    "title": query or "数据分析报告",
                    "summary": enriched.get("summary", ""),
                    "insights": enriched.get("insights", []),
                    "metrics": metrics,
                },
            })
            if isinstance(rendered, str):
                enriched["rendered_report"] = rendered
            outputs.append(rendered)
        elif name == "check_null_rate":
            for column in columns:
                output = await invoke(tool, {"rows": rows, "column": column})
                if output is not None:
                    outputs.append(output)
                if used >= limit:
                    break
        elif name == "check_duplicates" and columns:
            outputs.append(await invoke(tool, {"rows": rows, "column": columns[0]}))
        elif name == "detect_outliers":
            for column in numeric_columns:
                output = await invoke(tool, {"rows": rows, "column": column})
                if output is not None:
                    outputs.append(output)
                if used >= limit:
                    break
        else:
            logger.debug("分析后 Skill 工具跳过", tool=name, reason="未注册参数适配器")
            continue
        cleaned_outputs = [item for item in outputs if item is not None]
        if cleaned_outputs:
            tool_outputs[name] = cleaned_outputs

    if tool_outputs:
        enriched["skill_outputs"] = tool_outputs
    logger.info(
        "分析后 Skill 工具完成",
        tool_count=len(tools),
        output_count=len(tool_outputs),
        call_count=used,
        limit=limit,
    )
    return enriched, used


async def _llm_analyze(
    rows,
    sql,
    stats,
    trend,
    outlier,
    conc,
    history=None,
    *,
    user_query: str = "",
    result_full_count: int | None = None,
    result_truncated: bool = False,
    business_context: str = "",
    schema_context: str = "",
    supplemental_evidence: str = "",
    chart_type: str = "table",
    intent: str = "query",
) -> dict:
    """基于问题、结果完整性、统计和业务证据生成分析报告。

    Args:
        rows: 进入分析器的数据行。
        sql: 实际执行的 SQL。
        stats: 确定性统计摘要。
        trend: 规则引擎趋势发现。
        outlier: 规则引擎异常发现。
        conc: 规则引擎集中度发现。
        history: 裁剪前的对话历史。
        user_query: 用户当前原始问题。
        result_full_count: 查询返回的总行数。
        result_truncated: 查询结果是否被安全上限截断。
        business_context: 业务规则和知识库证据。
        schema_context: 当前授权数据源的真实表字段摘要。
        supplemental_evidence: 当前请求此前自主补查得到的证据。
        chart_type: 规则引擎根据真实列类型推荐的图表类型。
        intent: 当前用户问题的真实意图。

    Returns:
        兼容基础字段并包含质量、限制、置信度和行动建议的分析结果。
    """
    logger.debug(
        "LLM 数据分析入口",
        user_query=user_query[:80],
        input_rows=len(rows),
        result_full_count=result_full_count,
        result_truncated=result_truncated,
        business_chars=len(business_context),
        schema_chars=len(schema_context),
        supplemental_chars=len(supplemental_evidence),
    )
    total_rows = len(rows)
    sample = _to_compact(rows)
    sample_row_count = 0
    if sample.startswith("["):
        try:
            parsed_sample = json.loads(sample)
            sample_row_count = len(parsed_sample) if isinstance(parsed_sample, list) else 0
        except json.JSONDecodeError:
            logger.warning("LLM 分析样本 JSON 解析失败", sample_preview=sample[:120])
    is_full = sample_row_count == total_rows
    stat_text = json.dumps(stats.get("columns", {}), ensure_ascii=False, default=_json_default,
                           separators=(",", ":"))

    context_text = ""
    if history:
        from src.memory.context_builder import build_llm_context
        context_text = await build_llm_context(history, node_name="analyze_result")
    history_block = f"## 对话历史\n{context_text}" if context_text else ""

    input_label = f"输入全量 {total_rows} 行" if is_full else f"输入分源均匀采样 {sample_row_count}/{total_rows} 行"
    database_count = result_full_count if result_full_count is not None else total_rows
    completeness = (
        f"数据库查询返回 {database_count} 行；{input_label}；"
        f"安全截断={'是' if result_truncated else '否'}。"
    )
    business_block = business_context or "(无额外业务口径，以 SQL 与统计结果为准)"
    question = user_query or "(未提供原问题，仅解释当前结果)"
    prompt_sections = [
            PromptSection(
                "query",
                f"## 用户原问题\n{question}\n请给出分析报告。",
                priority=100,
                min_chars=400,
                max_chars=1800,
            ),
            PromptSection(
                "completeness",
                f"## 数据完整性\n{completeness}",
                priority=99,
                min_chars=150,
                max_chars=500,
            ),
            PromptSection(
                "sample_scope",
                f"## 确定性样本边界\n{_format_sample_scope(rows)}",
                priority=98,
                min_chars=600,
                max_chars=1500,
            ),
            PromptSection(
                "sql",
                f"## 执行的 SQL\n```sql\n{sql}\n```",
                priority=95,
                min_chars=500,
                max_chars=2200,
            ),
            PromptSection(
                "result_sample",
                f"## 查询结果 ({input_label})\n{sample}",
                priority=90,
                min_chars=2000,
                max_chars=4500,
            ),
            PromptSection(
                "statistics",
                f"## 统计摘要\n{stat_text}",
                priority=85,
                min_chars=700,
                max_chars=2200,
            ),
            PromptSection(
                "supplemental_evidence",
                f"## 已执行的补查证据\n{supplemental_evidence}"
                if supplemental_evidence else "",
                priority=88,
                min_chars=300,
                max_chars=2400,
            ),
            PromptSection(
                "deterministic_findings",
                f"## 确定性分析器发现\n{trend} | {outlier} | {conc}",
                priority=80,
                min_chars=200,
                max_chars=800,
            ),
            PromptSection(
                "authorized_schema",
                f"## 当前授权 Schema（仅可从这些真实字段规划补查）\n{schema_context}"
                if schema_context else "",
                priority=75,
                min_chars=800,
                max_chars=3200,
            ),
            PromptSection(
                "business_context",
                f"## 业务规则与知识上下文\n{business_block}",
                priority=60,
                min_chars=300,
                max_chars=1800,
            ),
            PromptSection(
                "history",
                history_block,
                priority=30,
                max_chars=1200,
            ),
        ]

    # 分析输出允许使用独立预算，避免沿用 SQL 生成的长超时拖住补查回环。
    from src.config import get_settings
    settings = get_settings()
    configured_timeout = getattr(settings, "analysis_llm_timeout", None)
    if configured_timeout is None:
        configured_timeout = getattr(settings, "llm_timeout", 60)
    timeout_seconds = max(0.1, min(float(configured_timeout), 3600.0))
    try:
        from src.llm.adapters.registry import get_adapter
        from src.llm.invocation import prepare_invocation
        llm = _get_analysis_llm(max_tokens=2048)
        logger.debug(
            "分析模型调用资源预算",
            timeout_seconds=timeout_seconds,
            max_tokens=2048,
        )
        prepared = prepare_invocation(
            "analysis.result",
            prompt_sections,
            task="analyze_result",
            temperature=0.3,
            metadata={"node": "analyze_result", "intent": intent},
            model=llm,
        )
        adapter = get_adapter(getattr(settings, "llm_model", ""))
        from src.llm.output_contracts import AnalysisOutput, parse_json_model

        deadline = time.monotonic() + timeout_seconds
        messages = list(prepared.messages)
        for attempt in range(2):
            resp = await asyncio.wait_for(
                prepared.model.ainvoke(messages),
                timeout=max(0.01, deadline - time.monotonic()),
            )
            parsed = adapter.parse_response(resp)
            parsed_content = str(getattr(parsed, "content", "") or "").strip()
            if not parsed_content:
                logger.warning("适配器未返回正文，兼容原始响应", model=get_settings().llm_model)
                parsed_content = str(getattr(resp, "content", "") or "").strip()
            try:
                result = parse_json_model(parsed_content, AnalysisOutput).model_dump()
                break
            except (json.JSONDecodeError, ValidationError) as exc:
                if attempt:
                    raise
                logger.warning("分析输出契约无效，限次重试", error_type=type(exc).__name__, retry_count=1)
                messages.append(HumanMessage(content="上次输出未通过 JSON 契约校验。请重新分析同一份证据，只返回一个符合要求的完整 JSON 对象；禁止连续多个对象、尾随解释或省略限制字段。"))
        if parsed.reasoning_content:
            logger.debug(
                "分析模型内部推理已抑制",
                reasoning_chars=len(parsed.reasoning_content),
            )
        logger.info(
            "LLM 数据分析完成",
            confidence=result["confidence"],
            insight_count=len(result["insights"]),
            limitation_count=len(result["limitations"]),
            evidence_status=result["evidence_status"],
            supplemental_question_count=len(result["supplemental_questions"]),
        )
        return result
    except TimeoutError:
        logger.warning(
            "LLM 分析调用超时，回退确定性分析",
            timeout_seconds=timeout_seconds,
            input_rows=len(rows),
        )
        return _rule_analyze(
            rows,
            stats,
            trend,
            outlier,
            conc,
            chart_type,
            intent,
        )
    except Exception as e:
        logger.error("LLM 分析失败", error=str(e), exc_info=True)
        logger.info(
            "LLM 分析失败回退边界",
            fallback="_rule_analyze",
            positional_argument_count=7,
            has_user_query=bool(user_query),
            chart_type=chart_type,
            intent=intent,
        )
        return _rule_analyze(
            rows,
            stats,
            trend,
            outlier,
            conc,
            chart_type,
            intent,
        )


def _rule_analyze(
    rows,
    stats,
    trend,
    outlier,
    conc,
    chart_type,
    intent,
    user_query: str = "",
) -> dict:
    """规则生成分析摘要。"""
    insights = []
    if trend:
        insights.append(trend)
    if conc:
        insights.append(conc)
    if outlier:
        insights.append(outlier)
    row_count = stats.get("row_count", len(rows))
    summary = f"共 {row_count} 行数据"
    if trend:
        summary += f" | {trend}"
    fups = [f"查看 {nc} 的分布" for nc in stats.get("numeric_columns", [])[:2]]
    result = {
        "summary": summary,
        "insights": insights,
        "recommended_chart_type": chart_type,
        "follow_up_questions": fups,
    }
    result.update(_build_rule_business_analysis(rows, stats, user_query))
    return result


# 方法作用：模型不可用时按指标语义补齐关联指标、影响、待验证原因和条件化建议。
# Args: rows - 查询结果；stats - 确定性统计；user_query - 用户原问题。
# Returns: 不包含编造数值的业务解读字段。
def _build_rule_business_analysis(
    rows: list[dict],
    stats: dict,
    user_query: str,
) -> dict:
    """规则回退只给分析框架，未查询的业务关系全部标记为待验证。"""
    numeric_columns = list(stats.get("numeric_columns", []) or [])
    evidence = " ".join((user_query, *numeric_columns)).lower()
    related_metrics: list[str]
    possible_causes: list[str]
    business_impacts: list[str]
    recommended_actions: list[str]

    if any(marker in evidence for marker in ("order", "workorder", "工单", "需求单")):
        related_metrics = [
            "建议联查交付工单数与需求工单数的差额，判断积压是否扩大",
            "建议联查完工率、平均工单时长和在制工单数，判断交付能力",
            "建议联查车间产能、在岗人数和物料可用库存，定位供给侧约束",
        ]
        business_impacts = [
            "需求持续高于交付时可能形成工单积压，并进一步影响交期和客户满意度",
            "需求持续低于产能时可能造成设备与人员利用不足，需要结合计划负荷判断",
        ]
        possible_causes = [
            "待验证：固定值可能来自真实稳定需求，也可能与批量下单、统计口径或上游同步规则有关；需核对工单明细和导入日志",
            "待验证：车间、产品或客户结构可能相互抵消形成总量稳定；需按这些维度拆分",
        ]
        recommended_actions = [
            "立即核查需求工单明细数、去重口径和同步日志，确认汇总值与源记录一致",
            "持续监控需求与交付差额、完工率和平均时长，建立积压预警",
            "若需求连续高于交付能力，再评估排产、人员和物料调整；若连续低于产能，再复核销售需求与生产计划",
        ]
    elif any(marker in evidence for marker in ("sale", "sales", "销售", "收入")):
        related_metrics = [
            "建议联查订单数、客单价和合同生效数，拆分销售变化来源",
            "建议联查出货量、库存和毛利，判断增长是否可交付且有收益",
        ]
        business_impacts = [
            "销售变化可能传导至库存周转、排产负荷、现金流和客户交付",
        ]
        possible_causes = [
            "待验证：渠道、区域、产品结构或价格变化可能共同驱动结果；需分维度对比",
        ]
        recommended_actions = [
            "先拆分订单量与客单价贡献，再按渠道、区域和产品核查变化来源",
            "若增长伴随库存下降或交付延迟，优先校验供给能力；若下降持续，再检查客户流失和渠道转化",
        ]
    elif any(marker in evidence for marker in ("stock", "inventory", "库存", "仓库")):
        related_metrics = [
            "建议联查入库量、出库量、计划出库量和库存周转天数",
            "建议联查生产计划、订单需求和缺货次数，判断库存是否匹配业务节奏",
        ]
        business_impacts = [
            "库存过高可能增加资金占用，库存不足可能影响生产连续性和订单交付",
        ]
        possible_causes = [
            "待验证：采购到货、生产节拍或需求预测偏差可能导致库存变化；需联查对应计划与实绩",
        ]
        recommended_actions = [
            "按物料和仓库拆分库存变化，并对照计划出入库识别偏差",
            "对连续低于安全库存或长期高库存项目分别设置补货和消化条件",
        ]
    else:
        related_metrics = [
            "建议联查该指标的上游投入、同期计划值和下游结果指标，形成完整业务链路",
        ]
        business_impacts = [
            "当前指标变化可能向相邻业务环节传导，影响程度需结合关联指标验证",
        ]
        possible_causes = [
            "待验证：业务量、结构、口径或数据同步变化都可能造成当前现象，现有结果不能单独证明原因",
        ]
        recommended_actions = [
            "先核对口径和原始明细，再按时间与核心业务维度拆分",
            "补充计划值、历史基线和下游结果指标后，再决定是否采取业务动作",
        ]

    flat_columns = [stats.get("columns", {}).get(column, {}) for column in numeric_columns]
    is_flat = bool(flat_columns) and all(column.get("std") == 0 for column in flat_columns)
    summary = ""
    flat_insights: list[str] = []
    if is_flat and numeric_columns:
        value = flat_columns[0].get("mean")
        metric_name = numeric_columns[0]
        if any(marker in evidence for marker in ("demand_order", "需求工单", "需求单")):
            metric_name = "单日需求工单数"
        elif any(marker in evidence for marker in ("sales_amount", "销售额")):
            metric_name = "销售额"
        elif any(marker in evidence for marker in ("inventory", "stock", "库存")):
            metric_name = "库存指标"
        summary = (
            f"查询返回 {len(rows)} 条记录，{metric_name}均为 {value:g}，"
            "当前数据范围内没有波动。"
        ) if isinstance(value, (int, float)) else "当前数据范围内指标保持不变。"
        flat_insights = [
            f"{len(rows)} 条记录的最小值、最大值和均值均为 {value:g}，极差和标准差均为 0。"
        ] if isinstance(value, (int, float)) else ["当前数据范围内各记录数值一致。"]
        business_impacts.insert(
            0,
            "指标持平有利于短期资源安排，但也需要排除固定值、统计上限或同步未更新等数据问题",
        )

    return {
        **({"summary": summary} if summary else {}),
        **({"insights": flat_insights} if flat_insights else {}),
        **({
            "recommended_chart_type": "line" if _find_time_col(rows) else "table",
        } if is_flat else {}),
        "related_metrics": related_metrics,
        "business_impacts": business_impacts,
        "possible_causes": possible_causes,
        "recommended_actions": recommended_actions,
        "limitations": ["未查询到的关联指标和原因仅作为待验证分析方向，不能视为已发生事实"],
        "confidence": "medium" if rows else "low",
        "evidence_status": "sufficient" if rows else "needs_user_input",
        "evidence_gaps": [] if rows else ["当前查询没有返回可分析记录"],
        "supplemental_questions": [],
        "follow_up_questions": [
            "是否继续联查关联指标并按核心业务维度拆分？",
            "是否需要与计划值或上一周期做对比？",
        ],
    }


# helpers
def _trend_label(t: str) -> str:
    return {"up": "上升", "down": "下降", "flat": "平稳"}.get(t, t)


def _find_category_col(rows):
    for k in rows[0]:
        if all(isinstance(r.get(k), str) for r in rows if r.get(k) is not None):
            return k
    return None


# 方法作用：找出可作为分组、名称或交叉透视维度的文本列。
# Args: rows - 查询结果行。
# Returns: 按原始列顺序返回文本维度列名。
def _find_category_cols(rows: list[dict]) -> list[str]:
    logger.debug("分类列识别入口", row_count=len(rows))
    if not rows:
        return []
    result = [
        str(key) for key in rows[0]
        if all(value is None or isinstance(value, str) for value in (row.get(key) for row in rows))
    ]
    logger.debug("分类列识别完成", columns=result)
    return result


# 方法作用：根据列名语义从候选列中选择最符合目标角色的列。
# Args: columns - 候选列名；keywords - 角色关键词。
# Returns: 命中的列名，没有命中时返回空字符串。
def _match_column(columns: list[str], keywords: tuple[str, ...]) -> str:
    logger.debug("语义列匹配入口", columns=columns, keywords=keywords)
    for column in columns:
        normalized = column.lower()
        if any(keyword in normalized for keyword in keywords):
            logger.debug("语义列匹配完成", column=column)
            return column
    logger.debug("语义列匹配未命中", keywords=keywords)
    return ""


# 方法作用：为每种处理器构造所需的列参数，避免专用处理器收到通用参数而静默返回空结果。
# Args: rows - 查询结果行；processor_name - 处理器名称；numeric_columns - 数值列名。
# Returns: 处理器 process() 可直接消费的参数字典。
def _build_processor_params(
    rows: list[dict], processor_name: str, numeric_columns: list[str],
) -> dict[str, str | int | float]:
    logger.debug("构造处理器参数入口", processor=processor_name, numeric_columns=numeric_columns)
    category_columns = _find_category_cols(rows)
    time_column = _find_time_col(rows) or ""
    params: dict[str, str | int | float] = {
        "value_col": numeric_columns[0] if numeric_columns else "",
        "group_col": category_columns[0] if category_columns else "",
        "name_col": category_columns[0] if category_columns else "",
        "time_col": time_column,
    }

    if processor_name == "correlation":
        params.update({
            "col1": numeric_columns[0] if len(numeric_columns) > 0 else "",
            "col2": numeric_columns[1] if len(numeric_columns) > 1 else "",
        })
    elif processor_name == "rfm":
        params.update({
            "recency_col": _match_column(numeric_columns, ("recency", "recent", "r_"))
            or (numeric_columns[0] if len(numeric_columns) > 0 else ""),
            "frequency_col": _match_column(numeric_columns, ("frequency", "freq", "f_"))
            or (numeric_columns[1] if len(numeric_columns) > 1 else ""),
            "monetary_col": _match_column(numeric_columns, ("monetary", "money", "amount", "m_"))
            or (numeric_columns[2] if len(numeric_columns) > 2 else ""),
        })
    elif processor_name == "budget_variance":
        params.update({
            "actual_col": _match_column(numeric_columns, ("actual", "real", "执行"))
            or (numeric_columns[0] if numeric_columns else ""),
            "budget_col": _match_column(numeric_columns, ("budget", "plan", "target", "预算"))
            or (numeric_columns[1] if len(numeric_columns) > 1 else ""),
        })
    elif processor_name == "cross_pivot":
        params.update({
            "row_col": category_columns[0] if category_columns else "",
            "col_col": category_columns[1] if len(category_columns) > 1 else "",
        })
    elif processor_name == "market_basket":
        params.update({
            "id_col": _match_column(category_columns, ("transaction", "order", "trade", "id"))
            or (category_columns[0] if category_columns else ""),
            "item_col": _match_column(category_columns, ("item", "product", "sku", "商品"))
            or (category_columns[1] if len(category_columns) > 1 else ""),
        })
    elif processor_name == "funnel":
        params["name_col"] = category_columns[0] if category_columns else ""
    elif processor_name == "ab_test":
        params["group_col"] = _match_column(category_columns, ("group", "variant", "version", "组")) \
            or (category_columns[0] if category_columns else "")
    elif processor_name == "contribution":
        previous_col = _match_column(
            numeric_columns,
            ("previous", "prev", "prior", "last_period", "上期", "同期"),
        )
        current_candidates = [column for column in numeric_columns if column != previous_col]
        current_col = _match_column(
            current_candidates,
            ("current", "actual", "present", "本期", "当期"),
        ) or (current_candidates[0] if current_candidates else "")
        if not previous_col:
            previous_col = numeric_columns[1] if len(numeric_columns) > 1 else ""
        params.update({
            "dimension_col": category_columns[0] if category_columns else "",
            "current_value_col": current_col,
            "previous_value_col": previous_col,
        })

    logger.debug("构造处理器参数完成", processor=processor_name, params=params)
    return params


def _find_time_col(rows):
    for k in rows[0]:
        if any(w in k.lower() for w in ("date", "time", "day", "month", "year", "dt", "created", "updated")):
            return k
    return None


def _group_by(rows, cat, val):
    agg = {}
    for r in rows:
        k = str(r.get(cat, "?"))
        v = r.get(val, 0) or 0
        agg[k] = agg.get(k, 0.0) + (
            float(v)
            if not isinstance(v, bool) and isinstance(v, (int, float, Decimal))
            else 0.0
        )
    return sorted(agg.items(), key=lambda x: x[1], reverse=True)


def _recommend_chart_type(rows, num_cols):
    if _find_time_col(rows) and num_cols:
        return "line"
    if _find_category_col(rows) and num_cols:
        return "bar"
    return "table"
