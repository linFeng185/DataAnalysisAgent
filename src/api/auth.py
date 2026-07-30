"""JWT 认证模块：中间件 + ContextVar + 登录/注册端点。

架构:
  AuthMiddleware → 解析 JWT → 写入 ContextVar
  业务代码 → get_current_user_id() / get_current_tenant_id() → 获取身份
  PG RLS → current_setting('app.current_tenant_id') → 数据库级强制隔离

认证与租户隔离解耦：所有模式都必须登录，multi_tenant 只控制租户数据隔离。
"""

from __future__ import annotations

import asyncio
import os
import threading
import time
from contextvars import ContextVar, Token
from datetime import datetime, timedelta, timezone

import bcrypt
import jwt
from fastapi import APIRouter, HTTPException, Request, Response
from pydantic import BaseModel, Field
from starlette.responses import JSONResponse
from starlette.types import ASGIApp, Receive, Scope, Send

from src.app_context import get_tenant_policy
from src.config import get_settings
from src.logging_config import get_logger
from src.memory.pg_pool import get_pg_pool
from src.security.tenant_policy import (
    ANONYMOUS_ROLE,
    ANONYMOUS_USER_ID,
    DEFAULT_TENANT_ID,
    SUPER_ADMIN_USER_ID,
    RequestIdentity,
)

logger = get_logger(__name__)

# ── ContextVar: 协程级用户上下文 ──

_current_user_id: ContextVar[int] = ContextVar(
    "current_user_id",
    default=ANONYMOUS_USER_ID,
)
_current_tenant_id: ContextVar[int] = ContextVar(
    "current_tenant_id",
    default=DEFAULT_TENANT_ID,
)
_current_role: ContextVar[str] = ContextVar("current_role", default=ANONYMOUS_ROLE)
_current_tenant_code: ContextVar[str] = ContextVar("current_tenant_code", default="default")
_current_username: ContextVar[str] = ContextVar("current_username", default="")
ACCESS_TOKEN_COOKIE = "access_token"
_DUMMY_PASSWORD_HASH = (
    "$2b$12$yZTzGXG0vRKu0H18pTJmo.9LXjZUANU747KXcPehet4YrkgPnQKlG"
)
_registration_limits: dict[str, list[float]] = {}
_registration_rate_lock = threading.Lock()
_login_limits: dict[tuple[str, str, str], list[float]] = {}
_login_rate_lock = threading.Lock()
AuthIdentity = tuple[int, int, str, str, str]


def get_current_user_id() -> int:
    """获取当前请求的用户 ID。

    由 AuthMiddleware 在请求进入时设置。
    单租户模式下返回 0（anonymous）。

    Returns: 用户 ID
    """
    logger.debug("获取当前用户 ID 入口")
    result = _current_user_id.get()
    logger.debug("获取当前用户 ID 完成", user_id=result)
    return result


def get_current_tenant_id() -> int:
    """获取当前请求的租户 ID。

    单租户模式下返回 1（default 租户）。

    Returns: 租户 ID
    """
    logger.debug("获取当前租户 ID 入口")
    result = _current_tenant_id.get()
    logger.debug("获取当前租户 ID 完成", tenant_id=result)
    return result


def get_current_role() -> str:
    """获取当前请求的用户角色。

    Returns:
        当前角色；匿名请求返回 anonymous。
    """
    logger.debug("获取当前角色入口")
    result = _current_role.get()
    logger.debug("获取当前角色完成", role=result)
    return result


# 方法作用：获取当前请求令牌携带的租户编码。
# Args: 无。
# Returns: 当前租户编码，兼容旧令牌时返回 default。
def get_current_tenant_code() -> str:
    result = _current_tenant_code.get()
    logger.debug("获取当前租户编码完成", tenant_code=result)
    return result


# 方法作用：获取当前请求令牌携带的大小写敏感用户名。
# Args: 无。
# Returns: 当前用户名，兼容旧令牌时返回空字符串。
def get_current_username() -> str:
    result = _current_username.get()
    logger.debug("获取当前用户名完成", username=result)
    return result


# 方法作用：把三个认证 ContextVar 统一组装为租户策略身份快照。
# Args: 无。
# Returns: 当前请求的 RequestIdentity。
def get_current_identity() -> RequestIdentity:
    logger.debug("获取当前请求身份入口")
    result = RequestIdentity(
        tenant_id=get_current_tenant_id(),
        user_id=get_current_user_id(),
        role=get_current_role(),
    )
    logger.debug(
        "获取当前请求身份完成",
        tenant_id=result.tenant_id,
        user_id=result.user_id,
        role=result.role,
    )
    return result


# 要求当前身份具备平台超级管理员权限。
# Args: 无。
# Returns: 授权成功时无返回值，否则抛出 HTTP 403。
def require_super_admin() -> None:
    logger.debug(
        "校验超级管理员权限入口",
        role=get_current_role(),
        user_id=get_current_user_id(),
    )
    from src.knowledge.governance import is_super_admin

    role = get_current_role()
    user_id = get_current_user_id()
    if not is_super_admin(role) or user_id != SUPER_ADMIN_USER_ID:
        logger.warning("校验超级管理员权限拒绝", role=role, user_id=user_id)
        raise HTTPException(403, "需要超级管理员权限")
    logger.debug("校验超级管理员权限完成", role=role, user_id=user_id)


# 方法作用：判断当前请求是否为数据库保留的平台超级管理员身份。
# Args: 无。
# Returns: user_id=1 且角色为 super_admin 时返回 True。
def is_platform_super_admin() -> bool:
    logger.debug("判断固定超级管理员入口")
    from src.knowledge.governance import is_super_admin

    result = (
        get_current_user_id() == SUPER_ADMIN_USER_ID
        and is_super_admin(get_current_role())
    )
    logger.debug("判断固定超级管理员完成", allowed=result)
    return result


# 要求当前身份具备租户管理权限或更高的平台权限。
# Args: 无。
# Returns: 授权成功时无返回值，否则抛出 HTTP 403。
def require_tenant_admin() -> None:
    logger.debug("校验租户管理员权限入口", role=get_current_role())
    from src.knowledge.governance import is_super_admin, is_tenant_admin

    role = get_current_role()
    if is_super_admin(role):
        require_super_admin()
        logger.debug("校验租户管理员权限完成", role=role, platform_admin=True)
        return
    if not is_tenant_admin(role):
        logger.warning("校验租户管理员权限拒绝", role=role)
        raise HTTPException(403, "需要租户管理员权限")
    logger.debug("校验租户管理员权限完成", role=role)


# 方法作用：限制租户用户管理能力只能由 tenant_admin 使用。
# Args: 无。
# Returns: 授权成功时无返回值，否则抛出 HTTP 403。
def require_tenant_user_admin() -> None:
    role = get_current_role()
    logger.debug("校验租户用户管理员入口", role=role, tenant_id=get_current_tenant_id())
    if role != "tenant_admin":
        logger.warning("校验租户用户管理员拒绝", role=role)
        raise HTTPException(403, "需要当前租户管理员权限")
    logger.debug("校验租户用户管理员完成", role=role)


# 方法作用：使用 bcrypt 生成密码哈希并拒绝超出算法安全边界的输入。
# Args: password - 待哈希的明文密码。
# Returns: 可写入数据库的 bcrypt 哈希字符串。
def _hash_password(password: str) -> str:
    logger.debug("密码哈希入口", password_chars=len(password))
    encoded = password.encode("utf-8")
    if len(encoded) > 72:
        logger.warning("密码哈希拒绝", reason="超过 bcrypt 72 字节限制")
        raise ValueError("密码不能超过 72 字节")
    result = bcrypt.hashpw(encoded, bcrypt.gensalt(rounds=12)).decode("ascii")
    logger.debug("密码哈希完成")
    return result


# 方法作用：以恒定时间 bcrypt 校验密码且不记录敏感内容。
# Args: password - 用户提交密码；password_hash - 数据库存储的哈希。
# Returns: 密码匹配返回 True，非法哈希或超长输入返回 False。
def _verify_password(password: str, password_hash: str) -> bool:
    logger.debug("密码校验入口", password_chars=len(password))
    try:
        encoded = password.encode("utf-8")
        if len(encoded) > 72:
            logger.warning("密码校验拒绝", reason="超过 bcrypt 72 字节限制")
            return False
        result = bcrypt.checkpw(encoded, password_hash.encode("ascii"))
    except (TypeError, ValueError):
        logger.error("密码校验失败", exc_info=True)
        return False
    logger.debug("密码校验完成", matched=result)
    return result


def scope_thread_id(session_id: str) -> str:
    """为 Checkpointer 生成带租户和用户命名空间的线程 ID。

    Args:
        session_id: 对外暴露的会话 ID。

    Returns:
        不同用户无法碰撞的内部线程 ID。
    """
    tenant_id = get_current_tenant_id()
    user_id = get_current_user_id()
    logger.debug("会话线程命名空间入口", session_id=session_id[:20], tenant_id=tenant_id, user_id=user_id)
    scoped = f"tenant:{tenant_id}:user:{user_id}:session:{session_id}"
    logger.debug("会话线程命名空间完成", scoped_session=scoped[-40:])
    return scoped


# ── JWT ──

_secret_cache: str | None = None


def _secret() -> str:
    """获取 JWT 签名密钥。优先环境变量 JWT_SECRET，回退 config。

    未配置时仅开发和测试模式生成临时密钥，生产模式直接阻断。
    """
    global _secret_cache
    if _secret_cache is not None:
        return _secret_cache

    env_key = os.getenv("JWT_SECRET", "")
    settings = get_settings()
    cfg_key = settings.jwt_secret
    key = env_key or cfg_key

    if not key:
        if getattr(settings, "env", "prod") == "prod":
            logger.error("JWT_SECRET 未配置，生产签名已阻断")
            raise RuntimeError("生产环境必须配置 JWT_SECRET")
        import secrets
        key = secrets.token_hex(32)
        logger.warning("JWT_SECRET 未配置，已为非生产环境生成临时密钥")

    if key == "dev-secret-change-in-production" or len(key) < 16:
        logger.warning("JWT_SECRET 强度不足！生产环境请使用至少 32 字节的随机密钥。")

    _secret_cache = key
    return key


def create_access_token(
    user_id: int,
    tenant_id: int,
    role: str,
    tenant_code: str = "default",
    username: str = "",
) -> str:
    """创建 JWT access token。

    Args:
        user_id: 用户 ID
        tenant_id: 租户 ID
        role: 角色（admin/analyst/viewer）

    Returns: JWT 字符串
    """
    logger.debug("创建访问令牌入口", user_id=user_id, tenant_id=tenant_id, role=role)
    s = get_settings()
    token = jwt.encode({
        "user_id": user_id, "tenant_id": tenant_id, "role": role,
        "tenant_code": tenant_code, "username": username,
        "exp": datetime.now(timezone.utc) + timedelta(hours=s.jwt_access_token_expire_hours),
    }, _secret(), algorithm="HS256")
    logger.debug("创建访问令牌完成", user_id=user_id, tenant_id=tenant_id)
    return token


def _set_access_cookie(response: Response, token: str) -> None:
    """把访问令牌写入安全 Cookie。

    Args:
        response: FastAPI 响应对象。
        token: 已签名的 JWT。

    Returns:
        无返回值。
    """
    settings = get_settings()
    max_age = settings.jwt_access_token_expire_hours * 3600
    logger.debug("设置访问 Cookie 入口", secure=settings.env == "prod", max_age=max_age)
    response.set_cookie(
        key=ACCESS_TOKEN_COOKIE,
        value=token,
        max_age=max_age,
        httponly=True,
        secure=settings.env == "prod",
        samesite="lax",
        path="/",
    )
    logger.debug("设置访问 Cookie 完成", secure=settings.env == "prod")


def _unauthorized(detail: str) -> JSONResponse:
    """构造不会逃逸出中间件的 401 JSON 响应。

    Args:
        detail: 面向调用方的错误说明。

    Returns:
        HTTP 401 JSON 响应。
    """
    logger.debug("构造认证失败响应入口", detail=detail)
    response = JSONResponse({"detail": detail}, status_code=401)
    logger.debug("构造认证失败响应完成", detail=detail)
    return response


# ── Pydantic 模型 ──

class LoginRequest(BaseModel):
    """登录请求。"""
    tenant_code: str = Field(
        ...,
        min_length=1,
        max_length=32,
        pattern=r"^[A-Za-z0-9][A-Za-z0-9-]{0,31}$",
    )
    username: str = Field(..., min_length=1, max_length=64)
    password: str = Field(..., min_length=1, max_length=72)


class RegisterRequest(BaseModel):
    """注册请求。"""
    username: str = Field(..., min_length=1, max_length=64)
    password: str = Field(..., min_length=8, max_length=72)
    tenant_name: str = Field(default="default")


# 方法作用：按客户端地址限制公开注册请求，防止批量注册消耗数据库和 bcrypt CPU。
# Args: client_key - 反向代理解析后的客户端地址；limit - 时间窗口内最大注册数。
# Returns: 未超过限制返回 True，否则返回 False。
def _check_registration_rate_limit(client_key: str, limit: int) -> bool:
    logger.debug("注册限流检查入口", client_key=client_key, limit=limit)
    now = time.monotonic()
    window = now - 3600
    with _registration_rate_lock:
        for stale_key in [
            key for key, timestamps in _registration_limits.items()
            if not any(timestamp > window for timestamp in timestamps)
        ]:
            del _registration_limits[stale_key]
        timestamps = [timestamp for timestamp in _registration_limits.get(client_key, []) if timestamp > window]
        if len(timestamps) >= limit:
            _registration_limits[client_key] = timestamps
            logger.warning("注册频率限制触发", client_key=client_key, used=len(timestamps), limit=limit)
            return False
        timestamps.append(now)
        _registration_limits[client_key] = timestamps
    logger.debug("注册限流检查通过", client_key=client_key, used=len(timestamps), limit=limit)
    return True


# 方法作用：按客户端地址、租户编码和大小写敏感用户名限制登录尝试。
# Args: client_key - 客户端地址；tenant_code - 租户编码；username - 登录用户名；limit - 一小时最大尝试数。
# Returns: 未超过限制返回 True，否则返回 False。
def _check_login_rate_limit(
    client_key: str,
    tenant_code: str,
    username: str,
    limit: int,
) -> bool:
    normalized_tenant_code = tenant_code.strip().lower()
    normalized_username = username.strip()
    key = (client_key, normalized_tenant_code, normalized_username)
    now = time.monotonic()
    window = now - 3600
    logger.debug(
        "登录限流检查入口",
        client_key=client_key,
        tenant_code=normalized_tenant_code,
        username=normalized_username,
        limit=limit,
    )
    with _login_rate_lock:
        for stale_key in [
            candidate for candidate, timestamps in _login_limits.items()
            if not any(timestamp > window for timestamp in timestamps)
        ]:
            del _login_limits[stale_key]
        timestamps = [timestamp for timestamp in _login_limits.get(key, []) if timestamp > window]
        if len(timestamps) >= max(1, limit):
            _login_limits[key] = timestamps
            logger.warning(
                "登录频率限制触发",
                client_key=client_key,
                username=normalized_username,
                used=len(timestamps),
                limit=max(1, limit),
            )
            return False
        timestamps.append(now)
        _login_limits[key] = timestamps
    logger.debug(
        "登录限流检查通过",
        client_key=client_key,
        username=normalized_username,
        used=len(timestamps),
        limit=max(1, limit),
    )
    return True


# ── 路由 ──

auth_router = APIRouter(prefix="/auth", tags=["auth"])


@auth_router.post("/login")
async def login(req: LoginRequest, response: Response, request: Request = None):
    """用户登录——验证密码，返回 JWT。

    Args:
        req: LoginRequest

    Returns:
        不含明文令牌的用户身份信息；令牌写入 HttpOnly Cookie。
    """
    client_key = (
        request.client.host
        if request is not None and request.client is not None
        else "unknown"
    )
    settings = get_settings()
    if not _check_login_rate_limit(
        client_key,
        req.tenant_code,
        req.username,
        max(1, int(getattr(settings, "login_max_per_hour", 20))),
    ):
        raise HTTPException(429, "登录尝试过于频繁，请稍后重试")
    tenant_code = req.tenant_code.strip().lower()
    username = req.username.strip()
    logger.debug(
        "登录入口",
        tenant_code=tenant_code,
        username=username,
        client_key=client_key,
    )
    try:
        pool = await get_pg_pool()
        async with pool.acquire() as conn:
            row = await conn.fetchrow(
                "SELECT u.id, u.tenant_id, t.code AS tenant_code, u.role, "
                "u.password_hash, u.is_active, "
                "u.failed_login_attempts, u.locked_until, t.is_active AS tenant_active "
                "FROM users u JOIN tenants t ON t.id=u.tenant_id "
                "WHERE LOWER(t.code)=LOWER($1) AND u.username=$2",
                tenant_code,
                username,
            )
            if not row:
                await asyncio.to_thread(_verify_password, req.password, _DUMMY_PASSWORD_HASH)
                logger.warning("登录失败", tenant_code=tenant_code, username=username, reason="凭证无效")
                raise HTTPException(401, "租户编码、用户名或密码错误")
            if not bool(row.get("is_active", True)):
                logger.warning("登录失败", username=req.username, reason="账号停用")
                raise HTTPException(401, "租户编码、用户名或密码错误")
            if not bool(row.get("tenant_active", True)):
                logger.warning("登录失败", username=req.username, reason="租户停用")
                raise HTTPException(401, "租户编码、用户名或密码错误")
            now = datetime.now(timezone.utc)
            locked_until = row.get("locked_until")
            if locked_until is not None and locked_until > now:
                logger.warning("登录失败", username=req.username, reason="账号锁定")
                raise HTTPException(429, "登录尝试过于频繁，请稍后重试")
            password_valid = await asyncio.to_thread(
                _verify_password,
                req.password,
                str(row["password_hash"]),
            )
            if not password_valid:
                threshold = max(1, int(getattr(settings, "login_lockout_threshold", 5)))
                lock_minutes = max(1, int(getattr(settings, "login_lockout_minutes", 15)))
                updated = await conn.fetchrow(
                    "UPDATE users SET failed_login_attempts=failed_login_attempts+1, "
                    "locked_until=CASE WHEN failed_login_attempts+1 >= $1 "
                    "THEN $2 ELSE locked_until END WHERE id=$3 "
                    "RETURNING failed_login_attempts, locked_until",
                    threshold,
                    now + timedelta(minutes=lock_minutes),
                    row["id"],
                )
                attempts = int(updated["failed_login_attempts"])
                next_locked_until = updated["locked_until"]
                logger.warning(
                    "登录失败",
                    username=req.username,
                    reason="凭证无效",
                    attempts=attempts,
                    locked=next_locked_until is not None,
                )
                raise HTTPException(401, "租户编码、用户名或密码错误")
            await conn.execute(
                "UPDATE users SET failed_login_attempts=0, locked_until=NULL, "
                "last_login_at=NOW() WHERE id=$1",
                row["id"],
            )
        token = create_access_token(
            row["id"],
            row["tenant_id"],
            row["role"],
            str(row["tenant_code"]),
            username,
        )
        _set_access_cookie(response, token)
        logger.info("登录成功", username=req.username, user_id=row["id"])
        return {
            "user_id": row["id"],
            "tenant_id": row["tenant_id"],
            "tenant_code": str(row["tenant_code"]),
            "username": username,
            "role": row["role"],
        }
    except HTTPException:
        raise
    except Exception as exc:
        logger.error("登录异常", error=str(exc), exc_info=True)
        raise HTTPException(500, "登录服务暂不可用") from exc


@auth_router.post("/register")
async def register(req: RegisterRequest, response: Response, request: Request = None):
    """用户注册——创建用户，返回 JWT。

    Args:
        req: RegisterRequest

    Returns:
        不含明文令牌的用户身份信息；令牌写入 HttpOnly Cookie。
    """
    logger.warning("公开注册拒绝", reason="平台策略永久关闭", username=req.username)
    raise HTTPException(403, "公开注册未开启")


@auth_router.post("/logout")
async def logout(response: Response) -> dict:
    """清除访问 Cookie 并结束浏览器会话。

    Args:
        response: FastAPI 响应对象。

    Returns:
        登出成功状态。
    """
    logger.debug("登出入口")
    settings = get_settings()
    response.delete_cookie(
        key=ACCESS_TOKEN_COOKIE,
        path="/",
        httponly=True,
        secure=settings.env == "prod",
        samesite="lax",
    )
    logger.info("登出完成")
    return {"status": "ok"}


@auth_router.get("/me")
async def current_user() -> dict:
    """返回当前 Cookie/Bearer 身份和认证开关。

    Returns:
        当前身份、认证状态和服务端是否强制认证。
    """
    logger.debug("当前身份查询入口")
    user_id = get_current_user_id()
    result = {
        "authenticated": user_id > 0,
        "auth_required": get_tenant_policy().requires_authentication(),
        "registration_enabled": False,
        "user_id": user_id,
        "tenant_id": get_current_tenant_id(),
        "tenant_code": get_current_tenant_code(),
        "username": get_current_username(),
        "role": get_current_role(),
    }
    logger.info("当前身份查询完成", authenticated=result["authenticated"], user_id=user_id)
    return result


# 方法作用：读取访问策略中间件写入的策略，兼容直接单测时按 YAML 基线解析。
# Args: request - 当前 HTTP 请求。
# Returns: 当前方法和路径命中的访问策略。
def _request_access_policy(request: Request):
    from src.security.api_access_policy import AccessPolicy, ApiAccessPolicyManager

    state = request.scope.get("state", {})
    policy = state.get("api_access_policy") if isinstance(state, dict) else None
    if isinstance(policy, AccessPolicy):
        return policy
    settings = get_settings()
    client_ip = request.client.host if request.client is not None else "0.0.0.0"
    return ApiAccessPolicyManager(settings).resolve(
        request.url.path,
        request.method,
        client_ip,
    ).policy


class AuthMiddleware:
    """JWT 认证中间件。

    每个请求进入时：
    1. 公开端点直接放行
    2. 所有部署模式解析 JWT → 注入 ContextVar
    3. multi_tenant 仅影响后续租户隔离
    4. Token 过期/无效 → 401
    """

    # 方法作用：保存下游 ASGI 应用，供每个请求完成认证后调用。
    # Args: app - 下游 ASGI 应用。
    # Returns: 无返回值。
    def __init__(self, app: ASGIApp) -> None:
        """初始化纯 ASGI 认证中间件。"""
        logger.debug("认证中间件初始化入口")
        self.app = app
        logger.info("认证中间件初始化完成")

    # 方法作用：以纯 ASGI 协议认证 HTTP 请求并保持流式响应上下文。
    # Args: scope - ASGI 请求作用域；receive - 接收通道；send - 发送通道。
    # Returns: 无返回值。
    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        """处理 HTTP 请求，非 HTTP 协议直接交给下游应用。"""
        scope_type = scope.get("type", "")
        logger.debug("认证 ASGI 调用入口", scope_type=scope_type, path=scope.get("path", ""))
        if scope_type != "http":
            await self.app(scope, receive, send)
            logger.debug("认证 ASGI 调用完成", scope_type=scope_type, mode="passthrough")
            return

        request = Request(scope, receive=receive)
        identity, denial = self._authenticate_request(request)
        if denial is not None:
            await denial(scope, receive, send)
            logger.debug("认证 ASGI 调用完成", path=request.url.path, mode="denied")
            return
        if identity is None:
            await self.app(scope, receive, send)
            logger.debug("认证 ASGI 调用完成", path=request.url.path, mode="public")
            return

        contexts = self._set_identity(identity)
        try:
            await self.app(scope, receive, send)
            logger.debug("认证 ASGI 调用完成", path=request.url.path, user_id=identity[0])
        except Exception as exc:
            logger.error(
                "认证后 ASGI 请求异常",
                path=request.url.path,
                error=str(exc),
                exc_info=True,
            )
            raise
        finally:
            self._reset_identity(contexts, request.url.path)

    # 方法作用：兼容既有单元测试直接调用，并复用纯 ASGI 的认证决策。
    # Args: request - FastAPI Request；call_next - 测试或兼容调用链。
    # Returns: 下游响应或认证失败响应。
    async def dispatch(self, request: Request, call_next):
        """执行与生产 ASGI 路径一致的认证和上下文清理。

        Args:
            request: FastAPI Request
            call_next: 下一个中间件/路由

        Returns: Response
        """
        logger.debug("认证兼容调用入口", path=request.url.path, method=request.method)
        identity, denial = self._authenticate_request(request)
        if denial is not None:
            logger.debug("认证兼容调用完成", path=request.url.path, mode="denied")
            return denial
        if identity is None:
            response = await call_next(request)
            logger.debug("认证兼容调用完成", path=request.url.path, mode="public")
            return response

        contexts = self._set_identity(identity)
        try:
            response = await call_next(request)
            logger.debug("认证兼容调用完成", path=request.url.path, user_id=identity[0])
            return response
        except Exception as exc:
            logger.error("认证后请求异常", path=request.url.path, error=str(exc), exc_info=True)
            raise
        finally:
            self._reset_identity(contexts, request.url.path)

    # 方法作用：验证管理 Key 和 JWT，并返回身份或拒绝响应。
    # Args: request - 当前 HTTP 请求。
    # Returns: 公开请求返回空身份；拒绝时返回 401；成功时返回身份元组。
    def _authenticate_request(
        self,
        request: Request,
    ) -> tuple[AuthIdentity | None, JSONResponse | None]:
        """执行不消费请求体的同步认证决策。"""
        from src.security.api_access_policy import AuthMode

        logger.debug("认证决策入口", path=request.url.path, method=request.method)
        access_policy = _request_access_policy(request)
        auth_mode = access_policy.auth_mode
        if auth_mode is AuthMode.PUBLIC:
            logger.debug("认证决策完成", path=request.url.path, mode="public")
            return None, None

        settings = get_settings()
        policy = get_tenant_policy()
        # 平台管理 Key 仅作为无浏览器 Cookie 的自动化身份入口。
        admin_api_key = getattr(settings, "admin_api_key", "")
        authorization = request.headers.get("Authorization", "")
        bearer_token = authorization[7:].strip() if authorization.startswith("Bearer ") else ""
        cookie_token = request.cookies.get(ACCESS_TOKEN_COOKIE, "")
        token = bearer_token or cookie_token
        if admin_api_key and auth_mode is AuthMode.JWT_OR_ADMIN_KEY and not token:
            import hmac

            if not hmac.compare_digest(request.headers.get("X-Admin-Key", ""), admin_api_key):
                logger.warning("管理端点认证失败", path=request.url.path)
                return None, _unauthorized("管理端点需要 X-Admin-Key")
            logger.info("管理端点 API Key 认证完成", path=request.url.path)
            return (
                SUPER_ADMIN_USER_ID,
                DEFAULT_TENANT_ID,
                "super_admin",
                "default",
                "super_admin",
            ), None
        logger.debug(
            "认证令牌来源已选择",
            source="bearer" if bearer_token else "cookie" if cookie_token else "missing",
        )
        if not token:
            if auth_mode is AuthMode.OPTIONAL:
                logger.debug("可选认证匿名放行", path=request.url.path)
                return (
                    ANONYMOUS_USER_ID,
                    DEFAULT_TENANT_ID,
                    ANONYMOUS_ROLE,
                    "default",
                    "",
                ), None
            logger.warning("认证令牌缺失", path=request.url.path, auth_mode=auth_mode.value)
            return None, _unauthorized("未提供认证令牌")

        identity: AuthIdentity = (
            ANONYMOUS_USER_ID,
            DEFAULT_TENANT_ID,
            ANONYMOUS_ROLE,
            "default",
            "",
        )
        try:
            if token:
                payload = jwt.decode(token, _secret(), algorithms=["HS256"])
                identity = (
                    int(payload["user_id"]),
                    int(payload["tenant_id"]),
                    str(payload["role"]).strip().lower(),
                    str(payload.get("tenant_code", "default")).strip().lower(),
                    str(payload.get("username", "")),
                )
        except jwt.ExpiredSignatureError:
            logger.info("JWT 已过期")
            return None, _unauthorized("令牌已过期")
        except (jwt.PyJWTError, KeyError, TypeError, ValueError) as exc:
            logger.warning("JWT 无效", error=str(exc))
            return None, _unauthorized("令牌无效")

        try:
            policy.validate_identity(
                RequestIdentity(
                    user_id=identity[0],
                    tenant_id=identity[1],
                    role=identity[2],
                ),
            )
        except PermissionError as exc:
            logger.warning("JWT 身份不满足租户策略", error=str(exc))
            return None, _unauthorized("令牌身份无效")

        if auth_mode is AuthMode.SUPER_ADMIN and not (
            identity[0] == SUPER_ADMIN_USER_ID and identity[2] == "super_admin"
        ):
            logger.warning(
                "接口要求固定超级管理员",
                path=request.url.path,
                user_id=identity[0],
                role=identity[2],
            )
            return None, _unauthorized("接口需要平台超级管理员")

        logger.debug(
            "认证决策完成",
            path=request.url.path,
            user_id=identity[0],
            tenant_id=identity[1],
            role=identity[2],
        )
        return identity, None

    # 方法作用：把认证身份写入当前协程 ContextVar。
    # Args: identity - user_id、tenant_id、role、tenant_code、username 身份元组。
    # Returns: 用于后续精确 reset 的五个 ContextVar Token。
    def _set_identity(
        self,
        identity: AuthIdentity,
    ) -> tuple[Token[int], Token[int], Token[str], Token[str], Token[str]]:
        """注入请求身份并返回上下文令牌。"""
        logger.debug("请求身份注入入口", user_id=identity[0], tenant_id=identity[1])
        user_context = _current_user_id.set(identity[0])
        tenant_context = _current_tenant_id.set(identity[1])
        role_context = _current_role.set(identity[2])
        tenant_code_context = _current_tenant_code.set(identity[3])
        username_context = _current_username.set(identity[4])
        logger.debug("请求身份已注入", user_id=identity[0], tenant_id=identity[1], role=identity[2])
        return (
            user_context,
            tenant_context,
            role_context,
            tenant_code_context,
            username_context,
        )

    # 方法作用：使用请求进入时的 Token 精确恢复五个身份 ContextVar。
    # Args: contexts - 五个 ContextVar Token；path - 当前请求路径。
    # Returns: 无返回值。
    def _reset_identity(
        self,
        contexts: tuple[Token[int], Token[int], Token[str], Token[str], Token[str]],
        path: str,
    ) -> None:
        """清理请求身份，防止连接复用或并发请求间污染。"""
        logger.debug("请求身份清理入口", path=path)
        (
            user_context,
            tenant_context,
            role_context,
            tenant_code_context,
            username_context,
        ) = contexts
        _current_user_id.reset(user_context)
        _current_tenant_id.reset(tenant_context)
        _current_role.reset(role_context)
        _current_tenant_code.reset(tenant_code_context)
        _current_username.reset(username_context)
        logger.debug("请求身份已清理", path=path)
