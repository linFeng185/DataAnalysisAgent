"""4.8 analyze_result Node — 统计引擎 + LLM 洞察。"""

from __future__ import annotations

import asyncio
import json
import time
from datetime import date, datetime
from decimal import Decimal

from src.graph.state import AnalysisState
from src.llm.client import get_task_llm as _get_task_llm
from src.llm.client import is_task_llm_available as _is_task_llm_available
from src.llm.prompt_budget import PromptSection, build_budgeted_prompt
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


# 方法作用：兼容旧测试和扩展点，同时检查分析任务的模型可用性。
# Args: 无。
# Returns: 本地分析模型或显式远程兜底可用时返回 True。
def is_llm_available() -> bool:
    """返回结果分析任务的模型可用状态。"""
    logger.debug("分析任务模型可用性入口")
    available = _is_task_llm_available("analyze_result")
    logger.info("分析任务模型可用性完成", available=available)
    return available


# 方法作用：兼容旧 Mock 接口并创建结果分析任务模型。
# Args: temperature - 分析文本生成温度。
# Returns: 按 analyze_result 任务策略创建的 ChatModel。
def get_llm(temperature: float = 0.3):
    """创建结果分析模型，默认使用快速本地模型。"""
    logger.debug("创建分析任务模型入口", temperature=temperature)
    model = _get_task_llm("analyze_result", temperature=temperature, reasoning=False)
    logger.info("创建分析任务模型完成")
    return model


async def analyze_result_node(state: AnalysisState) -> dict:
    """描述统计 + 趋势 + 异常 + 占比 → LLM 解读 (有 API Key) 或规则摘要。"""
    _start = time.monotonic()
    logger.info("节点开始", node="analyze_result")
    rows: list[dict] = state.get("query_result_sample", [])
    intent = state.get("intent", "query")
    sql = state.get("generated_sql", "")
    logger.info(
        "分析节点边界输入",
        intent=intent,
        data_rows=len(rows),
        has_sql=bool(sql),
        history_turns=len(state.get("conversation_history", []) or []),
    )

    if not rows:
        logger.info("节点完成", node="analyze_result", elapsed_ms=round((time.monotonic() - _start) * 1000))
        return {"analysis_result": {"summary": "无数据可供分析", "insights": [], "recommended_chart_type": "table", "follow_up_questions": [], "statistics": compute_statistics([])}}

    stats = compute_statistics(rows)

    # 数据处理器（脚本精确计算，LLM 仅润色）
    processor_result = None
    try:
        from src.tools.data_processor import get_processor
        proc = get_processor(intent, query=state.get("user_query", ""))
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

    # 处理器结果有效 — 直接用脚本结果，LLM 仅做自然语言润色
    if processor_result and processor_result.data:
        result = {
            "summary": processor_result.summary,
            "insights": processor_result.insights,
            "recommended_chart_type": processor_result.chart_type,
            "follow_up_questions": [],
            "processor_name": getattr(proc, "name", "unknown"),
            "statistics": stats,
        }
        result = _attach_skill_outputs(result, rows, state.get("activated_skills", []) or [])
        # LLM 润色总结（不参与数值计算）
        if _is_task_llm_available("polish_result"):
            try:
                data_sample = _to_compact(processor_result.data)
                llm_polish = await _llm_polish(processor_result.summary, processor_result.insights, data_sample)
                if llm_polish:
                    result["summary"] = llm_polish.get("summary", result["summary"])
            except Exception as exc:
                from src.failure_policy import FailureDomain, fallback_allowed

                if not fallback_allowed(FailureDomain.LLM):
                    raise
                logger.error("LLM 润色失败", error=str(exc), exc_info=True)
        result, skill_calls = await _execute_post_analysis_skill_tools(result, rows, state)
        logger.info("节点完成", node="analyze_result", processor=result.get("processor_name"),
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
            user_query=state.get("user_query", ""),
            result_full_count=state.get("query_result_full_count", len(rows)),
            result_truncated=state.get("query_result_truncated", False),
            business_context=business_context,
            chart_type=chart_type,
            intent=intent,
        )
    else:
        result = _rule_analyze(rows, stats, trend_info, outlier_info, conc_info, chart_type, intent)

    result["statistics"] = stats
    result = _attach_skill_outputs(result, rows, state.get("activated_skills", []) or [])
    result, skill_calls = await _execute_post_analysis_skill_tools(result, rows, state)
    logger.info("节点完成", node="analyze_result", elapsed_ms=round((time.monotonic() - _start) * 1000))
    return {"analysis_result": result, "skill_tool_calls": skill_calls}


async def _llm_polish(summary: str, insights: list[str], data_sample: str) -> dict | None:
    """LLM 对处理器输出做自然语言润色，不参与数值计算。"""
    try:
        from langchain_core.messages import HumanMessage, SystemMessage
        llm = _get_task_llm("polish_result", temperature=0.3, reasoning=False)
        prompt = build_budgeted_prompt(
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
        )
        resp = await llm.ainvoke([
            SystemMessage(content=prompt.system),
            HumanMessage(content=prompt.human),
        ])
        from src.llm.output_contracts import PolishOutput, parse_json_model

        return parse_json_model(resp.content, PolishOutput).model_dump()
    except Exception as exc:
        logger.error("LLM 润色异常", error=str(exc), exc_info=True)
        return None


def _to_compact(rows: list[dict]) -> str:
    """全量数据转紧凑 JSON，超限按比例均匀抽取。上限通过 ANALYSIS_DATA_MAX_CHARS 环境变量配置。"""
    if not rows:
        return "[]"
    from src.config import get_settings
    limit = get_settings().analysis_data_max_chars
    text = json.dumps(rows, ensure_ascii=False, default=_json_default,
                      separators=(",", ":"))
    if len(text) <= limit:
        return text
    # 超限：按比例均匀取点，保证覆盖面
    row_size = len(text) / len(rows)
    max_rows = max(10, int(limit / row_size))
    step = max(1, len(rows) // max_rows)
    sampled = rows[::step]
    if rows[0] not in sampled:
        sampled.insert(0, rows[0])
    if rows[-1] not in sampled:
        sampled.append(rows[-1])
    return json.dumps(sampled, ensure_ascii=False, default=_json_default,
                      separators=(",", ":"))


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
        logger.info("附加 Skill 分析产物跳过", reason="未激活数据质量 Skill")
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
    tools = state.get("skill_tools", []) or []
    used = int(state.get("skill_tool_calls", 0) or 0)
    budget = int(state.get("skill_tool_budget", 0) or 0)
    limit = max(0, budget)
    if not tools:
        logger.info("分析后 Skill 工具跳过", reason="无已授权工具")
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
            query = str(state.get("user_query", "") or "")
            template = "monthly_report" if "月报" in query else "weekly_report"
            metrics = {
                key: value
                for key, value in (rows[0] if rows else {}).items()
                if not isinstance(value, bool) and isinstance(value, (int, float, Decimal))
            }
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
            logger.info("分析后 Skill 工具跳过", tool=name, reason="未注册参数适配器")
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

    input_label = f"输入全量 {total_rows} 行" if is_full else f"输入均匀采样 {sample_row_count}/{total_rows} 行"
    database_count = result_full_count if result_full_count is not None else total_rows
    completeness = (
        f"数据库查询返回 {database_count} 行；{input_label}；"
        f"安全截断={'是' if result_truncated else '否'}。"
    )
    business_block = business_context or "(无额外业务口径，以 SQL 与统计结果为准)"
    question = user_query or "(未提供原问题，仅解释当前结果)"
    prompt = build_budgeted_prompt(
        "analysis.result",
        [
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
                "deterministic_findings",
                f"## 确定性分析器发现\n{trend} | {outlier} | {conc}",
                priority=80,
                min_chars=200,
                max_chars=800,
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
        ],
    )

    try:
        from langchain_core.messages import HumanMessage, SystemMessage
        from src.llm.adapters.registry import get_adapter
        from src.config import get_settings
        llm = get_llm(temperature=0.3)
        resp = await llm.ainvoke([
            SystemMessage(content=prompt.system),
            HumanMessage(content=prompt.human),
        ])
        adapter = get_adapter(get_settings().llm_model)
        parsed = adapter.parse_response(resp)
        parsed_content = str(getattr(parsed, "content", "") or "").strip()
        if not parsed_content:
            logger.warning("适配器未返回正文，兼容原始响应", model=get_settings().llm_model)
            parsed_content = str(getattr(resp, "content", "") or "").strip()
        from src.llm.output_contracts import AnalysisOutput, parse_json_model

        result = parse_json_model(parsed_content, AnalysisOutput).model_dump()
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
        )
        return result
    except Exception as e:
        logger.error("LLM 分析失败", error=str(e), exc_info=True)
        return _rule_analyze(rows, stats, trend, outlier, conc, chart_type, intent)


def _rule_analyze(rows, stats, trend, outlier, conc, chart_type, intent) -> dict:
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
    return {"summary": summary, "insights": insights, "recommended_chart_type": chart_type, "follow_up_questions": fups}


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
    logger.info("分类列识别完成", columns=result)
    return result


# 方法作用：根据列名语义从候选列中选择最符合目标角色的列。
# Args: columns - 候选列名；keywords - 角色关键词。
# Returns: 命中的列名，没有命中时返回空字符串。
def _match_column(columns: list[str], keywords: tuple[str, ...]) -> str:
    logger.debug("语义列匹配入口", columns=columns, keywords=keywords)
    for column in columns:
        normalized = column.lower()
        if any(keyword in normalized for keyword in keywords):
            logger.info("语义列匹配完成", column=column)
            return column
    logger.info("语义列匹配未命中", keywords=keywords)
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

    logger.info("构造处理器参数完成", processor=processor_name, params=params)
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
