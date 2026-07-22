"""PG 连接池 — 统一 asyncpg 连接管理。"""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from functools import partial

import asyncpg

from src.app_context import get_app_context
from src.db.utils import to_asyncpg_url
from src.logging_config import get_logger

logger = get_logger(__name__)
_PG_POOL_RESOURCE = "pg_pool"


# 方法作用：根据当前应用配置创建 PostgreSQL 连接池。
# Args: database_url - PostgreSQL 数据库连接串。
# Returns: 新建的 asyncpg 连接池。
async def _create_pg_pool(database_url: str) -> asyncpg.Pool:
    logger.debug("创建 PG 池入口")
    try:
        result = await asyncpg.create_pool(
            to_asyncpg_url(database_url),
            min_size=2,
            max_size=10,
            command_timeout=30,
        )
    except Exception:
        logger.error("创建 PG 池失败", exc_info=True)
        raise
    logger.info("创建 PG 池完成")
    return result


# 方法作用：关闭 AppContext 持有的 PostgreSQL 连接池。
# Args: pool - 待关闭的 asyncpg 连接池。
# Returns: 无返回值。
async def _close_pg_pool_resource(pool: asyncpg.Pool) -> None:
    logger.debug("关闭 PG 池资源入口")
    try:
        await pool.close()
    except Exception:
        logger.error("关闭 PG 池资源失败", exc_info=True)
        raise
    logger.info("关闭 PG 池资源完成")


# 方法作用：获取并健康检查当前应用的 PostgreSQL 连接池，失效时自动重建。
# Args: 无。
# Returns: 可供应用共享的 asyncpg 连接池。
async def get_pg_pool() -> asyncpg.Pool:
    """获取当前应用连接池（min=2 max=10, timeout=30s）。"""
    context = get_app_context()
    pool = context.get_resource(_PG_POOL_RESOURCE)
    logger.debug("get_pg_pool 入口", has_pool=pool is not None)
    if pool is not None:
        try:
            async with pool.acquire() as c:
                await c.fetchval("SELECT 1")
            logger.info("get_pg_pool 完成", reused=True)
            return pool
        except Exception:
            logger.warning("PG 池失效，重建", exc_info=True)
            await context.close_resource(_PG_POOL_RESOURCE)
    try:
        result = await context.get_or_create_async(
            _PG_POOL_RESOURCE,
            partial(_create_pg_pool, context.settings.database_url),
            closer=_close_pg_pool_resource,
        )
        logger.info("get_pg_pool 完成", reused=False)
        return result
    except Exception:
        logger.error("get_pg_pool 失败", exc_info=True)
        raise


# 方法作用：关闭当前 AppContext 的 PostgreSQL 连接池并清除资源引用。
# Args: 无。
# Returns: 无返回值。
async def close_pg_pool() -> None:
    context = get_app_context()
    logger.debug(
        "close_pg_pool 入口",
        has_pool=context.get_resource(_PG_POOL_RESOURCE) is not None,
    )
    try:
        closed = await context.close_resource(_PG_POOL_RESOURCE)
        logger.info("close_pg_pool 完成", closed=closed)
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
