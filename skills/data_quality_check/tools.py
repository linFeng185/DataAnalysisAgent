"""确定性数据质量检查工具。"""

from __future__ import annotations

import json
import math
from collections import Counter
from typing import Any

from langchain_core.tools import BaseTool

from src.logging_config import get_logger

logger = get_logger(__name__)


class CheckNullRateTool(BaseTool):
    """检查指定字段的空值率。"""

    name: str = "check_null_rate"
    description: str = "计算指定字段的空值数量和空值率，字段缺失按空值处理。"

    def _run(self, *args: Any, **kwargs: Any) -> dict[str, Any]:
        """拒绝同步入口。"""
        del args, kwargs
        raise RuntimeError("check_null_rate 只允许异步执行")

    async def _arun(
        self,
        rows: list[dict[str, Any]] | str,
        column: str = "",
        threshold: float = 3.0,
        run_manager: Any = None,
    ) -> dict[str, Any]:
        """返回指定字段的空值数量和比例。"""
        del threshold, run_manager
        normalized = _normalize_rows(rows)
        normalized_column = column.strip()
        logger.info(
            "空值率检查开始",
            row_count=len(normalized),
            column=normalized_column,
        )
        if not normalized or not normalized_column:
            return {"error": "rows 和 column 参数必填"}
        nulls = sum(1 for row in normalized if row.get(normalized_column) is None)
        result = {
            "column": normalized_column,
            "total": len(normalized),
            "null_count": nulls,
            "null_rate": nulls / len(normalized),
        }
        logger.info("空值率检查完成", null_count=nulls, null_rate=result["null_rate"])
        return result


class CheckDuplicatesTool(BaseTool):
    """检查指定字段的重复值。"""

    name: str = "check_duplicates"
    description: str = "统计指定字段中出现次数大于一次的非空值。"

    def _run(self, *args: Any, **kwargs: Any) -> dict[str, Any]:
        """拒绝同步入口。"""
        del args, kwargs
        raise RuntimeError("check_duplicates 只允许异步执行")

    async def _arun(
        self,
        rows: list[dict[str, Any]] | str,
        column: str = "",
        threshold: float = 3.0,
        run_manager: Any = None,
    ) -> dict[str, Any]:
        """返回指定字段的唯一值数和重复值计数。"""
        del threshold, run_manager
        normalized = _normalize_rows(rows)
        normalized_column = column.strip()
        logger.info(
            "重复值检查开始",
            row_count=len(normalized),
            column=normalized_column,
        )
        if not normalized or not normalized_column:
            return {"error": "rows 和 column 参数必填"}
        values = [
            str(row.get(normalized_column))
            for row in normalized
            if row.get(normalized_column) is not None
        ]
        counts = Counter(values)
        duplicates = {value: count for value, count in sorted(counts.items()) if count > 1}
        result = {
            "column": normalized_column,
            "total": len(normalized),
            "unique": len(counts),
            "duplicated": len(duplicates),
            "duplicates": duplicates,
        }
        logger.info(
            "重复值检查完成",
            unique_count=result["unique"],
            duplicated_value_count=result["duplicated"],
        )
        return result


class DetectOutliersTool(BaseTool):
    """使用 Z-Score 检查指定数值字段的异常值。"""

    name: str = "detect_outliers"
    description: str = "使用总体标准差和给定阈值返回数值字段的异常行索引。"

    def _run(self, *args: Any, **kwargs: Any) -> dict[str, Any]:
        """拒绝同步入口。"""
        del args, kwargs
        raise RuntimeError("detect_outliers 只允许异步执行")

    async def _arun(
        self,
        rows: list[dict[str, Any]] | str,
        column: str = "",
        threshold: float = 3.0,
        run_manager: Any = None,
    ) -> dict[str, Any]:
        """返回超过阈值的原始行索引以及均值和总体标准差。"""
        del run_manager
        normalized = _normalize_rows(rows)
        normalized_column = column.strip()
        logger.info(
            "异常值检查开始",
            row_count=len(normalized),
            column=normalized_column,
            threshold=threshold,
        )
        if not normalized or not normalized_column:
            return {"error": "rows 和 column 参数必填"}
        if not 0 < threshold <= 10:
            raise ValueError("threshold 必须大于 0 且不超过 10")
        values = [float(row.get(normalized_column, 0) or 0) for row in normalized]
        if any(not math.isfinite(value) for value in values):
            raise ValueError("待检查字段不能包含非有限数值")
        if len(values) < 8:
            return {"error": "至少需要 8 行数据"}
        mean = sum(values) / len(values)
        std = (sum((v - mean) ** 2 for v in values) / len(values)) ** 0.5
        if std == 0:
            result = {
                "column": normalized_column,
                "total": len(values),
                "outlier_count": 0,
                "outlier_indices": [],
                "mean": mean,
                "std": std,
                "message": "标准差为 0",
            }
            logger.info("异常值检查完成", outlier_count=0, standard_deviation=0)
            return result
        outliers = [i for i, v in enumerate(values) if abs(v - mean) / std > threshold]
        result = {
            "column": normalized_column,
            "total": len(values),
            "outlier_count": len(outliers),
            "outlier_indices": outliers,
            "mean": mean,
            "std": std,
        }
        logger.info(
            "异常值检查完成",
            outlier_count=len(outliers),
            standard_deviation=std,
        )
        return result


def _normalize_rows(rows: list[dict[str, Any]] | str) -> list[dict[str, Any]]:
    """解析兼容的字符串输入并校验每一行为对象。"""
    parsed: Any = rows
    if isinstance(rows, str):
        try:
            parsed = json.loads(rows)
        except json.JSONDecodeError as exc:
            raise ValueError("rows 必须是合法 JSON 数组") from exc
    if not isinstance(parsed, list) or any(not isinstance(row, dict) for row in parsed):
        raise ValueError("rows 必须是对象数组")
    return parsed


_TOOLS: dict[str, BaseTool] = {
    "check_null_rate": CheckNullRateTool(),
    "check_duplicates": CheckDuplicatesTool(),
    "detect_outliers": DetectOutliersTool(),
}


def get_tool(name: str) -> BaseTool | None:
    """按名称返回已注册工具。"""
    return _TOOLS.get(name)


def get_tools() -> list[BaseTool]:
    """返回该 Skill 暴露的全部工具。"""
    return list(_TOOLS.values())
