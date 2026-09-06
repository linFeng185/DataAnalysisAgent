"""12.1.6 限流 + 12.3.1 脱敏 + 12.3.3 审计。

依据: SPEC §12 安全模块
"""

from __future__ import annotations

import hashlib
import re
from datetime import datetime
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

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


class MaskingField(BaseModel):
    """不包含原值和行身份的字段脱敏摘要。"""

    model_config = ConfigDict(extra="forbid")
    column: str
    categories: list[str] = Field(default_factory=list)
    masked_count: int = Field(ge=0)


class MaskingMetadata(BaseModel):
    """与响应、历史、多源结果共用的脱敏展示契约。"""

    model_config = ConfigDict(extra="forbid")
    applied: bool = False
    fields: list[MaskingField] = Field(default_factory=list)


# 方法作用：在脱敏的同一次遍历中记录处理字段，禁止从掩码内容反推。
# Args: rows - 原始查询行。
# Returns: 脱敏后的新行和不包含原值的字段级元数据。
def mask_sensitive_data_with_metadata(rows: list[dict]) -> tuple[list[dict], dict[str, Any]]:
    """沿用已有脱敏策略，只为实际处理的单元格聚合类别和数量。"""
    masked: list[dict] = []
    fields: dict[str, MaskingField] = {}
    for row in rows:
        new: dict = {}
        for column, value in row.items():
            categories = []
            if isinstance(value, str):
                if column.lower() in _SENSITIVE_COLS or any(
                    word in column.lower() for word in ('phone', 'mobile', 'idcard', 'email', 'password')
                ):
                    categories.append("敏感字段")
                categories.extend(label for pattern, label in _PII_PATTERNS if pattern.search(value))
            if categories:
                new[column] = _mask(value)
                field = fields.setdefault(column, MaskingField(column=column, masked_count=0))
                field.masked_count += 1
                field.categories = sorted(set(field.categories).union(categories))
            else:
                new[column] = value
        masked.append(new)
    metadata = MaskingMetadata(applied=bool(fields), fields=list(fields.values()))
    return masked, metadata.model_dump()


# 方法作用：保留只返回脱敏行的旧接口，避免影响自动化报告等调用方。
# Args: rows - 原始查询行。
# Returns: 脱敏后的新行。
def mask_sensitive_data(rows: list[dict]) -> list[dict]:
    """12.3.1 自动脱敏手机号、身份证号、邮箱。"""
    return mask_sensitive_data_with_metadata(rows)[0]


# 方法作用：合并多个来源的可信字段摘要，不再次处理数据值。
# Args: items - 来源脱敏元数据集合。
# Returns: 按列聚合的脱敏元数据。
def merge_masking_metadata(items: list[dict]) -> dict[str, Any]:
    """用于多数据源合并，只汇总元数据且不携带原始行。"""
    fields: dict[str, MaskingField] = {}
    for item in items:
        metadata = MaskingMetadata.model_validate(item)
        for source in metadata.fields:
            field = fields.setdefault(source.column, MaskingField(column=source.column, masked_count=0))
            field.masked_count += source.masked_count
            field.categories = sorted(set(field.categories).union(source.categories))
    return MaskingMetadata(applied=bool(fields), fields=list(fields.values())).model_dump()


# 方法作用：使用已有规则生成不可在页面恢复的掩码文本。
# Args: val - 待处理字符串。
# Returns: 掩码字符串。
def _mask(val: str) -> str:
    for p, label in _PII_PATTERNS:
        m = p.search(val)
        if m:
            if label == '手机号':
                return val[:3] + '****' + val[-4:]
            if label == '身份证':
                return val[:4] + '**********' + val[-4:]
            if label == '邮箱':
                at = val.index('@')
                return val[0] + '***' + val[at:]
    return val[:2] + '***' + val[-2:] if len(val) > 4 else '****'


# 方法作用：将所有查询入口统一委托给数据库共享额度服务。
# Args: user_id - 当前用户；tenant_id - 显式后台租户，缺省从请求读取。
# Returns: 未超限返回 True；存储失败由共享服务抛出 503。
async def check_rate_limit(user_id: str | int | None = None, *, tenant_id: int | None = None) -> bool:
    """不再保留进程内降级计数，防止多 Worker 分别放行。"""
    from src.api.auth import get_current_tenant_id, get_current_user_id
    from src.security.query_quota import consume_query_quota

    try:
        effective_user = int(user_id) if user_id is not None else get_current_user_id()
        effective_tenant = tenant_id if tenant_id is not None else get_current_tenant_id()
    except (ValueError, TypeError):
        return False
    return await consume_query_quota(effective_tenant, effective_user)


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
    safe_error = ""
    if error_message:
        error_digest = hashlib.sha256(error_message.encode("utf-8")).hexdigest()
        safe_error = f"sha256:{error_digest};chars:{len(error_message)}"
    entry = {
        "timestamp": datetime.now().isoformat(),
        "user_id": user_id,
        "tenant_id": tenant_id,
        "datasource": datasource,
        "sql_hash": hashlib.sha256(sql.encode("utf-8")).hexdigest(),
        "row_count": row_count,
        "duration_ms": elapsed_ms,
        "success": success,
        "error_message": safe_error,
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
    try:
        effective_pool = pg_pool
        if effective_pool is None:
            database_url = str(getattr(get_settings(), "database_url", "") or "")
            if not database_url or "postgres" not in database_url:
                logger.warning("查询审计跳过 PG 写入", reason="未配置 PostgreSQL")
                return
            from src.memory.pg_pool import get_pg_pool

            effective_pool = await get_pg_pool()
        await effective_pool.execute(
                "INSERT INTO query_audit_log "
                "(user_id,tenant_id,datasource,sql_hash,row_count,duration_ms,success,error_message,created_at) "
                "VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9)",
                user_id, tenant_id, datasource, entry["sql_hash"], row_count,
                elapsed_ms, success, entry["error_message"], datetime.now(),
            )
        logger.info("查询审计写入完成", sql_hash=entry["sql_hash"][:12])
    except Exception as exc:
        logger.error(
            "审计日志写入失败",
            exception_type=type(exc).__name__,
            error=str(exc),
            exc_info=True,
        )
