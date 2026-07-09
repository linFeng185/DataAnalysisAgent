"""PG 连接池 — 统一 asyncpg 连接管理。"""

from __future__ import annotations
import asyncpg
from src.config import get_settings
from src.logging_config import get_logger

logger = get_logger(__name__)
_pool: asyncpg.Pool | None = None


async def get_pg_pool() -> asyncpg.Pool:
    """获取全局连接池（单例，min=2 max=10, timeout=30s）。"""
    global _pool
    if _pool:
        try:
            async with _pool.acquire() as c:
                await c.fetchval("SELECT 1")
            return _pool
        except Exception:
            logger.warning("PG 池失效，重建")
            _pool = None
    s = get_settings()
    url = s.database_url.replace("postgresql+asyncpg://", "postgresql://")
    _pool = await asyncpg.create_pool(url, min_size=2, max_size=10, command_timeout=30)
    logger.info("PG 连接池已创建")
    return _pool


async def close_pg_pool():
    global _pool
    if _pool:
        await _pool.close()
        _pool = None
        logger.info("PG 连接池已关闭")
