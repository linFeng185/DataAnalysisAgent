"""结构化数据语义规范化工具。"""

from __future__ import annotations

import hashlib
import json
from copy import deepcopy
from datetime import datetime
from decimal import Decimal
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from langchain_core.tools import BaseTool

from src.logging_config import get_logger

logger = get_logger(__name__)
_OPERATIONS = {"null", "boolean", "datetime", "scale", "enum"}


class NormalizeSemanticsTool(BaseTool):
    """按显式规则统一空值、布尔、时间、单位和枚举语义。"""

    name: str = "normalize_semantics"
    description: str = "按显式字段规则规范化空值标记、布尔值、时区、数值单位和枚举别名。"

    def _run(self, *args: Any, **kwargs: Any) -> dict[str, Any]:
        """拒绝同步入口。"""
        del args, kwargs
        raise RuntimeError("normalize_semantics 只允许异步执行")

    async def _arun(
        self,
        rows: list[dict[str, Any]],
        rules: list[dict[str, Any]],
        run_manager: Any = None,
    ) -> dict[str, Any]:
        """返回规范化副本、逐值变更记录和无法转换的问题。"""
        del run_manager
        if any(not isinstance(row, dict) for row in rows):
            raise ValueError("rows 中的每一项都必须是对象")
        normalized_rules = _normalize_rules(rules)
        output_rows = deepcopy(rows)
        changes: list[dict[str, Any]] = []
        issues: list[dict[str, Any]] = []
        logger.info(
            "语义规范化开始",
            row_count=len(rows),
            rule_count=len(normalized_rules),
        )

        for rule in normalized_rules:
            column = rule["column"]
            operation = rule["operation"]
            for row_index, row in enumerate(output_rows):
                if column not in row:
                    issues.append(
                        {
                            "row_index": row_index,
                            "column": column,
                            "operation": operation,
                            "code": "COLUMN_MISSING",
                            "message": "字段不存在",
                        }
                    )
                    continue
                before = row[column]
                try:
                    after = _apply_rule(before, rule)
                except (ValueError, TypeError, ZoneInfoNotFoundError) as exc:
                    issues.append(
                        {
                            "row_index": row_index,
                            "column": column,
                            "operation": operation,
                            "code": "NORMALIZATION_FAILED",
                            "message": str(exc),
                        }
                    )
                    continue
                if _token(before) == _token(after):
                    continue
                row[column] = after
                changes.append(
                    {
                        "row_index": row_index,
                        "column": column,
                        "operation": operation,
                        "before": before,
                        "after": after,
                    }
                )

        fingerprint_source = json.dumps(
            output_rows,
            ensure_ascii=False,
            sort_keys=True,
            default=str,
            separators=(",", ":"),
        )
        result = {
            "rows": output_rows,
            "summary": {
                "input_rows": len(rows),
                "output_rows": len(output_rows),
                "changed_values": len(changes),
                "issue_count": len(issues),
            },
            "changes": changes,
            "issues": issues,
            "fingerprint": hashlib.sha256(fingerprint_source.encode("utf-8")).hexdigest(),
        }
        logger.info(
            "语义规范化完成",
            changed_values=len(changes),
            issue_count=len(issues),
        )
        return result


def _normalize_rules(rules: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """校验规则并补齐安全默认值。"""
    if not rules:
        raise ValueError("rules 必须包含至少一条规范化规则")
    result: list[dict[str, Any]] = []
    seen: set[tuple[str, str]] = set()
    for index, rule in enumerate(rules):
        if not isinstance(rule, dict):
            raise ValueError(f"第 {index} 条规则必须是对象")
        column = str(rule.get("column", "")).strip()
        operation = str(rule.get("operation", "")).strip()
        if not column:
            raise ValueError(f"第 {index} 条规则缺少 column")
        if operation not in _OPERATIONS:
            raise ValueError(f"不支持的规范化操作: {operation}")
        key = (column, operation)
        if key in seen:
            raise ValueError(f"规范化规则重复: {column}/{operation}")
        seen.add(key)
        normalized = dict(rule)
        normalized["column"] = column
        normalized["operation"] = operation
        result.append(normalized)
    return result


def _apply_rule(value: Any, rule: dict[str, Any]) -> Any:
    """对单个值应用一条显式规则。"""
    operation = rule["operation"]
    if operation == "null":
        markers = rule.get("null_values") or ["", "null", "none", "n/a", "na", "-"]
        marker_tokens = {_normalized_scalar(item) for item in markers}
        return None if _normalized_scalar(value) in marker_tokens else value
    if value is None:
        return None
    if operation == "boolean":
        true_values = rule.get("true_values") or [True, 1, "1", "true", "yes", "是"]
        false_values = rule.get("false_values") or [False, 0, "0", "false", "no", "否"]
        token = _normalized_scalar(value)
        if token in {_normalized_scalar(item) for item in true_values}:
            return True
        if token in {_normalized_scalar(item) for item in false_values}:
            return False
        raise ValueError("值不在布尔值映射中")
    if operation == "datetime":
        if not isinstance(value, str):
            raise TypeError("时间规范化只接受文本值")
        try:
            parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError as exc:
            raise ValueError("值不是有效的标准时间") from exc
        source_timezone = ZoneInfo(str(rule.get("source_timezone") or "UTC"))
        target_timezone = ZoneInfo(str(rule.get("target_timezone") or "UTC"))
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=source_timezone)
        return parsed.astimezone(target_timezone).isoformat()
    if operation == "scale":
        if not _is_number(value):
            raise TypeError("单位缩放只接受数值")
        factor = rule.get("factor", 1)
        offset = rule.get("offset", 0)
        if not _is_number(factor) or not _is_number(offset):
            raise TypeError("factor 和 offset 必须是数值")
        converted = float(value) * float(factor) + float(offset)
        return round(converted, 12)
    mapping = rule.get("enum_mapping")
    if not isinstance(mapping, dict) or not mapping:
        raise ValueError("枚举规范化必须提供非空 enum_mapping")
    token = str(value)
    if token not in mapping:
        raise ValueError("值不在枚举映射中")
    return mapping[token]


def _normalized_scalar(value: Any) -> str:
    """生成用于映射匹配的大小写无关标量。"""
    if isinstance(value, str):
        return value.strip().casefold()
    return _token(value)


def _is_number(value: Any) -> bool:
    """排除布尔值后判断数值。"""
    return isinstance(value, (int, float, Decimal)) and not isinstance(value, bool)


def _token(value: Any) -> str:
    """把值转换为稳定比较标记。"""
    return json.dumps(value, ensure_ascii=False, sort_keys=True, default=str, separators=(",", ":"))


_TOOLS: dict[str, BaseTool] = {"normalize_semantics": NormalizeSemanticsTool()}


def get_tool(name: str) -> BaseTool | None:
    """按名称返回已注册工具。"""
    return _TOOLS.get(name)


def get_tools() -> list[BaseTool]:
    """返回该 Skill 暴露的全部工具。"""
    return list(_TOOLS.values())
