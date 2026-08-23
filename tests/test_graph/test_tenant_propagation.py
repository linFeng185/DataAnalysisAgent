"""Graph 节点租户参数传递回归测试。"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock


class TestGraphTenantPropagation:
    """覆盖 retrieve_schema 与多数据源 worker 的请求身份边界。"""

    # 方法作用：验证 retrieve_schema 的 SchemaManager 和 Registry 回退均使用状态租户。
    # Args: self - pytest 测试类实例；monkeypatch - pytest 补丁工具。
    # Returns: 无返回值，断言失败时由 pytest 报告。
    async def test_retrieve_schema_passes_state_tenant_to_all_resolvers(self, monkeypatch) -> None:
        """Graph 节点不能在后台执行时回落到 ContextVar 默认租户。"""
        # Arrange
        import src.datasource.registry as registry_module
        import src.graph.nodes.retrieve_schema as retrieve_module
        import src.knowledge.schema_manager as schema_manager_module
        from src.datasource.schema_snapshot import SchemaSnapshot, TableSchema

        schema = SchemaSnapshot(tables=[TableSchema(name="orders")])
        manager = SimpleNamespace(
            get_or_fetch_schema=AsyncMock(return_value=SchemaSnapshot(tables=[])),
        )
        resolved = SimpleNamespace(schema=schema, dialect="postgres")
        registry = SimpleNamespace(resolve_or_none=AsyncMock(return_value=resolved))
        monkeypatch.setattr(schema_manager_module, "get_schema_manager", lambda: manager)
        monkeypatch.setattr(registry_module, "get_registry", lambda: registry)
        monkeypatch.setattr(retrieve_module, "_load_enum_dictionary", AsyncMock(return_value={}))
        monkeypatch.setattr(retrieve_module, "_load_knowledge_context", AsyncMock(return_value=""))

        # Act
        result = await retrieve_module.retrieve_schema_node({
            "datasource": "sales",
            "tenant_id": 42,
            "user_id": 7,
            "intent": "chat",
            "user_query": "查询订单",
        })

        # Assert
        assert result["dialect"] == "postgres"
        manager.get_or_fetch_schema.assert_awaited_once_with(
            "sales", user_query="查询订单", tenant_id=42,
        )
        assert registry.resolve_or_none.await_count == 2
        assert all(
            call.kwargs["tenant_id"] == 42
            for call in registry.resolve_or_none.await_args_list
        )

    # 方法作用：验证多数据源 worker 解析数据源时使用 SourceQueryRequest 中的租户。
    # Args: self - pytest 测试类实例；monkeypatch - pytest 补丁工具。
    # Returns: 无返回值，断言失败时由 pytest 报告。
    async def test_multi_source_worker_passes_request_tenant_to_registry(self, monkeypatch) -> None:
        """跨源 worker 不能使用当前进程默认租户连接。"""
        # Arrange
        import src.datasource.registry as registry_module
        from src.graph.nodes.multi_source import _analyze_one

        resolved = SimpleNamespace(schema=object(), dialect="postgres")
        registry = SimpleNamespace(resolve_or_none=AsyncMock(return_value=resolved))
        monkeypatch.setattr(registry_module, "get_registry", lambda: registry)

        class FakeSubgraph:
            """返回固定的单源成功结果。"""

            # 方法作用：执行替身 SQL 子图并返回最小成功契约。
            # Args: self - 子图替身；state - worker 状态；config - 运行配置。
            # Returns: 单源 SQL 子图结果字典。
            async def ainvoke(self, state, config):
                return {
                    "generated_sql": "SELECT 1",
                    "dialect": "postgres",
                    "validation_errors": [],
                    "explain_errors": [],
                    "execution_error": "",
                    "query_result_sample": [{"value": 1}],
                    "relevant_tables": [],
                }

        # Act
        result = await _analyze_one(
            "sales",
            {
                "user_query": "查询订单",
                "selected_datasources": ["sales", "archive"],
                "tenant_id": 42,
                "user_id": 7,
                "user_role": "analyst",
            },
            subgraph=FakeSubgraph(),
        )

        # Assert
        assert result["success"] is True
        assert registry.resolve_or_none.await_args.kwargs["tenant_id"] == 42
