"""SQL Server 连接器直接单元测试。"""

from __future__ import annotations

import logging
from unittest.mock import AsyncMock

import pytest

from src.datasource.config import DataSourceConfig

logger = logging.getLogger(__name__)


class TestSQLServerConnector:
    """覆盖 SQL Server URL 与 SHOWPLAN 清理路径。"""

    # 方法作用：验证 SHOWPLAN 查询失败时仍执行 OFF 清理。
    # Args: self - pytest 测试类实例。
    # Returns: 无返回值，断言失败时由 pytest 报告。
    def test_showplan_is_disabled_after_query_failure(self) -> None:
        """语义校验异常不得污染连接池中后续 SQL Server 请求。"""
        logger.debug("test_showplan_is_disabled_after_query_failure 入口")
        try:
            # Arrange
            from src.connectors.mssql import SQLServerConnector

            statements: list[str] = []

            class Connection:
                """记录 SHOWPLAN 语句并在目标 SQL 上失败。"""

                # 方法作用：进入模拟 SQL Server 连接上下文。
                # Args: self - 模拟连接实例。
                # Returns: 当前模拟连接。
                def __enter__(self):
                    return self

                # 方法作用：退出模拟连接上下文且不吞异常。
                # Args: self - 模拟连接实例；exc_type/exc/tb - 当前异常信息。
                # Returns: False，要求异常继续传播。
                def __exit__(self, exc_type, exc, tb):
                    return False

                # 方法作用：记录 SQL，并为业务查询制造失败。
                # Args: self - 模拟连接实例；statement - SQLAlchemy 语句。
                # Returns: SET 语句返回 None，业务查询抛出 RuntimeError。
                def execute(self, statement):
                    sql = str(statement)
                    statements.append(sql)
                    if sql == "SELECT broken":
                        raise RuntimeError("invalid query")
                    return None

            class Engine:
                """提供固定模拟连接的最小 Engine。"""

                # 方法作用：返回模拟 SQL Server 连接。
                # Args: self - 模拟 Engine 实例。
                # Returns: 新的 Connection 实例。
                def connect(self):
                    return Connection()

            connector = SQLServerConnector(DataSourceConfig(
                name="mssql-test",
                dialect="mssql",
                mode="external",
            ))
            connector._engine = Engine()  # noqa: SLF001

            # Act / Assert
            with pytest.raises(RuntimeError, match="invalid query"):
                connector._execute_showplan_sync("SELECT broken")  # noqa: SLF001
            assert statements == [
                "SET SHOWPLAN_TEXT ON",
                "SELECT broken",
                "SET SHOWPLAN_TEXT OFF",
            ]
            logger.info("test_showplan_is_disabled_after_query_failure 完成")
        except Exception as exc:
            logger.error(
                "test_showplan_is_disabled_after_query_failure 异常: %s",
                exc,
                exc_info=True,
            )
            raise

    # 方法作用：验证 SHOWPLAN OFF 失败时立即废弃被污染的连接。
    # Args: self - pytest 测试类实例。
    # Returns: 无返回值，断言失败时由 pytest 报告。
    def test_showplan_cleanup_failure_invalidates_connection(self) -> None:
        """无法关闭 SHOWPLAN 的连接不得回收到连接池。"""
        logger.debug("test_showplan_cleanup_failure_invalidates_connection 入口")
        from src.connectors.mssql import SQLServerConnector

        # Arrange
        class Connection:
            """在 SHOWPLAN OFF 时失败并记录 invalidate 调用。"""

            invalidated = False

            # 方法作用：进入模拟连接上下文。
            # Args: self - 当前连接。
            # Returns: 当前连接。
            def __enter__(self):
                return self

            # 方法作用：退出模拟连接上下文。
            # Args: self - 当前连接；exc_type/exc/tb - 异常信息。
            # Returns: False，不吞异常。
            def __exit__(self, exc_type, exc, tb):
                return False

            # 方法作用：在 SHOWPLAN OFF 语句上制造清理失败。
            # Args: self - 当前连接；statement - SQLAlchemy 语句。
            # Returns: 非 OFF 语句返回 None。
            def execute(self, statement):
                if str(statement) == "SET SHOWPLAN_TEXT OFF":
                    raise RuntimeError("cleanup failed")
                return None

            # 方法作用：标记连接已从池中废弃。
            # Args: self - 当前连接。
            # Returns: 无返回值。
            def invalidate(self) -> None:
                self.invalidated = True

        connection = Connection()

        class Engine:
            """返回固定模拟连接。"""

            # 方法作用：返回固定连接。
            # Args: self - 当前 Engine。
            # Returns: 模拟连接。
            def connect(self):
                return connection

        connector = SQLServerConnector(DataSourceConfig(
            name="mssql-test", dialect="mssql", mode="external",
        ))
        connector._engine = Engine()  # noqa: SLF001

        # Act / Assert
        with pytest.raises(RuntimeError, match="cleanup failed"):
            connector._execute_showplan_sync("SELECT 1")  # noqa: SLF001
        assert connection.invalidated is True
        logger.info("test_showplan_cleanup_failure_invalidates_connection 完成")

    # 方法作用：验证同步 SQL Server Engine 在线程池中释放。
    # Args: self - pytest 测试类实例；monkeypatch - pytest 补丁工具。
    # Returns: 无返回值，断言失败时由 pytest 报告。
    async def test_close_disposes_engine_in_thread(self, monkeypatch) -> None:
        """dispose 不能直接阻塞 asyncio 事件循环。"""
        logger.debug("test_close_disposes_engine_in_thread 入口")
        import src.connectors.mssql as mssql_module

        # Arrange
        engine = type("Engine", (), {"dispose": lambda self: None})()
        to_thread = AsyncMock(side_effect=lambda func, *args: func(*args))
        monkeypatch.setattr(mssql_module.asyncio, "to_thread", to_thread)
        connector = mssql_module.SQLServerConnector(DataSourceConfig(
            name="mssql-test", dialect="mssql", mode="external",
        ))
        connector._engine = engine  # noqa: SLF001

        # Act
        await connector.close()

        # Assert
        to_thread.assert_awaited_once_with(engine.dispose)
        assert connector.engine is None
        logger.info("test_close_disposes_engine_in_thread 完成")
