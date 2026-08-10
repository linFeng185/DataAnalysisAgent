"""按主键比较两个行式数据集。"""

from __future__ import annotations

import json
from decimal import Decimal
from typing import Any

from langchain_core.tools import BaseTool

from src.logging_config import get_logger

logger = get_logger(__name__)


class CompareDatasetsTool(BaseTool):
    """确定性识别数据集的新增、删除、修改和字段级差异。"""

    name: str = "compare_datasets"
    description: str = "按唯一主键比较两组行式数据，并返回有界的行级和字段级差异。"

    def _run(self, *args: Any, **kwargs: Any) -> dict[str, Any]:
        """拒绝同步入口。"""
        del args, kwargs
        raise RuntimeError("compare_datasets 只允许异步执行")

    async def _arun(
        self,
        baseline_rows: list[dict[str, Any]],
        current_rows: list[dict[str, Any]],
        primary_key: list[str],
        compare_columns: list[str] | None = None,
        numeric_tolerance: float = 0.0,
        max_details: int = 100,
        run_manager: Any = None,
    ) -> dict[str, Any]:
        """比较两个数据集且不修改输入。"""
        del run_manager
        _validate_options(primary_key, compare_columns, numeric_tolerance, max_details)
        baseline = _index_rows(baseline_rows, primary_key, "baseline_rows")
        current = _index_rows(current_rows, primary_key, "current_rows")
        columns = _comparison_columns(
            baseline_rows,
            current_rows,
            primary_key,
            compare_columns,
        )
        logger.info(
            "数据集差异比较开始",
            baseline_rows=len(baseline_rows),
            current_rows=len(current_rows),
            primary_key=primary_key,
            compare_columns=len(columns),
        )

        baseline_keys = set(baseline)
        current_keys = set(current)
        added_tokens = sorted(current_keys - baseline_keys)
        removed_tokens = sorted(baseline_keys - current_keys)
        shared_tokens = sorted(baseline_keys & current_keys)
        field_change_counts = {column: 0 for column in columns}
        modified: list[dict[str, Any]] = []
        unchanged = 0

        for token in shared_tokens:
            old_row = baseline[token][1]
            new_row = current[token][1]
            changes: list[dict[str, Any]] = []
            for column in columns:
                before = old_row.get(column)
                after = new_row.get(column)
                if _values_equal(before, after, numeric_tolerance):
                    continue
                change: dict[str, Any] = {
                    "column": column,
                    "before": before,
                    "after": after,
                }
                if _is_number(before) and _is_number(after):
                    change["absolute_difference"] = round(abs(float(after) - float(before)), 12)
                changes.append(change)
                field_change_counts[column] += 1
            if changes:
                modified.append({"key": baseline[token][0], "changes": changes})
            else:
                unchanged += 1

        remaining = max_details
        added_keys = [current[token][0] for token in added_tokens[:remaining]]
        remaining -= len(added_keys)
        removed_keys = [baseline[token][0] for token in removed_tokens[:remaining]]
        remaining -= len(removed_keys)
        modified_rows = modified[:remaining]
        total_changed = len(added_tokens) + len(removed_tokens) + len(modified)
        included_details = len(added_keys) + len(removed_keys) + len(modified_rows)
        result = {
            "identical": total_changed == 0,
            "primary_key": primary_key,
            "compared_columns": columns,
            "summary": {
                "baseline_rows": len(baseline_rows),
                "current_rows": len(current_rows),
                "added": len(added_tokens),
                "removed": len(removed_tokens),
                "modified": len(modified),
                "unchanged": unchanged,
            },
            "added_keys": added_keys,
            "removed_keys": removed_keys,
            "modified_rows": modified_rows,
            "field_change_counts": field_change_counts,
            "details_truncated": included_details < total_changed,
        }
        logger.info(
            "数据集差异比较完成",
            identical=result["identical"],
            added=len(added_tokens),
            removed=len(removed_tokens),
            modified=len(modified),
            details_truncated=result["details_truncated"],
        )
        return result


def _validate_options(
    primary_key: list[str],
    compare_columns: list[str] | None,
    numeric_tolerance: float,
    max_details: int,
) -> None:
    """校验比较选项。"""
    if not primary_key or any(not str(column).strip() for column in primary_key):
        raise ValueError("primary_key 必须包含至少一个非空字段名")
    if len(set(primary_key)) != len(primary_key):
        raise ValueError("primary_key 不能包含重复字段")
    if compare_columns is not None and len(set(compare_columns)) != len(compare_columns):
        raise ValueError("compare_columns 不能包含重复字段")
    if compare_columns and set(compare_columns) & set(primary_key):
        raise ValueError("compare_columns 不应重复包含主键字段")
    if numeric_tolerance < 0:
        raise ValueError("numeric_tolerance 不能小于零")
    if not 1 <= max_details <= 1000:
        raise ValueError("max_details 必须在 1 到 1000 之间")


def _index_rows(
    rows: list[dict[str, Any]],
    primary_key: list[str],
    label: str,
) -> dict[str, tuple[list[Any], dict[str, Any]]]:
    """校验主键并按稳定标记索引数据行。"""
    result: dict[str, tuple[list[Any], dict[str, Any]]] = {}
    for row_index, row in enumerate(rows):
        if not isinstance(row, dict):
            raise ValueError(f"{label} 第 {row_index} 行必须是对象")
        missing = [column for column in primary_key if column not in row or row[column] is None]
        if missing:
            raise ValueError(f"{label} 第 {row_index} 行主键缺失或为空: {', '.join(missing)}")
        key = [row[column] for column in primary_key]
        token = _token(key)
        if token in result:
            raise ValueError(f"{label} 存在重复主键: {key}")
        result[token] = (key, row)
    return result


def _comparison_columns(
    baseline_rows: list[dict[str, Any]],
    current_rows: list[dict[str, Any]],
    primary_key: list[str],
    requested: list[str] | None,
) -> list[str]:
    """解析需要比较的非主键字段。"""
    if requested is not None:
        return sorted(requested)
    columns = {
        str(column)
        for row in [*baseline_rows, *current_rows]
        for column in row
        if str(column) not in primary_key
    }
    return sorted(columns)


def _values_equal(before: Any, after: Any, tolerance: float) -> bool:
    """使用数值容差或稳定 JSON 语义比较值。"""
    if _is_number(before) and _is_number(after):
        return abs(float(before) - float(after)) <= tolerance
    return _token(before) == _token(after)


def _is_number(value: Any) -> bool:
    """排除布尔值后判断数值。"""
    return isinstance(value, (int, float, Decimal)) and not isinstance(value, bool)


def _token(value: Any) -> str:
    """把 JSON 兼容值转换为稳定比较标记。"""
    return json.dumps(value, ensure_ascii=False, sort_keys=True, default=str, separators=(",", ":"))


_TOOLS: dict[str, BaseTool] = {"compare_datasets": CompareDatasetsTool()}


def get_tool(name: str) -> BaseTool | None:
    """按名称返回已注册工具。"""
    return _TOOLS.get(name)


def get_tools() -> list[BaseTool]:
    """返回该 Skill 暴露的全部工具。"""
    return list(_TOOLS.values())
