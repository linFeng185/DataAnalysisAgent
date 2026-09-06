"""平台 MCP 安全阻断，独立于租户连接和凭证管理。"""

from typing import Literal, Self

from fastapi import HTTPException
from pydantic import BaseModel, ConfigDict, Field, model_validator

from src.logging_config import get_logger
from src.memory.pg_pool import pg_connection
from src.security.emergency_access import record_governance_audit

logger = get_logger(__name__)


class MCPSecurityBlockRequest(BaseModel):
    """仅识别资源和停用理由，不接受任何连接配置。"""

    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)
    tenant_id: int = Field(gt=0)
    scope: Literal["tenant", "private"]
    owner_user_id: int = Field(default=0, ge=0)
    name: str = Field(min_length=1, max_length=64, pattern=r"^[a-zA-Z0-9_-]+$")
    blocked: bool = True
    reason: str = Field(min_length=3, max_length=500)

    # 校验资源拥有者和范围是一致的。
    # Args: self - 已解析的安全请求。
    # Returns: 校验通过的请求。
    @model_validator(mode="after")
    def validate_owner(self) -> Self:
        if (self.scope == "tenant" and self.owner_user_id != 0) or (self.scope == "private" and self.owner_user_id <= 0):
            raise ValueError("tenant 不指定个人，private 必须指定拥有者")
        return self


# 设置或解除平台安全阻断，不读取和修改租户连接数据。
# Args: request - 资源定位、安全状态和理由。
# Returns: 安全状态，不含连接 URL 或凭证。
async def set_mcp_security_block(request: MCPSecurityBlockRequest) -> dict:
    """解除阻断不会自动启用租户连接；同名重建也沿用安全阻断。"""
    from src.api.auth import get_current_tenant_id, get_current_user_id, require_super_admin

    require_super_admin()
    user_id = get_current_user_id()
    logger.debug("MCP 平台安全操作入口", tenant_id=request.tenant_id, scope=request.scope, blocked=request.blocked)
    async with pg_connection(tenant_id=get_current_tenant_id(), user_id=user_id, role="super_admin") as connection:
        exists = await connection.fetchval("SELECT EXISTS(SELECT 1 FROM tenants WHERE id=$1)", request.tenant_id)
        if not exists:
            raise HTTPException(404, "租户不存在")
        await connection.execute(
            "INSERT INTO mcp_security_blocks (tenant_id,scope,owner_user_id,name,blocked,reason,updated_by) VALUES ($1,$2,$3,$4,$5,$6,$7) "
            "ON CONFLICT (tenant_id,scope,owner_user_id,name) DO UPDATE SET blocked=$5,reason=$6,updated_by=$7,updated_at=clock_timestamp()",
            request.tenant_id, request.scope, request.owner_user_id, request.name, request.blocked, request.reason, user_id,
        )
        resource = f"{request.scope}:{request.owner_user_id}:{request.name}"
        await record_governance_audit(connection, request.tenant_id, user_id, "mcp.block" if request.blocked else "mcp.unblock", resource, request.reason)
    logger.info("MCP 平台安全状态已更新", tenant_id=request.tenant_id, resource=resource, blocked=request.blocked)
    return request.model_dump()


# 列出平台安全状态，不读取受管连接表。
# Args: 无。
# Returns: 有界的停用元数据列表。
async def list_mcp_security_blocks() -> list[dict]:
    """仅超管可查看安全操作台，租户私有凭证始终不可见。"""
    from src.api.auth import get_current_tenant_id, get_current_user_id, require_super_admin

    require_super_admin()
    async with pg_connection(tenant_id=get_current_tenant_id(), user_id=get_current_user_id(), role="super_admin") as connection:
        rows = await connection.fetch("SELECT tenant_id,scope,owner_user_id,name,blocked,reason,updated_at FROM mcp_security_blocks ORDER BY updated_at DESC LIMIT 200")
    return [dict(row) for row in rows]
