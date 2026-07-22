"""SQL Server 连接器直接单元测试。"""

from __future__ import annotations

import logging

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
