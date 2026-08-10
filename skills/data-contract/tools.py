"""显式数据契约校验工具。"""

from __future__ import annotations

import hashlib
import json
import math
import re
from datetime import datetime
from decimal import Decimal
from typing import Any

from langchain_core.tools import BaseTool

from src.logging_config import get_logger

logger = get_logger(__name__)
_TYPES = {"string", "integer", "number", "boolean", "datetime", "object", "array"}


class ValidateDataContractTool(BaseTool):
    """按调用方声明的契约检查行式数据。"""

    name: str = "validate_data_contract"
    description: str = "校验字段存在性、类型、空值、唯一性、枚举、范围、正则和复合主键约束。"

    def _run(self, *args: Any, **kwargs: Any) -> dict[str, Any]:
        """拒绝同步入口。"""
        del args, kwargs
        raise RuntimeError("validate_data_contract 只允许异步执行")

    async def _arun(
        self,
        rows: list[dict[str, Any]],
        contract: dict[str, Any],
        max_violations: int = 1000,
        run_manager: Any = None,
    ) -> dict[str, Any]:
        """返回有界违规明细和稳定契约指纹。"""
        del run_manager
        logger.info(
            "数据契约校验开始",
            row_count=len(rows),
            column_rule_count=len(contract.get("columns", []) or []),
            max_violations=max_violations,
        )
        if any(not isinstance(row, dict) for row in rows):
            raise ValueError("rows 中的每一项都必须是对象")
        if not 1 <= max_violations <= 10000:
            raise ValueError("max_violations 必须在 1 到 10000 之间")
        rules = _validate_contract(contract)
        primary_key = [str(item) for item in (contract.get("primary_key") or [])]
        allow_extra = bool(contract.get("allow_extra_columns", True))
        declared = set(rules)
        violations: list[dict[str, Any]] = []
        total_violations = 0
        counts: dict[str, int] = {}

        def add(code: str, row_index: int | None, column: str, message: str) -> None:
            nonlocal total_violations
            total_violations += 1
            counts[code] = counts.get(code, 0) + 1
            if len(violations) < max_violations:
                violations.append(
                    {
                        "code": code,
                        "severity": "error",
                        "row_index": row_index,
                        "column": column,
                        "message": message,
                    }
                )

        if not allow_extra:
            extras = sorted({str(key) for row in rows for key in row} - declared)
            for column in extras:
                add("EXTRA_COLUMN", None, column, f"字段 {column} 未在契约中声明")

        for row_index, row in enumerate(rows):
            for column, rule in rules.items():
                present = column in row
                value = row.get(column)
                if not present:
                    if rule["required"]:
                        add("MISSING_COLUMN", row_index, column, "缺少必填字段")
                    continue
                if value is None:
                    if not rule["nullable"]:
                        add("NULL_NOT_ALLOWED", row_index, column, "字段不允许为空")
                    continue
                if not _matches_type(value, rule["type"]):
                    add(
                        "TYPE_MISMATCH",
                        row_index,
                        column,
                        f"实际类型 {_type_name(value)} 不符合 {rule['type']}",
                    )
                    continue
                if rule["enum_values"] and value not in rule["enum_values"]:
                    add("ENUM_VIOLATION", row_index, column, "字段值不在允许枚举中")
                if rule["pattern"] and not re.fullmatch(rule["pattern"], str(value)):
                    add("PATTERN_VIOLATION", row_index, column, "字段值不符合正则约束")
                if _is_number(value):
                    numeric = float(value)
                    if not math.isfinite(numeric):
                        add("NON_FINITE_NUMBER", row_index, column, "字段包含非有限数值")
                    elif rule["minimum"] is not None and numeric < float(rule["minimum"]):
                        add("MINIMUM_VIOLATION", row_index, column, "字段值低于最小值")
                    elif rule["maximum"] is not None and numeric > float(rule["maximum"]):
                        add("MAXIMUM_VIOLATION", row_index, column, "字段值高于最大值")

        for column, rule in rules.items():
            if not rule["unique"]:
                continue
            seen: dict[str, int] = {}
            for row_index, row in enumerate(rows):
                value = row.get(column)
                if value is None:
                    continue
                token = _token(value)
                if token in seen:
                    add(
                        "UNIQUE_VIOLATION",
                        row_index,
                        column,
                        f"与第 {seen[token]} 行存在重复值",
                    )
                else:
                    seen[token] = row_index

        if primary_key:
            seen_keys: dict[str, int] = {}
            for row_index, row in enumerate(rows):
                values = [row.get(column) for column in primary_key]
                if any(value is None for value in values):
                    add(
                        "PRIMARY_KEY_NULL",
                        row_index,
                        ",".join(primary_key),
                        "复合主键字段不能为空",
                    )
                    continue
                token = _token(values)
                if token in seen_keys:
                    add(
                        "PRIMARY_KEY_DUPLICATE",
                        row_index,
                        ",".join(primary_key),
                        f"与第 {seen_keys[token]} 行主键重复",
                    )
                else:
                    seen_keys[token] = row_index

        contract_text = json.dumps(contract, ensure_ascii=False, sort_keys=True, default=str)
        result = {
            "valid": total_violations == 0,
            "checked_rows": len(rows),
            "checked_columns": len(rules),
            "total_violations": total_violations,
            "violations": violations,
            "violation_counts": counts,
            "violations_truncated": total_violations > len(violations),
            "contract_fingerprint": hashlib.sha256(contract_text.encode("utf-8")).hexdigest(),
        }
        logger.info(
            "数据契约校验完成",
            valid=result["valid"],
            total_violations=total_violations,
            violations_truncated=result["violations_truncated"],
        )
        return result


def _validate_contract(contract: dict[str, Any]) -> dict[str, dict[str, Any]]:
    """校验契约结构并规范化列规则。"""
    columns = contract.get("columns")
    if not isinstance(columns, list) or not columns:
        raise ValueError("contract.columns 必须是非空数组")
    result: dict[str, dict[str, Any]] = {}
    for item in columns:
        if not isinstance(item, dict) or not str(item.get("name", "")).strip():
            raise ValueError("每条列规则都必须提供非空 name")
        name = str(item["name"])
        if name in result:
            raise ValueError(f"契约列重复: {name}")
        expected_type = str(item.get("type", "string"))
        if expected_type not in _TYPES:
            raise ValueError(f"不支持的契约类型: {expected_type}")
        pattern = str(item.get("pattern") or "")
        if pattern:
            try:
                re.compile(pattern)
            except re.error as exc:
                raise ValueError(f"字段 {name} 的正则无效") from exc
        result[name] = {
            "type": expected_type,
            "required": bool(item.get("required", False)),
            "nullable": bool(item.get("nullable", True)),
            "unique": bool(item.get("unique", False)),
            "enum_values": list(item.get("enum_values") or []),
            "minimum": item.get("minimum"),
            "maximum": item.get("maximum"),
            "pattern": pattern,
        }
    primary_key = [str(item) for item in (contract.get("primary_key") or [])]
    missing = [column for column in primary_key if column not in result]
    if missing:
        raise ValueError(f"主键字段未在 columns 中声明: {', '.join(missing)}")
    return result


def _matches_type(value: Any, expected: str) -> bool:
    """按 JSON 语义和标准时间格式检查字段类型。"""
    if expected == "string":
        return isinstance(value, str)
    if expected == "integer":
        return isinstance(value, int) and not isinstance(value, bool)
    if expected == "number":
        return _is_number(value)
    if expected == "boolean":
        return isinstance(value, bool)
    if expected == "object":
        return isinstance(value, dict)
    if expected == "array":
        return isinstance(value, list)
    if not isinstance(value, str):
        return False
    try:
        datetime.fromisoformat(value.replace("Z", "+00:00"))
        return True
    except ValueError:
        return False


def _is_number(value: Any) -> bool:
    """排除布尔值后判断数值。"""
    return isinstance(value, (int, float, Decimal)) and not isinstance(value, bool)


def _type_name(value: Any) -> str:
    """返回稳定类型名称。"""
    if isinstance(value, bool):
        return "boolean"
    if isinstance(value, int):
        return "integer"
    if isinstance(value, (float, Decimal)):
        return "number"
    if isinstance(value, str):
        return "string"
    if isinstance(value, dict):
        return "object"
    if isinstance(value, list):
        return "array"
    return type(value).__name__


def _token(value: Any) -> str:
    """把复杂值转换为稳定比较标记。"""
    return json.dumps(value, ensure_ascii=False, sort_keys=True, default=str, separators=(",", ":"))


_TOOLS: dict[str, BaseTool] = {"validate_data_contract": ValidateDataContractTool()}


def get_tool(name: str) -> BaseTool | None:
    """按名称返回已注册工具。"""
    return _TOOLS.get(name)


def get_tools() -> list[BaseTool]:
    """返回该 Skill 暴露的全部工具。"""
    return list(_TOOLS.values())
