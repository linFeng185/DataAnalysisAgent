"""页面数据源配置持久化测试，覆盖功能 21.3.4。"""

from __future__ import annotations

import logging
from unittest.mock import AsyncMock, MagicMock


logger = logging.getLogger(__name__)


# 方法作用：构造支持 acquire/transaction 的异步数据库替身。
# Args: 无。
# Returns: pool、connection 二元组。
def _fake_pool():
    logger.debug("构造数据源持久化测试池入口")
    connection = AsyncMock()
    transaction = MagicMock()
    transaction.__aenter__ = AsyncMock(return_value=None)
    transaction.__aexit__ = AsyncMock(return_value=False)
    connection.transaction = MagicMock(return_value=transaction)
    acquire = MagicMock()
    acquire.__aenter__ = AsyncMock(return_value=connection)
    acquire.__aexit__ = AsyncMock(return_value=False)
    pool = MagicMock()
    pool.acquire.return_value = acquire
    logger.info("构造数据源持久化测试池完成")
    return pool, connection


class TestDatasourcePersistence:
    """验证加密保存、启动恢复和租户范围删除。"""

    # 方法作用：验证持久化 SQL 只接收加密凭证并同步创建授权记录。
    # Args: self - pytest 测试类实例；monkeypatch - PG 池补丁。
    # Returns: 无返回值，断言失败时由 pytest 报告。
    async def test_persist_stores_encrypted_password(self, monkeypatch) -> None:
        """页面密码不得以明文进入状态数据库。"""
        logger.debug("test_persist_stores_encrypted_password 入口")
        import src.memory.pg_pool as pg_pool
        from src.datasource.config import DataSourceConfig
        from src.datasource.providers.external import ExternalDataSourceProvider

        pool, connection = _fake_pool()
        monkeypatch.setattr(pg_pool, "get_pg_pool", AsyncMock(return_value=pool))
        provider = ExternalDataSourceProvider()
        datasource = DataSourceConfig(
            name="managed", dialect="postgres", mode="external",
            password="v2:salt:ciphertext",
        )

        await provider.persist(datasource, tenant_id=2, owner_user_id=9)

        calls = connection.execute.await_args_list
        assert len(calls) == 2
        assert "v2:salt:ciphertext" in calls[0].args
        assert "plaintext-password" not in str(calls)
        assert "datasource_permissions" in calls[1].args[0]
        logger.info("test_persist_stores_encrypted_password 完成")

    # 方法作用：验证启动时从状态数据库恢复所有连接字段和所属身份。
    # Args: self - pytest 测试类实例；monkeypatch - PG 池补丁。
    # Returns: 无返回值，断言失败时由 pytest 报告。
    async def test_load_persisted_restores_datasource(self, monkeypatch) -> None:
        """服务重启后页面数据源仍应出现在 Provider。"""
        logger.debug("test_load_persisted_restores_datasource 入口")
        import src.memory.pg_pool as pg_pool
        from src.datasource.providers.external import ExternalDataSourceProvider

        pool, connection = _fake_pool()
        connection.fetch.return_value = [{
            "name": "managed", "tenant_id": 2, "owner_user_id": 9,
            "dialect": "postgres", "version": "16", "host": "db.internal",
            "port": 5432, "database_name": "analytics", "username": "reader",
            "encrypted_password": "v2:salt:ciphertext", "description": "生产只读库",
            "extra_params": {"ssl": True},
        }]
        monkeypatch.setattr(pg_pool, "get_pg_pool", AsyncMock(return_value=pool))
        provider = ExternalDataSourceProvider()

        count = await provider.load_persisted()
        datasource = await provider.lookup("managed")

        assert count == 1
        assert datasource is not None
        assert datasource.tenant_id == 2
        assert datasource.owner_user_id == 9
        assert datasource.password == "v2:salt:ciphertext"
        logger.info("test_load_persisted_restores_datasource 完成")

    # 方法作用：验证删除持久化配置时同时删除对应租户权限。
    # Args: self - pytest 测试类实例；monkeypatch - PG 池补丁。
    # Returns: 无返回值，断言失败时由 pytest 报告。
    async def test_delete_persisted_removes_permission(self, monkeypatch) -> None:
        """删除页面数据源不能残留可误命中的授权记录。"""
        logger.debug("test_delete_persisted_removes_permission 入口")
        import src.memory.pg_pool as pg_pool
        from src.datasource.providers.external import ExternalDataSourceProvider

        pool, connection = _fake_pool()
        connection.fetchrow.return_value = {"tenant_id": 2}
        monkeypatch.setattr(pg_pool, "get_pg_pool", AsyncMock(return_value=pool))
        provider = ExternalDataSourceProvider()

        removed = await provider.delete_persisted(
            "managed", tenant_id=2, platform_admin=False,
        )

        assert removed is True
        assert "datasource_permissions" in connection.execute.await_args.args[0]
        logger.info("test_delete_persisted_removes_permission 完成")
