"""SQL 只读校验、真实方言 EXPLAIN 与有界执行统一边界。"""

from __future__ import annotations

import re
from typing import Any

from pydantic import BaseModel, Field

from src.failure_policy import FailureDomain, must_fail_closed
from src.logging_config import get_logger


logger = get_logger(__name__)

_STATE_MUTATING_FUNCTIONS = frozenset({
    "benchmark",
    "dblink_exec",
    "load_file",
    "lo_export",
    "nextval",
    "pg_advisory_lock",
    "pg_advisory_lock_shared",
    "pg_read_file",
    "pg_reload_conf",
    "pg_try_advisory_lock",
    "pg_try_advisory_lock_shared",
    "set_config",
    "setval",
    "sleep",
})
_WRITE_PREFIX = re.compile(
    r"^\s*(?:INSERT|UPDATE|DELETE|DROP|CREATE|ALTER|TRUNCATE|RENAME|GRANT|"
    r"REVOKE|MERGE|REPLACE|CALL|VACUUM|SET|COPY|ATTACH|DBCC|EXEC)\b",
    re.IGNORECASE,
)
_ANSI_ESCAPE = re.compile(r"\x1b\[[0-9;]*m")


class SQLValidationResult(BaseModel):
    """只读 SQL 校验结果。"""

    valid: bool
    sql: str
    dialect: str
    errors: list[dict[str, Any]] = Field(default_factory=list)


class SQLExecutionResult(BaseModel):
    """SQL EXPLAIN 或执行的统一结果。"""

    success: bool
    sql: str
    dialect: str
    data: list[dict[str, Any]] = Field(default_factory=list)
    row_count: int = 0
    truncated: bool = False
    error: str = ""
    error_type: str = ""
    details: list[dict[str, Any]] = Field(default_factory=list)
    explain_plan: dict[str, Any] = Field(default_factory=dict)


# 方法作用：把项目方言别名转换为 sqlglot 可识别名称。
# Args: dialect - 数据源声明的方言。
# Returns: sqlglot 方言名称，空输入保持为空。
def normalize_sql_dialect(dialect: str) -> str:
    logger.debug("规范化 SQL 方言入口", dialect=dialect)
    normalized = (dialect or "").strip().lower()
    result = {
        "mssql": "tsql",
        "postgresql": "postgres",
    }.get(normalized, normalized)
    logger.info("规范化 SQL 方言完成", source=normalized, target=result)
    return result


# 方法作用：移除 SQL 注释以执行连接前的明显写操作预检。
# Args: sql - 原始 SQL 文本。
# Returns: 移除行注释和块注释后的文本。
def _strip_sql_comments(sql: str) -> str:
    logger.debug("移除 SQL 注释入口", sql_chars=len(sql))
    without_blocks = re.sub(r"/\*.*?\*/", " ", sql, flags=re.DOTALL)
    result = re.sub(r"--[^\r\n]*", " ", without_blocks)
    logger.info("移除 SQL 注释完成", sql_chars=len(result))
    return result


# 方法作用：在连接数据源前阻断空 SQL 和明显写操作。
# Args: sql - 原始 SQL 文本；dialect - 调用方提示方言。
# Returns: 预检错误列表，空列表表示继续按真实方言解析。
def _precheck_sql(sql: str, dialect: str) -> list[dict[str, Any]]:
    logger.debug("SQL 连接前预检入口", dialect=dialect, sql_chars=len(sql))
    if not sql.strip():
        result = [{"type": "syntax_error", "message": "SQL 不能为空"}]
        logger.warning("SQL 连接前预检拒绝", reason="SQL 为空")
        return result
    comment_free = _strip_sql_comments(sql)
    match = _WRITE_PREFIX.search(comment_free)
    if match:
        operation = match.group(0).strip().split(maxsplit=1)[0].upper()
        result = [{"type": "security_block", "message": f"禁止: {operation}"}]
        logger.warning("SQL 连接前预检拒绝", operation=operation)
        return result
    logger.debug("SQL 连接前预检完成", valid=True)
    return []


# 方法作用：检查查询 AST 中的写表、锁和状态变更函数副作用。
# Args: tree - sqlglot 单条 SQL AST；query_type - sqlglot Query 类型。
# Returns: 无副作用返回空字符串，否则返回阻断原因。
def _query_side_effect(tree: Any, query_type: type[Any]) -> str:
    logger.debug("检查查询副作用入口", statement_type=type(tree).__name__)
    try:
        from sqlglot import exp

        if isinstance(tree, exp.Select) and tree.args.get("into") is not None:
            result = "SELECT INTO"
        elif isinstance(tree, exp.Select) and tree.args.get("locks"):
            result = "SELECT FOR UPDATE/SHARE"
        elif not isinstance(tree, query_type):
            result = ""
        else:
            result = ""
            for function in tree.find_all(exp.Func):
                function_name = str(
                    function.name if isinstance(function, exp.Anonymous) else function.sql_name()
                ).strip().lower()
                if function_name in _STATE_MUTATING_FUNCTIONS or function_name.startswith("xp_"):
                    result = f"状态变更函数 {function_name}"
                    break
    except Exception as exc:
        logger.error("检查查询副作用失败", error=str(exc), exc_info=True)
        raise
    logger.info("检查查询副作用完成", blocked=bool(result), reason=result)
    return result


# 方法作用：使用真实数据库方言和 AST 校验单条 SQL 只读性。
# Args: sql - 待校验 SQL；dialect - 数据源真实方言。
# Returns: 包含有效性、方言和错误列表的 SQLValidationResult。
def validate_sql(sql: str, dialect: str) -> SQLValidationResult:
    logger.debug("统一 SQL 校验入口", dialect=dialect, sql_chars=len(sql))
    preliminary_errors = _precheck_sql(sql, dialect)
    if preliminary_errors:
        return SQLValidationResult(
            valid=False,
            sql=sql,
            dialect=(dialect or "").strip().lower(),
            errors=preliminary_errors,
        )

    parser_dialect = normalize_sql_dialect(dialect)
    try:
        import sqlglot
        from sqlglot import exp

        statements = sqlglot.parse(sql, read=parser_dialect or None)
        if len(statements) != 1 or statements[0] is None:
            errors = [{"type": "security_block", "message": "仅允许单条只读 SQL"}]
        else:
            tree = statements[0]
            side_effect = _query_side_effect(tree, exp.Query)
            errors: list[dict[str, Any]] = []
            if side_effect:
                errors.append({
                    "type": "security_block",
                    "message": f"禁止具有数据库副作用的查询: {side_effect}",
                })
            else:
                allowed = isinstance(tree, exp.Query)
                if isinstance(tree, exp.Show):
                    allowed = True
                elif isinstance(tree, exp.Describe):
                    allowed = isinstance(tree.this, (exp.Table, exp.Query))
                elif isinstance(tree, exp.Command):
                    command = str(tree.this or "").upper()
                    if command == "SHOW":
                        allowed = True
                    elif command == "EXPLAIN":
                        payload = tree.expression.this if tree.expression is not None else ""
                        nested = validate_sql(str(payload), dialect) if payload else None
                        allowed = bool(nested and nested.valid)
                        if nested and not nested.valid:
                            errors.extend(nested.errors)
                if not allowed and not errors:
                    operation = str(getattr(tree, "key", type(tree).__name__)).upper()
                    errors.append({
                        "type": "security_block",
                        "message": f"仅允许 SELECT/SHOW/DESCRIBE/EXPLAIN，当前为 {operation}",
                    })
    except Exception as exc:
        must_fail_closed(FailureDomain.SQL_SECURITY)
        message = _ANSI_ESCAPE.sub("", str(exc))[:500]
        logger.error(
            "统一 SQL 解析失败",
            dialect=dialect,
            error=message,
            exc_info=True,
        )
        errors = [{"type": "syntax_error", "message": message}]

    result = SQLValidationResult(
        valid=not errors,
        sql=sql,
        dialect=(dialect or "").strip().lower(),
        errors=errors,
    )
    logger.info(
        "统一 SQL 校验完成",
        dialect=result.dialect,
        valid=result.valid,
        error_count=len(result.errors),
    )
    return result


# 方法作用：在统一方言解析边界内提取 SQL 调用的函数名称。
# Args: sql - 已通过统一校验的 SQL；dialect - 数据源真实方言。
# Returns: AST 中出现的函数名称列表。
def extract_sql_function_names(sql: str, dialect: str) -> list[str]:
    logger.debug("提取 SQL 函数入口", dialect=dialect, sql_chars=len(sql))
    try:
        import sqlglot
        from sqlglot import exp

        tree = sqlglot.parse_one(sql, read=normalize_sql_dialect(dialect) or None)
        result = [
            str(node.sql_name() or "").upper()
            for node in tree.walk()
            if isinstance(node, (exp.Anonymous, exp.Func)) and node.sql_name()
        ]
    except Exception as exc:
        must_fail_closed(FailureDomain.SQL_SECURITY)
        logger.error("提取 SQL 函数失败", dialect=dialect, error=str(exc), exc_info=True)
        raise
    logger.info("提取 SQL 函数完成", dialect=dialect, function_count=len(result))
    return result


# 方法作用：解析数据源并取得绑定其共享引擎的 Connector。
# Args: datasource - 数据源名称；dialect - 调用方提示方言。
# Returns: 数据源配置、Connector 和 Registry 权威方言。
async def _resolve_sql_target(datasource: str, dialect: str) -> tuple[Any, Any, str]:
    logger.debug("解析 SQL 执行目标入口", datasource=datasource, dialect=dialect)
    from src.datasource.registry import get_registry

    registry = get_registry()
    resolver = getattr(registry, "resolve", None) or getattr(registry, "resolve_or_none", None)
    if resolver is None:
        logger.error("解析 SQL 执行目标失败", datasource=datasource, reason="Registry 缺少解析接口")
        raise RuntimeError("DataSourceRegistry 缺少解析接口")
    resolved = await resolver(datasource)
    if resolved is None:
        logger.error("解析 SQL 执行目标失败", datasource=datasource, reason="数据源不可用")
        raise RuntimeError(f"数据源 '{datasource}' 不可用")
    authoritative_dialect = str(getattr(resolved, "dialect", "") or dialect).strip().lower()
    if dialect and authoritative_dialect != str(dialect).strip().lower():
        logger.warning(
            "SQL 方言提示与数据源不一致",
            datasource=datasource,
            requested=dialect,
            authoritative=authoritative_dialect,
        )
    connector = getattr(resolved, "connector", None)
    if connector is None:
        from src.connectors.registry import create_connector

        connector = create_connector(resolved)
        engine = getattr(resolved, "engine", None)
        if engine is not None:
            connector = connector.attach_engine(engine)
        resolved.connector = connector
        logger.info("SQL 执行目标补建 Connector", datasource=datasource)
    logger.info(
        "解析 SQL 执行目标完成",
        datasource=datasource,
        dialect=authoritative_dialect,
        connector=type(connector).__name__,
    )
    return resolved, connector, authoritative_dialect


# 方法作用：构造统一 SQL 失败结果并保证错误不会被误报为成功。
# Args: sql - 最终 SQL；dialect - 权威方言；error - 错误摘要；error_type - 阶段分类；details - 结构化明细。
# Returns: success=False 的 SQLExecutionResult。
def _failure_result(
    sql: str,
    dialect: str,
    error: str,
    error_type: str,
    details: list[dict[str, Any]] | None = None,
) -> SQLExecutionResult:
    logger.debug("构造 SQL 失败结果入口", dialect=dialect, error_type=error_type)
    result = SQLExecutionResult(
        success=False,
        sql=sql,
        dialect=dialect,
        error=error,
        error_type=error_type,
        details=details or [],
    )
    logger.info("构造 SQL 失败结果完成", dialect=dialect, error_type=error_type)
    return result


# 方法作用：按真实数据源方言完成重写、只读校验和 EXPLAIN。
# Args: sql - 待检查 SQL；datasource - 数据源名；dialect - 调用方提示方言。
# Returns: EXPLAIN 成功或明确失败的 SQLExecutionResult。
async def validate_and_explain_sql(
    sql: str,
    datasource: str,
    dialect: str = "",
) -> SQLExecutionResult:
    logger.debug("统一 SQL EXPLAIN 入口", datasource=datasource, dialect=dialect)
    precheck_errors = _precheck_sql(sql, dialect)
    if precheck_errors:
        return _failure_result(sql, dialect, "SQL 校验失败", "security", precheck_errors)
    try:
        _, connector, authoritative_dialect = await _resolve_sql_target(datasource, dialect)
        from src.tools.sql_rewriter import rewrite_sql

        rewritten_sql = rewrite_sql(sql, authoritative_dialect)
        validation = validate_sql(rewritten_sql, authoritative_dialect)
        if not validation.valid:
            error_type = (
                "security"
                if any(item.get("type") == "security_block" for item in validation.errors)
                else "sql_semantic"
            )
            return _failure_result(
                rewritten_sql,
                authoritative_dialect,
                "SQL 校验失败",
                error_type,
                validation.errors,
            )
        plan = await connector.explain(rewritten_sql)
        if not plan.get("valid", False):
            errors = plan.get("errors", []) or [{
                "type": "semantic_error",
                "message": "EXPLAIN 校验失败",
            }]
            message = "; ".join(str(item.get("message", "EXPLAIN 校验失败")) for item in errors)
            return _failure_result(
                rewritten_sql,
                authoritative_dialect,
                message,
                "sql_semantic",
                errors,
            )
    except Exception as exc:
        must_fail_closed(FailureDomain.DATABASE)
        logger.error(
            "统一 SQL EXPLAIN 失败",
            datasource=datasource,
            error=str(exc),
            exc_info=True,
        )
        return _failure_result(sql, dialect, str(exc)[:500], "configuration")

    result = SQLExecutionResult(
        success=True,
        sql=rewritten_sql,
        dialect=authoritative_dialect,
        explain_plan=plan,
    )
    logger.info("统一 SQL EXPLAIN 完成", datasource=datasource, dialect=authoritative_dialect)
    return result


# 方法作用：按真实数据源方言完成只读校验、可选 EXPLAIN 和有界执行。
# Args: sql - 待执行 SQL；datasource - 数据源名；dialect - 调用方提示方言；explain - 是否执行 EXPLAIN；max_rows - 结果上限。
# Returns: 数据、截断状态或明确错误的 SQLExecutionResult。
async def validate_and_execute_sql(
    sql: str,
    datasource: str,
    dialect: str = "",
    *,
    explain: bool = True,
    max_rows: int | None = None,
) -> SQLExecutionResult:
    logger.debug(
        "统一 SQL 执行入口",
        datasource=datasource,
        dialect=dialect,
        explain=explain,
        max_rows=max_rows,
    )
    precheck_errors = _precheck_sql(sql, dialect)
    if precheck_errors:
        return _failure_result(sql, dialect, "SQL 校验失败", "security", precheck_errors)
    try:
        _, connector, authoritative_dialect = await _resolve_sql_target(datasource, dialect)
        from src.config import get_settings
        from src.tools.sql_rewriter import rewrite_sql

        rewritten_sql = rewrite_sql(sql, authoritative_dialect)
        validation = validate_sql(rewritten_sql, authoritative_dialect)
        if not validation.valid:
            error_type = (
                "security"
                if any(item.get("type") == "security_block" for item in validation.errors)
                else "sql_semantic"
            )
            return _failure_result(
                rewritten_sql,
                authoritative_dialect,
                "SQL 校验失败",
                error_type,
                validation.errors,
            )
        if explain:
            plan = await connector.explain(rewritten_sql)
            if not plan.get("valid", False):
                errors = plan.get("errors", []) or [{
                    "type": "semantic_error",
                    "message": "EXPLAIN 校验失败",
                }]
                message = "; ".join(
                    str(item.get("message", "EXPLAIN 校验失败")) for item in errors
                )
                return _failure_result(
                    rewritten_sql,
                    authoritative_dialect,
                    message,
                    "sql_semantic",
                    errors,
                )
        limit = max_rows if max_rows is not None else int(get_settings().max_result_rows)
        rows, truncated = await connector.execute_bounded(rewritten_sql, max(1, limit))
    except Exception as exc:
        must_fail_closed(FailureDomain.DATABASE)
        logger.error(
            "统一 SQL 执行失败",
            datasource=datasource,
            error=str(exc),
            exc_info=True,
        )
        return _failure_result(sql, dialect, str(exc)[:500], "execution")

    result = SQLExecutionResult(
        success=True,
        sql=rewritten_sql,
        dialect=authoritative_dialect,
        data=rows,
        row_count=len(rows),
        truncated=truncated,
    )
    logger.info(
        "统一 SQL 执行完成",
        datasource=datasource,
        dialect=authoritative_dialect,
        row_count=result.row_count,
        truncated=truncated,
    )
    return result
