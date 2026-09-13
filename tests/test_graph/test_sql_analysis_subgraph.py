"""多数据源 SQL 子图复用和 RunnableConfig 传递测试。"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock

from langgraph.checkpoint.memory import MemorySaver
from langgraph.graph import END


class TestSQLAnalysisSubgraph:
    """覆盖多源 worker 与统一 SQL 子图的边界契约。"""

    # 方法作用：验证注册表缓存预热后，SQL 子图仍读取节点模块的当前 handler。
    # Args: self - pytest 测试类实例；monkeypatch - pytest 补丁工具。
    # Returns: 无返回值，断言失败时由 pytest 报告。
    def test_flow_resolves_current_handler_after_registry_cache(
        self,
        monkeypatch,
    ) -> None:
        """共享拓扑不能因注册表缓存而忽略测试替身或运行时模块替换。"""
        # Arrange
        import src.graph.nodes.retrieve_schema as retrieve_module
        import src.graph.subgraphs.sql_analysis as sql_module
        from src.graph.node_registry import get_node_definitions

        get_node_definitions()
        replacement = AsyncMock(return_value={"relevant_tables": []})
        monkeypatch.setattr(retrieve_module, "retrieve_schema_node", replacement)

        # Act
        handlers = sql_module._get_sql_analysis_handlers()

        # Assert
        assert handlers["retrieve_schema"] is replacement

    # 方法作用：验证多源 worker 向 SQL 子图透传父级追踪信息。
    # Args: self - pytest 测试类实例；monkeypatch - pytest 补丁工具。
    # Returns: 无返回值，断言失败时由 pytest 报告。
    async def test_worker_preserves_parent_metadata_and_tags(self, monkeypatch) -> None:
        """多源子图必须继承父图 metadata/tags，保证流式与链路追踪不断裂。"""
        # Arrange
        import src.datasource.registry as registry_module
        from src.graph.nodes.multi_source import _analyze_one

        resolved = SimpleNamespace(schema=object(), dialect="mysql")
        monkeypatch.setattr(
            registry_module,
            "get_registry",
            lambda: SimpleNamespace(resolve_or_none=AsyncMock(return_value=resolved)),
        )

        class FakeSubgraph:
            """记录子图调用参数并返回固定成功状态。"""

            # 方法作用：初始化子图替身的调用捕获字段。
            # Args: self - 子图替身。
            # Returns: 无返回值。
            def __init__(self) -> None:
                self.state = None
                self.config = None

            # 方法作用：捕获单源 worker 输入并返回固定成功状态。
            # Args: self - 子图替身；state - SQL 状态；config - RunnableConfig。
            # Returns: 固定 SQL 执行成功状态。
            async def ainvoke(self, state, config):
                self.state = state
                self.config = config
                return {
                    "generated_sql": "SELECT 1",
                    "dialect": "mysql",
                    "validation_errors": [],
                    "explain_errors": [],
                    "execution_error": "",
                    "query_result_sample": [{"value": 1}],
                    "relevant_tables": [{"name": "metrics"}],
                }

        subgraph = FakeSubgraph()

        # Act
        result = await _analyze_one(
            "analytics",
            {
                "user_query": "查询指标",
                "selected_datasources": ["analytics", "archive"],
            },
            config={"metadata": {"request_id": "r-1"}, "tags": ["parent"]},
            subgraph=subgraph,
        )

        # Assert
        assert result is not None and result["success"] is True
        assert subgraph.config["metadata"]["request_id"] == "r-1"
        assert subgraph.config["metadata"]["datasource"] == "analytics"
        assert "parent" in subgraph.config["tags"]
        assert "multi_source" in subgraph.config["tags"]
        assert subgraph.state["datasource"] == "analytics"

    # 方法作用：验证主图和多源子图都调用同一个 SQL 流程装配函数。
    # Args: self - pytest 测试类实例；monkeypatch - pytest 补丁工具。
    # Returns: 无返回值，断言失败时由 pytest 报告。
    async def test_main_and_worker_graphs_share_flow_assembler(self, monkeypatch) -> None:
        """两套图只能配置不同终点，不能分别维护 SQL 节点和重试边。"""
        # Arrange
        import src.graph.subgraphs.sql_analysis as sql_module
        import src.graph.workflow as workflow_module
        import src.memory.checkpointer as checkpointer_module

        calls: list[dict] = []
        original = sql_module.add_sql_analysis_flow

        # 方法作用：记录每次共享装配调用的终点配置并执行真实装配。
        # Args: graph - 待装配 StateGraph；kwargs - 路由函数和终点参数。
        # Returns: 原装配函数的返回值。
        def spy(graph, **kwargs):
            calls.append(dict(kwargs))
            return original(graph, **kwargs)

        monkeypatch.setattr(sql_module, "add_sql_analysis_flow", spy)
        monkeypatch.setattr(
            checkpointer_module,
            "get_checkpointer",
            AsyncMock(return_value=MemorySaver()),
        )

        # Act
        main_graph = await workflow_module.build_workflow()
        worker_graph = sql_module.build_sql_analysis_subgraph()

        # Assert
        assert len(calls) == 2
        assert calls[0]["direct_target"] == "llm_direct_answer"
        assert calls[0]["failure_target"] == "build_response"
        assert calls[0]["success_target"] == "analyze_result"
        assert calls[1]["direct_target"] == END
        assert calls[1]["failure_target"] == END
        assert calls[1]["success_target"] == END
        main_edges = {(edge.source, edge.target) for edge in main_graph.get_graph().edges}
        worker_edges = {(edge.source, edge.target) for edge in worker_graph.get_graph().edges}
        assert ("generate_sql", "layer3_validate") in main_edges
        assert ("generate_sql", "layer3_validate") in worker_edges
        assert ("execute_sql", "generate_sql") in main_edges
        assert ("execute_sql", "generate_sql") in worker_edges


class TestMultiSourceFailureReason:
    """覆盖单源失败原因的回传：模型证据缺口不能被写成「SQL 生成失败」。

    回归 2026-09-13：external_correlation 单元的两个源都是模型判定证据缺口拒答，
    但多源节点丢弃了子图的 input_explanation，上层只能给出「SQL 生成失败」，
    再汇总成「所有数据源查询均失败，请检查数据源状态和访问权限」，把用户引向
    数据源故障这个错误方向。
    """

    # 方法作用：SQL 为空且无校验/执行错误时，必须回传子图给出的证据缺口说明。
    # Args: self - 测试实例；monkeypatch - 补丁夹具。
    # Returns: 断言通过时无返回值。
    async def test_evidence_gap_becomes_error_message(self, monkeypatch) -> None:
        # Arrange
        import src.datasource.registry as registry_module
        from src.graph.nodes.multi_source import _analyze_one

        resolved = SimpleNamespace(schema=object(), dialect="mysql")
        monkeypatch.setattr(
            registry_module,
            "get_registry",
            lambda: SimpleNamespace(resolve_or_none=AsyncMock(return_value=resolved)),
        )
        gap = "无法生成该查询，存在以下证据缺口：缺少「地区」字段。"

        class FakeSubgraph:
            """返回「模型拒答」状态的子图替身。"""

            # 方法作用：返回无 SQL 但带证据缺口说明的失败状态。
            # Args: self - 子图替身；state - SQL 状态；config - RunnableConfig。
            # Returns: 含 input_explanation 的失败状态。
            async def ainvoke(self, state, config):
                return {
                    "generated_sql": "",
                    "dialect": "mysql",
                    "validation_errors": [],
                    "explain_errors": [],
                    "execution_error": "",
                    "input_explanation": gap,
                }

        # Act
        result = await _analyze_one(
            "analytics",
            {"user_query": "按地区统计订单净销售额", "selected_datasources": ["analytics"]},
            config={},
            subgraph=FakeSubgraph(),
        )

        # Assert
        assert result["success"] is False
        # 证据缺口走面向用户的 user_error；error 保持内部语义，不混入模型文案。
        assert "缺少「地区」字段" in result["user_error"]
        assert result["error"] == "SQL 生成失败"

    # 方法作用：子图既无错误也无说明时保留兜底文案，不能返回空错误。
    # Args: self - 测试实例；monkeypatch - 补丁夹具。
    # Returns: 断言通过时无返回值。
    async def test_missing_explanation_keeps_fallback(self, monkeypatch) -> None:
        # Arrange
        import src.datasource.registry as registry_module
        from src.graph.nodes.multi_source import _analyze_one

        resolved = SimpleNamespace(schema=object(), dialect="mysql")
        monkeypatch.setattr(
            registry_module,
            "get_registry",
            lambda: SimpleNamespace(resolve_or_none=AsyncMock(return_value=resolved)),
        )

        class FakeSubgraph:
            """返回空 SQL 且不带任何说明的子图替身。"""

            # 方法作用：返回只有空 SQL 的失败状态。
            # Args: self - 子图替身；state - SQL 状态；config - RunnableConfig。
            # Returns: 无说明的失败状态。
            async def ainvoke(self, state, config):
                return {
                    "generated_sql": "",
                    "dialect": "mysql",
                    "validation_errors": [],
                    "explain_errors": [],
                    "execution_error": "",
                }

        # Act
        result = await _analyze_one(
            "analytics",
            {"user_query": "查询", "selected_datasources": ["analytics"]},
            config={},
            subgraph=FakeSubgraph(),
        )

        # Assert
        assert result["success"] is False
        assert result["error"] == "SQL 生成失败"
