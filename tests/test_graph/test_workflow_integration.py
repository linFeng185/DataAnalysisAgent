"""Fake LLM + SQLite 的完整 LangGraph 集成测试。"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from src.app_context import AppContext, use_app_context_async
from src.config import Settings


@pytest.mark.integration
class TestWorkflowIntegration:
    """覆盖功能 16.3.1-16.3.3 的成功、重试和安全短路链路。"""

    # 方法作用：配置工作流使用测试数据库、确定性 Schema 和 Fake LLM。
    # Args: self - pytest 测试类实例；monkeypatch - pytest 补丁工具；sqlite_memory_db - SQLite fixture；model - Fake LLM。
    # Returns: 测试 AppContext 和 SQLite Connector。
    async def _configure(
        self,
        monkeypatch,
        sqlite_memory_db,
        model,
    ) -> tuple[AppContext, object]:
        import src.datasource.registry as registry_module
        import src.graph.nodes.analyze_result as analyze_module
        import src.graph.nodes.execute_sql as execute_module
        import src.graph.nodes.generate_sql as generate_module
        import src.graph.nodes.retrieve_schema as retrieve_module
        import src.graph.skill_activation as activation_module
        import src.knowledge.schema_manager as schema_module

        datasource = sqlite_memory_db.datasource
        registry = SimpleNamespace(
            resolve=AsyncMock(return_value=datasource),
            resolve_or_none=AsyncMock(return_value=datasource),
        )
        monkeypatch.setattr(registry_module, "get_registry", lambda: registry)
        manager = SimpleNamespace(
            get_or_fetch_schema=AsyncMock(return_value=datasource.schema),
        )
        monkeypatch.setattr(schema_module, "get_schema_manager", lambda: manager)
        monkeypatch.setattr(
            retrieve_module,
            "_load_knowledge_context",
            AsyncMock(return_value=""),
        )
        monkeypatch.setattr(
            retrieve_module,
            "_load_enum_dictionary",
            AsyncMock(return_value={}),
        )
        monkeypatch.setattr(
            activation_module,
            "activate_skills",
            lambda state, tables=None: {"activated_skills": []},
        )
        monkeypatch.setattr(generate_module, "is_llm_available", lambda: True)
        monkeypatch.setattr(generate_module, "get_llm", lambda temperature=0: model)
        monkeypatch.setattr(analyze_module, "is_llm_available", lambda: False)
        monkeypatch.setattr(analyze_module, "_is_task_llm_available", lambda task: False)
        monkeypatch.setattr(execute_module, "_record_query_audit", AsyncMock())
        settings = Settings(
            env="test",
            database_url="",
            run_migrations_on_startup=False,
            multi_tenant=False,
            max_retry_count=3,
        )
        return AppContext(settings), datasource.connector

    # 方法作用：验证完整成功链从模型生成 SQL 到 SQLite 结果和最终响应。
    # Args: self - pytest 测试类实例；monkeypatch/sqlite_memory_db/fake_llm_factory - 测试 fixture。
    # Returns: 无返回值，断言失败时由 pytest 报告。
    async def test_complete_success_path(
        self,
        monkeypatch,
        sqlite_memory_db,
        fake_llm_factory,
    ) -> None:
        """真实校验、EXPLAIN 和执行后应返回 3 条订单的聚合结果。"""
        # Arrange
        from src.graph.workflow import build_workflow

        model = fake_llm_factory([
            '{"sql":"SELECT COUNT(*) AS total_orders FROM orders",'
            '"explanation":"统计订单总数"}',
        ])
        context, _ = await self._configure(
            monkeypatch,
            sqlite_memory_db,
            model,
        )

        # Act
        async with use_app_context_async(context):
            app = await build_workflow()
            result = await app.ainvoke({
                "user_query": "统计订单总数",
                "datasource": sqlite_memory_db.datasource.name,
                "selected_datasources": [sqlite_memory_db.datasource.name],
                "request_rate_limit_checked": True,
            }, {"configurable": {"thread_id": "integration-success"}})
        await context.close()

        # Assert
        assert result["final_response"]["success"] is True
        assert result["final_response"]["data"] == [{"total_orders": 3}]
        assert "COUNT" in result["final_response"]["sql"].upper()

    # 方法作用：验证首次 SQL 的真实 EXPLAIN 失败会回到生成节点并最终成功。
    # Args: self - pytest 测试类实例；monkeypatch/sqlite_memory_db/fake_llm_factory - 测试 fixture。
    # Returns: 无返回值，断言失败时由 pytest 报告。
    async def test_semantic_error_retries_then_succeeds(
        self,
        monkeypatch,
        sqlite_memory_db,
        fake_llm_factory,
    ) -> None:
        """重试必须经过 LangGraph 条件边，而不是测试直接调用两次 Node。"""
        # Arrange
        from src.graph.workflow import build_workflow

        model = fake_llm_factory([
            '{"sql":"SELECT missing_column FROM orders","explanation":"首轮错误"}',
            '{"sql":"SELECT SUM(amount) AS total_amount FROM orders","explanation":"修正"}',
        ])
        context, _ = await self._configure(
            monkeypatch,
            sqlite_memory_db,
            model,
        )

        # Act
        async with use_app_context_async(context):
            app = await build_workflow()
            result = await app.ainvoke({
                "user_query": "统计订单金额",
                "datasource": sqlite_memory_db.datasource.name,
                "selected_datasources": [sqlite_memory_db.datasource.name],
                "request_rate_limit_checked": True,
            }, {"configurable": {"thread_id": "integration-retry"}})
        await context.close()

        # Assert
        assert result["retry_count"] == 2
        assert result["final_response"]["success"] is True
        assert result["final_response"]["data"] == [{"total_amount": 300.0}]

    # 方法作用：验证危险 SQL 在 Layer 3 后直接构建失败响应且不访问 SQLite。
    # Args: self - pytest 测试类实例；monkeypatch/sqlite_memory_db/fake_llm_factory - 测试 fixture。
    # Returns: 无返回值，断言失败时由 pytest 报告。
    async def test_security_block_short_circuits_execution(
        self,
        monkeypatch,
        sqlite_memory_db,
        fake_llm_factory,
    ) -> None:
        """安全阻断是终局分支，禁止重试和数据库执行。"""
        # Arrange
        from src.graph.workflow import build_workflow

        model = fake_llm_factory([
            '{"sql":"DELETE FROM orders","explanation":"危险操作"}',
        ])
        context, connector = await self._configure(
            monkeypatch,
            sqlite_memory_db,
            model,
        )
        execute_spy = AsyncMock(wraps=connector.execute_bounded)
        monkeypatch.setattr(connector, "execute_bounded", execute_spy)

        # Act
        async with use_app_context_async(context):
            app = await build_workflow()
            result = await app.ainvoke({
                "user_query": "删除全部订单",
                "datasource": sqlite_memory_db.datasource.name,
                "selected_datasources": [sqlite_memory_db.datasource.name],
                "request_rate_limit_checked": True,
            }, {"configurable": {"thread_id": "integration-security"}})
        await context.close()

        # Assert
        assert result["final_response"]["success"] is False
        assert result["validation_errors"][0]["type"] == "security_block"
        execute_spy.assert_not_awaited()
