"""MCP Client 工具租户隔离与 Agent 降级测试。"""

from __future__ import annotations

import logging
import sys
from types import SimpleNamespace
from unittest.mock import AsyncMock


logger = logging.getLogger(__name__)


class TestMCPToolIsolation:
    """覆盖功能 8.1.13：系统与租户 MCP 工具隔离。"""

    # 方法作用：验证租户只能看到系统工具和本租户工具。
    # Args: self - pytest 测试类实例。
    # Returns: 无返回值，断言失败时由 pytest 报告。
    def test_get_all_tools_filters_tenant_specific_servers(self):
        """全局 MCP 单例不得把其他租户工具暴露给当前请求。"""
        logger.debug("test_get_all_tools_filters_tenant_specific_servers 入口")
        from src.mcp_client.client_manager import MCPClientManager

        manager = MCPClientManager()
        system_tool = object()
        tenant_one_tool = object()
        tenant_two_tool = object()
        manager.langchain_tools = {
            "filesystem__read": system_tool,
            "tenant_1_docs__search": tenant_one_tool,
            "tenant_2_docs__search": tenant_two_tool,
        }
        manager._server_tenants = {
            "filesystem": 0,
            "tenant_1_docs": 1,
            "tenant_2_docs": 2,
        }

        tools = manager.get_all_tools(tenant_id=1)

        assert system_tool in tools
        assert tenant_one_tool in tools
        assert tenant_two_tool not in tools
        logger.info("test_get_all_tools_filters_tenant_specific_servers 完成")

    # 方法作用：验证未指定租户时只返回系统级工具。
    # Args: self - pytest 测试类实例。
    # Returns: 无返回值，断言失败时由 pytest 报告。
    def test_get_all_tools_without_tenant_returns_system_only(self):
        """后台无身份调用不能获得任意租户工具。"""
        logger.debug("test_get_all_tools_without_tenant_returns_system_only 入口")
        from src.mcp_client.client_manager import MCPClientManager

        manager = MCPClientManager()
        system_tool = object()
        tenant_tool = object()
        manager.langchain_tools = {
            "filesystem__read": system_tool,
            "tenant_1_docs__search": tenant_tool,
        }
        manager._server_tenants = {"filesystem": 0, "tenant_1_docs": 1}

        tools = manager.get_all_tools()

        assert tools == [system_tool]
        logger.info("test_get_all_tools_without_tenant_returns_system_only 完成")

    # 方法作用：验证个人 MCP 工具必须同时匹配租户和用户。
    # Args: self - pytest 测试类实例。
    # Returns: 无返回值，断言失败时由 pytest 报告。
    def test_get_all_tools_filters_private_owner(self):
        """同租户其他用户也不能获得 private MCP 工具。"""
        logger.debug("test_get_all_tools_filters_private_owner 入口")
        from src.mcp_client.client_manager import MCPClientManager

        manager = MCPClientManager()
        system_tool = object()
        tenant_tool = object()
        own_tool = object()
        other_tool = object()
        manager.langchain_tools = {
            "system_docs__search": system_tool,
            "tenant_docs__search": tenant_tool,
            "own_files__read": own_tool,
            "other_files__read": other_tool,
        }
        manager._server_scopes = {
            "system_docs": "system",
            "tenant_docs": "tenant",
            "own_files": "private",
            "other_files": "private",
        }
        manager._server_tenants = {
            "system_docs": 0, "tenant_docs": 4, "own_files": 4, "other_files": 4,
        }
        manager._server_owners = {
            "system_docs": 0, "tenant_docs": 0, "own_files": 7, "other_files": 8,
        }

        tools = manager.get_all_tools(tenant_id=4, user_id=7)

        assert tools == [system_tool, tenant_tool, own_tool]
        logger.info("test_get_all_tools_filters_private_owner 完成")

    # 方法作用：验证禁用的系统 MCP 配置仅出现在管理列表，不进入工具集合。
    # Args: self - pytest 测试类实例。
    # Returns: 无返回值，断言失败时由 pytest 报告。
    def test_list_system_servers_includes_disabled_configuration(self):
        """超级管理员需要看见禁用配置，但普通 Agent 不能调用其工具。"""
        logger.debug("test_list_system_servers_includes_disabled_configuration 入口")
        from src.mcp_client.client_manager import MCPClientManager

        manager = MCPClientManager()
        manager._configured_system_servers = {
            "filesystem": {
                "transport": "stdio", "command": "npx", "args": ["server"],
                "enabled": False,
            },
        }

        servers = manager.list_system_servers()

        assert servers[0]["name"] == "filesystem"
        assert servers[0]["enabled"] is False
        assert servers[0]["is_builtin"] is True
        assert manager.get_all_tools(tenant_id=4, user_id=7) == []
        logger.info("test_list_system_servers_includes_disabled_configuration 完成")


class TestMCPScopedLifecycle:
    """覆盖功能 8.1.15：数据库 MCP 的身份加载、缓存和连接生命周期。"""

    # 方法作用：验证数据库可见配置会携带可信作用域参数建立连接。
    # Args: self - pytest 测试类实例；monkeypatch - pytest 补丁工具。
    # Returns: 无返回值，断言失败时由 pytest 报告。
    async def test_reload_from_db_connects_visible_scopes(self, monkeypatch):
        """system、当前租户和本人 private 配置应被加载，元数据不得丢失。"""
        logger.debug("test_reload_from_db_connects_visible_scopes 入口")
        import src.config as config_module
        from src.mcp_client.client_manager import MCPClientManager

        rows = [
            {"name": "system-docs", "scope": "system", "tenant_id": None,
             "owner_user_id": 0, "transport": "sse", "command": "", "args": "",
             "url": "http://system/sse", "env_vars": {}},
            {"name": "tenant-docs", "scope": "tenant", "tenant_id": 4,
             "owner_user_id": 0, "transport": "sse", "command": "", "args": "",
             "url": "http://tenant/sse", "env_vars": {}},
            {"name": "my-files", "scope": "private", "tenant_id": 4,
             "owner_user_id": 7, "transport": "sse", "command": "", "args": "",
             "url": "http://private/sse", "env_vars": {}},
        ]
        connection = SimpleNamespace(
            fetch=AsyncMock(return_value=rows), execute=AsyncMock(), close=AsyncMock(),
        )
        monkeypatch.setitem(
            sys.modules, "asyncpg",
            SimpleNamespace(connect=AsyncMock(return_value=connection)),
        )
        monkeypatch.setattr(
            config_module, "get_settings",
            lambda: SimpleNamespace(
                database_url="postgresql+asyncpg://test:test@db/test",
                mcp_remote_host_allowlist="system,tenant,private",
            ),
        )
        manager = MCPClientManager()
        connect = AsyncMock(return_value=[])
        monkeypatch.setattr(manager, "_connect_single", connect)

        count = await manager.reload_from_db(tenant_id=4, user_id=7)

        assert count == 3
        assert manager._server_scopes["private_4_7_my-files"] == "private"
        assert manager._server_owners["private_4_7_my-files"] == 7
        assert connect.await_count == 3
        logger.info("test_reload_from_db_connects_visible_scopes 完成")

    # 方法作用：验证数据库历史配置中的 stdio 和未授权主机不会被重新加载执行。
    # Args: self - pytest 测试类实例；monkeypatch - pytest 补丁工具。
    # Returns: 无返回值，断言失败时由 pytest 报告。
    async def test_reload_from_db_skips_unsafe_managed_servers(self, monkeypatch):
        """旧数据不能绕过新 API 校验重新形成进程执行或 SSRF。"""
        # Arrange
        import src.config as config_module
        from src.mcp_client.client_manager import MCPClientManager

        rows = [
            {"name": "legacy-process", "scope": "private", "tenant_id": 4,
             "owner_user_id": 7, "transport": "stdio", "command": "python", "args": "-c pass",
             "url": "", "env_vars": {}},
            {"name": "blocked-host", "scope": "private", "tenant_id": 4,
             "owner_user_id": 7, "transport": "sse", "command": "", "args": "",
             "url": "http://127.0.0.1:9000/sse", "env_vars": {}},
            {"name": "approved", "scope": "private", "tenant_id": 4,
             "owner_user_id": 7, "transport": "sse", "command": "", "args": "",
             "url": "https://mcp.example.com/sse", "env_vars": {}},
        ]
        connection = SimpleNamespace(
            fetch=AsyncMock(return_value=rows), execute=AsyncMock(), close=AsyncMock(),
        )
        monkeypatch.setitem(
            sys.modules, "asyncpg",
            SimpleNamespace(connect=AsyncMock(return_value=connection)),
        )
        monkeypatch.setattr(
            config_module,
            "get_settings",
            lambda: SimpleNamespace(
                database_url="postgresql+asyncpg://test:test@db/test",
                mcp_remote_host_allowlist="mcp.example.com",
            ),
        )
        manager = MCPClientManager()
        connect = AsyncMock(return_value=[])
        monkeypatch.setattr(manager, "_connect_single", connect)

        # Act
        count = await manager.reload_from_db(tenant_id=4, user_id=7)

        # Assert
        assert count == 1
        assert connect.await_count == 1
        assert connect.await_args.args[0].endswith("approved")

    # 方法作用：验证同一身份的 MCP 配置只惰性加载一次。
    # Args: self - pytest 测试类实例；monkeypatch - pytest 补丁工具。
    # Returns: 无返回值，断言失败时由 pytest 报告。
    async def test_ensure_scoped_servers_uses_identity_cache(self, monkeypatch):
        """普通工具调用不得每次重复连接相同 MCP Server。"""
        logger.debug("test_ensure_scoped_servers_uses_identity_cache 入口")
        from src.mcp_client.client_manager import MCPClientManager

        manager = MCPClientManager()
        reload_mock = AsyncMock(return_value=2)
        monkeypatch.setattr(manager, "reload_from_db", reload_mock)

        first = await manager.ensure_scoped_servers(4, 7)
        second = await manager.ensure_scoped_servers(4, 7)

        assert first == 2
        assert second == 0
        reload_mock.assert_awaited_once_with(tenant_id=4, user_id=7)
        logger.info("test_ensure_scoped_servers_uses_identity_cache 完成")

    # 方法作用：验证 MCP 作用域迁移由运行时初始化方法完整执行。
    # Args: self - pytest 测试类实例；monkeypatch - pytest 补丁工具。
    # Returns: 无返回值，断言失败时由 pytest 报告。
    async def test_ensure_schema_executes_resource_scope_migration(self, monkeypatch):
        """新环境不依赖手工先执行旧迁移，也能创建 MCP 三级作用域表。"""
        logger.debug("test_ensure_schema_executes_resource_scope_migration 入口")
        import src.config as config_module
        from src.mcp_client.client_manager import MCPClientManager

        connection = SimpleNamespace(execute=AsyncMock(), close=AsyncMock())
        monkeypatch.setitem(
            sys.modules, "asyncpg",
            SimpleNamespace(connect=AsyncMock(return_value=connection)),
        )
        monkeypatch.setattr(
            config_module, "get_settings",
            lambda: SimpleNamespace(database_url="postgresql+asyncpg://test:test@db/test"),
        )

        result = await MCPClientManager().ensure_schema()

        assert result is True
        migration_sql = connection.execute.await_args.args[0]
        assert "CREATE TABLE IF NOT EXISTS mcp_servers" in migration_sql
        assert "owner_user_id" in migration_sql
        connection.close.assert_awaited_once()
        logger.info("test_ensure_schema_executes_resource_scope_migration 完成")


class TestMCPAgentFallback:
    """覆盖功能 8.3.4：MCP Agent 失败输出契约。"""

    # 方法作用：验证模型不可用时返回标准失败响应而不是被出口改写为成功。
    # Args: self - pytest 测试类实例；monkeypatch - pytest 补丁工具。
    # Returns: 无返回值，断言失败时由 pytest 报告。
    async def test_unavailable_model_returns_standard_failure(self, monkeypatch):
        """MCP Agent 降级结果必须带 source、user_query 和失败标志。"""
        logger.debug("test_unavailable_model_returns_standard_failure 入口")
        import src.llm.client as client_module
        from src.graph.workflow import mcp_agent_node

        monkeypatch.setattr(client_module, "is_task_llm_available", lambda task: False)

        result = await mcp_agent_node({"user_query": "分析上传文件"})

        response = result["final_response"]
        assert response["success"] is False
        assert response["source"] == "mcp_agent"
        assert response["user_query"] == "分析上传文件"
        logger.info("test_unavailable_model_returns_standard_failure 完成")
