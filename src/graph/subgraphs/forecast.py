"""确定性预测独立能力子图。"""

from __future__ import annotations

import re
from decimal import Decimal
from typing import Any

from langgraph.graph import END, StateGraph

from src.graph.context import read_contexts
from src.graph.state import AnalysisState
from src.logging_config import get_logger
from src.tools.forecasting import ForecastingError, forecast_rows

logger = get_logger(__name__)


def _forecast_columns(rows: list[dict[str, Any]]) -> tuple[str, str]:
    """按列名和数据类型选择时间列与预测数值列。"""
    if not rows:
        return "", ""
    columns = list(rows[0])
    time_column = next(
        (column for column in columns if any(marker in column.lower() for marker in ("date", "time", "day", "month", "日期", "时间"))),
        columns[0] if columns else "",
    )
    value_column = next(
        (
            column
            for column in columns
            if column != time_column
            and not any(marker in column.lower() for marker in ("id", "编号", "序号"))
            and any(
                not isinstance(row.get(column), bool)
                and isinstance(row.get(column), (int, float, Decimal))
                for row in rows
            )
        ),
        "",
    )
    return time_column, value_column


async def forecast_node(state: AnalysisState) -> dict:
    """对 SQL 结果执行时间有序预测、回测和区间估计。"""
    contexts = read_contexts(state)
    rows = list(state.get("query_result_sample", []) or [])
    time_column, value_column = _forecast_columns(rows)
    match = re.search(r"(?:未来|预测)\s*(\d+)\s*(?:期|天|日|周|月|个)?", contexts.request.user_query)
    horizon = min(30, max(1, int(match.group(1)))) if match else 3
    logger.info(
        "预测子图执行开始",
        rows=len(rows),
        time_column=time_column,
        value_column=value_column,
        horizon=horizon,
    )
    try:
        if not time_column or not value_column:
            raise ForecastingError("预测需要一个时间列和一个数值列")
        forecast = forecast_rows(
            rows,
            time_col=time_column,
            value_col=value_column,
            horizon=horizon,
        ).to_dict()
    except (ForecastingError, ValueError, TypeError) as exc:
        logger.warning("预测子图输入不足", error=str(exc))
        return {
            "final_response": {
                "success": False,
                "status": "failed",
                "source": "forecast",
                "user_query": contexts.request.user_query,
                "error_code": "FORECAST_INPUT_INVALID",
                "error_message": "当前结果不满足预测所需的时间序列条件",
                "sql": state.get("generated_sql", ""),
                "data": rows,
                "analysis": {"summary": "当前结果不满足预测所需的时间序列条件"},
                "chart": {"type": "table", "option": {}},
            }
        }
    analysis = dict(state.get("analysis_result", {}) or {})
    analysis["forecast"] = forecast
    analysis["summary"] = (
        f"{analysis.get('summary', '').strip()} "
        f"已使用 {forecast.get('model', '基线模型')} 预测未来 {horizon} 期。"
    ).strip()
    analysis.setdefault("limitations", []).append("预测基于历史模式和回测，不构成确定性承诺。")
    labels = forecast.get("forecast_labels", []) or [f"预测{index + 1}" for index in range(horizon)]
    chart = {
        "type": "line",
        "option": {
            "xAxis": {"type": "category", "data": labels},
            "yAxis": {"type": "value"},
            "series": [{"type": "line", "name": value_column, "data": forecast.get("predictions", [])}],
        },
    }
    logger.info("预测子图执行完成", model=forecast.get("model", ""), horizon=horizon)
    return {"analysis_result": analysis, "chart_config": chart}


def build_forecast_subgraph():
    """构建预测执行子图。"""
    graph = StateGraph(AnalysisState)
    graph.add_node("forecast", forecast_node)
    graph.set_entry_point("forecast")
    graph.add_edge("forecast", END)
    return graph.compile()
