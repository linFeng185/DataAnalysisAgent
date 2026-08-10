"""结构化分析结果核验工具。"""

from __future__ import annotations

import hashlib
import json
from decimal import Decimal
from typing import Any

from langchain_core.tools import BaseTool

from src.logging_config import get_logger

logger = get_logger(__name__)
_OPERATIONS = {"non_empty", "row_count", "required_columns", "unique", "range", "aggregate"}
_AGGREGATIONS = {"sum", "count", "average", "minimum", "maximum"}


class VerifyResultTool(BaseTool):
    """核验明细、汇总、完整性标记和显式质量规则。"""

    name: str = "verify_result"
    description: str = "检查空结果、行数、字段、唯一性、范围、汇总与明细一致性以及截断状态。"

    def _run(self, *args: Any, **kwargs: Any) -> dict[str, Any]:
        """拒绝同步入口。"""
        del args, kwargs
        raise RuntimeError("verify_result 只允许异步执行")

    async def _arun(
        self,
        rows: list[dict[str, Any]],
        checks: list[dict[str, Any]],
        summary: dict[str, Any] | None = None,
        truncated: bool = False,
        partial: bool = False,
        numeric_tolerance: float = 1e-6,
        run_manager: Any = None,
    ) -> dict[str, Any]:
        """执行固定核验规则并返回逐项证据。"""
        del run_manager
        if any(not isinstance(row, dict) for row in rows):
            raise ValueError("rows 中的每一项都必须是对象")
        if not checks:
            raise ValueError("checks 必须包含至少一条核验规则")
        if numeric_tolerance < 0:
            raise ValueError("numeric_tolerance 不能小于零")
        summary = summary or {}
        logger.info(
            "结果核验开始",
            row_count=len(rows),
            check_count=len(checks),
            truncated=truncated,
            partial=partial,
        )
        check_results: list[dict[str, Any]] = []
        for index, check in enumerate(checks):
            if not isinstance(check, dict):
                raise ValueError(f"第 {index} 条核验规则必须是对象")
            operation = str(check.get("operation", ""))
            if operation not in _OPERATIONS:
                raise ValueError(f"不支持的核验操作: {operation}")
            check_results.append(
                _execute_check(index, operation, check, rows, summary, numeric_tolerance)
            )

        failed = sum(not item["passed"] for item in check_results)
        complete = not truncated and not partial
        warnings: list[str] = []
        if truncated:
            warnings.append("结果已截断，不能视为完整明细")
        if partial:
            warnings.append("结果被标记为部分结果")
        if not rows:
            warnings.append("明细结果为空")
        fingerprint_source = json.dumps(
            {"rows": rows, "summary": summary},
            ensure_ascii=False,
            sort_keys=True,
            default=str,
            separators=(",", ":"),
        )
        valid = failed == 0
        result = {
            "valid": valid,
            "complete": complete,
            "verified": valid and complete,
            "summary": {
                "total_checks": len(check_results),
                "passed_checks": len(check_results) - failed,
                "failed_checks": failed,
                "row_count": len(rows),
            },
            "check_results": check_results,
            "warnings": warnings,
            "result_fingerprint": hashlib.sha256(fingerprint_source.encode("utf-8")).hexdigest(),
        }
        logger.info(
            "结果核验完成",
            valid=valid,
            complete=complete,
            verified=result["verified"],
            failed_checks=failed,
        )
        return result


def _execute_check(
    index: int,
    operation: str,
    check: dict[str, Any],
    rows: list[dict[str, Any]],
    summary: dict[str, Any],
    tolerance: float,
) -> dict[str, Any]:
    """执行单条规则并生成统一结果。"""
    check_id = str(check.get("id") or f"check-{index + 1}")
    if operation == "non_empty":
        return _result(check_id, operation, bool(rows), True, bool(rows), "结果应至少包含一行")
    if operation == "row_count":
        minimum = check.get("minimum")
        maximum = check.get("maximum")
        expected = check.get("expected")
        if expected is None and minimum is None and maximum is None:
            raise ValueError("row_count 至少需要 expected、minimum 或 maximum")
        passed = (
            (expected is None or len(rows) == expected)
            and (minimum is None or len(rows) >= minimum)
            and (maximum is None or len(rows) <= maximum)
        )
        target = {"expected": expected, "minimum": minimum, "maximum": maximum}
        return _result(check_id, operation, passed, target, len(rows), "核对明细行数")
    if operation == "required_columns":
        columns = check.get("columns")
        if not isinstance(columns, list) or not columns:
            raise ValueError("required_columns 必须提供非空 columns")
        missing = sorted(
            {str(column) for column in columns if any(column not in row for row in rows)}
        )
        return _result(check_id, operation, not missing, [], missing, "核对必需字段")
    if operation == "unique":
        columns = check.get("columns")
        if not isinstance(columns, list) or not columns:
            raise ValueError("unique 必须提供非空 columns")
        seen: set[str] = set()
        duplicate_count = 0
        for row in rows:
            token = _token([row.get(str(column)) for column in columns])
            if token in seen:
                duplicate_count += 1
            seen.add(token)
        return _result(
            check_id, operation, duplicate_count == 0, 0, duplicate_count, "核对字段组合唯一性"
        )
    if operation == "range":
        column = str(check.get("column", ""))
        if not column:
            raise ValueError("range 必须提供 column")
        minimum = check.get("minimum")
        maximum = check.get("maximum")
        allow_null = bool(check.get("allow_null", True))
        violations = 0
        for row in rows:
            value = row.get(column)
            if value is None:
                violations += 0 if allow_null else 1
            elif not _is_number(value):
                violations += 1
            elif minimum is not None and value < minimum:
                violations += 1
            elif maximum is not None and value > maximum:
                violations += 1
        target = {"minimum": minimum, "maximum": maximum, "allow_null": allow_null}
        return _result(check_id, operation, violations == 0, target, violations, "核对数值范围")

    column = str(check.get("column", ""))
    summary_field = str(check.get("summary_field", ""))
    aggregation = str(check.get("aggregation", "sum"))
    if not column or not summary_field:
        raise ValueError("aggregate 必须提供 column 和 summary_field")
    if aggregation not in _AGGREGATIONS:
        raise ValueError(f"不支持的汇总方式: {aggregation}")
    if summary_field not in summary:
        return _result(check_id, operation, False, "汇总字段存在", None, "汇总字段缺失")
    values = [row.get(column) for row in rows if row.get(column) is not None]
    actual = _aggregate(values, aggregation)
    expected = summary[summary_field]
    passed = _equal_with_tolerance(actual, expected, tolerance)
    return _result(check_id, operation, passed, expected, actual, "核对汇总与明细计算结果")


def _aggregate(values: list[Any], aggregation: str) -> Any:
    """执行受支持的确定性汇总。"""
    if aggregation == "count":
        return len(values)
    if not values or any(not _is_number(value) for value in values):
        raise ValueError("数值汇总要求字段包含至少一个有效数值且不得混入非数值")
    numbers = [float(value) for value in values]
    if aggregation == "sum":
        return round(sum(numbers), 12)
    if aggregation == "average":
        return round(sum(numbers) / len(numbers), 12)
    if aggregation == "minimum":
        return min(numbers)
    return max(numbers)


def _result(
    check_id: str,
    operation: str,
    passed: bool,
    expected: Any,
    actual: Any,
    message: str,
) -> dict[str, Any]:
    """构造统一核验结果。"""
    return {
        "id": check_id,
        "operation": operation,
        "passed": passed,
        "expected": expected,
        "actual": actual,
        "message": message,
    }


def _equal_with_tolerance(actual: Any, expected: Any, tolerance: float) -> bool:
    """按数值容差或稳定 JSON 语义比较结果。"""
    if _is_number(actual) and _is_number(expected):
        return abs(float(actual) - float(expected)) <= tolerance
    return _token(actual) == _token(expected)


def _is_number(value: Any) -> bool:
    """排除布尔值后判断数值。"""
    return isinstance(value, (int, float, Decimal)) and not isinstance(value, bool)


def _token(value: Any) -> str:
    """把值转换为稳定比较标记。"""
    return json.dumps(value, ensure_ascii=False, sort_keys=True, default=str, separators=(",", ":"))


_TOOLS: dict[str, BaseTool] = {"verify_result": VerifyResultTool()}


def get_tool(name: str) -> BaseTool | None:
    """按名称返回已注册工具。"""
    return _TOOLS.get(name)


def get_tools() -> list[BaseTool]:
    """返回该 Skill 暴露的全部工具。"""
    return list(_TOOLS.values())
