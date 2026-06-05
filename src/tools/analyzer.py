"""13.1~6 数据分析引擎 — 纯计算，零 LLM 依赖。"""

from __future__ import annotations

import math


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
    trend = "up" if s2 > s1 * 1.05 else ("down" if s2 < s1 * 0.95 else "flat")
    w = min(3, len(vals))
    ma = [round(sum(vals[i:i+w]) / w, 4) for i in range(len(vals) - w + 1)]
    return {"trend": trend, "change_pct": change, "moving_avg": ma}


def detect_outliers_zscore(values: list[float], threshold: float = 3.0) -> list[dict]:
    """13.3 Z-Score: |z| > threshold → 异常。"""
    if len(values) < 4:
        return []
    mean = sum(values) / len(values)
    std = _std(values)
    if std == 0:
        return []
    return [{"index": i, "value": v, "z_score": round(abs(v - mean) / std, 4)}
            for i, v in enumerate(values) if abs(v - mean) / std > threshold]


def detect_outliers_iqr(values: list[float]) -> list[dict]:
    """13.4 IQR: Q1-1.5*IQR ~ Q3+1.5*IQR 外为异常。"""
    if len(values) < 4:
        return []
    sv = sorted(values)
    q1, q3 = _pct(sv, 0.25), _pct(sv, 0.75)
    lo, hi = q1 - 1.5 * (q3 - q1), q3 + 1.5 * (q3 - q1)
    return [{"index": i, "value": v, "bound": "lower" if v < lo else "upper"}
            for i, v in enumerate(values) if v < lo or v > hi]


def compute_concentration(values: list[float], top_n: int = 10) -> dict:
    """13.5 集中度 — Top N 占比。"""
    if not values or sum(values) == 0:
        return {"top_concentration": 0}
    total = sum(values)
    top = sum(sorted(values, reverse=True)[:top_n])
    return {"top_concentration": round(top / total * 100, 2), "total": total, "top_n": top_n}


def compute_correlation(col1: list[float], col2: list[float]) -> float:
    """13.6 Pearson 相关系数。"""
    n = min(len(col1), len(col2))
    if n < 3:
        return 0
    m1, m2 = sum(col1[:n]) / n, sum(col2[:n]) / n
    cov = sum((col1[i] - m1) * (col2[i] - m2) for i in range(n))
    s1 = math.sqrt(sum((x - m1) ** 2 for x in col1[:n]))
    s2 = math.sqrt(sum((x - m2) ** 2 for x in col2[:n]))
    return round(cov / (s1 * s2), 4) if s1 and s2 else 0


def _find_numeric(rows: list[dict]) -> list[str]:
    cols = []
    for key in rows[0]:
        if any(isinstance(r.get(key), (int, float)) and not isinstance(r.get(key), bool) for r in rows):
            cols.append(key)
    return cols


def _extract(rows: list[dict], col: str) -> list[float]:
    return [float(r[col]) for r in rows if r.get(col) is not None and isinstance(r.get(col), (int, float))]


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
