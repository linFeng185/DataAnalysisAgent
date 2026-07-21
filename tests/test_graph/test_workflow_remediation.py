"""工作流编排、轮次状态和 EXPLAIN 缺陷整改回归测试。"""

from __future__ import annotations

import logging
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest
from langgraph.checkpoint.memory import MemorySaver


logger = logging.getLogger(__name__)


class TestTurnPreparation:
    """覆盖功能 4.1.14：每轮状态初始化。"""

    # 方法作用：验证新轮次清空瞬态结果但保留对话历史。
    # Args: self - pytest 测试类实例。
    # Returns: 无返回值，断言失败时由 pytest 报告。
    async def test_prepare_turn_clears_transient_state_and_keeps_history(self):
        """checkpoint 恢复后不得把上一轮 SQL、错误和数据带入新问题。"""
        logger.debug("test_prepare_turn_clears_transient_state_and_keeps_history 入口")
        from src.graph.nodes.prepare_turn import prepare_turn_node

        history = [{"user_query": "上一轮", "analysis_summary": "旧结论"}]
        result = await prepare_turn_node({
            "user_query": "新问题",
            "conversation_history": history,
            "generated_sql": "SELECT old",
            "execution_error": "旧错误",
            "validation_errors": [{"type": "syntax_error"}],
            "query_result_sample": [{"old": 1}],
            "analysis_result": {"summary": "旧结论"},
            "chart_config": {"type": "bar"},
            "multi_source_results": [{"datasource": "old"}],
        })

        assert result["generated_sql"] == ""
        assert result["execution_error"] == ""
        assert result["validation_errors"] == []
        assert result["query_result_sample"] == []
        assert result["analysis_result"] == {}
        assert result["chart_config"] == {}
        assert result["multi_source_results"] == []
        assert "conversation_history" not in result
        logger.info("test_prepare_turn_clears_transient_state_and_keeps_history 完成")

    # 方法作用：验证清理当前轮字段前会保存上一轮结构化结果快照。
    # Args: self - pytest 测试类实例。
    # Returns: 无返回值，断言失败时由 pytest 报告。
    async def test_prepare_turn_preserves_previous_result_snapshot(self):
        """上一轮结果必须进入独立快照，不能与当前轮瞬态字段一起丢失。"""
        logger.debug("test_prepare_turn_preserves_previous_result_snapshot 入口")
        from src.graph.nodes.prepare_turn import prepare_turn_node

        result = await prepare_turn_node({
            "user_query": "列出订单金额",
            "datasource": "demo",
            "selected_datasources": ["demo"],
            "intent": "query",
            "generated_sql": "SELECT order_id, amount FROM orders",
            "query_result_sample": [{"order_id": 1, "amount": 128000}],
            "query_result_full_count": 1,
            "query_result_truncated": False,
            "query_result_statistics": {"row_count": 1},
            "analysis_result": {"summary": "共 1 行"},
            "chart_config": {"type": "table"},
            "conversation_history": [{"user_query": "列出订单金额"}],
        })

        snapshot = result["previous_turn_snapshot"]
        assert snapshot["source_query"] == "列出订单金额"
        assert snapshot["datasource"] == "demo"
        assert snapshot["generated_sql"] == "SELECT order_id, amount FROM orders"
        assert snapshot["query_result_sample"] == [{"order_id": 1, "amount": 128000}]
        assert snapshot["query_result_full_count"] == 1
        assert snapshot["result_available"] is True
        assert result["query_result_sample"] == []
        logger.info("test_prepare_turn_preserves_previous_result_snapshot 完成")


class TestPreviousTurnRestore:
    """覆盖功能 4.1.15：跨轮结构化结果复用。"""

    # 方法作用：验证同一数据源的结果追问会恢复上一轮结构化结果。
    # Args: self - pytest 测试类实例。
    # Returns: 无返回值，断言失败时由 pytest 报告。
    async def test_restore_previous_result_for_same_datasource(self):
        """meta 追问应恢复数据、SQL、统计和行数供分析节点使用。"""
        logger.debug("test_restore_previous_result_for_same_datasource 入口")
        from src.graph.nodes.restore_previous_result import restore_previous_result_node

        result = await restore_previous_result_node({
            "intent": "meta",
            "datasource": "demo",
            "previous_turn_snapshot": {
                "datasource": "demo",
                "selected_datasources": ["demo"],
                "generated_sql": "SELECT amount FROM orders",
                "query_result_sample": [{"amount": 100}, {"amount": 200}],
                "query_result_full_count": 2,
                "query_result_truncated": False,
                "query_result_statistics": {"row_count": 2},
                "result_available": True,
            },
        })

        assert result["previous_result_restored"] is True
        assert result["generated_sql"] == "SELECT amount FROM orders"
        assert result["query_result_sample"] == [{"amount": 100}, {"amount": 200}]
        assert result["query_result_full_count"] == 2
        logger.info("test_restore_previous_result_for_same_datasource 完成")

    # 方法作用：验证切换数据源后不会恢复其他数据源的旧结果。
    # Args: self - pytest 测试类实例。
    # Returns: 无返回值，断言失败时由 pytest 报告。
    async def test_restore_previous_result_rejects_datasource_change(self):
        """结果快照只能在原数据源上下文中使用。"""
        logger.debug("test_restore_previous_result_rejects_datasource_change 入口")
        from src.graph.nodes.restore_previous_result import restore_previous_result_node

        result = await restore_previous_result_node({
            "intent": "meta",
            "datasource": "postgres_main",
            "previous_turn_snapshot": {
                "datasource": "demo",
                "query_result_sample": [{"amount": 100}],
                "result_available": True,
            },
        })

        assert result["previous_result_restored"] is False
        assert result["query_result_sample"] == []
        assert "数据源已切换" in result["analysis_result"]["summary"]
        logger.info("test_restore_previous_result_rejects_datasource_change 完成")


class TestWorkflowTopology:
    """覆盖功能 4.1.2、4.1.7、4.1.8：完整编译图契约。"""

    # 方法作用：验证文件分析优先于多数据源 SQL 调度。
    # Args: self - pytest 测试类实例。
    # Returns: 无返回值，断言失败时由 pytest 报告。
    def test_file_analysis_takes_priority_over_multi_source(self):
        """选择多个数据源时，文件分析仍必须进入 MCP Agent。"""
        logger.debug("test_file_analysis_takes_priority_over_multi_source 入口")
        from src.graph.workflow import route_by_intent

        target = route_by_intent({
            "intent": "file_analysis",
            "selected_datasources": ["mysql", "postgres"],
        })

        assert target == "mcp_agent"
        logger.info("test_file_analysis_takes_priority_over_multi_source 完成")

    # 方法作用：验证 metadata 先检索 Schema 再直接回答。
    # Args: self - pytest 测试类实例。
    # Returns: 无返回值，断言失败时由 pytest 报告。
    def test_metadata_routes_through_schema_retrieval(self):
        """元数据问题不能绕过真实 Schema。"""
        logger.debug("test_metadata_routes_through_schema_retrieval 入口")
        from src.graph.workflow import after_retrieve_schema, route_by_intent

        assert route_by_intent({"intent": "metadata"}) == "retrieve_schema"
        assert after_retrieve_schema({"intent": "metadata"}) == "llm_direct_answer"
        assert after_retrieve_schema({"intent": "query"}) == "decompose_query"
        logger.info("test_metadata_routes_through_schema_retrieval 完成")

    # 方法作用：验证历史结果追问先恢复快照而不是直接读取已清空字段。
    # Args: self - pytest 测试类实例。
    # Returns: 无返回值，断言失败时由 pytest 报告。
    def test_meta_routes_through_previous_result_restore(self):
        """meta 意图必须先进入上一轮结果恢复节点。"""
        logger.debug("test_meta_routes_through_previous_result_restore 入口")
        from src.graph.workflow import route_by_intent

        assert route_by_intent({"intent": "meta", "datasource": "demo"}) == "restore_previous_result"
        logger.info("test_meta_routes_through_previous_result_restore 完成")

    # 方法作用：验证编译图包含 MCP Agent 的真实执行边和统一响应出口。
    # Args: self - pytest 测试类实例。
    # Returns: 无返回值，断言失败时由 pytest 报告。
    async def test_compiled_graph_executes_mcp_agent_before_response(self):
        """仅测试路由函数不足，必须检查 LangGraph 编译后的实际拓扑。"""
        logger.debug("test_compiled_graph_executes_mcp_agent_before_response 入口")
        import src.graph.workflow as workflow_module

        with patch(
            "src.memory.checkpointer.get_checkpointer",
            new=AsyncMock(return_value=MemorySaver()),
        ):
            graph = await workflow_module.build_workflow()

        edges = {(edge.source, edge.target) for edge in graph.get_graph().edges}
        assert ("classify_intent", "mcp_agent") in edges
        assert ("mcp_agent", "build_response") in edges
        assert ("llm_direct_answer", "build_response") in edges
        logger.info("test_compiled_graph_executes_mcp_agent_before_response 完成")


class TestMetadataGrounding:
    """覆盖功能 4.1.10：metadata 直接回答的 Schema 证据。"""

    # 方法作用：验证无模型回退也能列出 retrieve_schema 提供的真实表。
    # Args: self - pytest 测试类实例；monkeypatch - pytest 补丁工具。
    # Returns: 无返回值，断言失败时由 pytest 报告。
    async def test_metadata_fallback_uses_relevant_tables(self, monkeypatch):
        """metadata 回答不能只依赖可能为空的知识库文本。"""
        logger.debug("test_metadata_fallback_uses_relevant_tables 入口")
        import src.llm.client as client_module
        from src.graph.nodes.llm_answer import llm_direct_answer_node

        monkeypatch.setattr(client_module, "is_llm_available", lambda: False)
        result = await llm_direct_answer_node({
            "intent": "metadata",
            "user_query": "有哪些表",
            "relevant_tables": [{
                "name": "orders",
                "description": "订单表",
                "columns": [{"name": "order_id", "type": "INTEGER"}],
            }],
            "long_term_memories_text": "",
        })

        assert "orders" in result["final_response"]["analysis"]["summary"]
        logger.info("test_metadata_fallback_uses_relevant_tables 完成")

    # 方法作用：验证直接回答经过 build_response 时保留来源并写入历史。
    # Args: self - pytest 测试类实例。
    # Returns: 无返回值，断言失败时由 pytest 报告。
    async def test_build_response_preserves_direct_response(self):
        """chat/metadata/MCP 统一出口不得被改写成 sql_query。"""
        logger.debug("test_build_response_preserves_direct_response 入口")
        from src.graph.nodes.build_response import build_response_node

        result = await build_response_node({
            "user_query": "你好",
            "final_response": {
                "success": True,
                "source": "llm_direct",
                "user_query": "你好",
                "sql": "",
                "data": [],
                "analysis": {"summary": "你好", "insights": []},
                "chart": {"type": "table", "option": {}},
            },
            "analysis_result": {"summary": "你好", "insights": []},
        })

        assert result["final_response"]["source"] == "llm_direct"
        assert result["conversation_history"][-1]["user_query"] == "你好"
        logger.info("test_build_response_preserves_direct_response 完成")

    # 方法作用：验证用户重复提交相同文本时仍保存为独立轮次。
    # Args: self - pytest 测试类实例；monkeypatch - pytest 补丁工具。
    # Returns: 无返回值，断言失败时由 pytest 报告。
    async def test_build_response_records_repeated_query_as_new_turn(self, monkeypatch):
        """问题文本相同不代表节点重复执行，不得据此跳过当前请求。"""
        logger.debug("test_build_response_records_repeated_query_as_new_turn 入口")
        try:
            # Arrange
            from types import SimpleNamespace
            from unittest.mock import Mock

            import src.memory.history_store as history_module
            from src.graph.nodes.build_response import build_response_node

            history_add = Mock()
            monkeypatch.setattr(
                history_module, "get_history_store",
                lambda: SimpleNamespace(add=history_add),
            )
            previous = {
                "turn_id": 1, "user_query": "查询订单",
                "analysis_summary": "第一次回答",
            }

            # Act
            result = await build_response_node({
                "user_query": "查询订单",
                "session_id": "repeat-session",
                "datasource": "demo",
                "conversation_history": [previous],
                "generated_sql": "SELECT 2",
                "query_result_sample": [{"value": 2}],
                "query_result_full_count": 1,
                "analysis_result": {"summary": "第二次回答"},
                "chart_config": {"type": "table", "option": {}},
            })

            # Assert
            assert len(result["conversation_history"]) == 2
            assert result["conversation_history"][-1]["turn_id"] == 2
            assert result["conversation_history"][-1]["analysis_summary"] == "第二次回答"
            history_add.assert_called_once()
            logger.info("test_build_response_records_repeated_query_as_new_turn 完成")
        except Exception as exc:
            logger.error(
                "test_build_response_records_repeated_query_as_new_turn 异常: %s",
                exc,
                exc_info=True,
            )
            raise


class TestValidationRouting:
    """覆盖功能 4.1.12、4.5.4：幻觉和执行错误分流。"""

    # 方法作用：验证未知表错误直接回到 SQL 生成而不被 Layer 3 覆盖。
    # Args: self - pytest 测试类实例。
    # Returns: 无返回值，断言失败时由 pytest 报告。
    def test_hallucination_retries_before_layer3(self):
        """表幻觉必须保留为 LLM 修正上下文。"""
        logger.debug("test_hallucination_retries_before_layer3 入口")
        from src.graph.workflow import after_generate_sql

        target = after_generate_sql({
            "generated_sql": "SELECT * FROM missing",
            "retry_count": 1,
            "validation_errors": [{"type": "hallucination"}],
        })

        assert target == "generate_sql"
        logger.info("test_hallucination_retries_before_layer3 完成")

    # 方法作用：验证瞬态错误重试执行，SQL 语义错误才重新生成。
    # Args: self - pytest 测试类实例。
    # Returns: 无返回值，断言失败时由 pytest 报告。
    @pytest.mark.parametrize(("error_type", "expected"), [
        ("transient", "execute_sql"),
        ("sql_semantic", "generate_sql"),
        ("configuration", "build_response"),
        ("security", "build_response"),
    ])
    def test_execution_errors_route_by_type(self, error_type, expected):
        """数据库连接波动不得触发 LLM 改写正确 SQL。"""
        logger.debug("test_execution_errors_route_by_type 入口")
        from src.graph.workflow import should_retry

        result = should_retry({
            "execution_error": "受控错误",
            "execution_error_type": error_type,
            "retry_count": 1,
            "execution_retry_count": 1,
        })

        assert result == expected
        logger.info("test_execution_errors_route_by_type 完成")


class TestLayer4Explain:
    """覆盖功能 4.6.1：真实数据库 EXPLAIN。"""

    # 方法作用：验证 SQLite EXPLAIN QUERY PLAN 能识别不存在的字段。
    # Args: self - pytest 测试类实例；monkeypatch - pytest 补丁工具。
    # Returns: 无返回值，断言失败时由 pytest 报告。
    async def test_explain_rejects_invalid_sql(self, monkeypatch):
        """Layer 4 必须访问目标引擎，不能无条件返回成功。"""
        logger.debug("test_explain_rejects_invalid_sql 入口")
        from sqlalchemy.ext.asyncio import create_async_engine

        import src.datasource.registry as registry_module
        from src.graph.nodes.layer4_explain import layer4_explain_node

        engine = create_async_engine("sqlite+aiosqlite:///:memory:")
        datasource = SimpleNamespace(engine=engine, dialect="sqlite", name="sqlite-test")
        registry = SimpleNamespace(resolve_or_none=AsyncMock(return_value=datasource))
        monkeypatch.setattr(registry_module, "get_registry", lambda: registry)

        try:
            result = await layer4_explain_node({
                "datasource": "sqlite-test",
                "dialect": "sqlite",
                "generated_sql": "SELECT missing_column FROM missing_table",
            })
        finally:
            await engine.dispose()

        assert result["sql_valid"] is False
        assert result["explain_errors"][0]["type"] == "semantic_error"
        logger.info("test_explain_rejects_invalid_sql 完成")

    # 方法作用：验证合法 SQLite SQL 通过真实 EXPLAIN。
    # Args: self - pytest 测试类实例；monkeypatch - pytest 补丁工具。
    # Returns: 无返回值，断言失败时由 pytest 报告。
    async def test_explain_accepts_valid_sql(self, monkeypatch):
        """合法查询应返回空错误列表。"""
        logger.debug("test_explain_accepts_valid_sql 入口")
        from sqlalchemy.ext.asyncio import create_async_engine

        import src.datasource.registry as registry_module
        from src.graph.nodes.layer4_explain import layer4_explain_node

        engine = create_async_engine("sqlite+aiosqlite:///:memory:")
        datasource = SimpleNamespace(engine=engine, dialect="sqlite", name="sqlite-test")
        registry = SimpleNamespace(resolve_or_none=AsyncMock(return_value=datasource))
        monkeypatch.setattr(registry_module, "get_registry", lambda: registry)

        try:
            result = await layer4_explain_node({
                "datasource": "sqlite-test",
                "dialect": "sqlite",
                "generated_sql": "SELECT 1",
            })
        finally:
            await engine.dispose()

        assert result == {
            "explain_errors": [],
            "sql_valid": True,
            "generated_sql": "SELECT 1",
        }
        logger.info("test_explain_accepts_valid_sql 完成")

    # 方法作用：验证 PostgreSQL SQL 在 EXPLAIN 前完成方言重写并写回状态。
    # Args: self - pytest 测试类实例；monkeypatch - pytest 补丁工具。
    # Returns: 无返回值，断言失败时由 pytest 报告。
    async def test_explain_rewrites_postgres_sql_before_database(self, monkeypatch):
        """两参数 ROUND 必须在 EXPLAIN 前转换为 PostgreSQL numeric 调用。"""
        logger.debug("test_explain_rewrites_postgres_sql_before_database 入口")
        try:
            # Arrange：隔离数据库，仅捕获实际交给 EXPLAIN 的 SQL。
            from types import SimpleNamespace
            from unittest.mock import AsyncMock

            import src.datasource.registry as registry_module
            import src.graph.nodes.layer4_explain as explain_module

            registry = SimpleNamespace(resolve_or_none=AsyncMock(return_value=SimpleNamespace(
                engine=object(), dialect="postgres", name="postgres-test",
            )))
            execute_explain = AsyncMock(return_value=None)
            monkeypatch.setattr(registry_module, "get_registry", lambda: registry)
            monkeypatch.setattr(explain_module, "_execute_explain", execute_explain)
            original_sql = (
                "SELECT category_name, ROUND(SUM(subtotal), 2) AS total_spent "
                "FROM order_items GROUP BY category_name"
            )

            # Act
            result = await explain_module.layer4_explain_node({
                "datasource": "postgres-test",
                "dialect": "postgres",
                "generated_sql": original_sql,
            })

            # Assert：EXPLAIN 和状态写回都使用带 DECIMAL CAST 的处理后 SQL。
            explain_sql = execute_explain.await_args.args[1]
            assert "CAST" in explain_sql.upper()
            assert "DECIMAL" in explain_sql.upper()
            assert result["generated_sql"] != original_sql
            assert "CAST" in result["generated_sql"].upper()
            logger.info("test_explain_rewrites_postgres_sql_before_database 完成")
        except Exception as exc:
            logger.error(
                "test_explain_rewrites_postgres_sql_before_database 异常: %s",
                exc,
                exc_info=True,
            )
            raise

    # 方法作用：验证多数据源 worker 同样经过真实 EXPLAIN 边界。
    # Args: self - pytest 测试类实例；monkeypatch - pytest 补丁工具。
    # Returns: 无返回值，断言失败时由 pytest 报告。
    async def test_multi_source_worker_runs_explain_before_execute(self, monkeypatch):
        """跨源查询不能绕过单源主链已有的 Layer 4 校验。"""
        logger.debug("test_multi_source_worker_runs_explain_before_execute 入口")
        import src.datasource.registry as registry_module
        import src.graph.nodes.execute_sql as execute_module
        import src.graph.nodes.generate_sql as generate_module
        import src.graph.nodes.layer3_validate as layer3_module
        import src.graph.nodes.layer4_explain as layer4_module
        import src.graph.nodes.multi_source as multi_source_module
        import src.graph.nodes.retrieve_schema as retrieve_module

        resolved = SimpleNamespace(schema=object(), dialect="sqlite", engine=object())
        monkeypatch.setattr(
            registry_module,
            "get_registry",
            lambda: SimpleNamespace(resolve_or_none=AsyncMock(return_value=resolved)),
        )
        monkeypatch.setattr(
            retrieve_module,
            "retrieve_schema_node",
            AsyncMock(return_value={
                "relevant_tables": [{"name": "orders", "columns": []}],
                "dialect": "sqlite",
            }),
        )
        monkeypatch.setattr(
            generate_module,
            "generate_sql_node",
            AsyncMock(return_value={"generated_sql": "SELECT 1", "retry_count": 1}),
        )
        monkeypatch.setattr(
            layer3_module,
            "layer3_validate_node",
            AsyncMock(return_value={"validation_errors": []}),
        )
        explain_mock = AsyncMock(return_value={
            "explain_errors": [],
            "sql_valid": True,
            "generated_sql": "SELECT 1 /* explain rewritten */",
        })
        monkeypatch.setattr(layer4_module, "layer4_explain_node", explain_mock)
        execute_mock = AsyncMock(return_value={
            "generated_sql": "SELECT 1 /* rewritten */",
            "query_result_sample": [{"count": 1}],
            "execution_error": "",
        })
        monkeypatch.setattr(execute_module, "execute_sql_node", execute_mock)

        result = await multi_source_module._analyze_one(
            "sqlite-test",
            {
                "user_query": "统计订单数",
                "selected_datasources": ["sqlite-test", "other"],
                "datasource_access": {
                    "sqlite-test": {
                        "allowed_columns": ["order_id"],
                        "row_filter_sql": "org_id = 9",
                    },
                    "other": {
                        "allowed_columns": ["customer_id"],
                        "row_filter_sql": "org_id = 10",
                    },
                },
            },
        )

        assert result["success"] is True
        assert result["sql"] == "SELECT 1 /* rewritten */"
        assert execute_mock.await_args.args[0]["generated_sql"] == "SELECT 1 /* explain rewritten */"
        assert execute_mock.await_args.args[0]["allowed_columns"] == ["order_id"]
        assert execute_mock.await_args.args[0]["row_filter_sql"] == "org_id = 9"
        explain_mock.assert_awaited_once()
        logger.info("test_multi_source_worker_runs_explain_before_execute 完成")


class TestPlanningAndSkills:
    """覆盖功能 4.11.1、9.2.6：规划消费和确定性 Skill 输出。"""

    # 方法作用：验证查询分解结果实际进入 SQL grounding context。
    # Args: self - pytest 测试类实例。
    # Returns: 无返回值，断言失败时由 pytest 报告。
    def test_decompose_steps_are_consumed_by_sql_generation(self):
        """decompose_query 的输出不能成为无人读取的死状态。"""
        logger.debug("test_decompose_steps_are_consumed_by_sql_generation 入口")
        from src.graph.nodes.generate_sql import build_sql_grounding_context

        context = build_sql_grounding_context({
            "intent": "query",
            "needs_decompose": True,
            "decompose_steps": [
                {"step": 1, "question": "先找高价值客户", "depends_on": []},
                {"step": 2, "question": "再统计这些客户订单", "depends_on": [1]},
            ],
        })

        assert "查询规划" in context
        assert "先找高价值客户" in context
        assert "再统计这些客户订单" in context
        logger.info("test_decompose_steps_are_consumed_by_sql_generation 完成")

    # 方法作用：验证明确激活数据质量 Skill 时分析节点生成确定性质量报告。
    # Args: self - pytest 测试类实例；monkeypatch - pytest 补丁工具。
    # Returns: 无返回值，断言失败时由 pytest 报告。
    async def test_data_quality_skill_produces_deterministic_report(self, monkeypatch):
        """Skill 激活状态必须对应真实执行产物，而不只是 Prompt 文本。"""
        logger.debug("test_data_quality_skill_produces_deterministic_report 入口")
        import src.graph.nodes.analyze_result as analyze_module

        monkeypatch.setattr(analyze_module, "is_llm_available", lambda: False)
        rows = [
            {"id": 1, "amount": 10},
            {"id": 1, "amount": 10},
            {"id": 2, "amount": None},
            {"id": 3, "amount": 30},
        ]

        result = await analyze_module.analyze_result_node({
            "query_result_sample": rows,
            "intent": "query",
            "activated_skills": ["data-quality-check"],
        })
        quality = result["analysis_result"]["quality_report"]

        assert quality["row_count"] == 4
        assert quality["duplicate_row_count"] == 1
        assert quality["null_rates"]["amount"] == 0.25
        logger.info("test_data_quality_skill_produces_deterministic_report 完成")

    # 方法作用：验证普通查询不因宽泛 intent 自动激活数据质量 Skill。
    # Args: self - pytest 测试类实例。
    # Returns: 无返回值，断言失败时由 pytest 报告。
    async def test_normal_query_does_not_activate_quality_skill(self):
        """只有明确质量检查请求才应显示 Skill 已激活。"""
        logger.debug("test_normal_query_does_not_activate_quality_skill 入口")
        from src.skill_manager import SkillManager

        manager = SkillManager("skills")
        await manager.discover()
        names = [
            skill.name
            for skill in manager.match_skills("统计客户总数", "query", ["customers"])
        ]

        assert "data-quality-check" not in names
        logger.info("test_normal_query_does_not_activate_quality_skill 完成")


class TestIntentPrecision:
    """覆盖功能 4.2.7：避免宽泛关键词抢占真实查询意图。"""

    # 方法作用：验证包含“字段”的统计问题仍走查询链路。
    # Args: self - pytest 测试类实例。
    # Returns: 无返回值，断言失败时由 pytest 报告。
    async def test_field_word_inside_aggregation_is_not_metadata(self):
        """“按城市字段统计客户数”不是 Schema 问答。"""
        logger.debug("test_field_word_inside_aggregation_is_not_metadata 入口")
        from src.graph.nodes.classify_intent import classify_intent_node

        result = await classify_intent_node({"user_query": "按城市字段统计客户数"})

        assert result["intent"] in {"query", "aggregation"}
        logger.info("test_field_word_inside_aggregation_is_not_metadata 完成")

    # 方法作用：验证明确字段结构问法仍识别为 metadata。
    # Args: self - pytest 测试类实例。
    # Returns: 无返回值，断言失败时由 pytest 报告。
    async def test_explicit_column_question_remains_metadata(self):
        """“orders 有哪些字段”必须先检索 Schema。"""
        logger.debug("test_explicit_column_question_remains_metadata 入口")
        from src.graph.nodes.classify_intent import classify_intent_node

        result = await classify_intent_node({"user_query": "orders 有哪些字段"})

        assert result["intent"] == "metadata"
        logger.info("test_explicit_column_question_remains_metadata 完成")

    # 方法作用：验证数据质量问法进入查询并激活质量 Skill。
    # Args: self - pytest 测试类实例。
    # Returns: 无返回值，断言失败时由 pytest 报告。
    async def test_quality_query_activates_quality_skill(self, monkeypatch):
        """明确空值率请求应执行查询和确定性质量检查。"""
        logger.debug("test_quality_query_activates_quality_skill 入口")
        import src.skill_manager as skill_module
        from src.graph.nodes.classify_intent import classify_intent_node

        manager = skill_module.SkillManager("skills")
        await manager.discover()
        monkeypatch.setattr(skill_module, "get_skill_manager", lambda: manager)

        result = await classify_intent_node({"user_query": "检查订单金额空值率"})

        assert result["intent"] in {"query", "aggregation"}
        assert "data-quality-check" in result["activated_skills"]
        logger.info("test_quality_query_activates_quality_skill 完成")

    # 方法作用：验证未指定数据源时模型只能从授权候选中选择。
    # Args: self - pytest 测试类实例；monkeypatch - pytest 补丁工具。
    # Returns: 无返回值，断言失败时由 pytest 报告。
    async def test_missing_datasource_selects_from_authorized_candidates(self, monkeypatch):
        """自动选择结果必须同步写回主数据源和对应行列权限。"""
        # Arrange
        from unittest.mock import AsyncMock

        import src.graph.nodes.classify_intent as classify_module

        selector = AsyncMock(return_value="warehouse")
        monkeypatch.setattr(classify_module, "_select_authorized_datasource", selector, raising=False)
        access = {
            "sales": {"name": "sales", "allowed_columns": [], "row_filter_sql": ""},
            "warehouse": {
                "name": "warehouse",
                "description": "库存",
                "allowed_columns": ["sku", "stock"],
                "row_filter_sql": "org_id = 9",
            },
        }

        # Act
        result = await classify_module.classify_intent_node({
            "user_query": "库存还有多少",
            "datasource": "",
            "selected_datasources": [],
            "datasource_access": access,
        })

        # Assert
        assert result["datasource"] == "warehouse"
        assert result["selected_datasources"] == ["warehouse"]
        assert result["allowed_columns"] == ["sku", "stock"]
        assert result["row_filter_sql"] == "org_id = 9"
        selector.assert_awaited_once()

    # 方法作用：验证空描述不会在确定性回退中被当作查询命中。
    # Args: self - pytest 测试类实例；monkeypatch - pytest 补丁工具。
    # Returns: 无返回值，断言失败时由 pytest 报告。
    async def test_datasource_fallback_ignores_empty_description(self, monkeypatch):
        """空字符串是任意文本子串，不能因此让首个候选获得虚假分数。"""
        # Arrange
        import src.graph.nodes.classify_intent as classify_module
        import src.llm.client as llm_module

        monkeypatch.setattr(llm_module, "is_task_llm_available", lambda task: False)
        access = {
            "sales": {"name": "sales", "description": ""},
            "warehouse": {"name": "warehouse", "description": "库存"},
        }

        # Act
        selected = await classify_module._select_authorized_datasource("库存还有多少", access)

        # Assert
        assert selected == "warehouse"
