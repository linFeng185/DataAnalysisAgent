"""5.1.7 ChartGeneratorTool — 封装图表生成逻辑供 Agent 调用。

依据: SPEC §14 可视化引擎
"""

from __future__ import annotations

from decimal import Decimal
from typing import Any

from langchain_core.tools import BaseTool

from src.logging_config import get_logger

logger = get_logger(__name__)

_SUPPORTED_CHART_TYPES = frozenset({"auto", "line", "bar", "pie", "scatter", "heatmap", "table"})
_CHART_TYPE_HINTS: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("heatmap", ("热力图", "heatmap", "heat map")),
    ("scatter", ("散点图", "scatter")),
    ("line", ("折线图", "趋势图", "line")),
    ("bar", ("柱状图", "条形图", "bar")),
    ("pie", ("饼图", "环形图", "pie", "donut")),
    ("table", ("表格", "table")),
    ("auto", ("自动推荐", "自动选择", "auto")),
)


# 方法作用：把自然语言图表调整指令解析为受支持的白名单类型。
# Args: instruction - 用户输入的图表类型或自然语言调整指令。
# Returns: auto/line/bar/pie/scatter/heatmap/table 之一。
def resolve_chart_type_instruction(instruction: str) -> str:
    normalized = str(instruction or "").strip().lower()
    if normalized in _SUPPORTED_CHART_TYPES:
        return normalized
    for chart_type, hints in _CHART_TYPE_HINTS:
        if any(hint in normalized for hint in hints):
            return chart_type
    raise ValueError("无法识别图表类型，请选择受支持的图表")


class ChartGeneratorTool(BaseTool):
    """图表生成工具 — 根据数据自动推荐并生成 ECharts 图表配置。"""

    name: str = "chart_generator"
    description: str = (
        "根据查询结果自动推荐图表类型并生成 ECharts 配置。"
        "输入: {\"rows\": [{\"col\": val, ...}], \"chart_type\": \"auto\"}。"
        "chart_type 可选: auto/line/bar/pie/scatter/heatmap/table。"
    )

    def _run(
        self,
        rows: list[dict] | str,
        chart_type: str = "auto",
        run_manager: Any = None,
    ) -> dict:
        import json
        if isinstance(rows, str):
            try:
                rows = json.loads(rows)
            except json.JSONDecodeError:
                return {"error": "rows 参数需为 JSON 数组"}
        if not rows:
            return {"error": "rows 为空"}

        if chart_type not in _SUPPORTED_CHART_TYPES:
            return {"error": f"不支持的图表类型: {chart_type}"}

        logger.info("图表生成工具调用", row_count=len(rows), chart_type=chart_type)
        try:
            resolved = chart_type if chart_type != "auto" else _classify_chart_type(rows)
            return {"recommended_chart_type": resolved, "option": _build_option(rows, resolved)}
        except Exception as e:
            logger.error("图表生成失败", error=str(e), exc_info=True)
            return {"error": str(e)}


# 方法作用：根据时间、文本和数值列组合推荐图表类型。
# Args: rows - 非空查询结果行。
# Returns: line/bar/pie/scatter/table 之一。
def _classify_chart_type(rows: list[dict]) -> str:
    """14.1 智能选图 — 时间+数值→line / 分类+数值→bar / 少数类目→pie。"""
    logger.debug("图表类型分类入口", row_count=len(rows))
    cols = list(rows[0].keys())
    has_time = any(
        w in c.lower() for c in cols
        for w in ("date", "time", "day", "month", "year", "dt", "created", "updated")
    )
    numeric, text_cols = _infer_column_types(rows, cols)
    if has_time and numeric:
        logger.info("图表类型分类完成", chart_type="line")
        return "line"
    if text_cols and numeric:
        unique = len({r.get(text_cols[0]) for r in rows})
        result = "pie" if unique <= 8 else "bar"
        logger.info("图表类型分类完成", chart_type=result)
        return result
    if len(numeric) >= 2:
        logger.info("图表类型分类完成", chart_type="scatter")
        return "scatter"
    logger.info("图表类型分类完成", chart_type="table")
    return "table"


# 方法作用：根据查询行和图表类型构建 ECharts option。
# Args: rows - 非空查询结果行；chart_type - 已选择图表类型。
# Returns: ECharts 配置字典或 table 数据配置。
def _build_option(rows: list[dict], chart_type: str) -> dict:
    """14.2~14.6 生成 ECharts option JSON。"""
    logger.debug("图表配置构建入口", row_count=len(rows), chart_type=chart_type)
    cols = list(rows[0].keys())
    numeric, text_cols = _infer_column_types(rows, cols)
    label_col = text_cols[0] if text_cols else cols[0]
    value_col = numeric[0] if numeric else cols[-1]
    labels = [str(r.get(label_col, "")) for r in rows]
    values = [float(r.get(value_col, 0) or 0) for r in rows] if numeric else []

    base: dict[str, Any] = {
        "tooltip": {"trigger": "axis" if chart_type in ("line", "bar") else "item"},
    }

    if chart_type == "line":
        base["xAxis"] = {"type": "category", "data": labels}
        base["yAxis"] = {"type": "value"}
        base["series"] = [{"name": value_col, "type": "line", "data": values}]
    elif chart_type == "bar":
        base["xAxis"] = {"type": "category", "data": labels}
        base["yAxis"] = {"type": "value"}
        base["series"] = [{"name": value_col, "type": "bar", "data": values}]
    elif chart_type == "pie":
        base["series"] = [{
            "name": value_col, "type": "pie",
            "data": [
                {"name": label, "value": value}
                for label, value in zip(labels, values, strict=False)
            ],
        }]
    elif chart_type == "scatter" and len(numeric) >= 2:
        base["xAxis"] = {"type": "value"}
        base["yAxis"] = {"type": "value"}
        base["series"] = [{
            "name": value_col, "type": "scatter",
            "data": [[float(r.get(numeric[0], 0) or 0), float(r.get(numeric[1], 0) or 0)]
                      for r in rows],
        }]
    elif chart_type == "heatmap":
        if len(cols) < 3 or not numeric:
            raise ValueError("热力图需要两个维度列和一个数值列")
        value_col = numeric[-1]
        dimensions = [column for column in cols if column != value_col]
        if len(dimensions) < 2:
            raise ValueError("热力图需要两个维度列")
        x_col, y_col = dimensions[0], dimensions[1]
        x_labels = list(dict.fromkeys(str(row.get(x_col, "")) for row in rows))
        y_labels = list(dict.fromkeys(str(row.get(y_col, "")) for row in rows))
        x_index = {label: index for index, label in enumerate(x_labels)}
        y_index = {label: index for index, label in enumerate(y_labels)}
        heat_data = [
            [
                x_index[str(row.get(x_col, ""))],
                y_index[str(row.get(y_col, ""))],
                float(row.get(value_col, 0) or 0),
            ]
            for row in rows
        ]
        values_for_range = [item[2] for item in heat_data]
        base["tooltip"] = {"position": "top"}
        base["xAxis"] = {"type": "category", "data": x_labels, "splitArea": {"show": True}}
        base["yAxis"] = {"type": "category", "data": y_labels, "splitArea": {"show": True}}
        base["visualMap"] = {
            "min": min(values_for_range),
            "max": max(values_for_range),
            "calculable": True,
            "orient": "horizontal",
            "left": "center",
            "bottom": 0,
        }
        base["series"] = [{
            "name": value_col,
            "type": "heatmap",
            "data": heat_data,
            "label": {"show": True},
        }]
    else:
        base["columns"] = [{"field": c, "title": c} for c in cols]
        base["rows"] = rows[:50]

    logger.info("图表配置构建完成", chart_type=chart_type, series_count=len(base.get("series", [])))
    return base


# 方法作用：跳过 NULL 后从最多 100 个有效值推断数值列和文本列。
# Args: rows - 查询结果行；columns - 待检查列名。
# Returns: 数值列名列表和文本列名列表。
def _infer_column_types(
    rows: list[dict],
    columns: list[str],
) -> tuple[list[str], list[str]]:
    """避免固定查看前五行导致 NULL 前缀误判列类型。"""
    logger.debug("图表列类型推断入口", row_count=len(rows), column_count=len(columns))
    try:
        numeric: list[str] = []
        text: list[str] = []
        for column in columns:
            samples = [row.get(column) for row in rows if row.get(column) is not None][:100]
            if not samples:
                logger.info("图表列类型推断跳过", column=column, reason="无有效值")
                continue
            if all(_is_numeric_value(value) for value in samples):
                numeric.append(column)
            elif all(isinstance(value, str) for value in samples):
                text.append(column)
    except Exception as exc:
        logger.error("图表列类型推断失败", error=str(exc), exc_info=True)
        raise
    logger.info("图表列类型推断完成", numeric=numeric, text=text)
    return numeric, text


# 方法作用：判断值能否安全转换为图表数值。
# Args: value - 待判断值。
# Returns: int/float 或合法十进制字符串返回 True。
def _is_numeric_value(value: Any) -> bool:
    logger.debug("图表数值判断入口", value_type=type(value).__name__)
    result = isinstance(value, (int, float, Decimal)) and not isinstance(value, bool)
    if isinstance(value, str):
        try:
            float(value)
            result = True
        except ValueError:
            result = False
    logger.debug("图表数值判断完成", numeric=result)
    return result
