"""12.1.6 限流 + 12.3.1 脱敏 + 12.3.3 审计。

依据: SPEC §12 安全模块
"""

from __future__ import annotations

import re
import time
import hashlib
import threading
from datetime import datetime
from typing import Any

from src.config import get_settings
from src.logging_config import get_logger

logger = get_logger(__name__)

_PII_PATTERNS: list[tuple[re.Pattern, str]] = [
    (re.compile(r'1[3-9]\d{9}'), '手机号'),
    (re.compile(r'\d{17}[\dXx]|\d{15}'), '身份证'),
    (re.compile(r'[a-zA-Z0-9._%+-]+@[a-zA-Z0-9.-]+\.[a-zA-Z]{2,}'), '邮箱'),
]
_SENSITIVE_COLS = {
    'password','passwd','secret','token','api_key','credit_card','phone','mobile','email',
}
_rate_limits: dict[str, list[float]] = {}
_rate_limit_lock = threading.Lock()


def mask_sensitive_data(rows: list[dict]) -> list[dict]:
    """12.3.1 自动脱敏手机号、身份证号、邮箱。"""
    masked = []
    for row in rows:
        new = {}
        for k, v in row.items():
            if isinstance(v, str) and (
                k.lower() in _SENSITIVE_COLS or
                any(w in k.lower() for w in ('phone','mobile','idcard','email','password'))
                or any(p.search(v) for p, _ in _PII_PATTERNS)
            ):
                new[k] = _mask(v)
            else:
                new[k] = v
        masked.append(new)
    return masked


def _mask(val: str) -> str:
    for p, label in _PII_PATTERNS:
        m = p.search(val)
        if m:
            if label == '手机号': return val[:3] + '****' + val[-4:]
            if label == '身份证': return val[:4] + '**********' + val[-4:]
            if label == '邮箱':
                at = val.index('@')
                return val[0] + '***' + val[at:]
    return val[:2] + '***' + val[-2:] if len(val) > 4 else '****'


def check_rate_limit(user_id: str | int | None = None) -> bool:
    """按当前用户执行内存滑动窗口限流。

    Args:
        user_id: 可选用户 ID；未传时从认证上下文读取。

    Returns:
        未超限返回 True，否则返回 False。
    """
    if user_id is None:
        try:
            from src.api.auth import get_current_user_id
            user_id = get_current_user_id()
        except Exception:
            user_id = "anonymous"
    user_key = str(user_id)
    logger.debug("限流检查入口", user_id=user_key)
    max_rph = get_settings().max_queries_per_hour
    now = time.monotonic()
    window = now - 3600
    key = f"rate:{user_key}"
    with _rate_limit_lock:
        _rate_limits.setdefault(key, [])
        _rate_limits[key] = [t for t in _rate_limits[key] if t > window]
        if len(_rate_limits[key]) < max_rph:
            _rate_limits[key].append(now)
            logger.info("限流检查通过", user_id=user_key, used=len(_rate_limits[key]), limit=max_rph)
            return True
    logger.warning("频率限制触发", user_id=user_key)
    return False


def build_audit_entry(
    user_id: int,
    tenant_id: int,
    datasource: str,
    sql: str,
    row_count: int,
    elapsed_ms: int,
    success: bool,
    error_message: str = "",
) -> dict:
    """构建不包含明文 SQL 的查询审计条目。

    Args:
        user_id: 当前用户 ID。
        tenant_id: 当前租户 ID。
        datasource: 查询的数据源名称。
        sql: 原始 SQL，仅用于计算 SHA-256。
        row_count: 返回行数。
        elapsed_ms: 执行耗时毫秒数。
        success: 查询是否成功。
        error_message: 可选的错误摘要。

    Returns:
        与 query_audit_log 表字段一致的审计字典。
    """
    logger.debug("审计条目构建入口", user_id=user_id, tenant_id=tenant_id, datasource=datasource)
    entry = {
        "timestamp": datetime.now().isoformat(),
        "user_id": user_id,
        "tenant_id": tenant_id,
        "datasource": datasource,
        "sql_hash": hashlib.sha256(sql.encode("utf-8")).hexdigest(),
        "row_count": row_count,
        "duration_ms": elapsed_ms,
        "success": success,
        "error_message": error_message[:500],
    }
    logger.info("审计条目构建完成", user_id=user_id, tenant_id=tenant_id, sql_hash=entry["sql_hash"][:12])
    return entry


async def log_audit(
    user_id: int, tenant_id: int, datasource: str, sql: str,
    row_count: int, elapsed_ms: int, success: bool, pg_pool=None,
    error_message: str = "",
) -> None:
    """记录脱敏查询审计，并在可用时写入 PostgreSQL。

    Args:
        user_id: 当前用户 ID。
        tenant_id: 当前租户 ID。
        datasource: 查询的数据源名称。
        sql: 原始 SQL，仅用于计算 hash。
        row_count: 返回行数。
        elapsed_ms: 执行耗时毫秒数。
        success: 查询是否成功。
        pg_pool: 可选 asyncpg 连接池。
        error_message: 可选错误摘要。

    Returns:
        无返回值。
    """
    logger.debug("查询审计入口", user_id=user_id, tenant_id=tenant_id, datasource=datasource)
    entry = build_audit_entry(
        user_id, tenant_id, datasource, sql, row_count, elapsed_ms, success, error_message,
    )
    logger.info("查询审计", **entry)
    if pg_pool:
        try:
            await pg_pool.execute(
                "INSERT INTO query_audit_log "
                "(user_id,tenant_id,datasource,sql_hash,row_count,duration_ms,success,error_message,created_at) "
                "VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9)",
                user_id, tenant_id, datasource, entry["sql_hash"], row_count,
                elapsed_ms, success, error_message[:500], datetime.now(),
            )
            logger.info("查询审计写入完成", sql_hash=entry["sql_hash"][:12])
        except Exception as exc:
            logger.error("审计日志写入失败", error=str(exc), exc_info=True)
