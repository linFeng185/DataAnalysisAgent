"""9.2.2~9.2.4 数据质量检查工具。"""

from __future__ import annotations

from typing import Any

from langchain_core.tools import BaseTool

from src.logging_config import get_logger

logger = get_logger(__name__)


class CheckNullRateTool(BaseTool):
    """9.2.2 空值率检查。"""
    name: str = "check_null_rate"
    description: str = (
        "计算数据集中指定列的空值比例。"
        "输入: {\"rows\": [...], \"column\": \"列名\"}。"
    )

    def _run(self, rows: list[dict] | str, column: str = "", run_manager: Any = None) -> dict:
        import json
        if isinstance(rows, str):
            rows = json.loads(rows)
        if not rows or not column:
            return {"error": "rows 和 column 参数必填"}
        nulls = sum(1 for r in rows if r.get(column) is None)
        return {"column": column, "total": len(rows), "null_count": nulls,
                "null_rate": nulls / len(rows) if rows else 0}


class CheckDuplicatesTool(BaseTool):
    """9.2.3 重复值检查。"""
    name: str = "check_duplicates"
    description: str = (
        "检测数据集中指定列的重复值。"
        "输入: {\"rows\": [...], \"column\": \"列名\"}。"
    )

    def _run(self, rows: list[dict] | str, column: str = "", run_manager: Any = None) -> dict:
        import json
        from collections import Counter
        if isinstance(rows, str):
            rows = json.loads(rows)
        if not rows or not column:
            return {"error": "rows 和 column 参数必填"}
        values = [str(r.get(column)) for r in rows if r.get(column) is not None]
        counts = Counter(values)
        dups = {k: v for k, v in counts.items() if v > 1}
        return {"column": column, "total": len(rows), "unique": len(counts),
                "duplicated": len(dups), "duplicates": dups}


class DetectOutliersTool(BaseTool):
    """9.2.4 Z-Score 异常值检测。"""
    name: str = "detect_outliers"
    description: str = (
        "用 Z-Score 方法检测数值列的异常值。"
        "输入: {\"rows\": [...], \"column\": \"列名\", \"threshold\": 3}。"
    )

    def _run(self, rows: list[dict] | str, column: str = "", threshold: float = 3.0,
             run_manager: Any = None) -> dict:
        import json
        if isinstance(rows, str):
            rows = json.loads(rows)
        if not rows or not column:
            return {"error": "rows 和 column 参数必填"}
        values = [float(r.get(column, 0) or 0) for r in rows]
        if len(values) < 8:
            return {"error": "至少需要 8 行数据"}
        mean = sum(values) / len(values)
        std = (sum((v - mean) ** 2 for v in values) / len(values)) ** 0.5
        if std == 0:
            return {"column": column, "outliers": [], "message": "标准差为 0"}
        outliers = [i for i, v in enumerate(values) if abs(v - mean) / std > threshold]
        return {"column": column, "total": len(values), "outlier_count": len(outliers),
                "outlier_indices": outliers, "mean": mean, "std": std}


_TOOLS: dict[str, BaseTool] = {
    "check_null_rate": CheckNullRateTool(),
    "check_duplicates": CheckDuplicatesTool(),
    "detect_outliers": DetectOutliersTool(),
}


def get_tool(name: str) -> BaseTool | None:
    return _TOOLS.get(name)


def get_tools() -> list[BaseTool]:
    return list(_TOOLS.values())
