"""由知识拥有者签发的限时、单文件、只读应急授权。"""

from datetime import datetime
from typing import Any
from uuid import UUID, uuid4

from fastapi import HTTPException
from pydantic import BaseModel, ConfigDict, Field

from src.knowledge.governance import can_manage_knowledge_resource
from src.logging_config import get_logger
from src.memory.pg_pool import pg_connection

logger = get_logger(__name__)


class EmergencyGrantRequest(BaseModel):
    """授权必须指定文件、受让人、理由和最长期限。"""

    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)
    file_id: int = Field(gt=0)
    grantee_user_id: int = Field(gt=0)
    reason: str = Field(min_length=3, max_length=500)
    minutes: int = Field(default=15, ge=1, le=60)


# 判断当前身份能否授权该知识文件，不允许超管自批。
# Args: file - 文件元数据；tenant_id/user_id/role - 授权者身份。
# Returns: 允许授权时返回 True。
def can_grant_file(file: dict, tenant_id: int, user_id: int, role: str) -> bool:
    """私有文件严格要求拥有者，租户管理员不能代批。"""
    return role != "super_admin" and file["knowledge_scope"] != "system" and can_manage_knowledge_resource(
        file["knowledge_scope"], role=role, current_tenant_id=tenant_id,
        resource_tenant_id=file["tenant_id"], current_user_id=user_id, owner_user_id=file["user_id"],
    )


# 每次读取时检查受让人、有效期、授权者状态和原文件归属。
# Args: grant - 含当前授权者状态的授权；file - 当前文件；user_id/role - 受让身份；now - 数据库时间。
# Returns: 无返回值，无效授权抛出 404。
def validate_emergency_grant(grant: dict, file: dict, *, user_id: int, role: str, now: datetime) -> None:
    """授权不可转授，也不会在文件变更拥有者后继续生效。"""
    if (
        role != "super_admin" or grant["grantee_user_id"] != user_id
        or grant["revoked_at"] is not None or grant["expires_at"] <= now
        or not grant["issuer_active"] or not grant["tenant_active"]
        or file["tenant_id"] != grant["tenant_id"] or file["knowledge_scope"] != grant["scope"]
        or file["user_id"] != grant["owner_user_id"]
        or not can_grant_file(file, grant["tenant_id"], grant["issued_by"], grant["issuer_role"])
    ):
        raise HTTPException(404, "应急授权不存在或已失效")


# 在同一事务中记录不含文件内容或凭证的治理审计。
# Args: connection - 当前事务；tenant_id/user_id - 操作者；action/resource/reason - 审计摘要。
# Returns: 无返回值，审计失败则整个敏感操作失败。
async def record_governance_audit(connection: Any, tenant_id: int, user_id: int, action: str, resource: str, reason: str) -> None:
    """数据库审计写入失败时不得放行敏感访问。"""
    await connection.execute(
        "INSERT INTO governance_audit (tenant_id,actor_user_id,action,resource,reason) VALUES ($1,$2,$3,$4,$5)",
        tenant_id, user_id, action, resource, reason,
    )


# 创建一个租户或个人知识文件的限时只读授权。
# Args: request - 文件、受让超管及授权理由。
# Returns: 授权编号和数据库到期时间。
async def create_emergency_grant(request: EmergencyGrantRequest) -> dict:
    """先验证拥有者，再验证目标为当前启用的平台账号。"""
    from src.api.auth import get_current_role, get_current_tenant_id, get_current_user_id

    tenant_id, user_id, role = get_current_tenant_id(), get_current_user_id(), get_current_role()
    if role == "super_admin":
        raise HTTPException(403, "平台账号不能签发自己的应急授权")
    logger.debug("签发知识应急授权入口", tenant_id=tenant_id, user_id=user_id, file_id=request.file_id)
    async with pg_connection(tenant_id=tenant_id, user_id=user_id, role=role) as connection:
        file = await connection.fetchrow("SELECT tenant_id,user_id,knowledge_scope FROM knowledge_files WHERE id=$1 FOR SHARE", request.file_id)
        if not file or not can_grant_file(dict(file), tenant_id, user_id, role):
            raise HTTPException(404, "知识文件不存在或无权授权")
        # 仅通过内部系统连接确认受让账号状态，不返回任何跨租户用户信息。
        async with pg_connection(tenant_id=0, user_id=0, role="super_admin") as platform:
            valid = await platform.fetchval("SELECT EXISTS(SELECT 1 FROM users WHERE id=$1 AND role='super_admin' AND is_active)", request.grantee_user_id)
        if not valid:
            raise HTTPException(422, "受让人必须是启用的平台账号")
        grant_id = str(uuid4())
        row = await connection.fetchrow(
            "INSERT INTO knowledge_emergency_grants (id,tenant_id,file_id,scope,owner_user_id,issued_by,grantee_user_id,reason,expires_at) "
            "VALUES ($1::uuid,$2,$3,$4,$5,$6,$7,$8,clock_timestamp()+make_interval(mins => $9)) RETURNING expires_at",
            grant_id, tenant_id, request.file_id, file["knowledge_scope"], file["user_id"], user_id, request.grantee_user_id, request.reason, request.minutes,
        )
        await record_governance_audit(connection, tenant_id, user_id, "knowledge.grant", grant_id, request.reason)
    logger.info("知识应急授权已签发", tenant_id=tenant_id, grant_id=grant_id)
    return {"id": grant_id, "expires_at": row["expires_at"]}


# 撤销当前用户签发的授权，不允许其他租户或超管代操作。
# Args: grant_id - 授权 UUID。
# Returns: 幂等撤销状态。
async def revoke_emergency_grant(grant_id: UUID) -> dict:
    """与读取端使用同一授权行锁，保证已完成撤销后不能再读。"""
    from src.api.auth import get_current_role, get_current_tenant_id, get_current_user_id

    tenant_id, user_id, role = get_current_tenant_id(), get_current_user_id(), get_current_role()
    logger.debug("撤销应急授权入口", grant_id=str(grant_id), user_id=user_id)
    if role == "super_admin":
        raise HTTPException(403, "平台账号不能撤销拥有者的授权")
    async with pg_connection(tenant_id=tenant_id, user_id=user_id, role=role) as connection:
        row = await connection.fetchrow("UPDATE knowledge_emergency_grants SET revoked_at=COALESCE(revoked_at,clock_timestamp()) WHERE id=$1 AND tenant_id=$2 AND issued_by=$3 RETURNING id", grant_id, tenant_id, user_id)
        if not row:
            raise HTTPException(404, "授权不存在")
        await record_governance_audit(connection, tenant_id, user_id, "knowledge.revoke", str(grant_id), "授权者撤销")
    logger.info("知识应急授权已撤销", grant_id=str(grant_id))
    return {"revoked": True}


# 根据显式授权读取单个知识文件，并在返回正文前提交审计。
# Args: grant_id - 已签发的授权 UUID。
# Returns: 文件字节及内容类型。
async def read_emergency_file(grant_id: UUID) -> tuple[bytes, str]:
    """普通检索不使用此授权；每次显式访问重新验证当前所有权。"""
    from src.api.auth import get_current_tenant_id, get_current_user_id, require_super_admin

    require_super_admin()
    user_id = get_current_user_id()
    logger.debug("应急读取知识入口", grant_id=str(grant_id), user_id=user_id)
    async with pg_connection(tenant_id=get_current_tenant_id(), user_id=user_id, role="super_admin") as connection:
        grant = await connection.fetchrow(
            "SELECT g.*,u.role AS issuer_role,u.is_active AS issuer_active,t.is_active AS tenant_active "
            "FROM knowledge_emergency_grants g JOIN users u ON u.id=g.issued_by AND u.tenant_id=g.tenant_id "
            "JOIN tenants t ON t.id=g.tenant_id WHERE g.id=$1 AND g.grantee_user_id=$2 FOR UPDATE OF g",
            grant_id, user_id,
        )
        if not grant:
            raise HTTPException(404, "应急授权不存在或已失效")
        await connection.execute("SELECT set_config('app.knowledge_emergency_grant',$1,true)", str(grant_id))
        file = await connection.fetchrow("SELECT tenant_id,user_id,knowledge_scope,file_data,content_type FROM knowledge_files WHERE id=$1", grant["file_id"])
        if not file:
            raise HTTPException(404, "知识文件不存在或授权已失效")
        now = await connection.fetchval("SELECT clock_timestamp()")
        validate_emergency_grant(dict(grant), dict(file), user_id=user_id, role="super_admin", now=now)
        await record_governance_audit(connection, grant["tenant_id"], user_id, "knowledge.read", str(grant_id), grant["reason"])
        result = bytes(file["file_data"]), str(file["content_type"])
    logger.info("应急知识读取已审计", grant_id=str(grant_id), user_id=user_id)
    return result
