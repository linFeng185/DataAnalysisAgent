"""仅接受服务端查询模板的实时钻取，逐次复核权限且不调用模型。"""

from __future__ import annotations

import math
from typing import Any

import sqlglot
from sqlglot import exp

from src.logging_config import get_logger
from src.reporting.drilldown import validate_filters
from src.security.sql_execution import normalize_sql_dialect, validate_sql

logger = get_logger(__name__)


# 方法作用：判断模板是否是当前方言下可解析且通过只读校验的单条查询。
# Args: sql - 待签发的服务端 SQL；dialect - SQL 方言。
# Returns: 仅单条只读 Query 返回 True，解析或校验失败返回 False。
def is_single_query(sql: str, dialect: str) -> bool:
    if not str(sql or "").strip():
        return False
    try:
        normalized = normalize_sql_dialect(dialect)
        parsed = sqlglot.parse(sql, read=normalized)
        return len(parsed) == 1 and isinstance(parsed[0], exp.Query) and validate_sql(sql, normalized).valid
    except (sqlglot.errors.ParseError, TypeError, ValueError):
        return False


# 方法作用：只将有界标量转为 AST 字面量，不拼接客户端字符串。
# Args: value - 已校验过滤值。
# Returns: SQL 字面量表达式。
def _literal(value: Any) -> exp.Expression:
    if isinstance(value, str) and len(value) <= 500:
        return exp.Literal.string(value)
    if isinstance(value, bool):
        return exp.Boolean(this=value)
    if isinstance(value, (int, float)) and math.isfinite(value) and abs(value) <= 1e100:
        return exp.convert(value)
    if value is None:
        return exp.Null()
    raise ValueError("过滤值超过有界标量限制")


# 方法作用：在服务端只读模板外增加已签发列投影、字面量过滤与分页上限。
# Args: sql/dialect - 服务端模板和方言；definition - 已签发定义；filters - 参数；page/page_size - 分页。
# Returns: 通过统一只读校验的方言 SQL。
def build_live_sql(sql: str, dialect: str, definition: dict[str, Any], filters: dict[str, Any], page: int, page_size: int) -> str:
    """最多允许浏览前一千行，每次多取一行用于判断后续页面。"""
    dialect = normalize_sql_dialect(dialect)
    if not 1 <= page_size <= min(100, int(definition.get("max_rows", 100))) or page < 1 or page * page_size > 1000:
        raise ValueError("实时钻取超出分页或行数预算")
    try:
        parsed = sqlglot.parse(sql, read=dialect)
    except sqlglot.errors.ParseError as exc:
        raise ValueError("实时钻取模板必须是单条只读查询") from exc
    if len(parsed) != 1 or not isinstance(parsed[0], exp.Query) or not validate_sql(sql, dialect).valid:
        raise ValueError("实时钻取模板必须是单条只读查询")
    columns = list(dict.fromkeys(str(column) for column in definition.get("columns", [])))
    if not columns or len(columns) > 100 or any(not column or len(column) > 128 for column in columns):
        raise ValueError("实时钻取投影定义无效")
    normalized = validate_filters(definition, filters)
    query = exp.select(*(exp.column(column, quoted=True) for column in columns)).from_(parsed[0].subquery("report_live_source"))
    for field, value in normalized.items():
        if field not in columns:
            raise ValueError("过滤字段不在签发列集合中")
        rule = next(item for item in definition["allowed_filters"] if item["field"] == field)
        column = exp.column(field, quoted=True)
        operator = rule.get("operator", "eq")
        if operator == "eq":
            condition = exp.Is(this=column, expression=exp.Null()) if value is None else exp.EQ(this=column, expression=_literal(value))
        elif operator == "in":
            condition = exp.In(this=column, expressions=[_literal(item) for item in value]) if value else exp.false()
        else:
            condition = exp.Between(this=column, low=_literal(value[0]), high=_literal(value[1]))
        query = query.where(condition)
    query = query.order_by(*(exp.column(column, quoted=True) for column in columns)).limit(page_size + 1).offset((page - 1) * page_size)
    rendered = query.sql(dialect=dialect)
    if not validate_sql(rendered, dialect).valid:
        raise ValueError("实时钻取查询未通过安全校验")
    return rendered


# 方法作用：按当前行列权限重建只读查询，执行后再次投影并脱敏。
# Args: query - 私有模板；access - 当前授权；definition/filters - 签发定义和参数；page/page_size - 分页；tenant_id - 当前租户。
# Returns: 有界实时明细和分页状态。
async def execute_live_drilldown(query: dict[str, Any], access: dict[str, Any], definition: dict[str, Any], filters: dict[str, Any], page: int, page_size: int, *, tenant_id: int) -> dict[str, Any]:
    """不接受客户端 SQL，不依赖历史权限快照，不向调用方返回数据库错误正文。"""
    from src.security.data_masker import mask_sensitive_data_with_metadata
    from src.security.permission_check import check_column_whitelist, inject_row_filter
    from src.security.sql_execution import validate_and_execute_sql

    logger.debug("实时钻取执行入口", tenant_id=tenant_id, drilldown_id=definition.get("drilldown_id"), page=page)
    sql = str(query["sql"])
    dialect = str(access.get("dialect") or query["dialect"])
    allowed = list(access.get("allowed_columns", []) or [])
    if allowed and check_column_whitelist(sql, allowed):
        raise PermissionError("当前列权限已变更，不能继续实时钻取")
    # 先对原始物理查询重新注入行权限，再处理展示列过滤，避免聚合之后才过滤。
    row_filter = str(access.get("row_filter_sql", "") or "")
    if row_filter:
        sql = inject_row_filter(sql, row_filter)
    try:
        bounded_sql = build_live_sql(sql, dialect, definition, filters, page, page_size)
    except sqlglot.errors.ParseError as exc:
        # sqlglot 的解析异常不是 ValueError；统一转换后由 API 边界稳定映射为 422。
        raise ValueError("实时钻取模板必须是单条只读查询") from exc
    execution = await validate_and_execute_sql(bounded_sql, str(query["datasource"]), dialect, explain=True, max_rows=page_size + 1, tenant_id=tenant_id)
    if not execution.success:
        raise RuntimeError("实时查询失败，请重试或联系租户管理员")
    columns = list(definition["columns"])
    selected = [{column: row.get(column) for column in columns} for row in execution.data[:page_size]]
    rows, metadata = mask_sensitive_data_with_metadata(selected)
    logger.info("实时钻取脱敏契约探针", metadata_type=type(metadata).__name__, returned=len(rows))
    has_more = len(execution.data) > page_size or execution.truncated
    logger.info("实时钻取执行完成", tenant_id=tenant_id, returned=len(rows), has_more=has_more)
    return {"drilldown_id": definition["drilldown_id"], "mode": "live", "columns": columns, "rows": rows, "masking": metadata, "total": (page - 1) * page_size + len(rows) + int(has_more), "total_is_exact": not has_more, "has_more": has_more, "page": page, "page_size": page_size}
