"""报表快照 Drilldown 安全执行器。"""

from __future__ import annotations

from datetime import date, datetime
from typing import Any

from src.logging_config import get_logger

logger = get_logger(__name__)
_ALLOWED_OPERATORS = {"eq", "in", "between"}


# 方法作用：把客户端过滤值转换为可比较的稳定字符串或标量。
# Args: value - Drilldown 过滤值。
# Returns: 可安全比较的值。
def _normalize_filter_value(value: Any) -> Any:
    if isinstance(value, (datetime, date)):
        return value.isoformat()
    if isinstance(value, (str, int, float, bool)) or value is None:
        return value
    if isinstance(value, list) and len(value) <= 100:
        return [_normalize_filter_value(item) for item in value]
    raise ValueError("过滤值类型不受支持")


# 方法作用：校验客户端过滤字段、操作符和类型声明是否来自服务端签发定义。
# Args: definition - 页面版本中的 Drilldown 定义；filters - 客户端过滤字典。
# Returns: 规范化过滤字典。
def validate_filters(definition: dict[str, Any], filters: dict[str, Any]) -> dict[str, Any]:
    allowed = {
        str(item.get("field")): item
        for item in definition.get("allowed_filters", [])
        if isinstance(item, dict) and item.get("field")
    }
    result: dict[str, Any] = {}
    for field, raw_value in filters.items():
        rule = allowed.get(str(field))
        if rule is None:
            raise ValueError(f"过滤字段未签发: {field}")
        operator = str(rule.get("operator", "eq"))
        if operator not in _ALLOWED_OPERATORS:
            raise ValueError("过滤操作符不受支持")
        value = _normalize_filter_value(raw_value)
        if operator == "between" and (not isinstance(value, list) or len(value) != 2):
            raise ValueError("between 过滤必须提供两个值")
        if operator == "in" and not isinstance(value, list):
            raise ValueError("in 过滤必须提供数组")
        result[str(field)] = value
    return result


# 方法作用：在当前不可变版本的数据清单中执行有界快照筛选。
# Args: definition - 服务端签发的 Drilldown 定义；manifest - 版本数据清单；filters - 客户端过滤；page/page_size - 分页参数。
# Returns: 脱敏列、总数和分页行。
def execute_snapshot_drilldown(
    definition: dict[str, Any],
    manifest: dict[str, list[dict[str, Any]]],
    filters: dict[str, Any],
    page: int = 1,
    page_size: int = 50,
) -> dict[str, Any]:
    logger.debug("快照 Drilldown 入口", drilldown_id=str(definition.get("drilldown_id", "")), page=page)
    if str(definition.get("mode", "snapshot")) != "snapshot":
        raise ValueError("公开分享只允许 snapshot Drilldown")
    normalized_filters = validate_filters(definition, filters)
    data_ref = str(definition.get("data_ref", ""))
    rows = list(manifest.get(data_ref, []) or [])
    for field, value in normalized_filters.items():
        rule = next(item for item in definition.get("allowed_filters", []) if item.get("field") == field)
        operator = str(rule.get("operator", "eq"))
        if operator == "eq":
            rows = [row for row in rows if row.get(field) == value]
        elif operator == "in":
            rows = [row for row in rows if row.get(field) in value]
        else:
            low, high = value
            rows = [row for row in rows if low <= row.get(field) <= high]
    columns = [str(column) for column in definition.get("columns", [])][:100]
    max_rows = max(1, min(int(definition.get("max_rows", 100)), 100))
    page_size = max(1, min(int(page_size), max_rows))
    page = max(1, int(page))
    total = len(rows)
    start = (page - 1) * page_size
    selected = [{column: row.get(column) for column in columns} for row in rows[start:start + page_size]]
    result = {"drilldown_id": definition.get("drilldown_id", ""), "columns": columns, "rows": selected, "total": total, "page": page, "page_size": page_size}
    logger.info("快照 Drilldown 完成", drilldown_id=str(definition.get("drilldown_id", "")), total=total, returned=len(selected))
    return result


# 方法作用：从不可变页面文档中查找指定 Drilldown 定义。
# Args: document - ReportDocument 字典；drilldown_id - 客户端提交的 ID。
# Returns: 找到的定义，否则返回 None。
def find_drilldown(document: dict[str, Any], drilldown_id: str) -> dict[str, Any] | None:
    for definition in document.get("drilldowns", []) or []:
        if str(definition.get("drilldown_id", "")) == str(drilldown_id):
            return dict(definition)
    return None
