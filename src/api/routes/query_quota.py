"""租户查询额度管理入口。"""

from fastapi import APIRouter

from src.security.query_quota import QueryQuotaPolicy, tenant_query_policy

router = APIRouter(prefix="/admin/query-quota", tags=["查询额度"])


# 读取当前租户软额度及平台硬上限。
# Args: 无。
# Returns: 当前租户的额度说明。
@router.get("")
async def get_query_quota() -> dict:
    """仅当前租户管理员可读。"""
    return await tenant_query_policy()


# 更新当前租户软额度。
# Args: request - 严格校验的租户/用户额度。
# Returns: 更新后的额度说明。
@router.put("")
async def update_query_quota(request: QueryQuotaPolicy) -> dict:
    """平台硬上限不可由租户覆盖。"""
    return await tenant_query_policy(request)
