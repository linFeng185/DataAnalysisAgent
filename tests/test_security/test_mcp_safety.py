"""G24.4 MCP 运行时隔离与安全阻断回归。"""

from contextlib import asynccontextmanager
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest


class TestMCPSafety:
    """覆盖缓存连接跨身份复用及安全停用边界。"""

    # 验证同名私有连接不会从别的租户缓存中解析出来。
    # Args: self - 测试实例；monkeypatch - 请求身份替身。
    # Returns: 无返回值。
    def test_resolve_loaded_server_filters_identity(self, monkeypatch) -> None:
        """服务名与作用域相同仍必须匹配租户和拥有者。"""
        from src.mcp_client.client_manager import MCPClientManager

        manager = MCPClientManager()
        manager.sessions = {"private_2_7_search": object(), "private_3_8_search": object()}
        manager._server_scopes = {name: "private" for name in manager.sessions}
        manager._server_tenants = {"private_2_7_search": 2, "private_3_8_search": 3}
        manager._server_owners = {"private_2_7_search": 7, "private_3_8_search": 8}
        monkeypatch.setattr("src.api.auth.get_current_tenant_id", lambda: 4)
        monkeypatch.setattr("src.api.auth.get_current_user_id", lambda: 9)
        assert manager.resolve_loaded_server("search", "private") is None

    # 验证受管工具每次执行先校验共享安全状态，不能复用旧授权。
    # Args: self - 测试实例；monkeypatch - 安全服务替身。
    # Returns: 无返回值。
    async def test_tool_call_checks_security_before_remote(self, monkeypatch) -> None:
        """已在缓存中的工具仍被平台安全停用阻断，不调用远端。"""
        from src.mcp_client.client_manager import MCPClientManager

        manager = MCPClientManager()
        session = SimpleNamespace(call_tool=AsyncMock())
        manager.authorize_runtime_server = AsyncMock(side_effect=PermissionError("安全停用"))
        tool = manager._mcp_to_langchain_tool(SimpleNamespace(name="lookup", description="查询", inputSchema={}), session, "tenant_2_0_search")
        with pytest.raises(PermissionError):
            await tool.ainvoke({})
        session.call_tool.assert_not_awaited()

    # 验证真实运行时门禁每次读取共享安全状态，不缓存上一次放行。
    # Args: self - 测试实例；monkeypatch - 身份与数据库替身。
    # Returns: 无返回值。
    async def test_runtime_guard_rechecks_database_and_fails_closed(self, monkeypatch) -> None:
        """第一次可用，其他 Worker 停用后立即拒绝，数据库断开也不得放行。"""
        from src.mcp_client.client_manager import MCPClientManager

        manager = MCPClientManager()
        name = "tenant_2_0_search"
        manager._server_configs[name] = {"url": "https://example.invalid/sse"}
        manager._server_scopes[name] = "tenant"
        manager._server_tenants[name] = 2
        manager._server_owners[name] = 0
        connection = AsyncMock()
        connection.fetchrow.return_value = {"enabled": True, "url": "https://example.invalid/sse"}
        connection.fetchval.side_effect = [False, True, RuntimeError("数据库不可用")]

        # 只替换持久化传输，不替换被测运行时授权逻辑。
        # Args: kwargs - 当前身份。
        # Returns: 共享数据库替身。
        @asynccontextmanager
        async def connect(**kwargs):
            yield connection

        monkeypatch.setattr("src.memory.pg_pool.pg_connection", connect)
        monkeypatch.setattr("src.api.auth.get_current_role", lambda: "analyst")
        monkeypatch.setattr("src.api.auth.get_current_tenant_id", lambda: 2)
        monkeypatch.setattr("src.api.auth.get_current_user_id", lambda: 7)
        await manager.authorize_runtime_server(name)
        with pytest.raises(PermissionError):
            await manager.authorize_runtime_server(name)
        with pytest.raises(RuntimeError):
            await manager.authorize_runtime_server(name)
        assert connection.fetchval.await_count == 3
