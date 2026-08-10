"""非破坏式数据准备工具。"""

from __future__ import annotations

import json
from copy import deepcopy
from typing import Any

from langchain_core.tools import BaseTool

from src.logging_config import get_logger

logger = get_logger(__name__)
_ALLOWED_OPERATIONS = {"drop_duplicates", "fill_null", "cast", "rename", "select", "sort"}


class PrepareDataTool(BaseTool):
    """按显式步骤变换行式数据并返回逐步审计。"""

    name: str = "prepare_data"
    description: str = "非破坏地执行去重、空值填充、类型转换、重命名、选列和排序。"

    def _run(self, *args: Any, **kwargs: Any) -> dict[str, Any]:
        """拒绝同步入口。"""
        del args, kwargs
        raise RuntimeError("prepare_data 只允许异步执行")

    async def _arun(
        self,
        rows: list[dict[str, Any]],
        operations: list[dict[str, Any]],
        run_manager: Any = None,
    ) -> dict[str, Any]:
        """严格按顺序执行白名单操作，原输入保持不变。"""
        del run_manager
        logger.info("数据准备开始", row_count=len(rows), operation_count=len(operations))
        if any(not isinstance(row, dict) for row in rows):
            raise ValueError("rows 中的每一项都必须是对象")
        if not operations:
            raise ValueError("operations 不能为空")
        if len(operations) > 20:
            raise ValueError("单次最多允许 20 个数据准备步骤")

        result_rows = deepcopy(rows)
        audit: list[dict[str, Any]] = []
        warnings: list[str] = []
        for index, operation in enumerate(operations, start=1):
            if not isinstance(operation, dict):
                raise ValueError(f"第 {index} 个操作必须是对象")
            operation_name = str(operation.get("op", "")).strip()
            logger.info(
                "数据准备步骤输入边界",
                step=index,
                operation=operation_name,
                provided_fields=sorted(
                    key for key, value in operation.items() if value is not None
                ),
            )
            if operation_name not in _ALLOWED_OPERATIONS:
                raise ValueError(f"不支持的数据准备操作: {operation_name}")
            before_count = len(result_rows)
            result_rows, details = _apply_operation(result_rows, operation_name, operation)
            audit.append(
                {
                    "step": index,
                    "operation": operation_name,
                    "before_rows": before_count,
                    "after_rows": len(result_rows),
                    "details": details,
                }
            )
            logger.info(
                "数据准备步骤完成",
                step=index,
                operation=operation_name,
                before_rows=before_count,
                after_rows=len(result_rows),
            )

        if not result_rows:
            warnings.append("变换结果为空，请检查选列、去重和输入数据")
        result = {
            "status": "completed",
            "original_row_count": len(rows),
            "result_row_count": len(result_rows),
            "rows": result_rows,
            "audit": audit,
            "warnings": warnings,
        }
        logger.info("数据准备完成", result_row_count=len(result_rows), warning_count=len(warnings))
        return result


def _apply_operation(
    rows: list[dict[str, Any]],
    operation_name: str,
    operation: dict[str, Any],
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """执行单个白名单操作。"""
    if operation_name == "drop_duplicates":
        columns = [str(item) for item in (operation.get("columns") or [])]
        if columns:
            _require_columns(rows, columns)
        seen: set[str] = set()
        output: list[dict[str, Any]] = []
        for row in rows:
            selected = {column: row.get(column) for column in columns} if columns else row
            token = json.dumps(selected, ensure_ascii=False, sort_keys=True, default=str)
            if token not in seen:
                seen.add(token)
                output.append(row)
        return output, {"columns": columns, "removed_rows": len(rows) - len(output)}

    if operation_name == "fill_null":
        column = _required_text(operation, "column")
        _require_columns(rows, [column])
        if "value" not in operation:
            raise ValueError("fill_null 操作必须提供 value")
        fill_value = operation["value"]
        changed = 0
        for row in rows:
            if row.get(column) is None:
                row[column] = fill_value
                changed += 1
        return rows, {"column": column, "changed_rows": changed}

    if operation_name == "cast":
        column = _required_text(operation, "column")
        target_type = _required_text(operation, "data_type")
        _require_columns(rows, [column])
        changed = 0
        for row_index, row in enumerate(rows):
            if row.get(column) is None:
                continue
            try:
                row[column] = _cast_value(row[column], target_type)
                changed += 1
            except (TypeError, ValueError) as exc:
                raise ValueError(
                    f"第 {row_index + 1} 行字段 {column} 无法转换为 {target_type}"
                ) from exc
        return rows, {"column": column, "data_type": target_type, "changed_rows": changed}

    if operation_name == "rename":
        mapping = operation.get("mapping")
        if not isinstance(mapping, dict) or not mapping:
            raise ValueError("rename 操作必须提供非空 mapping")
        normalized = {str(source): str(target) for source, target in mapping.items()}
        _require_columns(rows, list(normalized))
        if len(set(normalized.values())) != len(normalized):
            raise ValueError("rename 的目标字段名不能重复")
        for row in rows:
            collisions = {
                target
                for source, target in normalized.items()
                if target in row and target != source
            }
            if collisions:
                raise ValueError(f"rename 会覆盖已有字段: {', '.join(sorted(collisions))}")
            for source, target in normalized.items():
                if source in row:
                    row[target] = row.pop(source)
        return rows, {"mapping": normalized}

    if operation_name == "select":
        columns = [str(item) for item in (operation.get("columns") or [])]
        if not columns:
            raise ValueError("select 操作必须提供非空 columns")
        _require_columns(rows, columns)
        return [{column: row.get(column) for column in columns} for row in rows], {
            "columns": columns
        }

    column = _required_text(operation, "column")
    _require_columns(rows, [column])
    descending = bool(operation.get("descending", False))
    non_null = [row for row in rows if row.get(column) is not None]
    null_rows = [row for row in rows if row.get(column) is None]
    try:
        non_null.sort(key=lambda row: row[column], reverse=descending)
    except TypeError as exc:
        raise ValueError(f"字段 {column} 包含不可比较的混合类型") from exc
    return non_null + null_rows, {"column": column, "descending": descending, "nulls_last": True}


def _required_text(operation: dict[str, Any], key: str) -> str:
    """读取操作中的必填非空文本字段。"""
    raw_value = operation.get(key)
    value = str(raw_value).strip() if raw_value is not None else ""
    if not value:
        raise ValueError(f"操作必须提供 {key}")
    return value


def _require_columns(rows: list[dict[str, Any]], columns: list[str]) -> None:
    """确保目标字段至少出现在一行中。"""
    available = {str(key) for row in rows for key in row}
    missing = [column for column in columns if column not in available]
    if missing:
        raise ValueError(f"字段不存在: {', '.join(missing)}")


def _cast_value(value: Any, target_type: str) -> Any:
    """执行受控标量类型转换。"""
    if target_type == "string":
        return str(value)
    if target_type == "integer":
        if isinstance(value, bool):
            raise ValueError("布尔值不能隐式转换为整数")
        return int(value)
    if target_type == "number":
        if isinstance(value, bool):
            raise ValueError("布尔值不能隐式转换为数值")
        return float(value)
    if target_type == "boolean":
        if isinstance(value, bool):
            return value
        normalized = str(value).strip().lower()
        if normalized in {"true", "1", "yes", "是"}:
            return True
        if normalized in {"false", "0", "no", "否"}:
            return False
        raise ValueError("无法识别布尔值")
    raise ValueError(f"不支持的目标类型: {target_type}")


_TOOLS: dict[str, BaseTool] = {"prepare_data": PrepareDataTool()}


def get_tool(name: str) -> BaseTool | None:
    """按名称返回已注册工具。"""
    return _TOOLS.get(name)


def get_tools() -> list[BaseTool]:
    """返回该 Skill 暴露的全部工具。"""
    return list(_TOOLS.values())
