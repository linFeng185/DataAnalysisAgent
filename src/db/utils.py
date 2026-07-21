"""PostgreSQL 数据库连接工具。"""

from __future__ import annotations

from urllib.parse import urlsplit, urlunsplit

from src.logging_config import get_logger


logger = get_logger(__name__)


# 方法作用：把 SQLAlchemy PostgreSQL URL 规范化为 asyncpg 可接受的 DSN。
# Args: database_url - PostgreSQL 数据库连接地址。
# Returns: 保留凭证、主机、路径和查询参数的 asyncpg DSN。
def to_asyncpg_url(database_url: str) -> str:
    logger.debug("to_asyncpg_url 入口")
    try:
        parsed = urlsplit(database_url)
        if parsed.scheme not in {"postgresql", "postgresql+asyncpg"}:
            raise ValueError("仅支持 PostgreSQL 数据库 URL")
        if not parsed.netloc:
            raise ValueError("PostgreSQL 数据库 URL 缺少连接地址")

        result = urlunsplit(
            ("postgresql", parsed.netloc, parsed.path, parsed.query, parsed.fragment),
        )
        logger.info("to_asyncpg_url 完成", scheme=parsed.scheme)
        return result
    except Exception:
        logger.error("to_asyncpg_url 失败", exc_info=True)
        raise
