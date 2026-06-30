"""4.7 execute_sql Node — 通过 DataSourceRegistry 执行 SQL。"""

from __future__ import annotations

import time

from src.config import get_settings
from src.graph.state import AnalysisState
from src.logging_config import get_logger

logger = get_logger(__name__)


async def execute_sql_node(state: AnalysisState) -> dict:
    """Phase 2: registry → connector.execute()。Phase 1: 返回空数据 + 提示。"""
    _start = time.monotonic()
    logger.info("节点开始", node="execute_sql")
    sql = (state.get("generated_sql", "") or "").strip()
    ds_name = state.get("datasource", "")

    # LLM 返回空 SQL（如问题无法用现有数据回答）时直接跳过执行
    if not sql:
        logger.info("SQL 为空，跳过数据库执行", datasource=ds_name)
        return {"query_result_sample": [], "query_result_full_count": 0,
                "query_result_statistics": {"row_count": 0}}

    # 12.2.1 频率限制检查
    from src.security.data_masker import check_rate_limit
    if not check_rate_limit():
        return {"execution_error": "请求频率超限",
                "query_result_sample": [], "query_result_full_count": 0,
                "query_result_statistics": {"row_count": 0}}

    # 尝试连接数据源
    try:
        from src.datasource.registry import get_registry
        registry = get_registry()
        ds = await registry.resolve_or_none(ds_name)
        if ds and ds.engine:
            # 直接使用已注入的 engine 执行
            import sqlalchemy as sa
            async with ds.engine.connect() as conn:
                # SQLite 跳过超时设置
                if ds.dialect != "sqlite":
                    settings = get_settings()
                    from src.config import get_settings as _gs
                    timeout_s = _gs().max_execution_time
                    if ds.dialect == "clickhouse":
                        await conn.execute(sa.text(f"SET max_execution_time = {timeout_s}"))
                    elif ds.dialect == "mysql":
                        await conn.execute(sa.text(f"SET SESSION max_execution_time = {timeout_s * 1000}"))
                    elif ds.dialect == "postgres":
                        await conn.execute(sa.text(f"SET statement_timeout = '{timeout_s * 1000}ms'"))

                result = await conn.execute(sa.text(sql))
                rows = [dict(row._mapping) for row in result]
            elapsed = round((time.monotonic() - _start) * 1000)
            # 12.3.3 审计日志
            import asyncio
            from src.security.data_masker import log_audit
            asyncio.create_task(log_audit("anonymous", ds_name, sql, len(rows), elapsed, True))
            logger.info("节点完成", node="execute_sql", elapsed_ms=elapsed)
            return {
                "query_result_sample": rows[:200],
                "query_result_full_count": len(rows),
                "query_result_statistics": {"row_count": len(rows)},
                "execution_error": "",
            }
    except Exception as e:
        logger.warning("数据源执行失败", datasource=ds_name, error=str(e))

    # 无数据源时返回空
    logger.info("无可用数据源，返回空结果", datasource=ds_name, sql=sql[:100])
    logger.info("节点完成", node="execute_sql", elapsed_ms=round((time.monotonic() - _start) * 1000))
    return {
        "query_result_sample": [],
        "query_result_full_count": 0,
        "query_result_statistics": {"row_count": 0},
        "execution_error": f"数据源 '{ds_name}' 未配置或不可用。请先通过 POST /api/v1/datasources 注册数据源，或配置 DATASOURCE_NAME 环境变量。",
    }
