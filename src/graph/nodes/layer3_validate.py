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
]


async def layer3_validate_node(state: AnalysisState) -> dict:
    """安全拦截 + sqlglot 语法校验。"""
    _start = time.monotonic()
    logger.info("节点开始", node="layer3_validate")
    sql = state.get("generated_sql", "").strip()

    # 安全拦截
    for pattern, label in _DANGEROUS:
        if re.search(pattern, sql, re.IGNORECASE):
            logger.info("节点完成", node="layer3_validate", elapsed_ms=round((time.monotonic() - _start) * 1000))
            return {
                "sql_valid": False,
                "validation_errors": [{"type": "security_block", "message": f"禁止: {label}"}],
                "validation_warnings": [],
                "transpiled_sql": sql,
            }

    # sqlglot 语法校验
    errors: list[dict] = []
    try:
        import sqlglot
        sqlglot.parse(sql, dialect=state.get("dialect", "clickhouse"))
    except Exception as e:
        errors.append({"type": "syntax_error", "message": str(e)[:500]})

    logger.info("节点完成", node="layer3_validate", elapsed_ms=round((time.monotonic() - _start) * 1000))
    return {
        "sql_valid": len(errors) == 0,
        "validation_errors": errors,
        "validation_warnings": [],
        "transpiled_sql": sql,
    }
