"""PG 连接池 — 统一 asyncpg 连接管理。"""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

import asyncpg

from src.config import get_settings
from src.db.utils import to_asyncpg_url
from src.logging_config import get_logger

logger = get_logger(__name__)
_pool: asyncpg.Pool | None = None


# 方法作用：获取并健康检查全局 PostgreSQL 连接池，失效时自动重建。
# Args: 无。
# Returns: 可供应用共享的 asyncpg 连接池。
async def get_pg_pool() -> asyncpg.Pool:
    """获取全局连接池（单例，min=2 max=10, timeout=30s）。"""
    global _pool
    logger.debug("get_pg_pool 入口", has_pool=_pool is not None)
    if _pool:
        try:
            async with _pool.acquire() as c:
                await c.fetchval("SELECT 1")
            logger.info("get_pg_pool 完成", reused=True)
            return _pool
        except Exception:
            logger.warning("PG 池失效，重建", exc_info=True)
            _pool = None
    try:
        settings = get_settings()
        url = to_asyncpg_url(settings.database_url)
        _pool = await asyncpg.create_pool(
            url,
            min_size=2,
            max_size=10,
            command_timeout=30,
        )
        logger.info("get_pg_pool 完成", reused=False)
        return _pool
    except Exception:
        logger.error("get_pg_pool 失败", exc_info=True)
        raise


# 方法作用：关闭全局 PostgreSQL 连接池并清除单例引用。
# Args: 无。
# Returns: 无返回值。
async def close_pg_pool() -> None:
    global _pool
    logger.debug("close_pg_pool 入口", has_pool=_pool is not None)
    try:
        if _pool:
            await _pool.close()
            _pool = None
        logger.info("close_pg_pool 完成")
    except Exception:
        logger.error("close_pg_pool 失败", exc_info=True)
        raise


# 方法作用：在事务内借用连接并设置请求身份，确保连接归还时身份自动清除。
# Args: tenant_id - 当前租户编号；user_id - 当前用户编号；role - 当前用户角色。
# Returns: 事务范围内可用的 asyncpg 连接。
@asynccontextmanager
async def pg_connection(
    tenant_id: int,
    user_id: int,
    role: str,
) -> AsyncIterator[asyncpg.Connection]:
    logger.debug(
        "pg_connection 入口",
        tenant_id=tenant_id,
        user_id=user_id,
        role=role,
    )
    try:
        pool = await get_pg_pool()
        async with pool.acquire() as connection:
            async with connection.transaction():
                await connection.execute(
                    "SELECT set_config('app.current_tenant_id', $1, true), "
                    "set_config('app.current_user_id', $2, true), "
                    "set_config('app.current_role', $3, true)",
                    str(tenant_id),
                    str(user_id),
                    role,
                )
                yield connection
        logger.info(
            "pg_connection 完成",
            tenant_id=tenant_id,
            user_id=user_id,
        )
    except Exception:
        logger.error(
            "pg_connection 失败",
            tenant_id=tenant_id,
            user_id=user_id,
            exc_info=True,
        )
        raise
