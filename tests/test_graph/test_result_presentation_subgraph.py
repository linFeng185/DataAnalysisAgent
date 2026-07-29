"""多源结果展示子图编排测试。"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import AsyncMock


class TestResultPresentationSubgraph:
    """覆盖多源合并后的分析、图表固定边与精确分析短路。"""

    # 方法作用：验证普通合并结果依次经过分析和图表节点。
    # Args: self - pytest 测试类实例；monkeypatch - pytest 补丁工具。
    # Returns: 无返回值，断言失败时由 pytest 报告。
    async def test_standard_result_runs_analysis_then_chart(self, monkeypatch) -> None:
        """未预计算分析时，子图必须执行统一分析并把结果交给图表节点。"""
        # Arrange
        import src.graph.nodes.analyze_result as analyze_module
        import src.graph.nodes.generate_chart as chart_module
        from src.graph.subgraphs.result_presentation import (
            build_result_presentation_subgraph,
        )

        calls: list[str] = []

        # 方法作用：模拟分析节点并记录调用顺序。
        # Args: state - 展示子图状态。
        # Returns: 固定分析结果状态增量。
        async def analyze(state: dict) -> dict:
            calls.append("analyze_result")
            return {"analysis_result": {"summary": "统计完成"}}

        # 方法作用：模拟图表节点并验证接收到上游分析结果。
        # Args: state - 展示子图状态。
        # Returns: 固定柱状图状态增量。
        async def chart(state: dict) -> dict:
            calls.append("generate_chart")
            assert state["analysis_result"]["summary"] == "统计完成"
            return {"chart_config": {"type": "bar", "option": {}}}

        monkeypatch.setattr(analyze_module, "analyze_result_node", analyze)
        monkeypatch.setattr(chart_module, "generate_chart_node", chart)
        subgraph = build_result_presentation_subgraph()

        # Act
        result = await subgraph.ainvoke({
            "query_result_sample": [{"value": 1}],
            "multi_source_analysis_precomputed": False,
        })

        # Assert
        assert calls == ["analyze_result", "generate_chart"]
        assert result["chart_config"]["type"] == "bar"

    # 方法作用：验证精确跨源聚合不会被通用分析节点覆盖。
    # Args: self - pytest 测试类实例；monkeypatch - pytest 补丁工具。
    # Returns: 无返回值，断言失败时由 pytest 报告。
    async def test_precomputed_result_skips_analysis(self, monkeypatch) -> None:
        """已有精确分析时，子图应直接生成图表并保留原分析。"""
        # Arrange
        import src.graph.nodes.analyze_result as analyze_module
        import src.graph.nodes.generate_chart as chart_module
        from src.graph.subgraphs.result_presentation import (
            build_result_presentation_subgraph,
        )

        analyze = AsyncMock(side_effect=AssertionError("精确分析不得重复执行"))
        chart = AsyncMock(return_value={"chart_config": {"type": "table", "option": {}}})
        monkeypatch.setattr(analyze_module, "analyze_result_node", analyze)
        monkeypatch.setattr(chart_module, "generate_chart_node", chart)
        subgraph = build_result_presentation_subgraph()
        original_analysis = {"summary": "精确汇总完成"}

        # Act
        result = await subgraph.ainvoke({
            "query_result_sample": [{"total": 3}],
            "analysis_result": original_analysis,
            "multi_source_analysis_precomputed": True,
        })

        # Assert
        analyze.assert_not_awaited()
        chart.assert_awaited_once()
        assert result["analysis_result"] == original_analysis

    # 方法作用：验证多源合并源码不再直接等待分析或图表节点。
    # Args: self - pytest 测试类实例。
    # Returns: 无返回值，断言失败时由 pytest 报告。
    def test_merge_node_has_no_direct_analysis_node_calls(self) -> None:
        """合并节点必须通过子图获得节点事件，不能维护隐藏调用链。"""
        # Arrange
        source = Path("src/graph/nodes/multi_source.py").read_text(encoding="utf-8")

        # Act
        forbidden_calls = (
            "await analyze_result_node",
            "await generate_chart_node",
        )

        # Assert
        assert all(call not in source for call in forbidden_calls)

    # 方法作用：验证多源合并把父级运行配置传入结果展示子图。
    # Args: self - pytest 测试类实例；monkeypatch - pytest 补丁工具。
    # Returns: 无返回值，断言失败时由 pytest 报告。
    async def test_merge_forwards_parent_runnable_config(self, monkeypatch) -> None:
        """结果展示必须保留父级 metadata/tags，保证流式与追踪上下文连续。"""
        # Arrange
        from types import SimpleNamespace

        import src.graph.nodes.multi_source as multi_source_module
        import src.graph.subgraphs.result_presentation as presentation_module
        import src.llm.client as llm_module

        captured: dict = {}

        # 方法作用：记录展示子图收到的状态和父级配置。
        # Args: state - 合并状态；config - RunnableConfig。
        # Returns: 附加固定分析和图表的状态。
        async def invoke(state: dict, config: dict) -> dict:
            captured["state"] = state
            captured["config"] = config
            return {
                **state,
                "analysis_result": {"summary": "合并完成"},
                "chart_config": {"type": "table", "option": {}},
            }

        subgraph = SimpleNamespace(ainvoke=invoke)
        monkeypatch.setattr(
            presentation_module,
            "build_result_presentation_subgraph",
            lambda: subgraph,
        )
        monkeypatch.setattr(llm_module, "is_task_llm_available", lambda task: False)
        state = {
            "user_query": "比较两个来源的销售额",
            "multi_source_results": [
                {
                    "datasource": "mysql_a",
                    "success": True,
                    "data": [{"total_sales": 10}],
                },
                {
                    "datasource": "postgres_b",
                    "success": True,
                    "data": [{"total_sales": 20}],
                },
            ],
        }

        # Act
        await multi_source_module.merge_results_node(
            state,
            config={"metadata": {"tenant_id": 2}, "tags": ["parent"]},
        )

        # Assert
        assert captured["config"]["metadata"] == {
            "tenant_id": 2,
            "worker": "multi_source_result_presentation",
        }
        assert captured["config"]["tags"] == [
            "parent",
            "multi_source",
            "result_presentation",
        ]
