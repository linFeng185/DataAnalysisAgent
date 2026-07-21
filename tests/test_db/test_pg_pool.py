"""共享 PostgreSQL 连接池和租户身份边界测试。"""

from __future__ import annotations

import logging
from unittest.mock import AsyncMock, MagicMock

import pytest


logger = logging.getLogger(__name__)


class TestPGPool:
    """覆盖共享连接池事务身份隔离和 URL 归一化。"""

    # 方法作用：验证连接身份使用事务局部 set_config，避免池连接串租户。
    # Args: self - pytest 测试类实例；monkeypatch - pytest 补丁工具。
    # Returns: 无返回值，断言失败时由 pytest 报告。
    @pytest.mark.asyncio
    async def test_connection_identity_is_transaction_local(self, monkeypatch) -> None:
        """池化连接归还后不能保留上一请求的租户身份。"""
        logger.debug("test_connection_identity_is_transaction_local 入口")
        from src.memory import pg_pool as pool_module

        connection = AsyncMock()
        transaction = MagicMock()
        transaction.__aenter__ = AsyncMock(return_value=None)
        transaction.__aexit__ = AsyncMock(return_value=None)
        connection.transaction = MagicMock(return_value=transaction)
        acquire = MagicMock()
        acquire.__aenter__ = AsyncMock(return_value=connection)
        acquire.__aexit__ = AsyncMock(return_value=None)
        pool = MagicMock()
        pool.acquire.return_value = acquire
        monkeypatch.setattr(pool_module, "get_pg_pool", AsyncMock(return_value=pool))

        async with pool_module.pg_connection(
            tenant_id=7,
            user_id=11,
            role="analyst",
        ) as acquired:
            assert acquired is connection

        connection.execute.assert_awaited_once_with(
            "SELECT set_config('app.current_tenant_id', $1, true), "
            "set_config('app.current_user_id', $2, true), "
            "set_config('app.current_role', $3, true)",
            "7",
            "11",
            "analyst",
        )
        transaction.__aexit__.assert_awaited_once()
        logger.info("test_connection_identity_is_transaction_local 完成")

    # 方法作用：验证 SQLAlchemy asyncpg URL 被稳定转换为 asyncpg DSN。
    # Args: self - pytest 测试类实例。
    # Returns: 无返回值，断言失败时由 pytest 报告。
    def test_to_asyncpg_url_preserves_connection_parts(self) -> None:
        """URL 工具应保留凭证、端口、数据库和查询参数。"""
        logger.debug("test_to_asyncpg_url_preserves_connection_parts 入口")
        from src.db.utils import to_asyncpg_url

        result = to_asyncpg_url(
            "postgresql+asyncpg://user:p%40ss@localhost:5432/app?ssl=require",
        )

        assert result == "postgresql://user:p%40ss@localhost:5432/app?ssl=require"
        logger.info("test_to_asyncpg_url_preserves_connection_parts 完成")

    # 方法作用：验证非 PostgreSQL URL 会被明确拒绝。
    # Args: self - pytest 测试类实例。
    # Returns: 无返回值，断言失败时由 pytest 报告。
    def test_to_asyncpg_url_rejects_other_dialects(self) -> None:
        """异常输入不能通过字符串替换伪装成 PostgreSQL。"""
        logger.debug("test_to_asyncpg_url_rejects_other_dialects 入口")
        from src.db.utils import to_asyncpg_url

        with pytest.raises(ValueError, match="PostgreSQL"):
            to_asyncpg_url("mysql+aiomysql://user:pass@localhost/app")
        logger.info("test_to_asyncpg_url_rejects_other_dialects 完成")
