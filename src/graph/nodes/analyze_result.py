"""4.8 analyze_result Node — 统计引擎 + LLM 洞察。"""

from __future__ import annotations

import json
from datetime import date, datetime
from decimal import Decimal

from src.graph.state import AnalysisState
from src.llm.client import get_llm, is_llm_available
from src.llm.prompts import DATA_ANALYSIS_SYSTEM
from src.logging_config import get_logger


def _json_default(obj):
    """处理 date/datetime/Decimal，供 json.dumps 使用。"""
    if isinstance(obj, (datetime, date)):
        return obj.isoformat()
    if isinstance(obj, Decimal):
        return float(obj)
    raise TypeError(f"Type {type(obj)} not serializable")
from src.tools.analyzer import (
    compute_concentration,
    compute_statistics,
    compute_trend,
    detect_outliers_zscore,
    _extract,
    _find_numeric,
)

logger = get_logger(__name__)


async def analyze_result_node(state: AnalysisState) -> dict:
    """描述统计 + 趋势 + 异常 + 占比 → LLM 解读 (有 API Key) 或规则摘要。"""
    rows: list[dict] = state.get("query_result_sample", [])
    intent = state.get("intent", "query")
    sql = state.get("generated_sql", "")

    if not rows:
        return {"analysis_result": {"summary": "无数据可供分析", "insights": [], "recommended_chart_type": "table", "follow_up_questions": []}}

    stats = compute_statistics(rows)
    numeric_cols = stats.get("numeric_columns", [])

    # 统计计算
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

    # LLM 分析 (如有 API Key)
    if is_llm_available():
        result = await _llm_analyze(rows, sql, stats, trend_info, outlier_info, conc_info)
    else:
        result = _rule_analyze(rows, stats, trend_info, outlier_info, conc_info, chart_type, intent)

    result["statistics"] = stats
    return {"analysis_result": result}


async def _llm_analyze(rows, sql, stats, trend, outlier, conc) -> dict:
    """LLM 生成分析报告。"""
    sample = json.dumps(rows[:20], ensure_ascii=False, indent=2, default=_json_default)
    stat_text = json.dumps(stats.get("columns", {}), ensure_ascii=False, default=_json_default)

    user_msg = f"""## 执行的 SQL
```sql
{sql}
```

## 查询结果 (前 20 行)
{sample}

## 统计摘要
{stat_text}

## 发现
{trend} | {outlier} | {conc}

请给出分析报告。"""

    try:
        from langchain_core.messages import HumanMessage, SystemMessage
        from src.llm.adapters.registry import get_adapter
        from src.config import get_settings
        llm = get_llm(temperature=0.3)
        resp = await llm.ainvoke([SystemMessage(content=DATA_ANALYSIS_SYSTEM), HumanMessage(content=user_msg)])
        adapter = get_adapter(get_settings().llm_model)
        parsed = adapter.parse_response(resp)
        data = json.loads(resp.content.strip().removeprefix("```json").removesuffix("```").strip())
        result = {
            "summary": data.get("summary", ""),
            "insights": data.get("insights", []),
            "recommended_chart_type": data.get("recommended_chart_type", "table"),
            "follow_up_questions": data.get("follow_up_questions", []),
        }
        if parsed.reasoning_content:
            result["analysis_reasoning_content"] = parsed.reasoning_content
        return result
    except Exception as e:
        logger.error("LLM 分析失败", error=str(e))
        return _rule_analyze(rows, stats, trend, outlier, conc, "table", "query")


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
        agg[k] = agg.get(k, 0) + (float(v) if isinstance(v, (int, float)) else 0)
    return sorted(agg.items(), key=lambda x: x[1], reverse=True)


def _recommend_chart_type(rows, num_cols):
    if _find_time_col(rows) and num_cols:
        return "line"
    if _find_category_col(rows) and num_cols:
        return "bar"
    return "table"
