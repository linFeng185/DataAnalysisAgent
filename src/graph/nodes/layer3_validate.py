"""4.5 layer3_validate Node — sqlglot 语法校验 + 安全拦截。"""

from __future__ import annotations

import time

from src.graph.state import AnalysisState
from src.logging_config import get_logger

logger = get_logger(__name__)

# 方法作用：兼容旧调用并委托统一 SQL 安全校验服务。
# Args: sql - 待验证 SQL；dialect - 数据源真实方言。
# Returns: 错误列表，空列表表示只读校验通过。
def validate_readonly_sql(sql: str, dialect: str) -> list[dict]:
    """使用正则与 sqlglot AST 验证 SQL 仅包含只读语句。

    Args:
        sql: 待验证的 SQL 文本。
        dialect: sqlglot 使用的数据库方言。

    Returns:
        错误列表；空列表表示校验通过。
    """
    logger.debug("只读 SQL 兼容校验入口", dialect=dialect, sql_chars=len(sql))
    from src.security.sql_execution import validate_sql

    result = validate_sql(sql, dialect)
    logger.info("只读 SQL 兼容校验完成", dialect=dialect, valid=result.valid)
    return result.errors


async def layer3_validate_node(state: AnalysisState) -> dict:
    """安全拦截 + sqlglot 语法校验。"""
    from src.graph.context import read_contexts

    contexts = read_contexts(state)
    _start = time.monotonic()
    logger.debug("节点开始", node="layer3_validate")
    sql = state.get("generated_sql", "").strip()

    errors = validate_readonly_sql(sql, contexts.execution.dialect or "clickhouse")

    # 列引用校验必须早于 layer4_explain：EXPLAIN 会直接把 SQL 打到数据库，
    # 幻觉列在那里只能拿到原始 DB 报错（column "x" does not exist），模型看不到可用列清单，
    # 重试只能继续猜。放在这里失败会被写进 validation_errors，由 after_layer3 路由回 generate_sql。
    if not errors:
        from src.graph.nodes.execute_sql import _validate_column_references

        column_error = _validate_column_references(
            sql,
            state.get("relevant_tables", []) or [],
            contexts.execution.dialect or "",
        )
        if column_error:
            errors = [{"type": "column_reference", "message": column_error}]
            logger.warning(
                "列名校验拦截",
                dialect=contexts.execution.dialect,
                error=column_error,
            )

    logger.debug("节点完成", node="layer3_validate", elapsed_ms=round((time.monotonic() - _start) * 1000))
    update = {
        "sql_valid": len(errors) == 0,
        "validation_errors": errors,
        "validation_warnings": [],
        "transpiled_sql": sql,
    }
    from src.graph.context import with_execution_context

    return with_execution_context(state, update)
