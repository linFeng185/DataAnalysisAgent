"""节点路由护栏契约：模型只选节点集合，顺序与安全边界由服务端固定。

覆盖 spec/50-report-node-routing.md 的 G1~G6。
"""

from __future__ import annotations

import pytest

from src.graph.node_registry import (
    CANONICAL_ORDER,
    MANDATORY_NODE_NAMES,
    ROUTABLE_NODE_NAMES,
    get_mandatory_node_names,
)
from src.graph.node_routing import render_node_catalog, sanitize_node_sequence


class TestCanonicalOrder:
    """规范顺序必须与静态图同构，否则成序会造出静态图不允许的链路。"""

    # 方法作用：验证规范顺序覆盖静态图的 SQL 流全部节点。
    def test_canonical_order_covers_sql_flow(self) -> None:
        sql_flow = ["retrieve_schema", "decompose_query", "generate_sql", "layer3_validate", "layer4_explain", "execute_sql"]

        assert set(sql_flow) <= set(CANONICAL_ORDER)

    # 方法作用：验证安全校验节点在规范顺序里排在执行之前。
    def test_validation_precedes_execution(self) -> None:
        assert CANONICAL_ORDER.index("layer3_validate") < CANONICAL_ORDER.index("execute_sql")
        assert CANONICAL_ORDER.index("layer4_explain") < CANONICAL_ORDER.index("execute_sql")

    # 方法作用：验证入口、出口名次固定。
    def test_entry_and_exit_are_outermost(self) -> None:
        assert CANONICAL_ORDER[0] == "prepare_turn"
        assert CANONICAL_ORDER[-1] == "build_response"

    # 方法作用：验证必选集合来自骨架与数据表的并集。
    def test_mandatory_covers_skeleton_and_declared(self) -> None:
        mandatory = get_mandatory_node_names()

        assert MANDATORY_NODE_NAMES <= mandatory
        # 安全闸门在数据表里声明了 mandatory，必须被并进来
        assert {"layer3_validate", "layer4_explain"} <= mandatory

    # 方法作用：验证不可路由节点确实不在可路由集合里。
    def test_non_routable_nodes_are_excluded(self) -> None:
        for name in ("supplemental_query", "action_subgraph", "save_preference", "restore_previous_result"):
            assert name not in ROUTABLE_NODE_NAMES

    # 方法作用：验证能力子图不可被模型选中（它们会劫持取数单元）。
    def test_capability_subgraphs_are_not_routable(self) -> None:
        """实机事故：external_environment 被 market_research_subgraph 接管，
        已取到的 2016 行天气数据被覆盖成「市场研究未获得可追溯证据」失败。"""
        for name in ("market_research_subgraph", "file_analysis_subgraph", "report_subgraph", "mcp_agent"):
            assert name not in ROUTABLE_NODE_NAMES


class TestRouteGuardrails:
    """护栏 G1~G5：模型越界不得削弱安全边界。"""

    # 方法作用：G1 —— 未知节点被丢弃，不会去取它的 handler。
    def test_unknown_node_is_dropped(self) -> None:
        sequence = sanitize_node_sequence(["__hack__", "execute_sql", "rm_rf"])

        assert "__hack__" not in sequence
        assert "rm_rf" not in sequence

    # 方法作用：G2 —— 必选节点始终在场，模型无法移除。
    def test_mandatory_nodes_always_present(self) -> None:
        sequence = sanitize_node_sequence(["retrieve_schema", "generate_sql", "execute_sql"])

        assert "prepare_turn" in sequence
        assert "build_response" in sequence

    # 方法作用：G3 —— 只给 execute_sql 时自动补齐校验节点，且排在其前。
    def test_sql_flow_integrity_enforced(self) -> None:
        """模型若能把执行排到校验之前，就是安全漏洞。"""
        sequence = sanitize_node_sequence(["execute_sql"])

        for name in ("layer3_validate", "layer4_explain", "generate_sql", "retrieve_schema"):
            assert name in sequence
        assert sequence.index("layer3_validate") < sequence.index("execute_sql")
        assert sequence.index("layer4_explain") < sequence.index("execute_sql")

    # 方法作用：分析或图表节点不能在没有取数的情况下形成空数据短路。
    # Args: self - 测试实例。
    # Returns: 无返回值。
    @pytest.mark.parametrize("chosen", [["analyze_result"], ["generate_chart"], ["layer3_validate"]])
    def test_sql_dependent_node_expands_complete_flow(self, chosen: list[str]) -> None:
        """模型少选节点时补齐完整 SQL 流，不能只跑空状态分析或校验。"""
        sequence = sanitize_node_sequence(chosen)

        for name in ("retrieve_schema", "decompose_query", "generate_sql", "layer3_validate", "layer4_explain", "execute_sql"):
            assert name in sequence
        assert sequence.index("execute_sql") < sequence.index(chosen[0]) if chosen[0] in {"analyze_result", "generate_chart"} else True

    # 方法作用：G3b —— 多源扇出必须接合并节点。
    def test_multi_source_requires_merge(self) -> None:
        sequence = sanitize_node_sequence(["multi_source_dispatch"], allow_multi_source=True)

        assert "merge_results" in sequence
        assert sequence.index("multi_source_dispatch") < sequence.index("merge_results")

    # 方法作用：验证单源流与多源流互斥，且多源时剔除全部单源专属节点。
    def test_multi_source_strips_single_source_nodes(self) -> None:
        """只删 SQL 流是不够的：analyze_result 在规范顺序里排在扇出之前，
        留下它会在没有数据时先分析一遍（实机表现为 0 行空 SQL 的假成功）。"""
        sequence = sanitize_node_sequence(["execute_sql", "multi_source_dispatch"], allow_multi_source=True)

        assert "merge_results" in sequence
        for name in ("execute_sql", "analyze_result", "retrieve_schema", "decompose_query"):
            assert name not in sequence

    # 方法作用：验证多源单元即使模型只选单源 SQL，也必须提升为完整多源流。
    # Args: self - 测试实例。
    # Returns: 无返回值。
    def test_multi_source_sql_choice_is_promoted_to_multi_source_flow(self) -> None:
        """路由已命中多个源时，单源 SQL 图只能查询首源，会静默遗漏其余来源。"""
        sequence = sanitize_node_sequence(["execute_sql", "analyze_result"], allow_multi_source=True)

        assert "multi_source_dispatch" in sequence
        assert "merge_results" in sequence
        for name in ("execute_sql", "analyze_result", "retrieve_schema", "decompose_query"):
            assert name not in sequence

    # 方法作用：验证实际只有一个授权源时，多源扇出被回退成单源取数链路。
    def test_multi_source_without_allowance_falls_back_to_sql_flow(self) -> None:
        sequence = sanitize_node_sequence(["multi_source_dispatch"], allow_multi_source=False)

        assert "multi_source_dispatch" not in sequence
        assert "merge_results" not in sequence
        for name in ("retrieve_schema", "decompose_query", "generate_sql", "layer3_validate", "layer4_explain", "execute_sql"):
            assert name in sequence

    # 方法作用：验证多源分支里不会混进单源取数节点。
    def test_multi_source_branch_has_no_single_source_nodes(self) -> None:
        sequence = sanitize_node_sequence(["multi_source_dispatch"], allow_multi_source=True)

        assert sequence == [
            name for name in sequence
            if name in {"prepare_turn", "classify_intent", "multi_source_dispatch", "merge_results", "build_response",
                        "layer3_validate", "layer4_explain"}
        ]

    # 方法作用：G4 —— 首末节点固定。
    def test_first_and_last_are_fixed(self) -> None:
        sequence = sanitize_node_sequence(["analyze_result"])

        assert sequence[0] == "prepare_turn"
        assert sequence[-1] == "build_response"

    # 方法作用：G5 —— 超长序列被截断且仍以 build_response 收尾。
    def test_over_long_sequence_is_truncated(self) -> None:
        # Arrange：整表选择必然超出上限
        import src.graph.node_routing as routing

        # Act
        sequence = sanitize_node_sequence(list(CANONICAL_ORDER))

        # Assert
        assert len(sequence) <= routing._MAX_SEQUENCE
        assert sequence[0] == "prepare_turn"
        assert sequence[-1] == "build_response"

    # 方法作用：验证序列始终严格按规范顺序排列。
    def test_sequence_follows_canonical_order(self) -> None:
        sequence = sanitize_node_sequence(["analyze_result", "execute_sql", "retrieve_schema"])

        assert sequence == [name for name in CANONICAL_ORDER if name in set(sequence)]

    # 方法作用：验证只选骨架时收敛为空，交给调用方退回静态图。
    def test_skeleton_only_selection_returns_empty(self) -> None:
        assert sanitize_node_sequence(["prepare_turn", "build_response"]) == []

    # 方法作用：验证空输入返回空序列。
    def test_empty_selection_returns_empty(self) -> None:
        assert sanitize_node_sequence([]) == []
        assert sanitize_node_sequence(None) == []


class TestNodeCatalog:
    """节点数据表渲染：只暴露可路由节点，且带齐路由元数据。"""

    # 方法作用：验证目录不含不可路由节点。
    def test_catalog_lists_only_routable_nodes(self) -> None:
        catalog = render_node_catalog()

        for name in ("supplemental_query", "action_subgraph", "save_preference"):
            assert name not in catalog

    # 方法作用：验证目录含用途、适用场景与产出三列。
    def test_catalog_carries_routing_metadata(self) -> None:
        catalog = render_node_catalog()

        assert "classify_intent" in catalog
        assert "何时选" in catalog
        assert "产出" in catalog

    # 方法作用：验证必选节点被显式标注，模型不必自己填。
    def test_catalog_marks_mandatory_nodes(self) -> None:
        catalog = render_node_catalog()

        assert "【必选】" in catalog


class TestDecisionFallback:
    """G6：决策失败一律退回静态图，不阻断执行。"""

    # 方法作用：验证模型不可用时返回空序列。
    @pytest.mark.asyncio
    async def test_model_unavailable_returns_empty(self, monkeypatch) -> None:
        # Arrange
        from src.graph import node_routing

        monkeypatch.setattr("src.llm.client.is_task_llm_available", lambda task: False)

        # Act
        sequence = await node_routing.decide_node_sequence("按年月统计净销售额")

        # Assert
        assert sequence == []

    # 方法作用：验证决策抛错时返回空序列而不是向上冒泡。
    @pytest.mark.asyncio
    async def test_decision_error_returns_empty(self, monkeypatch) -> None:
        # Arrange
        from src.graph import node_routing

        async def _boom(*args, **kwargs):
            raise RuntimeError("模型超时")

        monkeypatch.setattr(node_routing, "_invoke_node_route", _boom)

        # Act
        sequence = await node_routing.decide_node_sequence("按年月统计净销售额")

        # Assert
        assert sequence == []

    # 方法作用：验证空序列不建模，调用方据此退回静态图。
    @pytest.mark.asyncio
    async def test_empty_sequence_builds_no_graph(self) -> None:
        from src.graph.node_routing import build_sequence_graph

        assert await build_sequence_graph([]) is None

    # 方法作用：验证动态 SQL 图复用静态图的条件边装配器。
    # Args: self - 测试实例；monkeypatch - 依赖替换工具。
    # Returns: 无返回值。
    @pytest.mark.asyncio
    async def test_dynamic_sql_graph_reuses_conditional_flow(self, monkeypatch) -> None:
        """安全校验、EXPLAIN 或执行失败必须能重试/终止，不能沿直线继续。"""
        # Arrange
        from unittest.mock import AsyncMock

        from langgraph.checkpoint.memory import MemorySaver

        import src.graph.subgraphs.sql_analysis as sql_flow
        from src.graph.node_routing import build_sequence_graph

        calls: list[dict] = []
        original = sql_flow.add_sql_analysis_flow

        # 方法作用：记录动态图对共享 SQL 条件拓扑的装配调用。
        # Args: graph - 待装配图；kwargs - 条件路由与终点配置。
        # Returns: 原装配函数的返回值。
        def spy(graph, **kwargs):
            calls.append(dict(kwargs))
            return original(graph, **kwargs)

        monkeypatch.setattr(sql_flow, "add_sql_analysis_flow", spy)
        monkeypatch.setattr(
            "src.memory.checkpointer.get_checkpointer",
            AsyncMock(return_value=MemorySaver()),
        )
        sequence = sanitize_node_sequence(["execute_sql", "analyze_result"])

        # Act
        graph = await build_sequence_graph(sequence)

        # Assert
        assert graph is not None
        assert len(calls) == 1
        edges = {(edge.source, edge.target) for edge in graph.get_graph().edges}
        assert ("layer3_validate", "generate_sql") in edges
        assert ("layer3_validate", "build_response") in edges
        assert ("layer4_explain", "build_response") in edges
        assert ("execute_sql", "generate_sql") in edges
        assert ("execute_sql", "analyze_result") in edges
