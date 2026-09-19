"""4.7 execute_sql Node — 通过 DataSourceRegistry 执行 SQL。"""

from __future__ import annotations

import time
from typing import Any

from src.app_context import get_tenant_policy
from src.config import get_settings
from src.graph.state import AnalysisState
from src.logging_config import get_logger

logger = get_logger(__name__)


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
    from src.graph.context import resolve_request_identity
    from src.security.data_masker import log_audit

    effective_tenant_id, effective_user_id = resolve_request_identity(state)
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
    from src.graph.context import read_contexts, resolve_request_identity

    contexts = read_contexts(state)
    effective_tenant_id, _ = resolve_request_identity(state)
    _start = time.monotonic()
    logger.debug("节点开始", node="execute_sql")
    sql = (state.get("generated_sql", "") or "").strip()
    ds_name = contexts.routing.datasource

    dialect = contexts.execution.dialect

    from src.graph.context import with_execution_context

    def complete(update: dict) -> dict:
        """同步当前节点增量和轻量执行上下文。"""
        if rate_limit_checked:
            update["request_rate_limit_checked"] = True
            update["request_context"] = {**contexts.request.model_dump(), "request_rate_limit_checked": True}
        return with_execution_context(state, update)

    rate_limit_checked = contexts.request.request_rate_limit_checked

    # LLM 返回空 SQL（如问题无法用现有数据回答）时直接跳过执行
    if not sql:
        logger.info("SQL 为空，跳过数据库执行", datasource=ds_name)
        return complete({"query_result_sample": [], "query_result_full_count": 0,
                         "query_result_statistics": {"row_count": 0},
                         "query_result_truncated": False,
                         "execution_error": "", "execution_error_type": "",
                         "execution_retry_count": 0})

    # 12.2.1 非 API 调用保留节点级兜底，API 请求不得重复计数。
    if not contexts.request.request_rate_limit_checked:
        from src.security.data_masker import check_rate_limit
        if not await check_rate_limit(contexts.request.user_id, tenant_id=effective_tenant_id):
            logger.warning("SQL 执行节点频率超限", user_id=contexts.request.user_id)
            await _record_query_audit(
                state, ds_name, sql, _start, success=False, error_message="rate_limit",
            )
            return complete({"execution_error": "请求频率超限",
                             "execution_error_type": "rate_limit",
                             "query_result_sample": [], "query_result_full_count": 0,
                             "query_result_statistics": {"row_count": 0}})
    else:
        logger.info("SQL 执行节点复用入口配额检查", user_id=contexts.request.user_id)

    rate_limit_checked = True

    # 行列级权限（多租户时生效）
    if get_tenant_policy().datasource_isolation_enabled:
        allowed = contexts.permission.allowed_columns
        rfilter = contexts.permission.row_filter_sql
        if allowed:
            from src.security.permission_check import check_column_whitelist
            col_err_perm = check_column_whitelist(sql, allowed)
            if col_err_perm:
                logger.warning("列权限拦截", datasource=ds_name, error=col_err_perm)
                await _record_query_audit(
                    state, ds_name, sql, _start, success=False, error_message=col_err_perm,
                )
                return complete({"query_result_sample": [], "query_result_full_count": 0,
                                 "query_result_statistics": {"row_count": 0},
                                 "generated_sql": sql, "execution_error": col_err_perm,
                                 "execution_error_type": "security"})
        if rfilter:
            from src.security.permission_check import inject_row_filter
            sql = inject_row_filter(sql, rfilter)

    # Layer 2: 列名验证
    col_err = _validate_column_references(
        sql,
        state.get("relevant_tables", []),
        dialect,
    )
    if col_err:
        logger.warning("列名验证失败，触发重试", datasource=ds_name, error=col_err)
        await _record_query_audit(
            state, ds_name, sql, _start, success=False, error_message=col_err,
        )
        return complete({"query_result_sample": [], "query_result_full_count": 0,
                         "query_result_statistics": {"row_count": 0},
                         "generated_sql": sql, "execution_error": col_err,
                         "execution_error_type": "sql_semantic"})

    # 统一服务在真实数据源方言下重新校验并有界执行。
    try:
        from src.security.sql_execution import validate_and_execute_sql

        execution = await validate_and_execute_sql(
            sql,
            ds_name,
            dialect,
            explain=not contexts.execution.sql_explain_checked,
            max_rows=get_settings().max_result_rows,
            tenant_id=effective_tenant_id,
        )
        sql = execution.sql or sql
        if execution.success:
            rows = execution.data
            truncated = execution.truncated
            elapsed = round((time.monotonic() - _start) * 1000)
            # 12.3.3 在返回前持久化审计，避免任务在请求结束时被取消。
            from src.security.data_masker import mask_sensitive_data_with_metadata
            await _record_query_audit(
                state, ds_name, sql, _start, success=True, row_count=len(rows),
            )
            masked_rows, masking = mask_sensitive_data_with_metadata(rows)
            settings = get_settings()
            collect_attribution_rows = (
                contexts.routing.intent == "attribution"
                or bool(state.get("collect_attribution_rows"))
            )
            attribution_rows = (
                masked_rows[:settings.attribution_max_touchpoint_rows]
                if collect_attribution_rows else []
            )
            logger.debug("节点完成", node="execute_sql", elapsed_ms=elapsed)
            # 回传行集上限改为可配置（原硬编码 200）：超出即静默砍行会让报表
            # 样本严重不足，且被裁掉的部分无从解释。超限由 sample_guard 交给模型决策。
            sample_limit = int(getattr(settings, "report_max_sample_rows", 0) or len(masked_rows) or 1)
            sample_capped = len(masked_rows) > sample_limit
            if sample_capped:
                logger.info(
                    "执行结果超出样本上限",
                    node="execute_sql",
                    row_count=len(masked_rows),
                    sample_limit=sample_limit,
                )
            return complete({
                "generated_sql": sql,  # 同步重写后的 SQL 到前端
                "query_result_sample": masked_rows[:sample_limit],
                "attribution_candidate_rows": attribution_rows,
                "query_result_full_count": len(masked_rows),
                "query_result_truncated": truncated,
                "query_result_sample_capped": sample_capped,
                "query_result_statistics": {"row_count": len(masked_rows), "truncated": truncated, "sample_capped": sample_capped, "masking": masking},
                "execution_error": "",
                "execution_error_type": "",
                "execution_retry_count": 0,
            })
        detail_message = "; ".join(
            str(item.get("message", ""))
            for item in execution.details
            if item.get("message")
        )
        err_msg = detail_message or execution.error or "SQL 执行失败"
        if execution.error_type in {"security", "sql_semantic", "configuration", "resource_limit"}:
            error_type = execution.error_type
        else:
            error_type = _classify_execution_error(err_msg)
    except Exception as exc:
        logger.error(
            "数据源执行失败",
            datasource=ds_name,
            error=str(exc),
            exc_info=True,
        )
        err_msg = str(exc)
        error_type = _classify_execution_error(err_msg)

    if len(err_msg) > 300:
        err_msg = err_msg[:300]
    logger.debug(
        "节点完成",
        node="execute_sql",
        elapsed_ms=round((time.monotonic() - _start) * 1000),
        success=False,
        error_type=error_type,
    )
    execution_retry_count = contexts.execution.execution_retry_count
    if error_type == "transient":
        execution_retry_count += 1
    await _record_query_audit(
        state, ds_name, sql, _start, success=False, error_message=err_msg,
    )
    return complete({
        "query_result_sample": [],
        "query_result_full_count": 0,
        "query_result_statistics": {"row_count": 0},
        "generated_sql": sql,
        "execution_error": err_msg,
        "execution_error_type": error_type,
        "execution_retry_count": execution_retry_count,
    })


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


# 方法作用：找出「列名在全局白名单存在、却不属于本语句引用表」的错配列（兼容适配器）。
# Args: tree - 已解析的 SQL 语法树；tables - 运行时表结构；
#       aliases - SELECT 中定义的别名（作用域感知实现已不再使用，仅为保持签名兼容保留）。
# Returns: 每个错配列的 column / referenced_table / provided_by 摘要；结构与旧实现一致。
def find_cross_table_columns(
    tree: Any, tables: list[dict], aliases: set[str]
) -> list[dict[str, Any]]:
    """保留旧 dict 契约，判定逻辑委托 src.security.sql_schema_validation 统一实现。"""
    from src.security.sql_schema_validation import (
        _collect_column_issues,
        build_table_index,
    )

    logger.debug("跨表列错配检测入口", table_count=len(tables))
    # 单源快照统一落到默认源名下，白名单因此真实生效：越界源的表会被跳过，
    # 而不是退化成空白名单的 fail-open 放行，也避免每次校验都打白名单缺失告警。
    index = build_table_index(tables, ["__local__"])
    by_table: dict[str, int] = {}
    for (_source, name) in index:
        by_table[name] = by_table.get(name, 0) + 1
    result: list[dict[str, Any]] = []
    for issue in _collect_column_issues(tree, index, "__local__"):
        if issue.type != "cross_table_column":
            continue
        qualifier = issue.qualifier
        providers = list(issue.provided_by)
        result.append(
            {
                "column": f"{qualifier}.{issue.column}" if qualifier else issue.column,
                "referenced_table": issue.table,
                "provided_by": providers[:10],
                "provider_count": len(providers),
                "schema_table_count": len(by_table),
            }
        )
    logger.info("跨表列错配检测完成", mismatch_count=len(result))
    return result


# 方法作用：执行前验证 SQL 中的列引用是否都存在于表结构中（最后防线，委托统一校验器）。
# Args: sql - 待验证 SQL；tables - relevant_tables 列表；dialect - 数据库方言。
# Returns: 错误消息字符串，None 表示通过。
def _validate_column_references(
    sql: str,
    tables: list[dict],
    dialect: str = "",
) -> str | None:
    """委托 src.security.sql_schema_validation，保留原签名与错误消息契约。

    与 should_retry 配合：验证失败 → execution_error → retry → LLM 修正。
    """
    logger.debug("列引用校验入口", dialect=dialect, table_count=len(tables))
    from src.security.sql_schema_validation import validate_sql_schema

    try:
        # 单源快照统一落到默认源名下：白名单真实生效（越界源的表被跳过，不再 fail-open），
        # 同时避开空白名单在热路径上每次校验都打「未提供数据源白名单」告警的噪音。
        issues = validate_sql_schema(
            sql, datasources=["__local__"], dialect=dialect, tables=tables
        )
    except Exception as exc:
        logger.error("列引用校验解析失败", error=str(exc), exc_info=True)
        return "列名校验解析失败"
    high_issues = [item for item in issues if item.confidence == "high"]
    if not high_issues:
        logger.info("列引用校验完成", result="通过")
        return None
    result = "列名校验失败: " + "; ".join(item.describe() for item in high_issues[:3])
    logger.info("列引用校验完成", result="失败", error_count=len(high_issues))
    return result
