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
from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, Field
from starlette.middleware.base import BaseHTTPMiddleware

from src.config import get_settings
from src.logging_config import get_logger

logger = get_logger(__name__)

# ── ContextVar: 协程级用户上下文 ──

_current_user_id: ContextVar[int] = ContextVar("current_user_id", default=0)
_current_tenant_id: ContextVar[int] = ContextVar("current_tenant_id", default=1)
_current_role: ContextVar[str] = ContextVar("current_role", default="anonymous")


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


# ── JWT ──

def _secret() -> str:
    """获取 JWT 签名密钥。

    优先读环境变量 JWT_SECRET，回退 config.jwt_secret。

    Returns: 签名密钥字符串
    """
    return os.getenv("JWT_SECRET") or get_settings().jwt_secret


def create_access_token(user_id: int, tenant_id: int, role: str) -> str:
    """创建 JWT access token。

    Args:
        user_id: 用户 ID
        tenant_id: 租户 ID
        role: 角色（admin/analyst/viewer）

    Returns: JWT 字符串
    """
    s = get_settings()
    return jwt.encode({
        "user_id": user_id, "tenant_id": tenant_id, "role": role,
        "exp": datetime.now(timezone.utc) + timedelta(hours=s.jwt_access_token_expire_hours),
    }, _secret(), algorithm="HS256")


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
async def login(req: LoginRequest):
    """用户登录——验证密码，返回 JWT。

    Args:
        req: LoginRequest

    Returns: {"access_token": str, "user_id": int, "tenant_id": int, "role": str}
    """
    logger.info("登录请求", username=req.username)
    try:
        import asyncpg
        s = get_settings()
        url = s.database_url.replace("postgresql+asyncpg://", "postgresql://")
        conn = await asyncpg.connect(url)
        row = await conn.fetchrow(
            "SELECT id, tenant_id, role, password_hash FROM users WHERE username=$1", req.username)
        await conn.close()
        if not row:
            logger.warning("登录失败：用户不存在", username=req.username)
            raise HTTPException(401, "用户名或密码错误")
        from passlib.hash import bcrypt
        if not bcrypt.verify(req.password, row["password_hash"]):
            logger.warning("登录失败：密码错误", username=req.username)
            raise HTTPException(401, "用户名或密码错误")
        token = create_access_token(row["id"], row["tenant_id"], row["role"])
        logger.info("登录成功", username=req.username, user_id=row["id"])
        return {"access_token": token, "user_id": row["id"],
                "tenant_id": row["tenant_id"], "role": row["role"]}
    except HTTPException:
        raise
    except Exception as e:
        logger.error("登录异常", error=str(e))
        raise HTTPException(500, "登录服务暂不可用")


@auth_router.post("/register")
async def register(req: RegisterRequest):
    """用户注册——创建用户，返回 JWT。

    Args:
        req: RegisterRequest

    Returns: {"access_token": str, "user_id": int, "tenant_id": int, "role": "analyst"}
    """
    logger.info("注册请求", username=req.username)
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
        await conn.close()
        token = create_access_token(uid, tid, "analyst")
        logger.info("注册成功", username=req.username, user_id=uid, tenant_id=tid)
        return {"access_token": token, "user_id": uid, "tenant_id": tid, "role": "analyst"}
    except Exception as e:
        logger.error("注册异常", error=str(e))
        raise HTTPException(500, "注册服务暂不可用")


# ── 无需认证的公开端点 ──

PUBLIC_PATHS = {"/api/v1/health", "/api/v1/auth/login", "/api/v1/auth/register"}


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
        # 公开端点放行
        if request.url.path in PUBLIC_PATHS or request.url.path.startswith("/api/v1/auth/"):
            return await call_next(request)

        s = get_settings()
        token = request.headers.get("Authorization", "").replace("Bearer ", "")

        if not token:
            if s.multi_tenant:
                raise HTTPException(401, "未提供认证令牌")
            # 单租户模式：不强制登录
            return await call_next(request)

        try:
            payload = jwt.decode(token, _secret(), algorithms=["HS256"])
            _current_user_id.set(payload["user_id"])
            _current_tenant_id.set(payload["tenant_id"])
            _current_role.set(payload["role"])
            logger.debug("JWT 认证通过", user_id=payload["user_id"], role=payload["role"])
        except jwt.ExpiredSignatureError:
            logger.info("JWT 已过期")
            raise HTTPException(401, "令牌已过期")
        except jwt.PyJWTError as e:
            logger.warning("JWT 无效", error=str(e))
            raise HTTPException(401, "令牌无效")

        return await call_next(request)
