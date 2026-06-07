"""5.1.7 ChartGeneratorTool — 封装图表生成逻辑供 Agent 调用。

依据: SPEC §14 可视化引擎
"""

from __future__ import annotations

from typing import Any

from langchain_core.tools import BaseTool

from src.logging_config import get_logger

logger = get_logger(__name__)


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

        logger.info("图表生成工具调用", row_count=len(rows), chart_type=chart_type)
        try:
            resolved = chart_type if chart_type != "auto" else _classify_chart_type(rows)
            return {"recommended_chart_type": resolved, "option": _build_option(rows, resolved)}
        except Exception as e:
            logger.error("图表生成失败", error=str(e))
            return {"error": str(e)}


def _classify_chart_type(rows: list[dict]) -> str:
    """14.1 智能选图 — 时间+数值→line / 分类+数值→bar / 少数类目→pie。"""
    cols = list(rows[0].keys())
    has_time = any(
        w in c.lower() for c in cols
        for w in ("date", "time", "day", "month", "year", "dt", "created", "updated")
    )
    numeric = [
        c for c in cols if all(
            isinstance(r.get(c), (int, float)) or
            (isinstance(r.get(c), str) and r.get(c, "").replace(".", "", 1).replace("-", "", 1).isdigit())
            for r in rows[:5]
        )
    ]
    text_cols = [c for c in cols if all(isinstance(r.get(c), str) for r in rows[:5] if r.get(c) is not None)]
    if has_time and numeric:
        return "line"
    if text_cols and numeric:
        unique = len({r.get(text_cols[0]) for r in rows})
        return "pie" if unique <= 8 else "bar"
    if len(numeric) >= 2:
        return "scatter"
    return "table"


def _build_option(rows: list[dict], chart_type: str) -> dict:
    """14.2~14.6 生成 ECharts option JSON。"""
    cols = list(rows[0].keys())
    numeric = [c for c in cols if all(isinstance(r.get(c), (int, float)) for r in rows[:5])]
    text_cols = [c for c in cols if all(isinstance(r.get(c), str) for r in rows[:5] if r.get(c) is not None)]
    label_col = text_cols[0] if text_cols else cols[0]
    value_col = numeric[0] if numeric else cols[-1]
    labels = [str(r.get(label_col, "")) for r in rows]
    values = [float(r.get(value_col, 0) or 0) for r in rows]

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
            "data": [{"name": l, "value": v} for l, v in zip(labels, values)],
        }]
    elif chart_type == "scatter" and len(numeric) >= 2:
        base["xAxis"] = {"type": "value"}
        base["yAxis"] = {"type": "value"}
        base["series"] = [{
            "name": value_col, "type": "scatter",
            "data": [[float(r.get(numeric[0], 0) or 0), float(r.get(numeric[1], 0) or 0)]
                      for r in rows],
        }]
    else:
        base["columns"] = [{"field": c, "title": c} for c in cols]
        base["rows"] = rows[:50]

    return base
