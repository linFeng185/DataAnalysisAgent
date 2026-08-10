"""数值精度、范围、舍入和单位完整性审计工具。"""

from __future__ import annotations

import hashlib
import json
import math
from decimal import ROUND_DOWN, ROUND_HALF_EVEN, ROUND_HALF_UP, Decimal, InvalidOperation
from typing import Any

from langchain_core.tools import BaseTool

from src.logging_config import get_logger

logger = get_logger(__name__)
_KINDS = {"number", "integer", "decimal", "money"}
_ROUNDING = {
    "half_even": ROUND_HALF_EVEN,
    "half_up": ROUND_HALF_UP,
    "down": ROUND_DOWN,
}


class AuditNumericIntegrityTool(BaseTool):
    """按字段规则检查数值类型、有限性、范围、精度和单位。"""

    name: str = "audit_numeric_integrity"
    description: str = "检查行式数据中的非有限值、类型、范围、十进制精度、舍入要求和单位一致性。"

    def _run(self, *args: Any, **kwargs: Any) -> dict[str, Any]:
        """拒绝同步入口。"""
        del args, kwargs
        raise RuntimeError("audit_numeric_integrity 只允许异步执行")

    async def _arun(
        self,
        rows: list[dict[str, Any]],
        rules: list[dict[str, Any]],
        max_findings: int = 500,
        run_manager: Any = None,
    ) -> dict[str, Any]:
        """返回有界数值问题和字段级统计，不修改输入值。"""
        del run_manager
        if any(not isinstance(row, dict) for row in rows):
            raise ValueError("rows 中的每一项都必须是对象")
        if not rules:
            raise ValueError("rules 必须包含至少一条数值规则")
        if not 1 <= max_findings <= 5000:
            raise ValueError("max_findings 必须在 1 到 5000 之间")
        normalized_rules = [_normalize_rule(rule, index) for index, rule in enumerate(rules)]
        columns = [rule["column"] for rule in normalized_rules]
        if len(set(columns)) != len(columns):
            raise ValueError("同一字段只能声明一条数值完整性规则")
        logger.info(
            "数值完整性审计开始",
            row_count=len(rows),
            rule_count=len(normalized_rules),
            max_findings=max_findings,
        )
        findings: list[dict[str, Any]] = []
        total_findings = 0
        total_errors = 0
        total_warnings = 0
        column_results: list[dict[str, Any]] = []

        def add(
            code: str,
            severity: str,
            row_index: int,
            column: str,
            message: str,
            suggested_value: str | None = None,
        ) -> None:
            nonlocal total_findings, total_errors, total_warnings
            total_findings += 1
            total_errors += severity == "error"
            total_warnings += severity == "warning"
            if len(findings) < max_findings:
                findings.append(
                    {
                        "code": code,
                        "severity": severity,
                        "row_index": row_index,
                        "column": column,
                        "message": message,
                        "suggested_value": suggested_value,
                    }
                )

        for rule in normalized_rules:
            column = rule["column"]
            before_errors = total_errors
            before_warnings = total_warnings
            valid_values = 0
            null_values = 0
            for row_index, row in enumerate(rows):
                if column not in row:
                    add("MISSING_COLUMN", "error", row_index, column, "字段不存在")
                    continue
                value = row[column]
                if value is None:
                    null_values += 1
                    if not rule["allow_null"]:
                        add("NULL_NOT_ALLOWED", "error", row_index, column, "字段不允许为空")
                    continue
                if isinstance(value, bool) or not isinstance(value, (int, float, Decimal)):
                    add("TYPE_MISMATCH", "error", row_index, column, "字段值不是有效数值")
                    continue
                if isinstance(value, float) and not math.isfinite(value):
                    add("NON_FINITE", "error", row_index, column, "字段包含非有限浮点值")
                    continue
                try:
                    decimal_value = Decimal(str(value))
                except InvalidOperation:
                    add("INVALID_DECIMAL", "error", row_index, column, "字段无法转换为十进制数")
                    continue
                valid_values += 1
                if rule["kind"] == "integer" and decimal_value != decimal_value.to_integral_value():
                    add("INTEGER_REQUIRED", "error", row_index, column, "字段要求整数但包含小数")
                if rule["minimum"] is not None and decimal_value < rule["minimum"]:
                    add("BELOW_MINIMUM", "error", row_index, column, "字段值低于允许下限")
                if rule["maximum"] is not None and decimal_value > rule["maximum"]:
                    add("ABOVE_MAXIMUM", "error", row_index, column, "字段值高于允许上限")
                decimal_places = rule["decimal_places"]
                if decimal_places is not None:
                    quantum = Decimal(1).scaleb(-decimal_places)
                    rounded = decimal_value.quantize(
                        quantum, rounding=_ROUNDING[rule["rounding_mode"]]
                    )
                    if rounded != decimal_value:
                        add(
                            "ROUNDING_REQUIRED",
                            "error",
                            row_index,
                            column,
                            f"字段小数位超过 {decimal_places} 位",
                            format(rounded, "f"),
                        )
                if rule["expected_unit"] is not None:
                    unit_column = rule["unit_column"]
                    observed_unit = row.get(unit_column) if unit_column else None
                    if observed_unit != rule["expected_unit"]:
                        add("UNIT_MISMATCH", "error", row_index, column, "字段单位与规则不一致")
            column_results.append(
                {
                    "column": column,
                    "kind": rule["kind"],
                    "checked_rows": len(rows),
                    "valid_numeric_values": valid_values,
                    "null_values": null_values,
                    "errors": total_errors - before_errors,
                    "warnings": total_warnings - before_warnings,
                }
            )

        source = json.dumps(
            {"rows": rows, "rules": rules},
            ensure_ascii=False,
            sort_keys=True,
            default=str,
            separators=(",", ":"),
        )
        result = {
            "valid": total_errors == 0,
            "summary": {
                "row_count": len(rows),
                "column_count": len(normalized_rules),
                "errors": total_errors,
                "warnings": total_warnings,
                "total_findings": total_findings,
            },
            "column_results": column_results,
            "findings": findings,
            "findings_truncated": total_findings > len(findings),
            "audit_fingerprint": hashlib.sha256(source.encode("utf-8")).hexdigest(),
            "input_modified": False,
        }
        logger.info(
            "数值完整性审计完成",
            valid=result["valid"],
            error_count=total_errors,
            warning_count=total_warnings,
            findings_truncated=result["findings_truncated"],
        )
        return result


def _normalize_rule(rule: Any, index: int) -> dict[str, Any]:
    """校验并规范化数值字段规则。"""
    if not isinstance(rule, dict):
        raise ValueError(f"rules 第 {index} 项必须是对象")
    column = str(rule.get("column", "")).strip()
    kind = str(rule.get("kind", "number"))
    rounding_mode = str(rule.get("rounding_mode", "half_even"))
    if not column:
        raise ValueError(f"rules 第 {index} 项缺少 column")
    if kind not in _KINDS:
        raise ValueError(f"不支持的数值类型: {kind}")
    if rounding_mode not in _ROUNDING:
        raise ValueError(f"不支持的舍入模式: {rounding_mode}")
    decimal_places = rule.get("decimal_places")
    if decimal_places is None and kind == "money":
        decimal_places = 2
    if decimal_places is not None and (
        not isinstance(decimal_places, int)
        or isinstance(decimal_places, bool)
        or not 0 <= decimal_places <= 18
    ):
        raise ValueError("decimal_places 必须在 0 到 18 之间或为空")
    minimum = _optional_decimal(rule.get("minimum"), "minimum")
    maximum = _optional_decimal(rule.get("maximum"), "maximum")
    if minimum is not None and maximum is not None and minimum > maximum:
        raise ValueError(f"字段 {column} 的 minimum 不能大于 maximum")
    unit_column = str(rule.get("unit_column") or "").strip() or None
    expected_unit = rule.get("expected_unit")
    if expected_unit is not None:
        expected_unit = str(expected_unit).strip()
        if not expected_unit or not unit_column:
            raise ValueError(f"字段 {column} 声明 expected_unit 时必须提供 unit_column")
    return {
        "column": column,
        "kind": kind,
        "minimum": minimum,
        "maximum": maximum,
        "decimal_places": decimal_places,
        "rounding_mode": rounding_mode,
        "allow_null": bool(rule.get("allow_null", True)),
        "unit_column": unit_column,
        "expected_unit": expected_unit,
    }


def _optional_decimal(value: Any, field: str) -> Decimal | None:
    """将可选数值边界转换为十进制数。"""
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, (int, float, Decimal)):
        raise ValueError(f"{field} 必须是数值或空值")
    decimal_value = Decimal(str(value))
    if not decimal_value.is_finite():
        raise ValueError(f"{field} 必须是有限数值")
    return decimal_value


_TOOLS: dict[str, BaseTool] = {"audit_numeric_integrity": AuditNumericIntegrityTool()}


def get_tool(name: str) -> BaseTool | None:
    """按名称返回已注册工具。"""
    return _TOOLS.get(name)


def get_tools() -> list[BaseTool]:
    """返回该 Skill 暴露的全部工具。"""
    return list(_TOOLS.values())
