"""4.7 execute_sql Node — 通过 DataSourceRegistry 执行 SQL。"""

from __future__ import annotations

import time
from decimal import Decimal

from src.config import get_settings
from src.graph.state import AnalysisState
from src.logging_config import get_logger

logger = get_logger(__name__)


def _row_to_dict(row) -> dict:
    """Row → dict，float→Decimal 保证后续运算精度。"""
    d = {}
    for k, v in dict(row._mapping).items():
        if isinstance(v, float) and not isinstance(v, bool):
            d[k] = Decimal(str(v))
        else:
            d[k] = v
    return d


# 方法作用：用显式状态身份同步记录一次 SQL 尝试，避免后台任务或 ContextVar 清理导致审计丢失。
# Args: state - 当前分析状态；datasource - 数据源名；sql - SQL 原文；started_at - 执行起点；success - 是否成功；row_count - 返回行数；error_message - 失败摘要。
# Returns: 无返回值；审计存储异常由 log_audit 内部记录，不影响查询响应。
async def _record_query_audit(
    state: AnalysisState,
    datasource: str,
    sql: str,
    started_at: float,
    *,
    success: bool,
    row_count: int = 0,
    error_message: str = "",
) -> None:
    from src.api.auth import get_current_tenant_id, get_current_user_id
    from src.security.data_masker import log_audit

    user_id = state.get("user_id")
    tenant_id = state.get("tenant_id")
    effective_user_id = get_current_user_id() if user_id is None else int(user_id)
    effective_tenant_id = get_current_tenant_id() if tenant_id is None else int(tenant_id)
    elapsed_ms = round((time.monotonic() - started_at) * 1000)
    logger.debug(
        "记录查询审计入口",
        user_id=effective_user_id,
        tenant_id=effective_tenant_id,
        datasource=datasource,
        success=success,
    )
    await log_audit(
        user_id=effective_user_id,
        tenant_id=effective_tenant_id,
        datasource=datasource,
        sql=sql,
        row_count=row_count,
        elapsed_ms=elapsed_ms,
        success=success,
        error_message=error_message,
    )
    logger.info(
        "记录查询审计完成",
        datasource=datasource,
        success=success,
        elapsed_ms=elapsed_ms,
    )

async def execute_sql_node(state: AnalysisState) -> dict:
    """Phase 2: registry → connector.execute()。Phase 1: 返回空数据 + 提示。"""
    _start = time.monotonic()
    logger.info("节点开始", node="execute_sql")
    sql = (state.get("generated_sql", "") or "").strip()
    ds_name = state.get("datasource", "")

    # SQL 方言重写 — 修复 LLM 常见语法错误（纯正则，非 LLM call）
    dialect = state.get("dialect", "")
    if sql and dialect:
        from src.tools.sql_rewriter import rewrite_sql
        sql = rewrite_sql(sql, dialect)

    # LLM 返回空 SQL（如问题无法用现有数据回答）时直接跳过执行
    if not sql:
        logger.info("SQL 为空，跳过数据库执行", datasource=ds_name)
        return {"query_result_sample": [], "query_result_full_count": 0,
                "query_result_statistics": {"row_count": 0},
                "query_result_truncated": False,
                "execution_error": "", "execution_error_type": "",
                "execution_retry_count": 0}

    # 12.2.1 非 API 调用保留节点级兜底，API 请求不得重复计数。
    if not state.get("request_rate_limit_checked", False):
        from src.security.data_masker import check_rate_limit
        if not check_rate_limit(state.get("user_id")):
            logger.warning("SQL 执行节点频率超限", user_id=state.get("user_id"))
            await _record_query_audit(
                state, ds_name, sql, _start, success=False, error_message="rate_limit",
            )
            return {"execution_error": "请求频率超限",
                    "execution_error_type": "rate_limit",
                    "query_result_sample": [], "query_result_full_count": 0,
                    "query_result_statistics": {"row_count": 0}}
    else:
        logger.info("SQL 执行节点复用入口配额检查", user_id=state.get("user_id"))

    # 行列级权限（多租户时生效）
    if get_settings().multi_tenant:
        allowed = state.get("allowed_columns", []) or []
        rfilter = state.get("row_filter_sql", "") or ""
        if allowed:
            from src.security.permission_check import check_column_whitelist
            col_err_perm = check_column_whitelist(sql, allowed)
            if col_err_perm:
                logger.warning("列权限拦截", datasource=ds_name, error=col_err_perm)
                await _record_query_audit(
                    state, ds_name, sql, _start, success=False, error_message=col_err_perm,
                )
                return {"query_result_sample": [], "query_result_full_count": 0,
                        "query_result_statistics": {"row_count": 0},
                        "generated_sql": sql, "execution_error": col_err_perm,
                        "execution_error_type": "security"}
        if rfilter:
            from src.security.permission_check import inject_row_filter
            sql = inject_row_filter(sql, rfilter)

    # Layer 2: 列名验证
    col_err = _validate_column_references(sql, state.get("relevant_tables", []))
    if col_err:
        logger.warning("列名验证失败，触发重试", datasource=ds_name, error=col_err)
        await _record_query_audit(
            state, ds_name, sql, _start, success=False, error_message=col_err,
        )
        return {"query_result_sample": [], "query_result_full_count": 0,
                "query_result_statistics": {"row_count": 0},
                "generated_sql": sql, "execution_error": col_err,
                "execution_error_type": "sql_semantic"}

    # 尝试连接数据源
    try:
        from src.datasource.registry import get_registry
        registry = get_registry()
        ds = await registry.resolve_or_none(ds_name)
        if ds and ds.engine:
            # 直接使用已注入的 engine 执行
            import sqlalchemy as sa
            from sqlalchemy.ext.asyncio import AsyncEngine

            async def _run_async(conn) -> tuple[list[dict], bool]:
                """流式读取异步查询结果并限制内存占用。

                Args:
                    conn: SQLAlchemy AsyncConnection。

                Returns:
                    有界结果行和是否截断。
                """
                if ds.dialect != "sqlite":
                    timeout_s = get_settings().max_execution_time
                    if ds.dialect == "clickhouse":
                        await conn.execute(sa.text(f"SET max_execution_time = {timeout_s}"))
                    elif ds.dialect == "mysql":
                        await conn.execute(sa.text(f"SET SESSION max_execution_time = {timeout_s * 1000}"))
                    elif ds.dialect == "postgres":
                        await conn.execute(sa.text(f"SET statement_timeout = '{timeout_s * 1000}ms'"))
                max_rows = get_settings().max_result_rows
                result = await conn.stream(sa.text(sql))
                rows: list[dict] = []
                async for row in result:
                    rows.append(_row_to_dict(row))
                    if len(rows) > max_rows:
                        break
                await result.close()
                truncated = len(rows) > max_rows
                return rows[:max_rows], truncated

            def _run_sync(conn) -> tuple[list[dict], bool]:
                """分批读取同步查询结果并限制内存占用。

                Args:
                    conn: SQLAlchemy Connection。

                Returns:
                    有界结果行和是否截断。
                """
                if ds.dialect != "sqlite":
                    timeout_s = get_settings().max_execution_time
                    if ds.dialect == "clickhouse":
                        conn.execute(sa.text(f"SET max_execution_time = {timeout_s}"))
                    elif ds.dialect == "mysql":
                        conn.execute(sa.text(f"SET SESSION max_execution_time = {timeout_s * 1000}"))
                    elif ds.dialect == "postgres":
                        conn.execute(sa.text(f"SET statement_timeout = '{timeout_s * 1000}ms'"))
                max_rows = get_settings().max_result_rows
                result = conn.execution_options(stream_results=True).execute(sa.text(sql))
                fetched = result.fetchmany(max_rows + 1)
                result.close()
                truncated = len(fetched) > max_rows
                return [_row_to_dict(row) for row in fetched[:max_rows]], truncated

            if isinstance(ds.engine, AsyncEngine):
                async with ds.engine.connect() as conn:
                    rows, truncated = await _run_async(conn)
            else:
                import asyncio

                # 方法作用：在线程池中创建同步连接并执行查询。
                # Args: 无，使用闭包中的数据源引擎和 SQL。
                # Returns: 有界行列表和截断标志。
                def _run_sync_with_connection() -> tuple[list[dict], bool]:
                    """在线程池中创建同步连接并执行查询，避免阻塞事件循环。"""
                    with ds.engine.connect() as conn:
                        return _run_sync(conn)

                logger.debug("同步数据源切换线程池", datasource=ds_name, dialect=ds.dialect)
                rows, truncated = await asyncio.to_thread(_run_sync_with_connection)
                logger.info("同步数据源线程池执行完成", datasource=ds_name, row_count=len(rows))
            elapsed = round((time.monotonic() - _start) * 1000)
            # 12.3.3 在返回前持久化审计，避免任务在请求结束时被取消。
            from src.security.data_masker import mask_sensitive_data
            await _record_query_audit(
                state, ds_name, sql, _start, success=True, row_count=len(rows),
            )
            masked_rows = mask_sensitive_data(rows)
            logger.info("节点完成", node="execute_sql", elapsed_ms=elapsed)
            return {
                "generated_sql": sql,  # 同步重写后的 SQL 到前端
                "query_result_sample": masked_rows[:200],
                "query_result_full_count": len(masked_rows),
                "query_result_truncated": truncated,
                "query_result_statistics": {"row_count": len(masked_rows), "truncated": truncated},
                "execution_error": "",
                "execution_error_type": "",
                "execution_retry_count": 0,
            }
    except Exception as e:
        logger.warning("数据源执行失败", datasource=ds_name, error=str(e))
        err_msg = str(e)
        # 提取简洁错误信息
        if len(err_msg) > 300:
            # 截取 pymysql/mysql 错误的关键部分
            import re
            match = re.search(r'\((\d+), "([^"]+)"\)', err_msg)
            if match:
                err_msg = f"SQL 执行错误 [{match.group(1)}]: {match.group(2)}"
            else:
                err_msg = err_msg[:300]
        logger.info("节点完成", node="execute_sql", elapsed_ms=round((time.monotonic() - _start) * 1000))
        error_type = _classify_execution_error(err_msg)
        execution_retry_count = state.get("execution_retry_count", 0)
        if error_type == "transient":
            execution_retry_count += 1
        await _record_query_audit(
            state, ds_name, sql, _start, success=False, error_message=err_msg,
        )
        return {
            "query_result_sample": [],
            "query_result_full_count": 0,
            "query_result_statistics": {"row_count": 0},
            "generated_sql": sql,
            "execution_error": err_msg,
            "execution_error_type": error_type,
            "execution_retry_count": execution_retry_count,
        }

    logger.error("execute_sql 未预期路径", datasource=ds_name)
    await _record_query_audit(
        state, ds_name, sql, _start, success=False, error_message="datasource unavailable",
    )
    return {"query_result_sample": [], "query_result_full_count": 0,
            "query_result_statistics": {"row_count": 0},
            "generated_sql": sql,
            "execution_error": f"数据源 '{ds_name}' 内部错误",
            "execution_error_type": "configuration",
            "retry_count": 99}


# 方法作用：把数据库错误稳定分类为执行重试、SQL 重生成或直接终止三类。
# Args: message - 数据库驱动或 Registry 返回的错误文本。
# Returns: transient/sql_semantic/configuration 中的分类字符串。
def _classify_execution_error(message: str) -> str:
    """根据可审计关键词分类数据库执行错误，未知错误默认交给 SQL 修正。"""
    normalized = (message or "").lower()
    logger.debug("执行错误分类入口", error_preview=normalized[:160])
    transient_markers = (
        "timeout", "timed out", "connection reset", "connection refused",
        "connection closed", "server has gone away", "network", "temporarily unavailable",
        "deadlock", "lock wait timeout", "连接超时", "连接中断", "网络错误", "暂时不可用",
    )
    configuration_markers = (
        "not found", "unknown database", "authentication", "access denied",
        "未配置", "未找到", "不存在的数据源", "认证失败", "连接失败或不存在",
    )
    if any(marker in normalized for marker in transient_markers):
        result = "transient"
    elif any(marker in normalized for marker in configuration_markers):
        result = "configuration"
    else:
        result = "sql_semantic"
    logger.info("执行错误分类完成", error_type=result)
    return result


def _validate_column_references(sql: str, tables: list[dict]) -> str | None:
    """执行前验证 SQL 中的列引用是否都存在于表结构中。

    与 should_retry 配合：验证失败 → execution_error → retry → LLM 修正。
    跳过 SELECT 中 AS 定义的别名（如 SUM(x) AS total_sales），这些不是表列。

    Args:
        sql - 待验证的 SQL
        tables - relevant_tables 列表

    Returns: 错误消息字符串，None 表示通过
    """
    if not tables:
        return None
    try:
        import sqlglot
        from sqlglot import exp

        valid_cols: set[str] = set()
        valid_tables: set[str] = set()
        for t in tables:
            tname = t.get("name", "")
            if tname:
                valid_tables.add(tname.lower())
                valid_tables.add(tname)
            for c in t.get("columns", []):
                cname = c.get("name", "")
                if cname:
                    valid_cols.add(cname.lower())
                    valid_cols.add(cname)
                    if tname:
                        valid_cols.add(f"{tname.lower()}.{cname.lower()}")

        if not valid_cols:
            return None

        tree = sqlglot.parse_one(sql, read="mysql")
        if not tree:
            return None

        # 收集 SELECT 中 AS 定义的别名，这些不是表列，不校验
        aliases: set[str] = set()
        for sel in tree.find_all(exp.Select):
            for col in sel.expressions:
                alias = col.alias_or_name if hasattr(col, 'alias_or_name') else col.alias
                if alias and alias != col.name:
                    aliases.add(alias.lower())

        errors: list[str] = []
        for node in tree.find_all(exp.Column):
            col_name = node.name
            if col_name == "*" or col_name == "1":
                continue
            # 跳过 SELECT 中定义的别名
            if col_name.lower() in aliases:
                continue
            if col_name.lower() not in valid_cols:
                table_name = node.table
                full_ref = f"{table_name}.{col_name}" if table_name else col_name
                if full_ref.lower() not in valid_cols:
                    err = f"列 '{full_ref}' 不在可用列中（可用: {sorted(valid_cols)[:15]}）"
                    if err not in errors:
                        errors.append(err)

        if errors:
            return "列名校验失败: " + "; ".join(errors[:3])
        return None
    except Exception:
        return None
