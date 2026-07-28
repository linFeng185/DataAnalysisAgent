"""13.1~6 数据分析引擎 — 纯计算，零 LLM 依赖。"""

from __future__ import annotations

import math
from decimal import Decimal

from src.logging_config import get_logger

logger = get_logger(__name__)
_NUMERIC_TYPES = (int, float, Decimal)


def compute_statistics(rows: list[dict]) -> dict:
    """13.1 描述性统计 — 数值列均值/中位数/标准差/分位数/空值率。"""
    if not rows:
        return {"row_count": 0, "columns": {}, "numeric_columns": []}

    num_cols = _find_numeric(rows)
    cols: dict = {}
    for col in num_cols:
        vals = _extract(rows, col)
        if not vals:
            continue
        sv = sorted(vals)
        n = len(sv)
        cols[col] = {
            "mean": round(sum(vals) / n, 4), "median": round(_pct(sv, 0.5), 4),
            "std": round(_std(vals), 4), "min": sv[0], "max": sv[-1],
            "q1": round(_pct(sv, 0.25), 4), "q3": round(_pct(sv, 0.75), 4),
            "null_count": sum(1 for r in rows if r.get(col) is None),
        }
    return {"row_count": len(rows), "numeric_columns": num_cols, "columns": cols}


# 方法作用：计算时序值的环比、方向和移动平均，并兼容 Decimal 数值。
# Args: rows - 查询结果；time_col - 时间列名；value_col - 数值列名。
# Returns: 趋势方向、环比和移动平均。
def compute_trend(rows: list[dict], time_col: str, value_col: str) -> dict:
    """13.2 趋势分析 — 环比/方向/移动平均。"""
    if len(rows) < 2:
        return {"trend": "flat", "change_pct": 0, "moving_avg": []}
    vals = [r[value_col] for r in rows if value_col in r and r[value_col] is not None]
    if len(vals) < 2:
        return {"trend": "flat", "change_pct": 0, "moving_avg": vals}

    change = round((vals[-1] - vals[-2]) / vals[-2] * 100, 2) if vals[-2] else 0
    half = len(vals) // 2
    s1 = sum(vals[:half]) / max(half, 1)
    s2 = sum(vals[half:]) / max(len(vals) - half, 1)
    logger.info(
        "趋势方向计算边界",
        value_type=type(vals[0]).__name__,
        first_half_type=type(s1).__name__,
        second_half_type=type(s2).__name__,
        threshold_type=type(105).__name__,
    )
    trend = (
        "up" if s2 * 100 > s1 * 105
        else "down" if s2 * 100 < s1 * 95
        else "flat"
    )
    w = min(3, len(vals))
    ma = [round(sum(vals[i:i+w]) / w, 4) for i in range(len(vals) - w + 1)]
    return {"trend": trend, "change_pct": change, "moving_avg": ma}


# 方法作用：在统一 float 近似统计域中计算 Z-Score 离群值。
# Args: values - 整数、浮点或 Decimal 数值；threshold - Z-Score 阈值。
# Returns: 包含索引、数值和 Z-Score 的离群点列表。
def detect_outliers_zscore(
    values: list[int | float | Decimal],
    threshold: float = 3.0,
) -> list[dict]:
    """13.3 Z-Score: |z| > threshold → 异常。"""
    if len(values) < 4:
        return []
    numeric_values = [float(value) for value in values]
    mean = sum(numeric_values) / len(numeric_values)
    std = _std(numeric_values)
    if std == 0:
        return []
    return [{"index": i, "value": v, "z_score": round(abs(v - mean) / std, 4)}
            for i, v in enumerate(numeric_values) if abs(v - mean) / std > threshold]


# 方法作用：在统一 float 近似统计域中计算 IQR 离群值。
# Args: values - 整数、浮点或 Decimal 数值。
# Returns: 包含索引、数值和越界方向的离群点列表。
def detect_outliers_iqr(
    values: list[int | float | Decimal],
) -> list[dict]:
    """13.4 IQR: Q1-1.5*IQR ~ Q3+1.5*IQR 外为异常。"""
    if len(values) < 4:
        return []
    numeric_values = [float(value) for value in values]
    sv = sorted(numeric_values)
    q1, q3 = _pct(sv, 0.25), _pct(sv, 0.75)
    lo, hi = q1 - 1.5 * (q3 - q1), q3 + 1.5 * (q3 - q1)
    return [{"index": i, "value": v, "bound": "lower" if v < lo else "upper"}
            for i, v in enumerate(numeric_values) if v < lo or v > hi]


def compute_concentration(values: list[float], top_n: int = 10) -> dict:
    """13.5 集中度 — Top N 占比。"""
    if not values or sum(values) == 0:
        return {"top_concentration": 0}
    total = sum(values)
    top = sum(sorted(values, reverse=True)[:top_n])
    return {"top_concentration": round(top / total * 100, 2), "total": total, "top_n": top_n}


# 方法作用：在统一 float 近似统计域中计算两列 Pearson 相关系数。
# Args: col1 - 第一列数值；col2 - 第二列数值。
# Returns: 四位小数相关系数，样本不足或方差为零时返回 0。
def compute_correlation(
    col1: list[int | float | Decimal],
    col2: list[int | float | Decimal],
) -> float:
    """13.6 Pearson 相关系数。"""
    n = min(len(col1), len(col2))
    if n < 3:
        return 0
    left = [float(value) for value in col1[:n]]
    right = [float(value) for value in col2[:n]]
    m1, m2 = sum(left) / n, sum(right) / n
    cov = sum((left[i] - m1) * (right[i] - m2) for i in range(n))
    s1 = math.sqrt(sum((x - m1) ** 2 for x in left))
    s2 = math.sqrt(sum((x - m2) ** 2 for x in right))
    return round(cov / (s1 * s2), 4) if s1 and s2 else 0


# 识别查询结果中的数值列，并兼容数据库 Decimal 类型。
# Args: rows - 查询结果行列表。
# Returns: 至少包含一个有效数值的列名列表。
def _find_numeric(rows: list[dict]) -> list[str]:
    logger.debug("数值列识别入口", row_count=len(rows))
    if not rows:
        logger.info("数值列识别完成", columns=[])
        return []
    cols = []
    for key in rows[0]:
        if any(
            isinstance(r.get(key), _NUMERIC_TYPES) and not isinstance(r.get(key), bool)
            for r in rows
        ):
            cols.append(key)
    logger.info("数值列识别完成", columns=cols)
    return cols


# 提取指定列的有效数值，并归一化为 float 供统计函数计算。
# Args: rows - 查询结果行列表；col - 待提取的列名。
# Returns: 排除空值、布尔值和非数值后的浮点数列表。
def _extract(rows: list[dict], col: str) -> list[float]:
    logger.debug("数值提取入口", column=col, row_count=len(rows))
    values = [
        float(value)
        for row in rows
        if (value := row.get(col)) is not None
        and isinstance(value, _NUMERIC_TYPES)
        and not isinstance(value, bool)
    ]
    logger.info("数值提取完成", column=col, value_count=len(values))
    return values


def _pct(sorted_vals: list[float], p: float) -> float:
    n = len(sorted_vals)
    if n == 0:
        return 0
    if n == 1:
        return sorted_vals[0]
    k = (n - 1) * p
    f, c = math.floor(k), math.ceil(k)
    if f == c:
        return sorted_vals[int(k)]
    return sorted_vals[f] * (c - k) + sorted_vals[c] * (k - f)


def _std(values: list[float]) -> float:
    n = len(values)
    if n < 2:
        return 0
    m = sum(values) / n
    return math.sqrt(sum((x - m) ** 2 for x in values) / (n - 1))
