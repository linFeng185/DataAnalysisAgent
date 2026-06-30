"""12.1.6 限流 + 12.3.1 脱敏 + 12.3.3 审计。

依据: SPEC §12 安全模块
"""

from __future__ import annotations

import re
import time
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


def check_rate_limit(user_id: str = "anonymous") -> bool:
    """12.2.1 滑动窗口限流 (内存模式，生产应切 Redis)。"""
    max_rph = get_settings().max_queries_per_hour
    now = time.monotonic()
    window = now - 3600
    key = f"rate:{user_id}"
    _rate_limits.setdefault(key, [])
    _rate_limits[key] = [t for t in _rate_limits[key] if t > window]
    if len(_rate_limits[key]) < max_rph:
        _rate_limits[key].append(now)
        return True
    logger.warning("频率限制触发", user_id=user_id)
    return False


async def log_audit(
    user_id: str, datasource: str, sql: str,
    row_count: int, elapsed_ms: int, success: bool, pg_pool=None,
) -> None:
    """12.3.3 查询审计日志。"""
    entry = {"timestamp": datetime.now().isoformat(), "user_id": user_id,
             "datasource": datasource, "sql": sql[:500], "row_count": row_count,
             "elapsed_ms": elapsed_ms, "success": success}
    logger.info("查询审计", **entry)
    if pg_pool:
        try:
            await pg_pool.execute(
                "INSERT INTO query_audit_log (user_id,datasource,sql,row_count,elapsed_ms,success,created_at) VALUES ($1,$2,$3,$4,$5,$6,$7)",
                user_id, datasource, sql[:500], row_count, elapsed_ms, success, datetime.now(),
            )
        except Exception as e:
            logger.warning("审计日志写入失败", error=str(e))
