"""可审计的时间序列分析工具。"""

from __future__ import annotations

import math
from datetime import datetime
from typing import Any

from langchain_core.tools import BaseTool

from src.logging_config import get_logger
from src.tools.analyzer import compute_trend
from src.tools.forecasting import forecast_series

logger = get_logger(__name__)


class AnalyzeTimeseriesTool(BaseTool):
    """校验时间序列并执行趋势、回测和区间预测。"""

    name: str = "analyze_timeseries"
    description: str = "验证严格递增时间序列，比较基线模型并输出回测指标、预测值和预测区间。"

    def _run(self, *args: Any, **kwargs: Any) -> dict[str, Any]:
        """拒绝同步入口。"""
        del args, kwargs
        raise RuntimeError("analyze_timeseries 只允许异步执行")

    async def _arun(
        self,
        rows: list[dict[str, Any]],
        time_column: str,
        value_column: str,
        horizon: int = 3,
        confidence_level: float = 0.8,
        run_manager: Any = None,
    ) -> dict[str, Any]:
        """在无时间泄漏的前提下生成单变量预测。"""
        del run_manager
        logger.info(
            "时间序列分析开始",
            row_count=len(rows),
            time_column=time_column,
            value_column=value_column,
            horizon=horizon,
        )
        if len(rows) < 4:
            raise ValueError("时间序列至少需要 4 行数据")
        if not 1 <= horizon <= 30:
            raise ValueError("horizon 必须在 1 到 30 之间")
        if not 0.5 <= confidence_level < 1:
            raise ValueError("confidence_level 必须在 0.5 到 1 之间")

        timestamps: list[datetime] = []
        values: list[float] = []
        for index, row in enumerate(rows, start=1):
            if time_column not in row or value_column not in row:
                raise ValueError(f"第 {index} 行缺少时间列或数值列")
            try:
                timestamp = datetime.fromisoformat(str(row[time_column]).replace("Z", "+00:00"))
                value = float(row[value_column])
            except (TypeError, ValueError) as exc:
                raise ValueError(f"第 {index} 行的时间或数值无法解析") from exc
            if not math.isfinite(value):
                raise ValueError(f"第 {index} 行包含非有限数值")
            timestamps.append(timestamp)
            values.append(value)

        if any(timestamps[index] >= timestamps[index + 1] for index in range(len(timestamps) - 1)):
            raise ValueError("时间列必须严格递增且不能重复")
        intervals = [
            (timestamps[index + 1] - timestamps[index]).total_seconds()
            for index in range(len(timestamps) - 1)
        ]
        regular = max(intervals) - min(intervals) <= max(
            1.0, abs(sum(intervals) / len(intervals)) * 0.01
        )
        forecast = forecast_series(values, horizon=horizon, confidence_level=confidence_level)
        step_seconds = intervals[-1]
        forecast_labels = [
            (timestamps[-1] + (timestamps[-1] - timestamps[-2]) * step).isoformat()
            for step in range(1, horizon + 1)
        ]
        forecast.time_col = time_column
        forecast.forecast_labels = forecast_labels
        trend = compute_trend(rows, time_column, value_column)
        result = {
            "series_quality": {
                "row_count": len(rows),
                "start": timestamps[0].isoformat(),
                "end": timestamps[-1].isoformat(),
                "strictly_increasing": True,
                "regular_interval": regular,
                "last_interval_seconds": step_seconds,
            },
            "trend": trend,
            "forecast": forecast.to_dict(),
            "limitations": [
                "预测仅使用单变量历史序列，并不包含外生变量。",
                "预测区间来自历史残差，不能视为确定性承诺。",
            ],
        }
        if not regular:
            result["limitations"].append("历史时间间隔不规则，未来标签沿用最后一个观测间隔。")
        logger.info(
            "时间序列分析完成",
            model=forecast.model,
            regular_interval=regular,
            forecast_count=len(forecast.predictions),
        )
        return result


_TOOLS: dict[str, BaseTool] = {"analyze_timeseries": AnalyzeTimeseriesTool()}


def get_tool(name: str) -> BaseTool | None:
    """按名称返回已注册工具。"""
    return _TOOLS.get(name)


def get_tools() -> list[BaseTool]:
    """返回该 Skill 暴露的全部工具。"""
    return list(_TOOLS.values())
