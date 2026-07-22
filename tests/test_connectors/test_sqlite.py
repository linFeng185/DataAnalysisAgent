"""SQLite 连接器直接单元测试。"""

from __future__ import annotations

import logging
from types import SimpleNamespace
from unittest.mock import MagicMock

from src.datasource.config import DataSourceConfig


logger = logging.getLogger(__name__)


class TestSQLiteConnector:
    """覆盖 SQLite URL、超时和异步 Engine 创建。"""

    # 方法作用：验证 SQLite 使用 db_path 并禁用数据库超时语句。
    # Args: self - pytest 测试类实例。
    # Returns: 无返回值，断言失败时由 pytest 报告。
    def test_build_url_prefers_explicit_db_path(self) -> None:
        """显式 db_path 必须覆盖 database 字段且不生成远程连接 URL。"""
        logger.debug("test_build_url_prefers_explicit_db_path 入口")
        try:
            # Arrange
            from src.connectors.sqlite import SQLiteConnector

            connector = SQLiteConnector(DataSourceConfig(
                name="sqlite-test",
                dialect="sqlite",
                mode="embedded",
                database="ignored.db",
                extra_params={"db_path": "data/local.db"},
            ))

            # Act / Assert
            assert connector._build_url() == "sqlite+aiosqlite:///data/local.db"  # noqa: SLF001
            assert connector._get_timeout() is None  # noqa: SLF001
            logger.info("test_build_url_prefers_explicit_db_path 完成")
        except Exception as exc:
            logger.error(
                "test_build_url_prefers_explicit_db_path 异常: %s",
                exc,
                exc_info=True,
            )
            raise

    # 方法作用：验证 SQLite Engine 创建使用 StaticPool 并返回缓存实例。
    # Args: self - pytest 测试类实例；monkeypatch - pytest 补丁工具。
    # Returns: 无返回值，断言失败时由 pytest 报告。
    async def test_create_engine_uses_static_pool(self, monkeypatch) -> None:
        """内存与本地 SQLite 应使用 StaticPool，避免误用网络连接池参数。"""
        logger.debug("test_create_engine_uses_static_pool 入口")
        try:
            # Arrange
            import src.config as config_module
            import src.connectors.sqlite as sqlite_module

            engine = object()
            create_engine = MagicMock(return_value=engine)
            monkeypatch.setattr(sqlite_module, "create_async_engine", create_engine)
            monkeypatch.setattr(
                config_module,
                "get_settings",
                lambda: SimpleNamespace(env="test"),
            )
            connector = sqlite_module.SQLiteConnector(DataSourceConfig(
                name="sqlite-test",
                dialect="sqlite",
                mode="embedded",
                database=":memory:",
            ))

            # Act
            result = await connector.create_engine()

            # Assert
            assert result is engine
            assert connector.engine is engine
            assert create_engine.call_args.kwargs["echo"] is False
            logger.info("test_create_engine_uses_static_pool 完成")
        except Exception as exc:
            logger.error(
                "test_create_engine_uses_static_pool 异常: %s",
                exc,
                exc_info=True,
            )
            raise
