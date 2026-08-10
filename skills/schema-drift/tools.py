"""Schema 版本漂移比较工具。"""

from __future__ import annotations

from typing import Any

from langchain_core.tools import BaseTool

from src.logging_config import get_logger

logger = get_logger(__name__)
_WIDENING_TYPES = {("integer", "number"), ("date", "datetime")}


class CompareSchemaDriftTool(BaseTool):
    """比较两版数据库或文件 Schema 并分类兼容性。"""

    name: str = "compare_schema_drift"
    description: str = "识别表字段增删、类型、可空性、默认值和主键变化，并分类破坏性与兼容性。"

    def _run(self, *args: Any, **kwargs: Any) -> dict[str, Any]:
        """拒绝同步入口。"""
        del args, kwargs
        raise RuntimeError("compare_schema_drift 只允许异步执行")

    async def _arun(
        self,
        baseline: dict[str, Any],
        current: dict[str, Any],
        run_manager: Any = None,
    ) -> dict[str, Any]:
        """按保守兼容规则比较两个 Schema 快照。"""
        del run_manager
        baseline_tables = _normalize_schema(baseline, "baseline")
        current_tables = _normalize_schema(current, "current")
        logger.info(
            "Schema 漂移比较开始",
            baseline_tables=len(baseline_tables),
            current_tables=len(current_tables),
        )
        breaking: list[dict[str, Any]] = []
        compatible: list[dict[str, Any]] = []
        informational: list[dict[str, Any]] = []

        def add(
            target: list[dict[str, Any]], code: str, path: str, before: Any, after: Any, reason: str
        ) -> None:
            target.append(
                {
                    "code": code,
                    "path": path,
                    "before": before,
                    "after": after,
                    "reason": reason,
                }
            )

        for table_name in sorted(set(baseline_tables) - set(current_tables)):
            add(breaking, "TABLE_REMOVED", table_name, "present", "missing", "删除表会破坏现有查询")
        for table_name in sorted(set(current_tables) - set(baseline_tables)):
            add(compatible, "TABLE_ADDED", table_name, "missing", "present", "新增表不影响既有读取")

        for table_name in sorted(set(baseline_tables) & set(current_tables)):
            old_table = baseline_tables[table_name]
            new_table = current_tables[table_name]
            if old_table["primary_key"] != new_table["primary_key"]:
                add(
                    breaking,
                    "PRIMARY_KEY_CHANGED",
                    table_name,
                    old_table["primary_key"],
                    new_table["primary_key"],
                    "主键变化会改变唯一性和关联语义",
                )
            old_columns = old_table["columns"]
            new_columns = new_table["columns"]
            for column_name in sorted(set(old_columns) - set(new_columns)):
                add(
                    breaking,
                    "COLUMN_REMOVED",
                    f"{table_name}.{column_name}",
                    old_columns[column_name],
                    None,
                    "删除字段会破坏引用该字段的查询",
                )
            for column_name in sorted(set(new_columns) - set(old_columns)):
                column = new_columns[column_name]
                target = (
                    compatible if column["nullable"] or column["default"] is not None else breaking
                )
                reason = (
                    "新增可空或有默认值字段兼容既有写入"
                    if target is compatible
                    else "新增非空且无默认值字段会破坏既有写入"
                )
                add(
                    target,
                    "COLUMN_ADDED",
                    f"{table_name}.{column_name}",
                    None,
                    column,
                    reason,
                )
            for column_name in sorted(set(old_columns) & set(new_columns)):
                old_column = old_columns[column_name]
                new_column = new_columns[column_name]
                path = f"{table_name}.{column_name}"
                if old_column["type"] != new_column["type"]:
                    widening = (old_column["type"], new_column["type"]) in _WIDENING_TYPES
                    add(
                        compatible if widening else breaking,
                        "TYPE_CHANGED",
                        path,
                        old_column["type"],
                        new_column["type"],
                        "类型扩宽可兼容既有值" if widening else "类型变化可能导致解析或精度失败",
                    )
                if old_column["nullable"] != new_column["nullable"]:
                    tightened = old_column["nullable"] and not new_column["nullable"]
                    add(
                        breaking if tightened else compatible,
                        "NULLABILITY_CHANGED",
                        path,
                        old_column["nullable"],
                        new_column["nullable"],
                        "收紧可空性会拒绝既有空值" if tightened else "放宽可空性兼容既有数据",
                    )
                if old_column["default"] != new_column["default"]:
                    add(
                        informational,
                        "DEFAULT_CHANGED",
                        path,
                        old_column["default"],
                        new_column["default"],
                        "默认值变化需要业务确认，但不直接改变既有数据",
                    )
                if old_column["description"] != new_column["description"]:
                    add(
                        informational,
                        "DESCRIPTION_CHANGED",
                        path,
                        old_column["description"],
                        new_column["description"],
                        "字段描述发生变化",
                    )

        result = {
            "compatible": not breaking,
            "breaking_changes": breaking,
            "compatible_changes": compatible,
            "informational_changes": informational,
            "summary": {
                "breaking": len(breaking),
                "compatible": len(compatible),
                "informational": len(informational),
                "total": len(breaking) + len(compatible) + len(informational),
            },
        }
        logger.info(
            "Schema 漂移比较完成",
            compatible=result["compatible"],
            breaking_changes=len(breaking),
            total_changes=result["summary"]["total"],
        )
        return result


def _normalize_schema(snapshot: dict[str, Any], label: str) -> dict[str, dict[str, Any]]:
    """把 Schema 快照规范化为按名称索引的表字段结构。"""
    tables = snapshot.get("tables")
    if not isinstance(tables, list):
        raise ValueError(f"{label}.tables 必须是数组")
    result: dict[str, dict[str, Any]] = {}
    for table in tables:
        if not isinstance(table, dict) or not str(table.get("name", "")).strip():
            raise ValueError(f"{label} 包含无效表定义")
        table_name = str(table["name"])
        if table_name in result:
            raise ValueError(f"{label} 表名重复: {table_name}")
        columns = table.get("columns")
        if not isinstance(columns, list):
            raise ValueError(f"表 {table_name} 的 columns 必须是数组")
        normalized_columns: dict[str, dict[str, Any]] = {}
        for column in columns:
            if not isinstance(column, dict) or not str(column.get("name", "")).strip():
                raise ValueError(f"表 {table_name} 包含无效字段")
            column_name = str(column["name"])
            if column_name in normalized_columns:
                raise ValueError(f"表 {table_name} 字段重复: {column_name}")
            normalized_columns[column_name] = {
                "type": str(column.get("type", "unknown")).lower(),
                "nullable": bool(column.get("nullable", True)),
                "default": column.get("default"),
                "description": str(column.get("description", "")),
            }
        primary_key = [str(item) for item in (table.get("primary_key") or [])]
        missing_keys = [column for column in primary_key if column not in normalized_columns]
        if missing_keys:
            raise ValueError(f"表 {table_name} 的主键字段不存在: {', '.join(missing_keys)}")
        result[table_name] = {"columns": normalized_columns, "primary_key": primary_key}
    return result


_TOOLS: dict[str, BaseTool] = {"compare_schema_drift": CompareSchemaDriftTool()}


def get_tool(name: str) -> BaseTool | None:
    """按名称返回已注册工具。"""
    return _TOOLS.get(name)


def get_tools() -> list[BaseTool]:
    """返回该 Skill 暴露的全部工具。"""
    return list(_TOOLS.values())
