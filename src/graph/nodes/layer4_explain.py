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
    started_at = time.monotonic()
    sql = (state.get("generated_sql", "") or "").strip()
    dialect = (state.get("dialect", "") or "").lower()
    datasource = state.get("datasource", "") or ""
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
        return {"explain_errors": [error], "sql_valid": False}

    from src.tools.sql_rewriter import rewrite_sql

    original_sql = sql
    sql = rewrite_sql(sql, dialect)
    logger.info(
        "EXPLAIN SQL 方言重写完成",
        datasource=datasource,
        dialect=dialect,
        changed=sql != original_sql,
        sql=sql,
    )

    try:
        from src.datasource.registry import get_registry

        resolved = await get_registry().resolve_or_none(datasource)
        if resolved is None or resolved.engine is None:
            error = {"type": "configuration", "message": f"数据源 '{datasource}' 不可用"}
            logger.warning("EXPLAIN 数据源不可用", datasource=datasource)
            return {"explain_errors": [error], "sql_valid": False, "generated_sql": sql}

        connector = getattr(resolved, "connector", None)
        if connector is None:
            from src.connectors.registry import create_connector

            connector = create_connector(resolved).attach_engine(resolved.engine)
            resolved.connector = connector
            logger.warning(
                "EXPLAIN 补建连接器",
                datasource=datasource,
                dialect=resolved.dialect,
                reason="旧缓存缺少 connector",
            )
        validation = await connector.explain(sql)
        if not validation.get("valid", False):
            errors = validation.get("errors", []) or [{
                "type": "semantic_error",
                "message": "EXPLAIN 校验失败",
            }]
            logger.warning(
                "EXPLAIN 校验拒绝",
                datasource=datasource,
                dialect=dialect,
                errors=errors,
            )
            return {"explain_errors": errors, "sql_valid": False, "generated_sql": sql}
    except Exception as exc:
        error = {
            "type": "semantic_error",
            "message": str(exc).split("Stack trace:")[0][:500],
        }
        logger.error(
            "EXPLAIN 执行失败",
            datasource=datasource,
            dialect=dialect,
            error=error["message"],
            exc_info=True,
        )
        return {"explain_errors": [error], "sql_valid": False, "generated_sql": sql}

    elapsed_ms = round((time.monotonic() - started_at) * 1000)
    logger.info(
        "EXPLAIN 执行完成",
        datasource=datasource,
        dialect=dialect,
        elapsed_ms=elapsed_ms,
    )
    return {"explain_errors": [], "sql_valid": True, "generated_sql": sql}
