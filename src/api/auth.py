"""JWT 认证模块：中间件 + ContextVar + 登录/注册端点。

架构:
  AuthMiddleware → 解析 JWT → 写入 ContextVar
  业务代码 → get_current_user_id() / get_current_tenant_id() → 获取身份
  PG RLS → current_setting('app.current_tenant_id') → 数据库级强制隔离

多租户开关:
  multi_tenant=false（默认）→ 不强制登录，所有请求 tenant_id=1，行为不变
  multi_tenant=true → 必须登录，JWT 校验，RLS 生效
"""

from __future__ import annotations

import os
from contextvars import ContextVar
from datetime import datetime, timedelta, timezone

import jwt
from fastapi import APIRouter, HTTPException, Request, Response
from pydantic import BaseModel, Field
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.responses import JSONResponse

from src.config import get_settings
from src.logging_config import get_logger

logger = get_logger(__name__)

# ── ContextVar: 协程级用户上下文 ──

_current_user_id: ContextVar[int] = ContextVar("current_user_id", default=0)
_current_tenant_id: ContextVar[int] = ContextVar("current_tenant_id", default=1)
_current_role: ContextVar[str] = ContextVar("current_role", default="anonymous")
ACCESS_TOKEN_COOKIE = "access_token"


def get_current_user_id() -> int:
    """获取当前请求的用户 ID。

    由 AuthMiddleware 在请求进入时设置。
    单租户模式下返回 0（anonymous）。

    Returns: 用户 ID
    """
    return _current_user_id.get()


def get_current_tenant_id() -> int:
    """获取当前请求的租户 ID。

    单租户模式下返回 1（default 租户）。

    Returns: 租户 ID
    """
    return _current_tenant_id.get()


def get_current_role() -> str:
    """获取当前请求的用户角色。

    Returns:
        当前角色；匿名请求返回 anonymous。
    """
    return _current_role.get()


# 要求当前身份具备平台超级管理员权限。
# Args: 无。
# Returns: 授权成功时无返回值，否则抛出 HTTP 403。
def require_super_admin() -> None:
    logger.debug("校验超级管理员权限入口", role=get_current_role())
    from src.knowledge.governance import is_super_admin

    role = get_current_role()
    if not is_super_admin(role):
        logger.warning("校验超级管理员权限拒绝", role=role)
        raise HTTPException(403, "需要超级管理员权限")
    logger.info("校验超级管理员权限完成", role=role)


# 要求当前身份具备租户管理权限或更高的平台权限。
# Args: 无。
# Returns: 授权成功时无返回值，否则抛出 HTTP 403。
def require_tenant_admin() -> None:
    logger.debug("校验租户管理员权限入口", role=get_current_role())
    from src.knowledge.governance import is_super_admin, is_tenant_admin

    role = get_current_role()
    if not (is_super_admin(role) or is_tenant_admin(role)):
        logger.warning("校验租户管理员权限拒绝", role=role)
        raise HTTPException(403, "需要租户管理员权限")
    logger.info("校验租户管理员权限完成", role=role)


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
    logger.info("会话线程命名空间完成", scoped_session=scoped[-40:])
    return scoped


# ── JWT ──

_secret_cache: str | None = None


def _secret() -> str:
    """获取 JWT 签名密钥。优先环境变量 JWT_SECRET，回退 config。

    未配置时自动生成临时密钥（仅开发模式），打印生产警告。
    """
    global _secret_cache
    if _secret_cache is not None:
        return _secret_cache

    env_key = os.getenv("JWT_SECRET", "")
    cfg_key = get_settings().jwt_secret
    key = env_key or cfg_key

    if not key:
        import secrets
        key = secrets.token_hex(32)
        logger.warning("JWT_SECRET 未配置！已生成临时密钥（服务重启后所有 Token 失效）。生产环境必须设置 JWT_SECRET。")

    if key == "dev-secret-change-in-production" or len(key) < 16:
        logger.warning("JWT_SECRET 强度不足！生产环境请使用至少 32 字节的随机密钥。")

    _secret_cache = key
    return key


def create_access_token(user_id: int, tenant_id: int, role: str) -> str:
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
        "exp": datetime.now(timezone.utc) + timedelta(hours=s.jwt_access_token_expire_hours),
    }, _secret(), algorithm="HS256")
    logger.info("创建访问令牌完成", user_id=user_id, tenant_id=tenant_id)
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
    logger.info("设置访问 Cookie 完成", secure=settings.env == "prod")


def _unauthorized(detail: str) -> JSONResponse:
    """构造不会逃逸出中间件的 401 JSON 响应。

    Args:
        detail: 面向调用方的错误说明。

    Returns:
        HTTP 401 JSON 响应。
    """
    logger.debug("构造认证失败响应入口", detail=detail)
    response = JSONResponse({"detail": detail}, status_code=401)
    logger.info("构造认证失败响应完成", detail=detail)
    return response


# ── Pydantic 模型 ──

class LoginRequest(BaseModel):
    """登录请求。"""
    username: str = Field(..., min_length=1)
    password: str = Field(..., min_length=1)


class RegisterRequest(BaseModel):
    """注册请求。"""
    username: str = Field(..., min_length=1, max_length=64)
    password: str = Field(..., min_length=6)
    tenant_name: str = Field(default="default")


# ── 路由 ──

auth_router = APIRouter(prefix="/auth", tags=["auth"])


@auth_router.post("/login")
async def login(req: LoginRequest, response: Response):
    """用户登录——验证密码，返回 JWT。

    Args:
        req: LoginRequest

    Returns:
        不含明文令牌的用户身份信息；令牌写入 HttpOnly Cookie。
    """
    logger.debug("登录入口", username=req.username)
    conn = None
    try:
        import asyncpg
        s = get_settings()
        url = s.database_url.replace("postgresql+asyncpg://", "postgresql://")
        conn = await asyncpg.connect(url)
        row = await conn.fetchrow(
            "SELECT id, tenant_id, role, password_hash FROM users WHERE username=$1", req.username)
        if not row:
            logger.warning("登录失败：用户不存在", username=req.username)
            raise HTTPException(401, "用户名或密码错误")
        from passlib.hash import bcrypt
        if not bcrypt.verify(req.password, row["password_hash"]):
            logger.warning("登录失败：密码错误", username=req.username)
            raise HTTPException(401, "用户名或密码错误")
        token = create_access_token(row["id"], row["tenant_id"], row["role"])
        _set_access_cookie(response, token)
        logger.info("登录成功", username=req.username, user_id=row["id"])
        return {"user_id": row["id"], "tenant_id": row["tenant_id"], "role": row["role"]}
    except HTTPException:
        raise
    except Exception as exc:
        logger.error("登录异常", error=str(exc), exc_info=True)
        raise HTTPException(500, "登录服务暂不可用") from exc
    finally:
        if conn is not None:
            await conn.close()


@auth_router.post("/register")
async def register(req: RegisterRequest, response: Response):
    """用户注册——创建用户，返回 JWT。

    Args:
        req: RegisterRequest

    Returns:
        不含明文令牌的用户身份信息；令牌写入 HttpOnly Cookie。
    """
    logger.debug("注册入口", username=req.username)
    conn = None
    try:
        import asyncpg
        from passlib.hash import bcrypt
        s = get_settings()
        url = s.database_url.replace("postgresql+asyncpg://", "postgresql://")
        conn = await asyncpg.connect(url)
        tid = 1
        if s.multi_tenant:
            tid = await conn.fetchval(
                "INSERT INTO tenants (name) VALUES ($1) RETURNING id", req.tenant_name)
            logger.info("新租户创建", tenant_id=tid, name=req.tenant_name)
        pwd = bcrypt.hash(req.password)
        uid = await conn.fetchval(
            "INSERT INTO users (username, password_hash, role, tenant_id) "
            "VALUES ($1, $2, 'analyst', $3) RETURNING id", req.username, pwd, tid)
        token = create_access_token(uid, tid, "analyst")
        _set_access_cookie(response, token)
        logger.info("注册成功", username=req.username, user_id=uid, tenant_id=tid)
        return {"user_id": uid, "tenant_id": tid, "role": "analyst"}
    except Exception as exc:
        logger.error("注册异常", error=str(exc), exc_info=True)
        raise HTTPException(500, "注册服务暂不可用") from exc
    finally:
        if conn is not None:
            await conn.close()


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
        "auth_required": get_settings().multi_tenant,
        "user_id": user_id,
        "tenant_id": get_current_tenant_id(),
        "role": get_current_role(),
    }
    logger.info("当前身份查询完成", authenticated=result["authenticated"], user_id=user_id)
    return result


# ── 无需认证的公开端点 ──

PUBLIC_PATHS = {
    "/api/v1/health",
    "/api/v1/auth/login",
    "/api/v1/auth/register",
    "/api/v1/auth/logout",
}
_MGMT_METHODS = {"POST", "PUT", "DELETE", "PATCH"}


class AuthMiddleware(BaseHTTPMiddleware):
    """JWT 认证中间件。

    每个请求进入时：
    1. 公开端点直接放行
    2. 单租户模式不强制登录
    3. 多租户模式解析 JWT → 注入 ContextVar
    4. Token 过期/无效 → 401
    """

    async def dispatch(self, request: Request, call_next):
        """处理每个 HTTP 请求。

        Args:
            request: FastAPI Request
            call_next: 下一个中间件/路由

        Returns: Response
        """
        logger.debug("认证中间件入口", path=request.url.path, method=request.method)
        if request.url.path in PUBLIC_PATHS:
            response = await call_next(request)
            logger.info("认证中间件完成", path=request.url.path, mode="public")
            return response

        s = get_settings()

        # 管理端点保护 — 在 token 校验之前执行，确保单租户模式也生效
        admin_api_key = getattr(s, "admin_api_key", "")
        if admin_api_key and request.method in _MGMT_METHODS:
            if request.headers.get("X-Admin-Key", "") != admin_api_key:
                logger.warning("管理端点认证失败", path=request.url.path)
                return _unauthorized("管理端点需要 X-Admin-Key")

        authorization = request.headers.get("Authorization", "")
        bearer_token = authorization[7:].strip() if authorization.startswith("Bearer ") else ""
        token = request.cookies.get(ACCESS_TOKEN_COOKIE, "") or bearer_token
        is_auth_probe = request.url.path == "/api/v1/auth/me"

        if not token:
            if s.multi_tenant and not is_auth_probe:
                logger.warning("认证令牌缺失", path=request.url.path)
                return _unauthorized("未提供认证令牌")
            logger.info("认证中间件匿名回退", path=request.url.path, probe=is_auth_probe)

        identity = (0, 1, "anonymous")
        try:
            if token:
                payload = jwt.decode(token, _secret(), algorithms=["HS256"])
                identity = (
                    int(payload["user_id"]),
                    int(payload["tenant_id"]),
                    str(payload["role"]),
                )
        except jwt.ExpiredSignatureError:
            logger.info("JWT 已过期")
            return _unauthorized("令牌已过期")
        except (jwt.PyJWTError, KeyError, TypeError, ValueError) as exc:
            logger.warning("JWT 无效", error=str(exc))
            return _unauthorized("令牌无效")

        user_context = _current_user_id.set(identity[0])
        tenant_context = _current_tenant_id.set(identity[1])
        role_context = _current_role.set(identity[2])
        logger.info("请求身份已注入", user_id=identity[0], tenant_id=identity[1], role=identity[2])
        try:
            response = await call_next(request)
            logger.info("认证中间件完成", path=request.url.path, user_id=identity[0])
            return response
        except Exception as exc:
            logger.error("认证后请求异常", path=request.url.path, error=str(exc), exc_info=True)
            raise
        finally:
            _current_user_id.reset(user_context)
            _current_tenant_id.reset(tenant_context)
            _current_role.reset(role_context)
            logger.info("请求身份已清理", path=request.url.path)
