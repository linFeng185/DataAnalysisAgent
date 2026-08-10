"""数值和类别分布漂移比较工具。"""

from __future__ import annotations

import json
import math
from collections import Counter
from decimal import Decimal
from statistics import fmean, pstdev
from typing import Any

from langchain_core.tools import BaseTool

from src.logging_config import get_logger

logger = get_logger(__name__)
_EPSILON = 1e-6


class MeasureDistributionDriftTool(BaseTool):
    """比较两个行式数据集的字段分布并给出可解释指标。"""

    name: str = "measure_distribution_drift"
    description: str = "计算数值字段的 PSI、近似 KS 与统计量变化，以及类别字段的总变差距离。"

    def _run(self, *args: Any, **kwargs: Any) -> dict[str, Any]:
        """拒绝同步入口。"""
        del args, kwargs
        raise RuntimeError("measure_distribution_drift 只允许异步执行")

    async def _arun(
        self,
        baseline_rows: list[dict[str, Any]],
        current_rows: list[dict[str, Any]],
        columns: list[str] | None = None,
        numeric_bins: int = 10,
        psi_threshold: float = 0.2,
        categorical_threshold: float = 0.1,
        run_manager: Any = None,
    ) -> dict[str, Any]:
        """按基准集定义分箱并测量当前集漂移。"""
        del run_manager
        _validate_rows(baseline_rows, "baseline_rows")
        _validate_rows(current_rows, "current_rows")
        if not 2 <= numeric_bins <= 50:
            raise ValueError("numeric_bins 必须在 2 到 50 之间")
        if psi_threshold < 0 or categorical_threshold < 0:
            raise ValueError("漂移阈值不能小于零")
        selected_columns = _resolve_columns(baseline_rows, current_rows, columns)
        logger.info(
            "分布漂移比较开始",
            baseline_rows=len(baseline_rows),
            current_rows=len(current_rows),
            column_count=len(selected_columns),
        )
        results: list[dict[str, Any]] = []
        warnings: list[str] = []
        numeric_count = 0
        categorical_count = 0

        for column in selected_columns:
            baseline_values = [
                row.get(column) for row in baseline_rows if row.get(column) is not None
            ]
            current_values = [
                row.get(column) for row in current_rows if row.get(column) is not None
            ]
            baseline_null_rate = _null_rate(baseline_rows, column)
            current_null_rate = _null_rate(current_rows, column)
            numeric = bool(baseline_values or current_values) and all(
                _is_number(value) for value in [*baseline_values, *current_values]
            )
            if numeric and baseline_values and current_values:
                numeric_count += 1
                result = _numeric_drift(
                    column,
                    baseline_values,
                    current_values,
                    baseline_null_rate,
                    current_null_rate,
                    numeric_bins,
                    psi_threshold,
                )
            else:
                categorical_count += 1
                result = _categorical_drift(
                    column,
                    baseline_values,
                    current_values,
                    baseline_null_rate,
                    current_null_rate,
                    categorical_threshold,
                )
                if numeric and (not baseline_values or not current_values):
                    warnings.append(f"字段 {column} 有一侧无有效数值，已按类别分布比较")
            results.append(result)

        drifted_columns = [item["column"] for item in results if item["drifted"]]
        result = {
            "summary": {
                "total_columns": len(results),
                "numeric_columns": numeric_count,
                "categorical_columns": categorical_count,
                "drifted_columns": len(drifted_columns),
            },
            "drift_detected": bool(drifted_columns),
            "drifted_columns": drifted_columns,
            "columns": results,
            "warnings": warnings,
        }
        logger.info(
            "分布漂移比较完成",
            drift_detected=result["drift_detected"],
            drifted_columns=len(drifted_columns),
            warning_count=len(warnings),
        )
        return result


def _validate_rows(rows: list[dict[str, Any]], label: str) -> None:
    """校验行式数据结构。"""
    if any(not isinstance(row, dict) for row in rows):
        raise ValueError(f"{label} 中的每一项都必须是对象")


def _resolve_columns(
    baseline_rows: list[dict[str, Any]],
    current_rows: list[dict[str, Any]],
    columns: list[str] | None,
) -> list[str]:
    """解析并校验待比较字段。"""
    available = {str(key) for row in [*baseline_rows, *current_rows] for key in row}
    if columns is None:
        return sorted(available)
    if len(set(columns)) != len(columns):
        raise ValueError("columns 不能包含重复字段")
    missing = [column for column in columns if column not in available]
    if missing:
        raise ValueError(f"待比较字段不存在: {', '.join(missing)}")
    return sorted(columns)


def _numeric_drift(
    column: str,
    baseline_values: list[Any],
    current_values: list[Any],
    baseline_null_rate: float,
    current_null_rate: float,
    numeric_bins: int,
    threshold: float,
) -> dict[str, Any]:
    """计算数值分布的稳定性指标。"""
    baseline = [float(value) for value in baseline_values]
    current = [float(value) for value in current_values]
    boundaries = _quantile_boundaries(baseline, numeric_bins)
    baseline_shares = _bin_shares(baseline, boundaries)
    current_shares = _bin_shares(current, boundaries)
    psi = sum(
        (current_share - baseline_share)
        * math.log((current_share + _EPSILON) / (baseline_share + _EPSILON))
        for baseline_share, current_share in zip(baseline_shares, current_shares, strict=True)
    )
    ks = _ks_statistic(baseline, current)
    baseline_mean = fmean(baseline)
    current_mean = fmean(current)
    baseline_std = pstdev(baseline)
    current_std = pstdev(current)
    return {
        "column": column,
        "data_type": "numeric",
        "baseline_count": len(baseline),
        "current_count": len(current),
        "baseline_null_rate": baseline_null_rate,
        "current_null_rate": current_null_rate,
        "drift_score": round(psi, 6),
        "threshold": threshold,
        "drifted": psi >= threshold,
        "metrics": {
            "psi": round(psi, 6),
            "ks_statistic": round(ks, 6),
            "total_variation": None,
            "baseline_mean": round(baseline_mean, 6),
            "current_mean": round(current_mean, 6),
            "mean_change": round(current_mean - baseline_mean, 6),
            "baseline_std": round(baseline_std, 6),
            "current_std": round(current_std, 6),
        },
        "distribution": [
            {
                "bucket": _bucket_label(index, boundaries),
                "baseline_share": round(baseline_share, 6),
                "current_share": round(current_share, 6),
            }
            for index, (baseline_share, current_share) in enumerate(
                zip(baseline_shares, current_shares, strict=True)
            )
        ],
    }


def _categorical_drift(
    column: str,
    baseline_values: list[Any],
    current_values: list[Any],
    baseline_null_rate: float,
    current_null_rate: float,
    threshold: float,
) -> dict[str, Any]:
    """计算类别分布的总变差距离。"""
    baseline_counts = Counter(_token(value) for value in baseline_values)
    current_counts = Counter(_token(value) for value in current_values)
    values = sorted(set(baseline_counts) | set(current_counts))
    baseline_total = len(baseline_values)
    current_total = len(current_values)
    distribution: list[dict[str, Any]] = []
    absolute_sum = 0.0
    for token in values:
        baseline_share = baseline_counts[token] / baseline_total if baseline_total else 0.0
        current_share = current_counts[token] / current_total if current_total else 0.0
        absolute_sum += abs(baseline_share - current_share)
        distribution.append(
            {
                "bucket": token,
                "baseline_share": round(baseline_share, 6),
                "current_share": round(current_share, 6),
            }
        )
    total_variation = absolute_sum / 2
    return {
        "column": column,
        "data_type": "categorical",
        "baseline_count": baseline_total,
        "current_count": current_total,
        "baseline_null_rate": baseline_null_rate,
        "current_null_rate": current_null_rate,
        "drift_score": round(total_variation, 6),
        "threshold": threshold,
        "drifted": total_variation >= threshold,
        "metrics": {
            "psi": None,
            "ks_statistic": None,
            "total_variation": round(total_variation, 6),
            "baseline_mean": None,
            "current_mean": None,
            "mean_change": None,
            "baseline_std": None,
            "current_std": None,
        },
        "distribution": distribution[:50],
    }


def _quantile_boundaries(values: list[float], bins: int) -> list[float]:
    """从基准值生成去重后的内部近似分位点。"""
    ordered = sorted(values)
    boundaries: list[float] = []
    for index in range(1, bins):
        position = index * (len(ordered) - 1) / bins
        lower = int(position)
        upper = min(lower + 1, len(ordered) - 1)
        fraction = position - lower
        value = ordered[lower] * (1 - fraction) + ordered[upper] * fraction
        if not boundaries or value > boundaries[-1]:
            boundaries.append(value)
    return boundaries


def _bin_shares(values: list[float], boundaries: list[float]) -> list[float]:
    """计算各分箱占比。"""
    counts = [0] * (len(boundaries) + 1)
    for value in values:
        index = 0
        while index < len(boundaries) and value > boundaries[index]:
            index += 1
        counts[index] += 1
    return [count / len(values) for count in counts]


def _ks_statistic(baseline: list[float], current: list[float]) -> float:
    """在联合观测点上计算经验分布函数最大距离。"""
    baseline_sorted = sorted(baseline)
    current_sorted = sorted(current)
    points = sorted(set(baseline_sorted) | set(current_sorted))
    baseline_index = 0
    current_index = 0
    maximum = 0.0
    for point in points:
        while baseline_index < len(baseline_sorted) and baseline_sorted[baseline_index] <= point:
            baseline_index += 1
        while current_index < len(current_sorted) and current_sorted[current_index] <= point:
            current_index += 1
        maximum = max(
            maximum,
            abs(baseline_index / len(baseline_sorted) - current_index / len(current_sorted)),
        )
    return maximum


def _bucket_label(index: int, boundaries: list[float]) -> str:
    """返回稳定的数值分箱标签。"""
    if not boundaries:
        return "全部数值"
    if index == 0:
        return f"小于等于 {boundaries[0]:.6g}"
    if index == len(boundaries):
        return f"大于 {boundaries[-1]:.6g}"
    return f"({boundaries[index - 1]:.6g}, {boundaries[index]:.6g}]"


def _null_rate(rows: list[dict[str, Any]], column: str) -> float:
    """计算缺失或显式空值比例。"""
    if not rows:
        return 0.0
    return round(sum(row.get(column) is None for row in rows) / len(rows), 6)


def _is_number(value: Any) -> bool:
    """排除布尔值后判断数值。"""
    return isinstance(value, (int, float, Decimal)) and not isinstance(value, bool)


def _token(value: Any) -> str:
    """把类别值转换为稳定标签。"""
    return json.dumps(value, ensure_ascii=False, sort_keys=True, default=str, separators=(",", ":"))


_TOOLS: dict[str, BaseTool] = {"measure_distribution_drift": MeasureDistributionDriftTool()}


def get_tool(name: str) -> BaseTool | None:
    """按名称返回已注册工具。"""
    return _TOOLS.get(name)


def get_tools() -> list[BaseTool]:
    """返回该 Skill 暴露的全部工具。"""
    return list(_TOOLS.values())
