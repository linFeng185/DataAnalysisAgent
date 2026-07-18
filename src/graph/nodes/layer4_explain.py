"""4.6 layer4_explain Node — EXPLAIN 空跑校验。"""

from __future__ import annotations

import asyncio
import time

from src.graph.state import AnalysisState
from src.logging_config import get_logger

logger = get_logger(__name__)


_EXPLAIN_TEMPLATES = {
    "clickhouse": "EXPLAIN SYNTAX {sql}",
    "mysql": "EXPLAIN {sql}",
    "postgres": "EXPLAIN (ANALYZE FALSE, FORMAT JSON) {sql}",
    "sqlite": "EXPLAIN QUERY PLAN {sql}",
    "oracle": "EXPLAIN PLAN FOR {sql}",
    "mssql": "{sql}",
}


async def layer4_explain_node(state: AnalysisState) -> dict:
    """在目标数据源执行方言化 EXPLAIN，拦截真实执行前的语义错误。"""
    _start = time.monotonic()
    logger.info("节点开始", node="layer4_explain")
    logger.info(
        "EXPLAIN 边界输入",
        datasource=state.get("datasource", ""),
        dialect=state.get("dialect", ""),
        sql_preview=(state.get("generated_sql", "") or "")[:160],
        has_resolved_schema=state.get("resolved_schema") is not None,
    )
    sql = (state.get("generated_sql", "") or "").strip()
    dialect = (state.get("dialect", "") or "").lower()
    datasource = state.get("datasource", "") or ""
    if not sql:
        error = {"type": "semantic_error", "message": "EXPLAIN 的 SQL 不能为空"}
        logger.warning("EXPLAIN 拒绝", datasource=datasource, reason="SQL 为空")
        return {"explain_errors": [error], "sql_valid": False}

    # EXPLAIN 必须验证与执行节点完全相同的方言重写 SQL。
    from src.tools.sql_rewriter import rewrite_sql
    original_sql = sql
    sql = rewrite_sql(sql, dialect)
    logger.info(
        "EXPLAIN SQL 方言重写完成",
        datasource=datasource,
        dialect=dialect,
        changed=sql != original_sql,
        sql_preview=sql[:160],
    )

    from src.config import get_settings
    if dialect in get_settings().explain_skip_dialects:
        logger.warning("EXPLAIN 按配置跳过", datasource=datasource, dialect=dialect)
        return {"explain_errors": [], "sql_valid": True, "generated_sql": sql}

    template = _EXPLAIN_TEMPLATES.get(dialect)
    if not template:
        error = {"type": "configuration", "message": f"方言 {dialect} 未配置 EXPLAIN 模板"}
        logger.error("EXPLAIN 模板缺失", datasource=datasource, dialect=dialect)
        return {"explain_errors": [error], "sql_valid": False}

    try:
        from src.datasource.registry import get_registry

        resolved = await get_registry().resolve_or_none(datasource)
        if resolved is None or resolved.engine is None:
            error = {"type": "configuration", "message": f"数据源 '{datasource}' 不可用"}
            logger.warning("EXPLAIN 数据源不可用", datasource=datasource)
            return {"explain_errors": [error], "sql_valid": False}

        explain_sql = template.format(sql=sql)
        await _execute_explain(resolved.engine, explain_sql, dialect=dialect)
        elapsed = round((time.monotonic() - _start) * 1000)
        logger.info(
            "节点完成",
            node="layer4_explain",
            elapsed_ms=elapsed,
            datasource=datasource,
            dialect=dialect,
            valid=True,
        )
        return {"explain_errors": [], "sql_valid": True, "generated_sql": sql}
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


# 方法作用：兼容 SQLAlchemy 异步引擎、同步引擎和 ClickHouse 适配引擎执行 EXPLAIN。
# Args: engine - Registry 已解析并缓存的数据库引擎；explain_sql - 方言化 EXPLAIN SQL；dialect - 数据库方言。
# Returns: 执行成功返回 None，数据库异常原样抛给节点分类。
async def _execute_explain(engine, explain_sql: str, dialect: str = "") -> None:
    """复用 Registry 引擎执行 EXPLAIN，避免重复创建连接池。"""
    logger.debug(
        "执行 EXPLAIN 入口",
        engine_type=type(engine).__name__,
        sql_preview=explain_sql[:180],
    )
    import sqlalchemy as sa
    from sqlalchemy.ext.asyncio import AsyncEngine

    if dialect == "mssql":
        await _execute_mssql_showplan(engine, explain_sql)
    elif isinstance(engine, AsyncEngine):
        async with engine.connect() as connection:
            await connection.execute(sa.text(explain_sql))
    else:
        def _run_sync() -> None:
            with engine.connect() as connection:
                connection.execute(sa.text(explain_sql))

        await asyncio.to_thread(_run_sync)
    logger.info("执行 EXPLAIN 完成", engine_type=type(engine).__name__)


# 方法作用：在同一 SQL Server 连接中分批开启 SHOWPLAN、获取计划并确保关闭会话开关。
# Args: engine - SQL Server SQLAlchemy 引擎；sql - 已通过 Layer 3 的只读 SQL。
# Returns: 执行成功返回 None，数据库异常原样抛出。
async def _execute_mssql_showplan(engine, sql: str) -> None:
    """遵循 SQL Server 要求分批执行 SET SHOWPLAN_TEXT ON/OFF。"""
    logger.debug("SQL Server SHOWPLAN 入口", sql_preview=sql[:180])
    import sqlalchemy as sa
    from sqlalchemy.ext.asyncio import AsyncEngine

    if isinstance(engine, AsyncEngine):
        async with engine.connect() as connection:
            await connection.execute(sa.text("SET SHOWPLAN_TEXT ON"))
            try:
                await connection.execute(sa.text(sql))
            finally:
                await connection.execute(sa.text("SET SHOWPLAN_TEXT OFF"))
    else:
        def _run_sync() -> None:
            with engine.connect() as connection:
                connection.execute(sa.text("SET SHOWPLAN_TEXT ON"))
                try:
                    connection.execute(sa.text(sql))
                finally:
                    connection.execute(sa.text("SET SHOWPLAN_TEXT OFF"))

        await asyncio.to_thread(_run_sync)
    logger.info("SQL Server SHOWPLAN 完成")
