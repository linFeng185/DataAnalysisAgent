"""Grafana SimpleJSON 查询协议与租户权限适配。"""

from __future__ import annotations

import asyncio
from datetime import datetime
from typing import Any

from fastapi import APIRouter, Body, HTTPException

from src.api.auth import get_current_identity
from src.datasource.registry import get_registry
from src.logging_config import get_logger
from src.security.permission_check import (
    check_column_whitelist,
    inject_row_filter,
    resolve_datasource_access,
)
from src.security.sql_execution import SQLExecutionResult, validate_and_execute_sql, validate_sql

logger = get_logger(__name__)
router = APIRouter()
_MAX_TARGET_CONCURRENCY = 8
_TARGET_TIMEOUT_SECONDS = 30.0


@router.get("/grafana/search")
# 方法作用：列出当前 Grafana 身份可以查询的数据源名称。
# Args: 无。
# Returns: Grafana SimpleJSON 要求的名称数组。
async def search() -> list[str]:
    identity = get_current_identity()
    try:
        available = await _available_datasources(identity)
        access = await resolve_datasource_access(
            available,
            [],
            tenant_id=identity.tenant_id,
            user_id=identity.user_id,
            role=identity.role,
        )
    except PermissionError as exc:
        logger.warning("Grafana 数据源搜索权限拒绝", tenant_id=identity.tenant_id, user_id=identity.user_id)
        raise HTTPException(403, "没有可访问的数据源") from exc
    except Exception as exc:
        logger.error("Grafana 数据源搜索失败", error=str(exc), exc_info=True)
        raise HTTPException(503, "数据源服务暂时不可用") from exc
    result = sorted(access)
    logger.info("Grafana 数据源搜索完成", tenant_id=identity.tenant_id, count=len(result))
    return result


@router.post("/grafana/query")
# 方法作用：执行 Grafana targets 中的只读 SQL 并返回 table 或 time series 数据。
# Args: payload - SimpleJSON 查询请求体。
# Returns: Grafana 可识别的结果数组；权限或安全失败抛出 HTTPException。
async def query(payload: dict[str, Any] = Body(...)) -> list[dict[str, Any]]:
    identity = get_current_identity()
    targets = payload.get("targets") or []
    if not isinstance(targets, list) or not targets or len(targets) > 20:
        raise HTTPException(422, "targets 必须包含 1 到 20 个查询")
    available = await _available_datasources(identity)
    prepared: list[tuple[dict[str, Any], str]] = []
    for target in targets:
        if not isinstance(target, dict):
            raise HTTPException(422, "target 格式无效")
        datasource_name = _target_datasource(target, payload)
        raw_sql = str(target.get("rawSql", "") or "").strip()
        if not datasource_name or not raw_sql:
            raise HTTPException(422, "每个 target 必须提供 datasource 和 rawSql")
        if len(raw_sql) > 20_000:
            raise HTTPException(413, "Grafana SQL 超过长度限制")
        prepared.append((target, datasource_name))
    try:
        # 一个请求只解析一次 ACL，避免并发目标之间读取到不同权限版本。
        access = await resolve_datasource_access(
            available,
            list(dict.fromkeys(name for _, name in prepared)),
            tenant_id=identity.tenant_id,
            user_id=identity.user_id,
            role=identity.role,
        )
    except PermissionError as exc:
        logger.warning(
            "Grafana 查询数据源权限拒绝",
            tenant_id=identity.tenant_id,
            user_id=identity.user_id,
        )
        raise HTTPException(403, "无权使用请求中的数据源") from exc
    acl_snapshot = {name: dict(permission) for name, permission in access.items()}
    semaphore = asyncio.Semaphore(min(_MAX_TARGET_CONCURRENCY, len(prepared)))
    executions = await asyncio.gather(*(
        _execute_target(
            target,
            datasource_name,
            payload,
            available,
            acl_snapshot[datasource_name],
            semaphore,
        )
        for target, datasource_name in prepared
    ), return_exceptions=True)
    results: list[dict[str, Any]] = []
    for index, execution in enumerate(executions):
        if not isinstance(execution, BaseException):
            results.append(execution)
            continue
        if isinstance(execution, asyncio.CancelledError):
            raise execution
        if not isinstance(execution, Exception):
            raise execution
        target = prepared[index][0]
        ref_id = str(target.get("refId", f"T{index + 1}") or f"T{index + 1}")
        if isinstance(execution, HTTPException) and execution.status_code == 403:
            raise execution
        if len(prepared) == 1:
            if isinstance(execution, HTTPException):
                raise execution
            raise HTTPException(502, "Grafana 查询执行失败") from execution
        detail = execution.detail if isinstance(execution, HTTPException) else "查询执行失败"
        logger.warning(
            "Grafana 多目标查询局部失败",
            target=ref_id,
            error_type=type(execution).__name__,
        )
        results.append(_grafana_error_result(ref_id, str(detail)))
    logger.info(
        "Grafana 查询完成",
        tenant_id=identity.tenant_id,
        user_id=identity.user_id,
        target_count=len(results),
        failed_count=sum(1 for item in results if item.get("error")),
    )
    return results


# 方法作用：在并发和超时边界内执行单个已授权 Grafana target。
# Args: target/datasource_name/payload - 目标上下文；available - 数据源摘要；permission - ACL 快照；semaphore - 并发门限。
# Returns: Grafana table 或 time series 结果。
async def _execute_target(
    target: dict[str, Any],
    datasource_name: str,
    payload: dict[str, Any],
    available: list[dict[str, Any]],
    permission: dict[str, Any],
    semaphore: asyncio.Semaphore,
) -> dict[str, Any]:
    async with semaphore:
        datasource_meta = next(
            (item for item in available if str(item.get("name")) == datasource_name),
            {},
        )
        dialect = str(datasource_meta.get("dialect", "") or "")
        raw_sql = str(target.get("rawSql", "") or "").strip()
        raw_sql = _expand_macros(raw_sql, payload.get("range") or {})
        validation = validate_sql(raw_sql, dialect)
        if not validation.valid:
            message = "; ".join(
                str(item.get("message", "SQL 校验失败")) for item in validation.errors
            )
            logger.warning("Grafana 查询只读校验拒绝", datasource=datasource_name, error=message)
            raise HTTPException(422, message)
        column_error = check_column_whitelist(raw_sql, permission.get("allowed_columns", []))
        if column_error:
            raise HTTPException(403, column_error)
        try:
            scoped_sql = inject_row_filter(raw_sql, str(permission.get("row_filter_sql", "") or ""))
        except Exception as exc:
            logger.error("Grafana 行权限注入失败", datasource=datasource_name, error=str(exc), exc_info=True)
            raise HTTPException(403, "数据源行权限无法安全注入") from exc
        try:
            async with asyncio.timeout(_TARGET_TIMEOUT_SECONDS):
                execution = await validate_and_execute_sql(
                    scoped_sql,
                    datasource_name,
                    dialect,
                    explain=False,
                )
        except TimeoutError as exc:
            logger.warning("Grafana 目标查询超时", datasource=datasource_name)
            raise HTTPException(504, "Grafana 查询超时") from exc
        if not execution.success:
            status = 422 if execution.error_type in {"security", "sql_semantic"} else 502
            raise HTTPException(status, execution.error or "Grafana 查询执行失败")
        return _grafana_result(str(target.get("refId", "A") or "A"), execution)


@router.get("/grafana/annotations")
# 方法作用：提供 Grafana 注释协议的安全空实现。
# Args: 无。
# Returns: 空注释数组。
async def annotations() -> list[dict[str, Any]]:
    return []


@router.get("/grafana/tag-keys")
# 方法作用：提供 Grafana 标签键协议的安全空实现。
# Args: 无。
# Returns: 空标签键数组。
async def tag_keys() -> list[dict[str, Any]]:
    return []


@router.get("/grafana/tag-values")
# 方法作用：提供 Grafana 标签值协议的安全空实现。
# Args: 无。
# Returns: 空标签值数组。
async def tag_values() -> list[dict[str, Any]]:
    return []


# 方法作用：读取当前身份可见的数据源摘要供每个 target 复用。
# Args: identity - 当前请求身份。
# Returns: Registry 数据源摘要列表。
async def _available_datasources(identity: Any) -> list[dict[str, Any]]:
    try:
        return await get_registry().list_all(tenant_id=identity.tenant_id)
    except TypeError:
        return await get_registry().list_all()
    except Exception as exc:
        logger.error("Grafana 数据源列表读取失败", error=str(exc), exc_info=True)
        raise HTTPException(503, "数据源服务暂时不可用") from exc


# 方法作用：从 Grafana target 或请求级字段解析数据源名称。
# Args: target - 单个查询目标；payload - 查询请求体。
# Returns: 规范化数据源名称，无法解析时返回空字符串。
def _target_datasource(target: dict[str, Any], payload: dict[str, Any]) -> str:
    raw = target.get("datasource", payload.get("datasource", ""))
    if isinstance(raw, dict):
        raw = raw.get("name", raw.get("uid", ""))
    return str(raw or "").strip()


# 方法作用：将 Grafana 常用时间宏替换为安全的 ISO 字面量表达式。
# Args: sql - 原始 SQL；time_range - Grafana range 对象。
# Returns: 可交给统一 SQL 校验器的 SQL 文本。
def _expand_macros(sql: str, time_range: dict[str, Any]) -> str:
    start = _safe_time_literal(time_range.get("from"))
    end = _safe_time_literal(time_range.get("to"))
    if not start or not end:
        return sql
    import re

    pattern = re.compile(r"\$__timeFilter\(\s*([A-Za-z_][\w.]*)\s*\)")
    return pattern.sub(lambda match: f"{match.group(1)} BETWEEN '{start}' AND '{end}'", sql)


# 方法作用：校验 Grafana 时间值并转换为 SQL 可用的 UTC ISO 字符串。
# Args: value - Grafana range 的 from/to 值。
# Returns: 合法时间字符串；非法输入返回空字符串。
def _safe_time_literal(value: Any) -> str:
    raw = str(value or "").strip()
    if not raw or len(raw) > 64:
        return ""
    try:
        parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError:
        return ""
    return parsed.isoformat(sep=" ", timespec="seconds")


# 方法作用：把统一 SQL 执行结果转换成 Grafana table 或 time series 结构。
# Args: ref_id - Grafana target 引用；execution - 只读执行结果。
# Returns: SimpleJSON 结果对象。
def _grafana_result(ref_id: str, execution: SQLExecutionResult) -> dict[str, Any]:
    rows = execution.data
    columns = [{"text": str(key)} for key in (rows[0].keys() if rows else [])]
    if not rows:
        return {"target": ref_id, "type": "table", "columns": [], "rows": []}
    time_key = next(
        (key for key in rows[0] if str(key).lower() in {"time", "timestamp", "ts", "datetime"}),
        None,
    )
    value_keys = [key for key in rows[0] if key != time_key]
    if time_key and len(value_keys) == 1:
        datapoints = []
        for row in rows:
            timestamp = _timestamp_ms(row.get(time_key))
            if timestamp is not None:
                datapoints.append([row.get(value_keys[0]), timestamp])
        if datapoints:
            return {"target": ref_id, "type": "timeseries", "datapoints": datapoints}
    return {
        "target": ref_id,
        "type": "table",
        "columns": columns,
        "rows": [[row.get(column["text"]) for column in columns] for row in rows],
    }


# 方法作用：把多目标请求中的单目标故障转换为 Grafana 可展示的错误表。
# Args: ref_id - target 引用；detail - 已脱敏错误摘要。
# Returns: 不影响同请求其他目标的错误结果。
def _grafana_error_result(ref_id: str, detail: str) -> dict[str, Any]:
    message = str(detail or "查询失败")[:500]
    return {
        "target": ref_id,
        "type": "table",
        "columns": [{"text": "error"}],
        "rows": [[message]],
        "error": message,
    }


# 方法作用：把数据库时间值转换为 Grafana 毫秒时间戳。
# Args: value - datetime、数字或 ISO 字符串。
# Returns: 毫秒时间戳；无法转换返回 None。
def _timestamp_ms(value: Any) -> int | None:
    if isinstance(value, (int, float)):
        return int(value if value > 10_000_000_000 else value * 1000)
    if isinstance(value, datetime):
        return int(value.timestamp() * 1000)
    raw = str(value or "").strip()
    if not raw:
        return None
    try:
        return int(datetime.fromisoformat(raw.replace("Z", "+00:00")).timestamp() * 1000)
    except ValueError:
        return None
