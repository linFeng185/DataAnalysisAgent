"""SQL 方言重写器 — 修复 LLM 常见的方言语法错误。

代码级修正（非 LLM call），执行前自动运行。
每个方言有独立的规则集，按需扩展。
"""

from __future__ import annotations

import re

from src.logging_config import get_logger

logger = get_logger(__name__)

_MYSQL_FIXES: list[tuple[str, str, str]] = [
    # INTERVAL 'N' UNIT → INTERVAL N UNIT（去掉 LLM 误加的引号）
    (r"\bINTERVAL\s+'(\d+)'\s+(\w+)",
     r"INTERVAL \1 \2"),
    # CURDATE() - INTERVAL → DATE_SUB(CURDATE(), INTERVAL ...)
    (r"\bCURDATE\(\)\s*-\s*INTERVAL\s+(\d+)\s+(\w+)",
     r"DATE_SUB(CURDATE(), INTERVAL \1 \2)"),
    # NOW() - INTERVAL → DATE_SUB(NOW(), INTERVAL ...)
    (r"\bNOW\(\)\s*-\s*INTERVAL\s+(\d+)\s+(\w+)",
     r"DATE_SUB(NOW(), INTERVAL \1 \2)"),
    # DATE(col) - INTERVAL → DATE_SUB(DATE(col), INTERVAL ...)
    (r"\bDATE\((\w+)\)\s*-\s*INTERVAL\s+(\d+)\s+(\w+)",
     r"DATE_SUB(DATE(\1), INTERVAL \2 \3)"),
    # col - INTERVAL → DATE_SUB(col, INTERVAL ...)
    (r"(\w+)\s*-\s*INTERVAL\s+(\d+)\s+(\w+)",
     r"DATE_SUB(\1, INTERVAL \2 \3)"),
    # CURDATE() + INTERVAL → DATE_ADD (避免歧义)
    (r"\bCURDATE\(\)\s*\+\s*INTERVAL\s+(\d+)\s+(\w+)",
     r"DATE_ADD(CURDATE(), INTERVAL \1 \2)"),
    # STRING_AGG → GROUP_CONCAT (MySQL 无 STRING_AGG)
    (r"\bSTRING_AGG\(([^,]+),\s*([^)]+)\)",
     r"GROUP_CONCAT(\1 SEPARATOR \2)"),
    # ILIKE → LIKE
    (r"\bILIKE\b", r"LIKE"),
]

_CLICKHOUSE_FIXES: list[tuple[str, str, str]] = [
    (r"\bGROUP_CONCAT\(([^)]+)\)", r"arrayStringConcat(groupArray(\1), ',')"),
    (r"\bIFNULL\(([^,]+),\s*([^)]+)\)", r"ifNull(\1, \2)"),
]

_POSTGRES_FIXES: list[tuple[str, str, str]] = [
    (r"\bGROUP_CONCAT\(([^,]+),\s*SEPARATOR\s+([^)]+)\)",
     r"STRING_AGG(\1, \2)"),
    (r"\bIFNULL\(([^,]+),\s*([^)]+)\)", r"COALESCE(\1, \2)"),
    (r"\bDATE_FORMAT\(([^,]+),\s*'([^']+)'\)", r"TO_CHAR(\1, '\2')"),
]

_ORACLE_FIXES: list[tuple[str, str, str]] = [
    (r"\bIFNULL\(([^,]+),\s*([^)]+)\)", r"NVL(\1, \2)"),
    (r"\bGROUP_CONCAT\(([^,]+),\s*SEPARATOR\s+([^)]+)\)",
     r"LISTAGG(\1, \2) WITHIN GROUP (ORDER BY \1)"),
]

_MSSQL_FIXES: list[tuple[str, str, str]] = [
    (r"\bIFNULL\(([^,]+),\s*([^)]+)\)", r"ISNULL(\1, \2)"),
    (r"\bGROUP_CONCAT\(([^,]+),\s*SEPARATOR\s+([^)]+)\)",
     r"STRING_AGG(\1, \2)"),
]

_RULES: dict[str, list[tuple[str, str, str]]] = {
    "mysql": _MYSQL_FIXES,
    "clickhouse": _CLICKHOUSE_FIXES,
    "postgres": _POSTGRES_FIXES,
    "oracle": _ORACLE_FIXES,
    "mssql": _MSSQL_FIXES,
}


# 修正常见跨方言 SQL，并用目标方言重新渲染标识符。
# Args: sql - 待修正的 SQL；dialect - 目标数据库方言。
# Returns: 可交给目标数据库执行的修正后 SQL。
def rewrite_sql(sql: str, dialect: str) -> str:
    """两层修正：① 正则替换跨方言函数 ② sqlglot 标识符加引号。

    Layer 2 解决 year_month / rank / status 等别名撞保留字的问题。
    """
    logger.debug("rewrite_sql 入口", dialect=dialect, sql=sql[:150])
    rules = _RULES.get(dialect.lower(), [])
    applied = 0

    # Layer 1: 正则替换
    if rules:
        for pattern, replacement in rules:
            new_sql, count = re.subn(pattern, replacement, sql, flags=re.IGNORECASE)
            if count > 0:
                sql = new_sql
                applied += count

    # PostgreSQL 两参数 ROUND 仅接受 numeric，显式 CAST 避免 double precision 运行时失败。
    if dialect.lower() == "postgres":
        try:
            import sqlglot
            from sqlglot import exp

            tree = sqlglot.parse_one(sql, read="postgres")
            round_fixes = 0
            round_skips = 0
            for round_node in tree.find_all(exp.Round):
                if round_node.args.get("decimals") is None:
                    continue
                round_input = round_node.this
                cast_type = (
                    round_input.args.get("to")
                    if isinstance(round_input, exp.Cast)
                    else None
                )
                if (
                    isinstance(cast_type, exp.DataType)
                    and cast_type.this == exp.DataType.Type.DECIMAL
                ):
                    round_skips += 1
                    continue
                round_node.set(
                    "this",
                    exp.Cast(
                        this=round_input.copy(),
                        to=exp.DataType.build("DECIMAL"),
                    ),
                )
                round_fixes += 1
            if round_fixes:
                sql = tree.sql(dialect="postgres")
                applied += round_fixes
                logger.info("PostgreSQL ROUND 类型修正完成", fixes=round_fixes)
            if round_skips:
                logger.info(
                    "PostgreSQL ROUND 类型修正跳过",
                    reason="输入已是 DECIMAL",
                    skips=round_skips,
                )
        except Exception as exc:
            logger.error(
                "PostgreSQL ROUND 类型修正失败",
                error=str(exc),
                exc_info=True,
            )

    # Layer 2: 所有标识符统一加引号（防撞保留字 + 避免正则遗漏）
    # sqlglot 是 AST 解析器，能区分 列名/表名/别名 vs 函数名/关键字——
    # 不会给 NOW() 或 AS/FROM 加引号。方言自动选择引号类型:
    #   MySQL/ClickHouse → `backtick`
    #   PostgreSQL/Oracle  → "double-quote"
    #   MSSQL              → [bracket]
    try:
        import sqlglot
        quoted = sqlglot.transpile(sql, read=dialect, write=dialect, identify=True)
        if quoted and quoted[0] != sql:
            sql = quoted[0]
            applied += 1
    except Exception as exc:
        logger.error("SQL 标识符引用重写失败", error=str(exc), exc_info=True)

    if applied > 0:
        logger.info("SQL 方言重写", dialect=dialect, fixes=applied,
                    result=sql[:150])
    logger.info("rewrite_sql 完成", dialect=dialect, fixes=applied, sql_chars=len(sql))
    return sql
