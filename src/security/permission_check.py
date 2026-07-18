"""行列级权限校验——列白名单拦截 + 行过滤注入。"""

from __future__ import annotations

from src.logging_config import get_logger

logger = get_logger(__name__)


def check_column_whitelist(sql: str, allowed_columns: list[str]) -> str | None:
    """校验 SQL 引用的列是否全部在白名单中。空列表=全部允许。

    Args:
        sql: 待校验 SQL
        allowed_columns: 允许的列名列表

    Returns: 错误消息或 None（通过）
    """
    logger.debug("列白名单校验入口", allowed_count=len(allowed_columns), sql_preview=sql[:120])
    if not allowed_columns:
        logger.info("列白名单为空，允许全部列")
        return None
    try:
        import sqlglot
        from sqlglot import exp
        whitelist = set(c.lower() for c in allowed_columns)
        tree = sqlglot.parse_one(sql)
        if not tree:
            logger.warning("列白名单校验失败", reason="SQL 解析结果为空")
            return "列权限校验解析失败: SQL 为空"

        for select in tree.find_all(exp.Select):
            has_wildcard_projection = any(
                isinstance(projection, exp.Star)
                or (isinstance(projection, exp.Column) and projection.is_star)
                for projection in select.expressions
            )
            if has_wildcard_projection:
                logger.warning("列白名单校验失败", reason="投影包含通配符")
                return "列权限不足: 启用列白名单时禁止使用通配符 *"

        violations: list[str] = []
        for node in tree.find_all(exp.Column):
            col = node.name.lower()
            if col in ("*", "1", "true", "false"):
                continue
            if col not in whitelist and col not in violations:
                violations.append(col)
        if violations:
            message = f"列权限不足: {', '.join(violations[:5])} 不在允许范围内"
            logger.warning("列白名单校验失败", violations=violations[:5])
            return message
        logger.info("列白名单校验通过", referenced_columns=len(list(tree.find_all(exp.Column))))
        return None
    except Exception as exc:
        logger.error("列白名单 SQL 解析失败", error=str(exc), exc_info=True)
        return f"列权限校验解析失败: {str(exc)[:200]}"


def inject_row_filter(sql: str, row_filter: str) -> str:
    """用 sqlglot AST 解析后在 WHERE 注入行过滤条件。

    比字符串拼接安全——不会注入到子查询或字面量中。

    Args:
        sql: 原始 SQL
        row_filter: 行过滤片段，如 "org_id = 5"

    Returns: 注入后的 SQL
    """
    logger.debug("行过滤注入入口", sql_preview=sql[:120], has_filter=bool(row_filter.strip()))
    if not row_filter or not row_filter.strip():
        logger.info("行过滤为空，保留原 SQL")
        return sql
    try:
        import sqlglot
        from sqlglot import exp
        from src.exceptions import SQLSecurityError

        tree = sqlglot.parse_one(sql)
        if not tree:
            raise SQLSecurityError("原 SQL 无法解析", "ROW_FILTER")
        filter_expr = sqlglot.parse_one(row_filter)
        if not filter_expr or isinstance(filter_expr, (exp.Query, exp.Command)):
            raise SQLSecurityError("行过滤条件不是布尔表达式", "ROW_FILTER")
        where = tree.args.get("where")
        if where:
            where.this = exp.And(this=where.this, expression=filter_expr)
        else:
            tree.set("where", exp.Where(this=filter_expr))
        result = tree.sql()
        logger.info("行过滤注入完成", filter=row_filter[:80])
        return result
    except Exception as exc:
        from src.exceptions import SQLSecurityError

        if isinstance(exc, SQLSecurityError):
            logger.error("行过滤注入被安全阻断", error=str(exc), exc_info=True)
            raise
        logger.error("行过滤注入失败", error=str(exc), exc_info=True)
        raise SQLSecurityError("行过滤注入失败", "ROW_FILTER") from exc
