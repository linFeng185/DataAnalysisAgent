"""FastMCP Server 工具协议测试。"""

from __future__ import annotations

import logging
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

logger = logging.getLogger(__name__)


class FakeFastMCP:
    """捕获装饰器注册函数的内存 FastMCP。"""

    # 方法作用：初始化工具注册表。
    # Args: self - 当前 Fake；name - Server 名称。
    # Returns: 无返回值。
    def __init__(self, name: str) -> None:
        logger.debug("FakeFastMCP.__init__ 入口", extra={"server_name": name})
        self.name = name
        self.tools: dict[str, object] = {}
        logger.info("FakeFastMCP.__init__ 完成", extra={"server_name": name})

    # 方法作用：返回记录工具函数的装饰器。
    # Args: self - 当前 Fake。
    # Returns: 工具函数装饰器。
    def tool(self):
        logger.debug("FakeFastMCP.tool 入口")

        # 方法作用：保存被装饰的工具函数。
        # Args: function - MCP 工具函数。
        # Returns: 原工具函数。
        def decorator(function):
            logger.debug("FakeFastMCP.decorator 入口", extra={"tool": function.__name__})
            self.tools[function.__name__] = function
            logger.info("FakeFastMCP.decorator 完成", extra={"tool": function.__name__})
            return function

        logger.info("FakeFastMCP.tool 完成")
        return decorator


class TestMCPServer:
    """覆盖四个 MCP 工具的成功协议与 Registry 字典契约。"""

    # 方法作用：构造捕获工具函数的 MCP Server。
    # Args: self - pytest 测试类实例；monkeypatch - pytest 补丁工具。
    # Returns: FakeFastMCP 实例。
    def _server(self, monkeypatch) -> FakeFastMCP:
        logger.debug("TestMCPServer._server 入口")
        import mcp.server.fastmcp as fastmcp_module

        from src.mcp_client.server import create_mcp_server

        monkeypatch.setattr(fastmcp_module, "FastMCP", FakeFastMCP)
        result = create_mcp_server()
        logger.info("TestMCPServer._server 完成")
        return result

    # 方法作用：验证查询、列表和表结构工具返回稳定字典协议。
    # Args: self - pytest 测试类实例；monkeypatch - pytest 补丁工具。
    # Returns: 无返回值，断言失败时由 pytest 报告。
    async def test_query_list_and_schema_tools(self, monkeypatch) -> None:
        """Registry 的 list_all 字典摘要不能被当成属性对象读取。"""
        logger.debug("test_query_list_and_schema_tools 入口")
        import src.datasource.registry as registry_module
        import src.graph.workflow as workflow_module
        import src.knowledge.schema_manager as schema_module
        from src.datasource.schema_snapshot import ColumnInfo, SchemaSnapshot, TableSchema

        server = self._server(monkeypatch)
        monkeypatch.setattr(
            workflow_module,
            "app",
            SimpleNamespace(ainvoke=AsyncMock(return_value={
                "final_response": {
                    "success": True,
                    "sql": "SELECT 1",
                    "analysis": {"summary": "ok"},
                    "data": [{"value": 1}],
                },
            })),
        )
        monkeypatch.setattr(
            registry_module,
            "get_registry",
            lambda: SimpleNamespace(list_all=AsyncMock(return_value=[{
                "name": "demo",
                "dialect": "sqlite",
                "description": "演示",
            }])),
        )
        schema = SchemaSnapshot(tables=[TableSchema(
            name="orders",
            description="订单",
            columns=[ColumnInfo(name="id", type="INTEGER", comment="主键")],
        )])
        monkeypatch.setattr(
            schema_module,
            "get_schema_manager",
            lambda: SimpleNamespace(get_or_fetch_schema=AsyncMock(return_value=schema)),
        )

        query_result = await server.tools["query_database"]("问题", "demo", True)
        list_result = await server.tools["list_datasources"]()
        schema_result = await server.tools["get_table_schema"]("demo", "orders")

        assert query_result["data"] == [{"value": 1}]
        assert list_result == {"datasources": [{
            "name": "demo",
            "dialect": "sqlite",
            "description": "演示",
        }]}
        assert schema_result["columns"][0]["name"] == "id"
        logger.info("test_query_list_and_schema_tools 完成")

    # 方法作用：验证 MCP Server 的工作流、数据源列表和 Schema 查询使用当前身份。
    # Args: self - pytest 测试类实例；monkeypatch - pytest 补丁工具。
    # Returns: 无返回值，断言失败时由 pytest 报告。
    async def test_tools_pass_current_identity_to_workflow_and_schema_boundaries(self, monkeypatch) -> None:
        """MCP 工具不能在无显式租户参数时访问默认租户资源。"""
        # Arrange
        import src.api.auth as auth_module
        import src.datasource.registry as registry_module
        import src.graph.workflow as workflow_module
        import src.knowledge.schema_manager as schema_module
        from src.datasource.schema_snapshot import SchemaSnapshot

        server = self._server(monkeypatch)
        identity = SimpleNamespace(tenant_id=42, user_id=7, role="analyst")
        workflow = SimpleNamespace(ainvoke=AsyncMock(return_value={
            "final_response": {"success": True, "sql": "SELECT 1", "analysis": {}},
        }))
        registry = SimpleNamespace(list_all=AsyncMock(return_value=[]))
        manager = SimpleNamespace(
            get_or_fetch_schema=AsyncMock(return_value=SchemaSnapshot(tables=[])),
        )
        monkeypatch.setattr(auth_module, "get_current_identity", lambda: identity)
        monkeypatch.setattr(workflow_module, "app", workflow)
        monkeypatch.setattr(registry_module, "get_registry", lambda: registry)
        monkeypatch.setattr(schema_module, "get_schema_manager", lambda: manager)

        # Act
        await server.tools["query_database"]("问题", "sales", False)
        await server.tools["list_datasources"]()
        await server.tools["get_table_schema"]("sales", "orders")

        # Assert
        workflow_call = workflow.ainvoke.await_args.args[0]
        assert workflow_call["tenant_id"] == 42
        assert workflow_call["user_id"] == 7
        assert workflow_call["user_role"] == "analyst"
        assert registry.list_all.await_args.kwargs["tenant_id"] == 42
        assert manager.get_or_fetch_schema.await_args.kwargs["tenant_id"] == 42

    # 方法作用：验证指标工具通过 VectorStore 检索业务规则。
    # Args: self - pytest 测试类实例；monkeypatch - pytest 补丁工具。
    # Returns: 无返回值，断言失败时由 pytest 报告。
    async def test_metrics_tool_uses_available_business_rule_store(self, monkeypatch) -> None:
        """指标工具不得导入不存在的 `get_business_rule_store`。"""
        logger.debug("test_metrics_tool_uses_available_business_rule_store 入口")
        import src.memory.vector_store as vector_module
        from src.knowledge.models import KnowledgeEntry, KnowledgeSource

        server = self._server(monkeypatch)
        vector_store = SimpleNamespace(get_by_filter=AsyncMock(return_value=[]))
        monkeypatch.setattr(vector_module, "get_vector_store", AsyncMock(return_value=vector_store))
        rule = KnowledgeEntry(
            id="rule-1",
            content="GMV 口径",
            source=KnowledgeSource.MANUAL_DOC,
            category="business_rule",
        )
        monkeypatch.setattr(
            "src.knowledge.business_rules.BusinessRuleStore.search_business_rules",
            AsyncMock(return_value=[rule]),
        )

        result = await server.tools["get_metrics"]("GMV")

        assert result == {
            "metric_name": "GMV",
            "rules": [{"content": "GMV 口径", "category": "business_rule"}],
        }
        logger.info("test_metrics_tool_uses_available_business_rule_store 完成")

    # 方法作用：验证四个 MCP 工具的异常响应不会泄露内部连接与路径信息。
    # Args: self - pytest 测试类实例；monkeypatch - pytest 补丁工具。
    # Returns: 无返回值，断言失败时由 pytest 报告。
    async def test_tool_failures_hide_internal_exception_details(self, monkeypatch) -> None:
        """MCP 协议错误字段应稳定可判定，完整异常只进入服务端日志。"""
        # Arrange
        import src.datasource.registry as registry_module
        import src.graph.workflow as workflow_module
        import src.knowledge.schema_manager as schema_module
        import src.memory.vector_store as vector_module

        secret = "password=secret; host=10.20.30.40; path=C:\\private"
        server = self._server(monkeypatch)
        monkeypatch.setattr(
            workflow_module,
            "app",
            SimpleNamespace(ainvoke=AsyncMock(side_effect=RuntimeError(secret))),
        )
        monkeypatch.setattr(
            registry_module,
            "get_registry",
            lambda: SimpleNamespace(list_all=AsyncMock(side_effect=RuntimeError(secret))),
        )
        monkeypatch.setattr(
            schema_module,
            "get_schema_manager",
            lambda: SimpleNamespace(
                get_or_fetch_schema=AsyncMock(side_effect=RuntimeError(secret)),
            ),
        )
        monkeypatch.setattr(
            vector_module,
            "get_vector_store",
            AsyncMock(side_effect=RuntimeError(secret)),
        )

        # Act
        results = [
            await server.tools["query_database"]("问题", "demo", True),
            await server.tools["list_datasources"](),
            await server.tools["get_table_schema"]("demo", "orders"),
            await server.tools["get_metrics"]("GMV"),
        ]

        # Assert
        assert results == [
            {"error": "数据库查询暂不可用"},
            {"error": "数据源列表暂不可用"},
            {"error": "表结构查询暂不可用"},
            {"error": "指标查询暂不可用"},
        ]
        assert all(secret not in result["error"] for result in results)

    # 方法作用：验证公开的 src.mcp 模块入口会启动 FastMCP Server。
    # Args: self - pytest 测试类实例；monkeypatch - pytest 补丁工具。
    # Returns: 无返回值，断言失败时由 pytest 报告。
    def test_src_mcp_module_entrypoint_runs_server(self, monkeypatch) -> None:
        """功能清单声明的 `python -m src.mcp` 必须有真实可调用入口。"""
        # Arrange
        from src.mcp import __main__ as entrypoint

        server = SimpleNamespace(run=MagicMock())
        monkeypatch.setattr(entrypoint, "create_mcp_server", lambda: server)

        # Act
        entrypoint.main()

        # Assert
        server.run.assert_called_once_with()
