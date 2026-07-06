"""4.9 generate_chart Node — 根据数据和分析结果生成 ECharts 配置。"""

from __future__ import annotations

import time

from src.graph.state import AnalysisState
from src.logging_config import get_logger

logger = get_logger(__name__)


async def generate_chart_node(state: AnalysisState) -> dict:
    _start = time.monotonic()
    logger.info("节点开始", node="generate_chart")

    data = state.get("query_result_sample", []) or []
    analysis = state.get("analysis_result", {}) or {}
    chart_type = _pick_type(analysis, data)

    if not data or len(data) == 0:
        logger.info("节点完成（无数据）", node="generate_chart",
                    elapsed_ms=round((time.monotonic() - _start) * 1000))
        return {"chart_config": {"type": chart_type, "option": {}}}

    option = _build_echarts_option(data, chart_type)

    elapsed = round((time.monotonic() - _start) * 1000)
    logger.info("节点完成", node="generate_chart", elapsed_ms=elapsed,
                chart_type=chart_type, data_rows=len(data))
    return {"chart_config": {"type": chart_type, "option": option}}


# 支持的图表类型列表（LLM 只能从这里面选）
_SUPPORTED_TYPES = ("bar", "line", "pie", "scatter", "table")


def _pick_type(analysis: dict, data: list[dict]) -> str:
    """选择图表类型：LLM 推荐优先，仅做白名单校验。"""
    rec = analysis.get("recommended_chart_type", "")
    if rec in _SUPPORTED_TYPES:
        return rec
    # LLM 推荐不合法或为空 → 简单回退
    if not data:
        return "table"
    cols = list(data[0].keys())
    if len(cols) >= 2:
        v = list(data[0].values())[1]
        if isinstance(v, (int, float)) and not isinstance(v, bool):
            return "bar"
    return "table"


def _build_echarts_option(rows: list[dict], chart_type: str) -> dict:
    """从查询结果构建 ECharts option。"""
    if not rows:
        return {}
    keys = list(rows[0].keys())
    if chart_type in ("pie",):
        return _pie_option(rows, keys)
    if chart_type in ("line", "bar", "scatter"):
        return _axis_option(rows, keys, chart_type)
    return {}


def _pie_option(rows: list[dict], keys: list[str]) -> dict:
    """饼图：第一列标签，第二列数值。"""
    name_col = keys[0]
    val_col = keys[1] if len(keys) > 1 else None
    if not val_col:
        return {}
    items = [{"name": str(r.get(name_col, "")), "value": _to_num(r.get(val_col))}
             for r in rows[:30]]
    return {
        "title": {"text": "", "left": "center"},
        "tooltip": {"trigger": "item"},
        "series": [{"type": "pie", "radius": "60%", "data": items}],
    }


def _axis_option(rows: list[dict], keys: list[str], chart_type: str) -> dict:
    """柱状/折线/散点图：第一列 X 轴，其余列 Y 轴系列。"""
    x_col = keys[0]
    y_cols = keys[1:] if len(keys) > 1 else [keys[0]]
    categories = [str(r.get(x_col, "")) for r in rows[:50]]
    series = []
    for col in y_cols:
        vals = [_to_num(r.get(col)) for r in rows[:50]]
        if vals:
            series.append({"name": col, "type": chart_type, "data": vals,
                           "smooth": chart_type == "line"})
    return {
        "title": {"text": "", "left": "center"},
        "tooltip": {"trigger": "axis"},
        "xAxis": {"type": "category", "data": categories,
                   "axisLabel": {"rotate": len(categories) > 8 and 45 or 0}},
        "yAxis": {"type": "value"},
        "series": series,
    }


def _to_num(val) -> float:
    """安全转为数值。"""
    if val is None:
        return 0
    if isinstance(val, (int, float)) and not isinstance(val, bool):
        return float(val)
    try:
        return float(str(val))
    except (ValueError, TypeError):
        return 0
