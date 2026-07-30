"""平台超级管理员的租户、用户和安全配置管理路由。"""

from __future__ import annotations

import asyncio

from fastapi import APIRouter, HTTPException, Query
from pydantic import BaseModel, Field

from src.logging_config import get_logger

logger = get_logger(__name__)
router = APIRouter(prefix="/admin", tags=["platform-admin"])
_MANAGED_ROLES = {"tenant_admin", "analyst", "viewer"}


class TenantCreateRequest(BaseModel):
    """创建租户及首个租户管理员的请求。"""

    code: str = Field(
        ...,
        min_length=1,
        max_length=32,
        pattern=r"^[A-Za-z0-9][A-Za-z0-9-]{0,31}$",
    )
    name: str = Field(..., min_length=1, max_length=128)
    admin_username: str = Field(..., min_length=1, max_length=64)
    admin_password: str = Field(..., min_length=8, max_length=72)


class TenantStatusRequest(BaseModel):
    """启用或停用租户的请求。"""

    is_active: bool


class UserCreateRequest(BaseModel):
    """由当前租户管理员创建租户用户的请求。"""

    username: str = Field(..., min_length=1, max_length=64)
    password: str = Field(..., min_length=8, max_length=72)
    role: str = Field(default="analyst")


class UserUpdateRequest(BaseModel):
    """修改普通用户角色或启用状态的请求。"""

    role: str | None = None
    is_active: bool | None = None


class PasswordResetRequest(BaseModel):
    """重置指定用户密码并解除锁定的请求。"""

    password: str = Field(..., min_length=8, max_length=72)


# 方法作用：把数据库唯一约束异常转换为稳定的 HTTP 409。
# Args: exc - 数据库写入异常；message - 面向管理员的冲突说明。
# Returns: 始终抛出 HTTPException。
def _raise_write_error(exc: Exception, message: str) -> None:
    logger.debug("平台管理写入异常转换入口", exception_type=type(exc).__name__)
    if type(exc).__name__ in {"UniqueViolationError", "ForeignKeyViolationError"}:
        logger.warning("平台管理写入冲突", exception_type=type(exc).__name__)
        raise HTTPException(409, message) from exc
    logger.error("平台管理写入失败", error=str(exc), exc_info=True)
    raise HTTPException(500, "平台管理操作失败") from exc


# 方法作用：阻止角色或状态更新移除租户最后一个启用管理员。
# Args: connection - 数据库连接；user_id - 目标用户；tenant_id - 当前租户；role - 目标角色；is_active - 目标状态。
# Returns: 目标用户当前角色与状态，不存在时抛出 HTTP 404。
async def _ensure_tenant_admin_continuity(
    connection,
    *,
    user_id: int,
    tenant_id: int,
    role: str | None,
    is_active: bool | None,
) -> dict:
    logger.info(
        "租户管理员连续性检查边界",
        user_id=user_id,
        tenant_id=tenant_id,
        requested_role=role or "",
        requested_active=is_active,
    )
    current = await connection.fetchrow(
        "SELECT role, is_active FROM users WHERE id=$1 AND tenant_id=$2",
        user_id,
        tenant_id,
    )
    if current is None:
        raise HTTPException(404, "用户不存在")
    current_role = str(current["role"])
    current_active = bool(current["is_active"])
    next_role = role if role is not None else current_role
    next_active = is_active if is_active is not None else current_active
    removes_active_admin = (
        current_role == "tenant_admin"
        and current_active
        and not (next_role == "tenant_admin" and next_active)
    )
    if removes_active_admin:
        active_admins = int(await connection.fetchval(
            "SELECT COUNT(*) FROM users WHERE tenant_id=$1 "
            "AND role='tenant_admin' AND is_active=TRUE",
            tenant_id,
        ) or 0)
        if active_admins <= 1:
            logger.warning(
                "租户管理员连续性检查拒绝",
                user_id=user_id,
                tenant_id=tenant_id,
            )
            raise HTTPException(409, "租户必须至少保留一个启用的管理员")
    return dict(current)


@router.get("/tenants")
# 方法作用：分页列出租户、状态和账号数量。
# Args: page - 页码；page_size - 每页数量。
# Returns: 租户分页结果。
async def list_tenants(
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=20, ge=1, le=100),
) -> dict:
    """仅固定超级管理员可查看全部租户。"""
    from src.api.auth import require_super_admin
    from src.memory.pg_pool import get_pg_pool

    logger.debug("平台租户列表入口", page=page, page_size=page_size)
    require_super_admin()
    try:
        pool = await get_pg_pool()
        offset = (page - 1) * page_size
        async with pool.acquire() as connection:
            total = int(await connection.fetchval("SELECT COUNT(*) FROM tenants") or 0)
            rows = await connection.fetch(
                "SELECT t.id, t.code, t.name, t.is_active, t.created_at, "
                "COUNT(u.id) AS user_count "
                "FROM tenants t LEFT JOIN users u ON u.tenant_id=t.id "
                "GROUP BY t.id, t.code, t.name, t.is_active, t.created_at "
                "ORDER BY t.id LIMIT $1 OFFSET $2",
                page_size,
                offset,
            )
        tenants = [dict(row) for row in rows]
        result = {"tenants": tenants, "total": total, "page": page, "page_size": page_size}
        logger.info("平台租户列表完成", total=total, returned=len(tenants))
        return result
    except HTTPException:
        raise
    except Exception as exc:
        logger.error("平台租户列表失败", error=str(exc), exc_info=True)
        raise HTTPException(500, "租户列表加载失败") from exc


@router.post("/tenants", status_code=201)
# 方法作用：原子创建租户和首个 tenant_admin 账号。
# Args: request - 租户名称与管理员凭证。
# Returns: 新租户和管理员摘要。
async def create_tenant(request: TenantCreateRequest) -> dict:
    """平台超级管理员创建租户时必须同时建立可登录管理员。"""
    from src.api.auth import _hash_password, require_super_admin
    from src.memory.pg_pool import get_pg_pool

    tenant_code = request.code.strip().lower()
    logger.debug(
        "平台租户创建入口",
        tenant_name=request.name,
        tenant_code=tenant_code,
        admin=request.admin_username,
    )
    require_super_admin()
    try:
        password_hash = await asyncio.to_thread(_hash_password, request.admin_password)
        pool = await get_pg_pool()
        async with pool.acquire() as connection:
            async with connection.transaction():
                duplicate_tenant = await connection.fetchval(
                    "SELECT id FROM tenants WHERE LOWER(code)=LOWER($1) "
                    "OR LOWER(name)=LOWER($2)",
                    tenant_code,
                    request.name.strip(),
                )
                if duplicate_tenant is not None:
                    raise HTTPException(409, "租户编码或名称已存在")
                tenant_id = await connection.fetchval(
                    "INSERT INTO tenants (code, name, is_active) "
                    "VALUES ($1, $2, TRUE) RETURNING id",
                    tenant_code,
                    request.name.strip(),
                )
                user_id = await connection.fetchval(
                    "INSERT INTO users (username, password_hash, role, tenant_id, is_active) "
                    "VALUES ($1, $2, 'tenant_admin', $3, TRUE) RETURNING id",
                    request.admin_username.strip(),
                    password_hash,
                    tenant_id,
                )
        result = {
            "tenant": {
                "id": int(tenant_id),
                "code": tenant_code,
                "name": request.name.strip(),
                "is_active": True,
            },
            "admin": {
                "id": int(user_id), "username": request.admin_username.strip(),
                "role": "tenant_admin", "tenant_id": int(tenant_id), "is_active": True,
            },
        }
        logger.info("平台租户创建完成", tenant_id=tenant_id, admin_user_id=user_id)
        return result
    except HTTPException:
        raise
    except Exception as exc:
        _raise_write_error(exc, "租户名称或管理员用户名已存在")


@router.patch("/tenants/{tenant_id}")
# 方法作用：启用或停用非默认租户。
# Args: tenant_id - 租户编号；request - 目标状态。
# Returns: 更新后的租户状态。
async def update_tenant_status(tenant_id: int, request: TenantStatusRequest) -> dict:
    """默认租户承载固定超级管理员，不能被停用。"""
    from src.api.auth import require_super_admin
    from src.memory.pg_pool import get_pg_pool

    logger.debug("平台租户状态更新入口", tenant_id=tenant_id, is_active=request.is_active)
    require_super_admin()
    if tenant_id == 1 and not request.is_active:
        logger.warning("平台租户状态更新拒绝", tenant_id=tenant_id, reason="默认租户")
        raise HTTPException(403, "默认租户不能停用")
    pool = await get_pg_pool()
    try:
        async with pool.acquire() as connection:
            row = await connection.fetchrow(
                "UPDATE tenants SET is_active=$1 WHERE id=$2 RETURNING id, name, is_active",
                request.is_active,
                tenant_id,
            )
        if row is None:
            raise HTTPException(404, "租户不存在")
        result = dict(row)
        logger.info("平台租户状态更新完成", tenant_id=tenant_id, is_active=request.is_active)
        return result
    except HTTPException:
        raise
    except Exception as exc:
        _raise_write_error(exc, "租户状态更新冲突")


@router.get("/users")
# 方法作用：按租户、角色和状态分页列出平台用户。
# Args: tenant_id - 可选租户；role - 可选角色；is_active - 可选状态；page/page_size - 分页。
# Returns: 用户分页结果，不含密码哈希。
async def list_users(
    role: str | None = None,
    is_active: bool | None = None,
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=20, ge=1, le=100),
) -> dict:
    """返回管理页面所需的最小账号信息。"""
    from src.api.auth import get_current_tenant_id, require_tenant_user_admin
    from src.memory.pg_pool import get_pg_pool

    tenant_id = get_current_tenant_id()
    logger.debug("租户用户列表入口", tenant_id=tenant_id, role=role or "", page=page)
    require_tenant_user_admin()
    pool = await get_pg_pool()
    try:
        offset = (page - 1) * page_size
        async with pool.acquire() as connection:
            total = int(await connection.fetchval(
                "SELECT COUNT(*) FROM users WHERE tenant_id=$1 "
                "AND ($2::text IS NULL OR role=$2) AND ($3::bool IS NULL OR is_active=$3)",
                tenant_id, role, is_active,
            ) or 0)
            rows = await connection.fetch(
                "SELECT id, username, tenant_id, role, is_active, failed_login_attempts, "
                "locked_until, last_login_at, created_at FROM users "
                "WHERE tenant_id=$1 "
                "AND ($2::text IS NULL OR role=$2) AND ($3::bool IS NULL OR is_active=$3) "
                "ORDER BY id LIMIT $4 OFFSET $5",
                tenant_id, role, is_active, page_size, offset,
            )
        users = [dict(row) for row in rows]
        result = {"users": users, "total": total, "page": page, "page_size": page_size}
        logger.info("平台用户列表完成", total=total, returned=len(users))
        return result
    except Exception as exc:
        logger.error("平台用户列表失败", error=str(exc), exc_info=True)
        raise HTTPException(500, "用户列表加载失败") from exc


@router.post("/users", status_code=201)
# 方法作用：在已存在的启用租户中创建普通或租户管理员账号。
# Args: request - 租户、用户名、密码和角色。
# Returns: 新用户摘要。
async def create_user(request: UserCreateRequest) -> dict:
    """禁止通过通用端点创建第二个 super_admin。"""
    from src.api.auth import (
        _hash_password,
        get_current_tenant_id,
        require_tenant_user_admin,
    )
    from src.memory.pg_pool import get_pg_pool

    tenant_id = get_current_tenant_id()
    logger.debug("租户用户创建入口", tenant_id=tenant_id, role=request.role)
    require_tenant_user_admin()
    role = request.role.strip().lower()
    if role not in _MANAGED_ROLES:
        raise HTTPException(400, "不支持的用户角色")
    try:
        password_hash = await asyncio.to_thread(_hash_password, request.password)
        pool = await get_pg_pool()
        async with pool.acquire() as connection:
            row = await connection.fetchrow(
                "INSERT INTO users (username, password_hash, role, tenant_id, is_active) "
                "SELECT $1, $2, $3, $4, TRUE FROM tenants "
                "WHERE id=$4 AND is_active=TRUE "
                "RETURNING id, username, tenant_id, role, is_active, created_at",
                request.username.strip(), password_hash, role, tenant_id,
            )
        if row is None:
            raise HTTPException(409, "当前租户不存在或已停用")
        result = dict(row)
        logger.info("租户用户创建完成", user_id=result["id"], tenant_id=tenant_id)
        return result
    except HTTPException:
        raise
    except Exception as exc:
        _raise_write_error(exc, "用户名已存在")


@router.patch("/users/{user_id}")
# 方法作用：修改非固定账号的角色或启用状态。
# Args: user_id - 用户编号；request - 可选角色和状态。
# Returns: 更新后的用户摘要。
async def update_user(user_id: int, request: UserUpdateRequest) -> dict:
    """固定超级管理员不能通过后台被降权或停用。"""
    from src.api.auth import (
        SUPER_ADMIN_USER_ID,
        get_current_tenant_id,
        require_tenant_user_admin,
    )
    from src.memory.pg_pool import get_pg_pool

    tenant_id = get_current_tenant_id()
    logger.debug(
        "租户用户更新入口",
        user_id=user_id,
        tenant_id=tenant_id,
        role=request.role or "",
    )
    require_tenant_user_admin()
    if user_id == SUPER_ADMIN_USER_ID:
        raise HTTPException(403, "固定超级管理员不能修改")
    role = request.role.strip().lower() if request.role is not None else None
    if role is not None and role not in _MANAGED_ROLES:
        raise HTTPException(400, "不支持的用户角色")
    if role is None and request.is_active is None:
        raise HTTPException(400, "没有可更新字段")
    pool = await get_pg_pool()
    try:
        async with pool.acquire() as connection:
            await _ensure_tenant_admin_continuity(
                connection,
                user_id=user_id,
                tenant_id=tenant_id,
                role=role,
                is_active=request.is_active,
            )
            row = await connection.fetchrow(
                "UPDATE users SET role=COALESCE($1, role), is_active=COALESCE($2, is_active), "
                "failed_login_attempts=CASE WHEN $2=TRUE THEN 0 ELSE failed_login_attempts END, "
                "locked_until=CASE WHEN $2=TRUE THEN NULL ELSE locked_until END "
                "WHERE id=$3 AND tenant_id=$4 "
                "RETURNING id, username, tenant_id, role, is_active, created_at",
                role, request.is_active, user_id, tenant_id,
            )
        if row is None:
            raise HTTPException(404, "用户不存在")
        result = dict(row)
        logger.info("平台用户更新完成", user_id=user_id, role=result["role"])
        return result
    except HTTPException:
        raise
    except Exception as exc:
        _raise_write_error(exc, "用户状态更新冲突")


@router.post("/users/{user_id}/reset-password")
# 方法作用：重置非固定或固定用户密码并清除登录锁定。
# Args: user_id - 用户编号；request - 新密码。
# Returns: 重置成功状态。
async def reset_user_password(user_id: int, request: PasswordResetRequest) -> dict:
    """密码重置不返回任何哈希或明文。"""
    from src.api.auth import (
        _hash_password,
        get_current_tenant_id,
        require_tenant_user_admin,
    )
    from src.memory.pg_pool import get_pg_pool

    tenant_id = get_current_tenant_id()
    logger.debug("租户用户密码重置入口", user_id=user_id, tenant_id=tenant_id)
    require_tenant_user_admin()
    try:
        password_hash = await asyncio.to_thread(_hash_password, request.password)
        pool = await get_pg_pool()
        async with pool.acquire() as connection:
            status = await connection.execute(
                "UPDATE users SET password_hash=$1, failed_login_attempts=0, locked_until=NULL "
                "WHERE id=$2 AND tenant_id=$3",
                password_hash, user_id, tenant_id,
            )
        if status == "UPDATE 0":
            raise HTTPException(404, "用户不存在")
        logger.info("平台用户密码重置完成", user_id=user_id)
        return {"status": "ok", "user_id": user_id}
    except HTTPException:
        raise
    except Exception as exc:
        _raise_write_error(exc, "密码重置失败")


@router.get("/config")
# 方法作用：返回不包含任何密钥和连接凭证的运行配置摘要。
# Args: 无。
# Returns: 环境、功能开关、限制参数和依赖配置状态。
async def get_config_summary() -> dict:
    """配置后台只展示安全摘要，不提供在线读取密钥能力。"""
    from src.api.auth import require_super_admin
    from src.config import get_settings

    logger.debug("平台配置摘要入口")
    require_super_admin()
    settings = get_settings()
    result = {
        "environment": settings.env,
        "multi_tenant": settings.multi_tenant,
        "registration_enabled": False,
        "database_configured": bool(settings.database_url),
        "jwt_configured": bool(settings.jwt_secret),
        "credential_key_configured": bool(settings.credential_encryption_key),
        "vector_store_type": settings.vector_store_type,
        "datasource_cache_backend": settings.datasource_cache_backend,
        "login_max_per_hour": settings.login_max_per_hour,
        "login_lockout_threshold": settings.login_lockout_threshold,
        "login_lockout_minutes": settings.login_lockout_minutes,
        "max_queries_per_hour": settings.max_queries_per_hour,
        "mcp_server_count": len(settings.mcp_servers),
    }
    logger.info("平台配置摘要完成", environment=settings.env)
    return result
