"""出站数据库地址策略与 ClickHouse SSRF 回归测试。"""

from __future__ import annotations

import logging
import socket
from unittest.mock import MagicMock

import pytest

from src.datasource.config import DataSourceConfig


logger = logging.getLogger(__name__)


class TestOutboundHostPolicy:
    """覆盖私网、回环地址拒绝和部署方 allowlist 放行。"""

    # 方法作用：验证未授权私网地址在 TCP 连接前被拒绝。
    # Args: self - pytest 测试类实例；monkeypatch - pytest 补丁工具。
    # Returns: 无返回值，断言失败时由 pytest 报告。
    async def test_clickhouse_private_address_is_rejected_before_connect(
        self,
        monkeypatch,
    ) -> None:
        """攻击者提供回环地址时不得触发 socket.create_connection。"""
        logger.debug("test_clickhouse_private_address_is_rejected_before_connect 入口")
        try:
            # Arrange
            from src.connectors.clickhouse import ClickHouseConnector
            import src.connectors.clickhouse as clickhouse_module
            import clickhouse_connect

            connector = ClickHouseConnector(DataSourceConfig(
                name="blocked",
                dialect="clickhouse",
                mode="external",
                host="metadata.internal",
                port=8123,
            ))
            connect = MagicMock()
            monkeypatch.setattr(
                socket,
                "getaddrinfo",
                lambda *args, **kwargs: [
                    (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("127.0.0.1", 8123)),
                ],
            )
            monkeypatch.setattr(socket, "create_connection", connect)
            monkeypatch.setattr(clickhouse_connect, "get_client", MagicMock(return_value=MagicMock()))
            monkeypatch.setattr(
                clickhouse_module,
                "get_settings",
                lambda: type("Settings", (), {"datasource_host_allowlist": ""})(),
            )

            # Act / Assert
            with pytest.raises(PermissionError, match="出站地址"):
                await connector.create_engine()
            connect.assert_not_called()
            logger.info("test_clickhouse_private_address_is_rejected_before_connect 完成")
        except Exception as exc:
            logger.error(
                "test_clickhouse_private_address_is_rejected_before_connect 异常: %s",
                exc,
                exc_info=True,
            )
            raise

    # 方法作用：验证部署方可以显式允许可信私网 ClickHouse 主机。
    # Args: self - pytest 测试类实例；monkeypatch - pytest 补丁工具。
    # Returns: 无返回值，断言失败时由 pytest 报告。
    async def test_clickhouse_allowlisted_private_host_is_permitted(
        self,
        monkeypatch,
    ) -> None:
        """精确主机 allowlist 应允许受管内网数据库连接。"""
        logger.debug("test_clickhouse_allowlisted_private_host_is_permitted 入口")
        try:
            # Arrange
            from src.connectors.clickhouse import ClickHouseConnector
            import src.connectors.clickhouse as clickhouse_module
            import clickhouse_connect

            connector = ClickHouseConnector(DataSourceConfig(
                name="trusted",
                dialect="clickhouse",
                mode="external",
                host="clickhouse.internal",
                port=8123,
            ))
            connection = MagicMock()
            connection.__enter__.return_value = connection
            connection.__exit__.return_value = False
            client = MagicMock()
            monkeypatch.setattr(
                socket,
                "getaddrinfo",
                lambda *args, **kwargs: [
                    (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("10.0.0.8", 8123)),
                ],
            )
            monkeypatch.setattr(socket, "create_connection", MagicMock(return_value=connection))
            monkeypatch.setattr(clickhouse_connect, "get_client", MagicMock(return_value=client))
            monkeypatch.setattr(
                clickhouse_module,
                "get_settings",
                lambda: type(
                    "Settings",
                    (),
                    {"datasource_host_allowlist": "clickhouse.internal"},
                )(),
            )

            # Act
            result = await connector.create_engine()

            # Assert
            assert result.client is client
            logger.info("test_clickhouse_allowlisted_private_host_is_permitted 完成")
        except Exception as exc:
            logger.error(
                "test_clickhouse_allowlisted_private_host_is_permitted 异常: %s",
                exc,
                exc_info=True,
            )
            raise
