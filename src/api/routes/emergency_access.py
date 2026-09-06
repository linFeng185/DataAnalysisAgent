"""显式、限时的知识应急授权 API。"""

from uuid import UUID

from fastapi import APIRouter, HTTPException, Query
from fastapi.responses import Response

from src.api.auth import SUPER_ADMIN_USER_ID, get_current_user_id
from src.logging_config import get_logger
from src.memory.pg_pool import pg_connection
from src.security.emergency_access import (
    EmergencyGrantRequest,
    create_emergency_grant,
    read_emergency_file,
    revoke_emergency_grant,
)

router = APIRouter(prefix="/knowledge/emergency-grants", tags=["知识应急授权"])
logger = get_logger(__name__)


# 按显示账号模糊搜索可接受应急授权的平台用户，不暴露用户管理目录。
# Args: query - 搜索文本，作为字面子串处理。
# Returns: 最多二十条账号编号与名称。
@router.get("/recipients")
async def recipients(query: str = Query("", max_length=128)) -> dict:
    """只返回实际受平台身份约束支持的启用账号。"""
    if get_current_user_id() <= 0:
        raise HTTPException(401, "请先登录")
    logger.debug("应急授权用户搜索入口", actor_user_id=get_current_user_id(), query_length=len(query))
    async with pg_connection(tenant_id=0, user_id=0, role="super_admin") as connection:
        rows = await connection.fetch(
            "SELECT id,username FROM users WHERE id=$1 AND role='super_admin' AND is_active "
            "AND strpos(lower(username),lower($2))>0 ORDER BY username LIMIT 20",
            SUPER_ADMIN_USER_ID, query.strip(),
        )
    return {"items": [dict(row) for row in rows]}


# 签发单文件授权。
# Args: request - 授权请求。
# Returns: 授权 ID 和到期时间。
@router.post("")
async def create_grant(request: EmergencyGrantRequest) -> dict:
    """由拥有者或对应租户管理员签发。"""
    return await create_emergency_grant(request)


# 撤销本人签发的授权。
# Args: grant_id - 授权 ID。
# Returns: 撤销状态。
@router.delete("/{grant_id}")
async def revoke_grant(grant_id: UUID) -> dict:
    """不允许超管代替授权者操作。"""
    return await revoke_emergency_grant(grant_id)


# 在专用端点读取获准的文件，不将授权加入普通知识检索。
# Args: grant_id - 明确指定的授权 ID。
# Returns: 不可缓存的文件下载响应。
@router.get("/{grant_id}/content")
async def read_granted_file(grant_id: UUID) -> Response:
    """以附件而非内联页面交付，避免主动内容在平台域执行。"""
    content, content_type = await read_emergency_file(grant_id)
    return Response(content, media_type=content_type, headers={"Cache-Control": "no-store", "Content-Disposition": "attachment", "X-Content-Type-Options": "nosniff"})
