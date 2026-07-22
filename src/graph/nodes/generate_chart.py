"""4.9 generate_chart Node — 根据数据和分析结果生成 ECharts 配置。"""

from __future__ import annotations

import time
from decimal import Decimal

from src.graph.state import AnalysisState
from src.logging_config import get_logger

logger = get_logger(__name__)
_NUMERIC_TYPES = (int, float, Decimal)


# 方法作用：从结果行中寻找指定列首个非布尔数值。
# Args: rows - 查询结果行；column - 待检查列名。
# Returns: 首个数值；不存在时返回 None。
def _first_numeric_value(rows: list[dict], column: str):
    """忽略前置空值识别实际数值列。"""
    logger.debug("图表数值样本查找入口", column=column, row_count=len(rows))
    for row in rows:
        value = row.get(column)
        if isinstance(value, _NUMERIC_TYPES) and not isinstance(value, bool):
            logger.info("图表数值样本查找完成", column=column, found=True)
            return value
    logger.info("图表数值样本查找完成", column=column, found=False)
    return None


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


_SUPPORTED_TYPES = ("bar", "line", "pie", "scatter", "table")


# 根据分析推荐和数据列类型选择图表类型。
# Args: analysis - 分析节点输出；data - 查询结果数据。
# Returns: 受支持的 ECharts 图表类型。
def _pick_type(analysis: dict, data: list[dict]) -> str:
    rec = analysis.get("recommended_chart_type", "")
    logger.debug("图表类型选择入口", recommended=rec, data_rows=len(data))
    if rec in _SUPPORTED_TYPES:
        logger.info("图表类型选择完成", chart_type=rec, source="analysis")
        return rec
    if not data:
        logger.info("图表类型选择完成", chart_type="table", source="empty_data")
        return "table"
    cols = list(data[0].keys())
    if len(cols) >= 2:
        for col in cols[1:]:
            value = _first_numeric_value(data, col)
            if value is not None:
                low = col.lower()
                if any(w in low for w in ("phone", "tel", "mobile", "手机", "电话")):
                    logger.info("图表数值列跳过", column=col, reason="联系方式")
                    continue
                if (any(w in low for w in ("id", "no", "编号", "序号"))
                        and isinstance(value, int) and value > 999):
                    logger.info("图表数值列跳过", column=col, reason="标识符")
                    continue
                logger.info("图表类型选择完成", chart_type="bar", source="numeric_column")
                return "bar"
    logger.info("图表类型选择完成", chart_type="table", source="no_numeric_column")
    return "table"


def _build_echarts_option(rows: list[dict], chart_type: str) -> dict:
    if not rows:
        return {}
    keys = list(rows[0].keys())
    if chart_type in ("pie",):
        return _pie_option(rows, keys)
    if chart_type in ("line", "bar", "scatter"):
        return _axis_option(rows, keys, chart_type)
    return {}


def _pie_option(rows: list[dict], keys: list[str]) -> dict:
    name_col = keys[0]
    val_col = keys[1] if len(keys) > 1 else None
    if not val_col:
        return {}
    items = [{"name": str(r.get(name_col, "")), "value": _to_num(r.get(val_col))}
             for r in rows[:30]]
    return {
        "tooltip": {"trigger": "item"},
        "series": [{"type": "pie", "radius": "60%", "data": items}],
    }


# 构建坐标轴图表配置，并支持 Decimal 数值及交叉分组数据。
# Args: rows - 查询结果行；keys - 数据列顺序；chart_type - 图表类型。
# Returns: 可直接传给 ECharts 的 option；无数值列时返回空字典。
def _axis_option(rows: list[dict], keys: list[str], chart_type: str) -> dict:
    logger.debug("坐标轴配置构建入口", chart_type=chart_type, row_count=len(rows), columns=keys)
    # 找数值列
    numeric_col = None
    for col in keys[1:]:
        if _first_numeric_value(rows, col) is not None:
            numeric_col = col
            break
    if not numeric_col:
        logger.warning("坐标轴配置回退", reason="无数值列", columns=keys)
        return {}
    x_col = keys[0]

    # 检测交叉透视：X 列值大量重复 → 应按第三列分组
    x_vals = [str(r.get(x_col, "")) for r in rows]
    uniq_x = len(set(x_vals))
    is_cross = uniq_x < len(rows) * 0.6 and uniq_x > 1 and len(keys) >= 3
    group_col = None
    if is_cross:
        for col in keys[1:]:
            if col == numeric_col:
                continue
            if any(isinstance(row.get(col), str) for row in rows):
                group_col = col
                break

    if is_cross and group_col:
        # 交叉透视：按分组列拆多系列
        group_vals = sorted(set(str(r.get(group_col, "")) for r in rows))
        if len(group_vals) > 12:
            group_vals = group_vals[:12]
        categories = sorted(set(x_vals))
        series = []
        for gv in group_vals:
            data_map = {}
            for r in rows:
                if str(r.get(group_col, "")) == gv:
                    data_map[str(r.get(x_col, ""))] = _to_num(r.get(numeric_col))
            vals = [data_map.get(c, 0) for c in categories]
            series.append({"name": gv, "type": chart_type, "data": vals,
                           "smooth": chart_type == "line"})
        option = {
            "tooltip": {"trigger": "axis"},
            "legend": {"data": group_vals, "bottom": 0},
            "xAxis": {"type": "category", "data": categories,
                       "axisLabel": {"rotate": len(categories) > 8 and 45 or 0}},
            "yAxis": {"type": "value"},
            "series": series,
        }
        logger.info("坐标轴配置构建完成", mode="cross", series_count=len(series), category_count=len(categories))
        return option

    # 普通模式
    if len(rows) > 30:
        rows = rows[:25]
    categories = [str(r.get(x_col, "")) for r in rows]
    vals = [_to_num(r.get(numeric_col)) for r in rows]
    option = {
        "tooltip": {"trigger": "axis"},
        "xAxis": {"type": "category", "data": categories,
                   "axisLabel": {"rotate": len(categories) > 8 and 45 or 0}},
        "yAxis": {"type": "value"},
        "series": [{"type": chart_type, "data": vals,
                     "smooth": chart_type == "line"}],
    }
    logger.info("坐标轴配置构建完成", mode="standard", series_count=1, category_count=len(categories))
    return option


def _to_num(val) -> float:
    if val is None:
        return 0
    if isinstance(val, (int, float)) and not isinstance(val, bool):
        return float(val)
    try:
        return float(str(val))
    except (ValueError, TypeError):
        return 0
