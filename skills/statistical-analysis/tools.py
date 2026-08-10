"""统一确定性统计分析工具。"""

from __future__ import annotations

from decimal import Decimal
from typing import Any

from langchain_core.tools import BaseTool

from src.logging_config import get_logger
from src.tools.analyzer import (
    compute_concentration,
    compute_correlation,
    compute_statistics,
    compute_trend,
    detect_outliers_iqr,
    detect_outliers_zscore,
)

logger = get_logger(__name__)
_NUMERIC_TYPES = (int, float, Decimal)
_MODES = {"descriptive", "trend", "outliers", "correlation", "concentration"}


class AnalyzeStatisticsTool(BaseTool):
    """通过一个受控入口执行常用统计分析。"""

    name: str = "analyze_statistics"
    description: str = "执行描述统计、趋势、异常、相关性和集中度计算，所有数值均由确定性代码生成。"

    def _run(self, *args: Any, **kwargs: Any) -> dict[str, Any]:
        """拒绝同步入口。"""
        del args, kwargs
        raise RuntimeError("analyze_statistics 只允许异步执行")

    async def _arun(
        self,
        rows: list[dict[str, Any]],
        modes: list[str] | None = None,
        time_column: str = "",
        value_column: str = "",
        top_n: int = 10,
        z_threshold: float = 3.0,
        run_manager: Any = None,
    ) -> dict[str, Any]:
        """按所选模式执行统计并说明无法执行的项目。"""
        del run_manager
        selected_modes = modes or [
            "descriptive",
            "trend",
            "outliers",
            "correlation",
            "concentration",
        ]
        logger.info("统计分析开始", row_count=len(rows), modes=selected_modes)
        unknown = sorted(set(selected_modes) - _MODES)
        if unknown:
            raise ValueError(f"不支持的统计模式: {', '.join(unknown)}")
        if not 1 <= top_n <= 100:
            raise ValueError("top_n 必须在 1 到 100 之间")
        if not 0.5 <= z_threshold <= 10:
            raise ValueError("z_threshold 必须在 0.5 到 10 之间")
        if any(not isinstance(row, dict) for row in rows):
            raise ValueError("rows 中的每一项都必须是对象")

        numeric_columns = _numeric_columns(rows)
        selected_value = value_column or (numeric_columns[0] if numeric_columns else "")
        selected_time = time_column or _infer_time_column(rows)
        analyses: dict[str, Any] = {}
        skipped: list[dict[str, str]] = []

        if "descriptive" in selected_modes:
            analyses["descriptive"] = compute_statistics(rows)

        if "trend" in selected_modes:
            if selected_time and selected_value:
                analyses["trend"] = {
                    "time_column": selected_time,
                    "value_column": selected_value,
                    **compute_trend(rows, selected_time, selected_value),
                }
            else:
                skipped.append({"mode": "trend", "reason": "缺少时间列或数值列"})

        if "outliers" in selected_modes:
            analyses["outliers"] = {
                column: {
                    "zscore": detect_outliers_zscore(_numeric_values(rows, column), z_threshold),
                    "iqr": detect_outliers_iqr(_numeric_values(rows, column)),
                }
                for column in numeric_columns
            }
            if not numeric_columns:
                skipped.append({"mode": "outliers", "reason": "没有可分析的数值列"})

        if "correlation" in selected_modes:
            pairs: list[dict[str, Any]] = []
            for left_index, left in enumerate(numeric_columns):
                for right in numeric_columns[left_index + 1 :]:
                    paired = [
                        (row.get(left), row.get(right))
                        for row in rows
                        if _is_number(row.get(left)) and _is_number(row.get(right))
                    ]
                    pairs.append(
                        {
                            "left": left,
                            "right": right,
                            "sample_size": len(paired),
                            "pearson": compute_correlation(
                                [item[0] for item in paired],
                                [item[1] for item in paired],
                            ),
                        }
                    )
            analyses["correlation"] = pairs
            if len(numeric_columns) < 2:
                skipped.append({"mode": "correlation", "reason": "至少需要两个数值列"})

        if "concentration" in selected_modes:
            if selected_value:
                values = _numeric_values(rows, selected_value)
                analyses["concentration"] = {
                    "value_column": selected_value,
                    **compute_concentration(values, min(top_n, max(1, len(values)))),
                }
            else:
                skipped.append({"mode": "concentration", "reason": "没有可分析的数值列"})

        result = {
            "row_count": len(rows),
            "numeric_columns": numeric_columns,
            "analyses": analyses,
            "skipped": skipped,
            "limitations": [
                "相关性不代表因果关系。",
                "异常检测结果依赖当前样本，不应直接视为数据错误。",
            ],
        }
        logger.info(
            "统计分析完成",
            numeric_column_count=len(numeric_columns),
            analysis_count=len(analyses),
            skipped_count=len(skipped),
        )
        return result


def _numeric_columns(rows: list[dict[str, Any]]) -> list[str]:
    """识别至少包含一个有效数值且不含非数值非空值的字段。"""
    columns = sorted({str(key) for row in rows for key in row})
    result: list[str] = []
    for column in columns:
        non_null = [row.get(column) for row in rows if row.get(column) is not None]
        if non_null and all(_is_number(value) for value in non_null):
            result.append(column)
    return result


def _is_number(value: Any) -> bool:
    """排除布尔值后判断数值。"""
    return isinstance(value, _NUMERIC_TYPES) and not isinstance(value, bool)


def _numeric_values(rows: list[dict[str, Any]], column: str) -> list[float]:
    """提取有限数值；非有限值由浮点转换后的比较拒绝。"""
    values = [float(row[column]) for row in rows if _is_number(row.get(column))]
    if any(value != value or value in {float("inf"), float("-inf")} for value in values):
        raise ValueError(f"字段 {column} 包含非有限数值")
    return values


def _infer_time_column(rows: list[dict[str, Any]]) -> str:
    """按常见名称选择时间列，仅作为统计展示提示。"""
    columns = sorted({str(key) for row in rows for key in row})
    markers = ("date", "time", "day", "month", "year", "日期", "时间", "月份", "年度")
    return next(
        (column for column in columns if any(marker in column.lower() for marker in markers)), ""
    )


_TOOLS: dict[str, BaseTool] = {"analyze_statistics": AnalyzeStatisticsTool()}


def get_tool(name: str) -> BaseTool | None:
    """按名称返回已注册工具。"""
    return _TOOLS.get(name)


def get_tools() -> list[BaseTool]:
    """返回该 Skill 暴露的全部工具。"""
    return list(_TOOLS.values())
