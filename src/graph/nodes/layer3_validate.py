"""4.5 layer3_validate Node — sqlglot 语法校验 + 安全拦截。"""

from __future__ import annotations

import re
import time

from src.graph.state import AnalysisState
from src.logging_config import get_logger

logger = get_logger(__name__)

_DANGEROUS = [
    (r"\bINSERT\b", "INSERT"), (r"\bUPDATE\b", "UPDATE"), (r"\bDELETE\b", "DELETE"),
    (r"\bDROP\b", "DROP"), (r"\bCREATE\b", "CREATE"), (r"\bALTER\b", "ALTER"),
    (r"\bTRUNCATE\b", "TRUNCATE"), (r"\bRENAME\b", "RENAME"),
    (r"\bGRANT\b", "GRANT"), (r"\bREVOKE\b", "REVOKE"),
    (r"\bMERGE\b", "MERGE"), (r"\bREPLACE\b", "REPLACE"),
    (r"\bEXEC\b", "EXEC"), (r"\b(SLEEP|BENCHMARK)\s*\(|\bsleep\s*\(|\bbenchmark\s*\(", "危险函数"),
    (r"\bCOPY\s+.*\bTO\s+PROGRAM\b", "COPY TO PROGRAM"),
    (r"\bINTO\s+OUTFILE\b", "INTO OUTFILE"), (r"\bINTO\s+DUMPFILE\b", "INTO DUMPFILE"),
    (r"\bLOAD_FILE\s*\(", "LOAD_FILE"), (r"\bATTACH\b", "ATTACH DATABASE"),
    (r"\bpg_read_file\b", "pg_read_file"), (r"\bDBCC\b", "DBCC"),
    (r"\bxp_\w+", "扩展存储过程"),
]


def validate_readonly_sql(sql: str, dialect: str) -> list[dict]:
    """使用正则与 sqlglot AST 验证 SQL 仅包含只读语句。

    Args:
        sql: 待验证的 SQL 文本。
        dialect: sqlglot 使用的数据库方言。

    Returns:
        错误列表；空列表表示校验通过。
    """
    logger.debug("只读 SQL 校验入口", dialect=dialect, sql_preview=sql[:120])
    if not sql.strip():
        logger.warning("只读 SQL 校验失败", reason="SQL 为空")
        return [{"type": "syntax_error", "message": "SQL 不能为空"}]

    for pattern, label in _DANGEROUS:
        if re.search(pattern, sql, re.IGNORECASE):
            logger.warning("只读 SQL 校验拦截", operation=label)
            return [{"type": "security_block", "message": f"禁止: {label}"}]

    try:
        import sqlglot
        from sqlglot import exp

        sqlglot_dialect = "tsql" if (dialect or "").lower() == "mssql" else dialect
        if sqlglot_dialect != dialect:
            logger.info(
                "SQL 校验方言别名映射",
                source_dialect=dialect,
                target_dialect=sqlglot_dialect,
            )
        statements = sqlglot.parse(sql, read=sqlglot_dialect or None)
        if len(statements) != 1 or statements[0] is None:
            logger.warning("只读 SQL 校验失败", reason="仅允许单条 SQL")
            return [{"type": "security_block", "message": "仅允许单条只读 SQL"}]

        tree = statements[0]
        allowed = isinstance(tree, exp.Query)

        if isinstance(tree, exp.Show):
            allowed = True
        elif isinstance(tree, exp.Describe):
            target = tree.this
            allowed = isinstance(target, (exp.Table, exp.Query))
        elif isinstance(tree, exp.Command):
            command = str(tree.this or "").upper()
            if command == "SHOW":
                allowed = True
            elif command == "EXPLAIN":
                payload = tree.expression.this if tree.expression is not None else ""
                allowed = bool(payload) and not validate_readonly_sql(str(payload), dialect)

        if not allowed:
            operation = str(getattr(tree, "key", type(tree).__name__)).upper()
            logger.warning("只读 SQL AST 拦截", operation=operation)
            return [{
                "type": "security_block",
                "message": f"仅允许 SELECT/SHOW/DESCRIBE/EXPLAIN，当前为 {operation}",
            }]

        logger.info("只读 SQL 校验通过", dialect=dialect, statement=type(tree).__name__)
        return []
    except Exception as exc:
        logger.error("只读 SQL 解析失败", error=str(exc), exc_info=True)
        return [{"type": "syntax_error", "message": str(exc)[:500]}]


async def layer3_validate_node(state: AnalysisState) -> dict:
    """安全拦截 + sqlglot 语法校验。"""
    _start = time.monotonic()
    logger.info("节点开始", node="layer3_validate")
    sql = state.get("generated_sql", "").strip()

    errors = validate_readonly_sql(sql, state.get("dialect", "clickhouse"))

    logger.info("节点完成", node="layer3_validate", elapsed_ms=round((time.monotonic() - _start) * 1000))
    return {
        "sql_valid": len(errors) == 0,
        "validation_errors": errors,
        "validation_warnings": [],
        "transpiled_sql": sql,
    }
