"""PostgreSQL 共享查询额度；数据库故障时失败关闭。"""

from typing import Self

from fastapi import HTTPException
from pydantic import BaseModel, ConfigDict, Field, model_validator

from src.config import get_settings
from src.logging_config import get_logger
from src.memory.pg_pool import pg_connection

logger = get_logger(__name__)


class QueryQuotaPolicy(BaseModel):
    """当前租户聚合额度及每用户额度，零值表示暂停。"""

    model_config = ConfigDict(extra="forbid", strict=True)
    tenant_per_hour: int = Field(ge=0, le=1_000_000_000)
    user_per_hour: int = Field(ge=0, le=1_000_000_000)

    # 校验用户软额度不会超过租户聚合额度。
    # Args: self - 已解析的额度对象。
    # Returns: 校验通过的额度对象。
    @model_validator(mode="after")
    def validate_order(self) -> Self:
        if self.user_per_hour > self.tenant_per_hour:
            raise ValueError("用户额度不能超过租户额度")
        return self


# 以数据库时钟和租户事务锁原子消费一次用户与租户额度。
# Args: tenant_id - 业务租户；user_id - 业务用户。
# Returns: 允许执行返回 True，额度耗尽或无效身份返回 False，存储异常抛出 503。
async def consume_query_quota(tenant_id: int, user_id: int) -> bool:
    """不使用进程缓存，同一租户的所有 Worker 共用数据库计数。"""
    if tenant_id <= 0 or user_id <= 0:
        return False
    hard_limit = max(0, int(get_settings().max_queries_per_hour))
    logger.debug("共享查询额度入口", tenant_id=tenant_id, user_id=user_id)
    try:
        async with pg_connection(tenant_id=tenant_id, user_id=user_id, role="analyst") as connection:
            await connection.execute("SELECT pg_advisory_xact_lock(hashtextextended('query_quota:' || $1::text, 0))", str(tenant_id))
            window = await connection.fetchval("SELECT floor(extract(epoch FROM clock_timestamp()) / 3600)::bigint")
            row = await connection.fetchrow("SELECT tenant_per_hour,user_per_hour FROM query_quota_policies WHERE tenant_id=$1", tenant_id)
            tenant_limit = min(hard_limit, int(row["tenant_per_hour"])) if row else hard_limit
            user_limit = min(tenant_limit, int(row["user_per_hour"])) if row else hard_limit
            tenant_used = await connection.fetchval("SELECT used FROM query_rate_usage WHERE tenant_id=$1 AND user_id=$2 AND window_hour=$3", tenant_id, 0, window) or 0
            user_used = await connection.fetchval("SELECT used FROM query_rate_usage WHERE tenant_id=$1 AND user_id=$2 AND window_hour=$3", tenant_id, user_id, window) or 0
            logger.debug("共享查询额度快照", tenant_id=tenant_id, user_id=user_id, tenant_used=tenant_used, user_used=user_used, tenant_limit=tenant_limit, user_limit=user_limit)
            if tenant_used >= tenant_limit or user_used >= user_limit:
                logger.warning("共享查询额度拒绝", tenant_id=tenant_id, user_id=user_id)
                return False
            # 持有租户锁直到两个计数一起提交，拒绝和异常不会留下半次扣减。
            for owner in (0, user_id):
                await connection.execute(
                    "INSERT INTO query_rate_usage (tenant_id,user_id,window_hour,used) VALUES ($1,$2,$3,1) "
                    "ON CONFLICT (tenant_id,user_id,window_hour) DO UPDATE SET used=query_rate_usage.used+1",
                    tenant_id, owner, window,
                )
            await connection.execute("DELETE FROM query_rate_usage WHERE tenant_id=$1 AND window_hour < $2", tenant_id, window - 24)
        return True
    except Exception as exc:
        logger.error("共享查询额度存储失败", tenant_id=tenant_id, user_id=user_id, exc_info=True)
        raise HTTPException(503, "查询额度服务不可用，请稍后重试") from exc


# 获取或更新当前租户软额度，平台身份不具备代配权限。
# Args: policy - 可选新策略，为 None 时只读取。
# Returns: 软额度、硬上限和窗口说明。
async def tenant_query_policy(policy: QueryQuotaPolicy | None = None) -> dict:
    """在租户管理边界执行额度配置，并与消费路径使用同一事务锁。"""
    from src.api.auth import get_current_tenant_id, get_current_user_id, require_tenant_user_admin

    require_tenant_user_admin()
    tenant_id, user_id = get_current_tenant_id(), get_current_user_id()
    hard_limit = max(0, int(get_settings().max_queries_per_hour))
    if policy is not None and policy.tenant_per_hour > hard_limit:
        raise HTTPException(422, "租户额度不能超过平台硬上限")
    logger.debug("租户查询策略入口", tenant_id=tenant_id, update=policy is not None)
    try:
        async with pg_connection(tenant_id=tenant_id, user_id=user_id, role="tenant_admin") as connection:
            await connection.execute("SELECT pg_advisory_xact_lock(hashtextextended('query_quota:' || $1::text, 0))", str(tenant_id))
            if policy is not None:
                await connection.execute(
                    "INSERT INTO query_quota_policies (tenant_id,tenant_per_hour,user_per_hour,updated_by) VALUES ($1,$2,$3,$4) "
                    "ON CONFLICT (tenant_id) DO UPDATE SET tenant_per_hour=$2,user_per_hour=$3,updated_by=$4,updated_at=NOW()",
                    tenant_id, policy.tenant_per_hour, policy.user_per_hour, user_id,
                )
                logger.info("租户查询策略已更新", tenant_id=tenant_id, user_id=user_id, **policy.model_dump())
            row = await connection.fetchrow("SELECT tenant_per_hour,user_per_hour FROM query_quota_policies WHERE tenant_id=$1", tenant_id)
        tenant_limit = min(hard_limit, int(row["tenant_per_hour"])) if row else hard_limit
        return {"tenant_per_hour": tenant_limit, "user_per_hour": min(tenant_limit, int(row["user_per_hour"])) if row else hard_limit, "hard_limit": hard_limit, "window": "UTC 自然小时，多个 Worker 共享计数"}
    except Exception as exc:
        logger.error("租户查询策略存储失败", tenant_id=tenant_id, exc_info=True)
        raise HTTPException(503, "查询额度配置暂不可用") from exc
