"""管理端、MCP 与会话持久化故障可见性回归测试。"""

from __future__ import annotations

import logging
from contextlib import asynccontextmanager
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from fastapi import HTTPException
from pydantic import ValidationError


logger = logging.getLogger(__name__)


class TestManagementInputValidation:
    """覆盖模型测试端点的结构化输入边界。"""

    # 方法作用：验证模型 ID 为空、过长或含控制字符时被 Pydantic 拒绝。
    # Args: self - pytest 测试类实例。
    # Returns: 无返回值，断言失败时由 pytest 报告。
    def test_model_test_request_rejects_invalid_id(self) -> None:
        """裸 dict 不得把任意大输入传到模型工厂。"""
        logger.debug("test_model_test_request_rejects_invalid_id 入口")
        from src.api.schemas import ModelTestRequest

        for model_id in ("", "x" * 129, "model\nname"):
            with pytest.raises(ValidationError):
                ModelTestRequest(model_id=model_id)
        logger.info("test_model_test_request_rejects_invalid_id 完成")


class TestMCPFailureVisibility:
    """覆盖 MCP 管理数据库故障的 HTTP 状态。"""

    # 方法作用：验证 MCP 数据库查询失败返回 503 而不是 HTTP 200 空列表。
    # Args: self - pytest 测试类实例；monkeypatch - pytest 补丁工具。
    # Returns: 无返回值，断言失败时由 pytest 报告。
    async def test_list_mcp_servers_database_failure_is_503(self, monkeypatch) -> None:
        """管理员必须能区分空配置和数据库故障。"""
        logger.debug("test_list_mcp_servers_database_failure_is_503 入口")
        import src.api.auth as auth_module
        import src.api.routes as routes_package
        import src.api.routes.mcp as mcp_routes
        import src.mcp_client.client_manager as manager_module

        # 方法作用：模拟 MCP 数据库连接失败。
        # Args: 无。
        # Returns: 不返回连接，进入时抛出异常。
        @asynccontextmanager
        async def broken_connection():
            raise RuntimeError("database unavailable")
            yield  # pragma: no cover

        monkeypatch.setattr(auth_module, "get_current_tenant_id", lambda: 4)
        monkeypatch.setattr(auth_module, "get_current_user_id", lambda: 7)
        monkeypatch.setattr(auth_module, "get_current_role", lambda: "tenant_admin")
        monkeypatch.setattr(routes_package, "_connect_scoped_mcp_db", broken_connection)
        monkeypatch.setattr(
            manager_module,
            "get_mcp_client_manager",
            lambda: SimpleNamespace(list_system_servers=lambda: []),
        )

        with pytest.raises(HTTPException) as caught:
            await mcp_routes.list_mcp_servers()
        assert caught.value.status_code == 503
        assert caught.value.detail == "MCP Server 配置存储不可用"
        logger.info("test_list_mcp_servers_database_failure_is_503 完成")


class TestSessionFailureVisibility:
    """覆盖会话状态加载故障不得伪装为空历史。"""

    # 方法作用：验证最新状态加载故障向上抛出。
    # Args: self - pytest 测试类实例；monkeypatch - pytest 补丁工具。
    # Returns: 无返回值，断言失败时由 pytest 报告。
    async def test_latest_state_failure_is_not_silenced(self, monkeypatch) -> None:
        """Checkpointer 故障不得返回 None 冒充无最新状态。"""
        logger.debug("test_latest_state_failure_is_not_silenced 入口")
        import src.api.routes as routes_package
        import src.api.routes.session as session_routes

        monkeypatch.setattr(
            routes_package,
            "_load_checkpoint_tuple",
            AsyncMock(side_effect=RuntimeError("checkpoint unavailable")),
        )
        with pytest.raises(RuntimeError, match="checkpoint unavailable"):
            await session_routes._load_latest_state("session-1")
        logger.info("test_latest_state_failure_is_not_silenced 完成")

    # 方法作用：验证会话轮次加载故障向上抛出。
    # Args: self - pytest 测试类实例；monkeypatch - pytest 补丁工具。
    # Returns: 无返回值，断言失败时由 pytest 报告。
    async def test_turn_history_failure_is_not_silenced(self, monkeypatch) -> None:
        """持久化故障不得返回空列表冒充无历史记录。"""
        logger.debug("test_turn_history_failure_is_not_silenced 入口")
        import src.api.routes as routes_package
        import src.api.routes.session as session_routes

        monkeypatch.setattr(
            routes_package,
            "_load_checkpoint_tuple",
            AsyncMock(side_effect=RuntimeError("checkpoint unavailable")),
        )
        with pytest.raises(RuntimeError, match="checkpoint unavailable"):
            await session_routes._load_session_turns("session-1")
        logger.info("test_turn_history_failure_is_not_silenced 完成")


class TestHistoryWriteDurability:
    """覆盖查询历史写入必须由调用链 await。"""

    # 方法作用：验证 add 等待 PostgreSQL 写入完成。
    # Args: self - pytest 测试类实例；monkeypatch - pytest 补丁工具。
    # Returns: 无返回值，断言失败时由 pytest 报告。
    async def test_add_awaits_postgres_insert(self, monkeypatch) -> None:
        """写入任务不得以无引用 fire-and-forget 方式静默丢失。"""
        logger.debug("test_add_awaits_postgres_insert 入口")
        import src.api.auth as auth_module
        from src.memory.history_store import HistoryStore

        monkeypatch.setattr(auth_module, "get_current_tenant_id", lambda: 4)
        monkeypatch.setattr(auth_module, "get_current_user_id", lambda: 7)
        store = HistoryStore()
        insert = AsyncMock()
        monkeypatch.setattr(store, "_pg_insert", insert)

        await store.add("query", "demo", "session-1")

        insert.assert_awaited_once()
        logger.info("test_add_awaits_postgres_insert 完成")
