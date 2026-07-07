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
    if not allowed_columns:
        return None
    try:
        import sqlglot
        from sqlglot import exp
        whitelist = set(c.lower() for c in allowed_columns)
        tree = sqlglot.parse_one(sql)
        if not tree:
            return None
        violations: list[str] = []
        for node in tree.find_all(exp.Column):
            col = node.name.lower()
            if col in ("*", "1", "true", "false"):
                continue
            if col not in whitelist and col not in violations:
                violations.append(col)
        if violations:
            return f"列权限不足: {', '.join(violations[:5])} 不在允许范围内"
        return None
    except Exception:
        return None


def inject_row_filter(sql: str, row_filter: str) -> str:
    """用 sqlglot AST 解析后在 WHERE 注入行过滤条件。

    比字符串拼接安全——不会注入到子查询或字面量中。

    Args:
        sql: 原始 SQL
        row_filter: 行过滤片段，如 "org_id = 5"

    Returns: 注入后的 SQL
    """
    if not row_filter or not row_filter.strip():
        return sql
    try:
        import sqlglot
        from sqlglot import exp
        tree = sqlglot.parse_one(sql)
        if not tree:
            return sql
        where = tree.find(exp.Where)
        filter_expr = sqlglot.parse_one(row_filter)
        if where:
            where.this = exp.And(this=where.this, expression=filter_expr)
        else:
            tree.set("where", exp.Where(this=filter_expr))
        result = tree.sql()
        logger.info("行过滤注入完成", filter=row_filter[:80])
        return result
    except Exception as e:
        logger.warning("行过滤注入失败，使用原 SQL", error=str(e))
        return sql
