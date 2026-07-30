"""4.6 layer4_explain Node，通过注册连接器执行方言 EXPLAIN。"""

from __future__ import annotations

import time

from src.graph.state import AnalysisState
from src.logging_config import get_logger

logger = get_logger(__name__)


# 方法作用：重写 SQL 后委托数据源 Connector 执行 EXPLAIN 校验。
# Args: state - 当前 LangGraph 分析状态。
# Returns: explain_errors、sql_valid 和重写后的 generated_sql。
async def layer4_explain_node(state: AnalysisState) -> dict:
    """在真实执行前使用与执行节点相同的方言连接器检查 SQL。"""
    from src.graph.context import read_contexts

    contexts = read_contexts(state)
    started_at = time.monotonic()
    sql = (state.get("generated_sql", "") or "").strip()
    dialect = contexts.execution.dialect.lower()
    datasource = contexts.routing.datasource
    logger.info(
        "EXPLAIN 边界输入",
        datasource=datasource,
        dialect=dialect,
        sql=sql,
        has_resolved_schema=state.get("resolved_schema") is not None,
    )
    if not sql:
        error = {"type": "semantic_error", "message": "EXPLAIN 的 SQL 不能为空"}
        logger.warning("EXPLAIN 拒绝", datasource=datasource, reason="SQL 为空")
        from src.graph.context import with_execution_context

        return with_execution_context(
            state,
            {"explain_errors": [error], "sql_valid": False},
        )

    from src.security.sql_execution import validate_and_explain_sql

    validation = await validate_and_explain_sql(sql, datasource, dialect)
    if not validation.success:
        errors = validation.details or [{
            "type": validation.error_type or "semantic_error",
            "message": validation.error or "EXPLAIN 校验失败",
        }]
        logger.warning(
            "EXPLAIN 校验拒绝",
            datasource=datasource,
            dialect=validation.dialect or dialect,
            errors=errors,
        )
        update = {
            "explain_errors": errors,
            "sql_valid": False,
            "generated_sql": validation.sql or sql,
            "sql_explain_checked": False,
        }
        from src.graph.context import with_execution_context

        return with_execution_context(state, update)
    sql = validation.sql

    elapsed_ms = round((time.monotonic() - started_at) * 1000)
    logger.info(
        "EXPLAIN 执行完成",
        datasource=datasource,
        dialect=dialect,
        elapsed_ms=elapsed_ms,
    )
    update = {
        "explain_errors": [],
        "sql_valid": True,
        "generated_sql": sql,
        "sql_explain_checked": True,
    }
    from src.graph.context import with_execution_context

    return with_execution_context(state, update)
