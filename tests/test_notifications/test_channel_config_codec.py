"""通知渠道 config_json 的 JSONB 编解码契约。

事故（2026-09-14）：``get_channels`` 用 ``dict(row["config_json"] or {})`` 解析配置，
而本环境的 asyncpg 把 jsonb **按文本解码**（``SELECT '{}'::jsonb`` 实测返回 ``str``），
于是 ``dict('{}')`` 逐字符迭代并抛
``ValueError: dictionary update sequence element #0 has length 1``，
``get_channels`` 整条失败，租户的邮件/飞书/Slack 全部退化成仅站内信。

排查要点（别再走一遍）：库里的数据是**好的**——4 行全是 033 播种的 ``{}``，
``jsonb_typeof`` 实测为 ``object``，``updated_at`` 自播种后未变。不是写入侧双重编码。

契约要点：
1. 读取侧必须自己 ``json.loads``，不能假设驱动一定解成对象；
2. 写入侧必须传 JSON 字符串——传 dict 会直接 ``DataError: expected str, got dict``。
"""

from __future__ import annotations

import json
from contextlib import asynccontextmanager
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock


# 方法作用：构造一个可供 ``async with`` 使用的事务占位上下文。
# Args: 无。
# Returns: 异步上下文管理器实例。
@asynccontextmanager
async def _transaction():
    yield None


# 方法作用：装配走 PostgreSQL 分支的 Store，并注入假连接。
# Args: monkeypatch - pytest 补丁工具；connection - 假 asyncpg 连接。
# Returns: 使用假连接的 NotificationStore。
def _store_with(monkeypatch, connection: Any):
    from src.memory import pg_pool as pg_pool_module
    from src.notifications.store import NotificationStore

    @asynccontextmanager
    # 方法作用：为 Store 提供不访问真实数据库的连接上下文。
    # Args: pool - 占位 Pool；tenant_id/user_id/role - RLS 身份。
    # Returns: 假连接异步上下文。
    async def fake_connection(pool, tenant_id, user_id, role):
        del pool, tenant_id, user_id, role
        yield connection

    monkeypatch.setattr(pg_pool_module, "pg_pool_connection", fake_connection)
    return NotificationStore(pg_pool=object())


class TestChannelConfigWrite:
    """覆盖渠道配置写入侧的 jsonb 编码。"""

    # 方法作用：验证写入参数是本环境驱动要求的 JSON 字符串。
    # Args: self - 测试实例；monkeypatch - pytest 补丁工具。
    # Returns: 无返回值，断言失败时由 pytest 报告。
    async def test_save_channels_serializes_config_for_driver(self, monkeypatch) -> None:
        """实测本环境的 asyncpg 对 jsonb 参数只收字符串：传 dict 直接 DataError。

        因此写入侧必须自己 json.dumps；服务端按 jsonb 解析后仍是对象，
        不是双重编码（`jsonb_typeof` 实测为 object）。
        """
        # Arrange
        captured: dict[str, Any] = {}
        connection = SimpleNamespace(
            execute=AsyncMock(side_effect=lambda sql, *args: captured.update({"sql": sql, "args": args})),
            fetch=AsyncMock(return_value=[]),
            transaction=_transaction,
        )
        store = _store_with(monkeypatch, connection)

        # Act
        await store.save_channels(3, {"email": {"enabled": True, "config": {"smtp_host": "smtp.example.com"}}}, 7)

        # Assert
        assert captured["args"], "未捕获到渠道配置写入参数"
        config = captured["args"][3]
        assert isinstance(config, str), f"配置参数应为 JSON 字符串，实际 {type(config).__name__}"
        assert json.loads(config) == {"smtp_host": "smtp.example.com"}


class TestChannelConfigRead:
    """覆盖渠道配置读取侧的兼容解析。"""

    # 方法作用：验证驱动若解成对象也照常读回。
    # Args: self - 测试实例；monkeypatch - pytest 补丁工具。
    # Returns: 无返回值，断言失败时由 pytest 报告。
    async def test_get_channels_reads_object_row(self, monkeypatch) -> None:
        """换成会解对象形态的驱动时，dict 分支必须同样可用。"""
        # Arrange
        connection = SimpleNamespace(
            fetch=AsyncMock(return_value=[{
                "channel": "email",
                "enabled": True,
                "config_json": {"smtp_host": "smtp.example.com"},
                "updated_by": 7,
                "updated_at": None,
            }]),
        )
        store = _store_with(monkeypatch, connection)

        # Act
        rows = await store.get_channels(3)

        # Assert
        email = next(row for row in rows if row["channel"] == "email")
        assert email["config"] == {"smtp_host": "smtp.example.com"}
        assert email["enabled"] is True

    # 方法作用：验证驱动按文本返回 jsonb 时能正确解析。
    # Args: self - 测试实例；monkeypatch - pytest 补丁工具。
    # Returns: 无返回值，断言失败时由 pytest 报告。
    async def test_get_channels_parses_json_text_row(self, monkeypatch) -> None:
        """本环境 asyncpg 返回的就是 str，必须先 json.loads 再当配置用。"""
        # Arrange
        connection = SimpleNamespace(
            fetch=AsyncMock(return_value=[{
                "channel": "email",
                "enabled": True,
                "config_json": '{"smtp_host": "smtp.example.com"}',
                "updated_by": 7,
                "updated_at": None,
            }]),
        )
        store = _store_with(monkeypatch, connection)

        # Act
        rows = await store.get_channels(3)

        # Assert
        email = next(row for row in rows if row["channel"] == "email")
        assert email["config"] == {"smtp_host": "smtp.example.com"}
