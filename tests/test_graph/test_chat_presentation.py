"""覆盖 28.4：分析提示词遵循用户语言及界面语言兜底。"""

import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest


class TestChatPresentation:
    """用户未提供可识别语言时，分析正文仍应使用界面语言。"""

    # 覆盖原英文日期问题从任务计划到 SQL 生成节点的真实传递边界。
    # Args: self - 实例；monkeypatch - 模型边界替身；dialect - 数据库方言。
    # Returns: 无返回值。
    @pytest.mark.parametrize("dialect", ["mysql", "postgres", "clickhouse"])
    async def test_explicit_english_dates_reach_sql_generation(self, monkeypatch: pytest.MonkeyPatch, dialect: str) -> None:
        """日期修复不修改原问题，三种方言均进入模型生成并通过 sqlglot 校验。"""
        import sqlglot

        from src.graph.contracts import build_task_plan
        from src.graph.nodes import generate_sql as sql_module

        # Arrange
        query = "Show regional sales from January 1 to January 7, 2026"
        sql = "SELECT order_date, SUM(sales_amount) AS sales FROM orders WHERE order_date BETWEEN '2026-01-01' AND '2026-01-07' GROUP BY order_date"
        model = AsyncMock(return_value=(sql, "", ""))
        monkeypatch.setattr(sql_module, "is_llm_available", lambda: True)
        monkeypatch.setattr(sql_module, "_llm_generate", model)
        state = {"user_query": query, "dialect": dialect, "task_plan": build_task_plan("aggregation", query=query).model_dump(),
                 "relevant_tables": [{"name": "orders", "columns": [{"name": "order_date", "type": "date"}, {"name": "sales_amount", "type": "decimal"}]}]}
        # Act
        result = await sql_module.generate_sql_node(state, {})
        # Assert
        assert result["generated_sql"] == sql
        assert result["needs_time_range"] is False and result["needs_input"] is False
        assert model.await_count == 1
        assert model.await_args.kwargs["dialect"] == dialect
        assert query in str(model.await_args)
        assert sqlglot.parse_one(sql, read=dialect) is not None

    # 验证预计算跨源结果绕过分析模型后仍经过实际展示子图的标签兜底。
    # Args: self - 实例；monkeypatch - 断言模型节点不可调用的替身。
    # Returns: 无返回值。
    async def test_precomputed_cross_source_subgraph_localizes_without_llm(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """真实条件边路由到图表节点，缺失模型字段说明不会留下已知英文别名。"""
        from src.graph.subgraphs.result_presentation import (
            _route_presentation,
            build_result_presentation_subgraph,
        )

        # Arrange
        analysis = AsyncMock(side_effect=AssertionError("预计算结果不应再次调用模型分析"))
        monkeypatch.setattr("src.graph.nodes.analyze_result.analyze_result_node", analysis)
        state = {"user_query": "按日期展示各地区销售金额，用中文分析", "intent": "aggregation",
                 "multi_source_analysis_precomputed": True,
                 "analysis_result": {"summary": "已完成精确跨源汇总", "recommended_chart_type": "line"},
                 "query_result_sample": [{"order_date": "2026-01-01", "region_code": "CN-BJ", "sales_amount": 10, "_datasource": "mysql"},
                                         {"order_date": "2026-01-01", "region_code": "CN-SH", "sales_amount": 20, "_datasource": "oracle"}]}
        # Act
        output = await build_result_presentation_subgraph().ainvoke(state)
        # Assert
        assert _route_presentation(state) == "generate_chart"
        analysis.assert_not_awaited()
        assert output["chart_config"]["column_labels"]["region_code"] == "地区编码"
        assert output["chart_config"]["column_labels"]["sales_amount"] == "销售金额"
        assert output["query_result_sample"] == state["query_result_sample"]

    # 验证跨源精确汇总不调用模型时，初始图表和手动切换仍共用中文标签。
    # Args: self - 实例；kind - 图表类型。
    # Returns: 无返回值。
    @pytest.mark.parametrize("kind", ["line", "bar", "pie", "scatter", "heatmap"])
    async def test_cross_source_no_model_labels_reach_chart_and_switch(self, kind: str) -> None:
        """原始数据、空缺和来源不改写，已知别名在初始及切换中保持中文。"""
        from copy import deepcopy

        from src.api.routes.visualization import adjust_chart
        from src.api.schemas import ChartAdjustRequest
        from src.graph.nodes.generate_chart import generate_chart_node

        # Arrange
        rows = [{"order_date": day, "region_code": region, "sales_amount": index * 10,
                 "order_count": index, "_datasource": source}
                for index, day in enumerate(["2026-01-01", "2026-01-02"], start=1)
                for source, region in [("mysql", "CN-BJ"), ("oracle", "CN-SH")]]
        original = deepcopy(rows)
        # Act
        output = await generate_chart_node({"user_query": "每天各地区销售金额和订单数，用中文分析", "intent": "aggregation",
                                            "query_result_sample": rows, "analysis_result": {"recommended_chart_type": "line"}})
        chart = output["chart_config"]
        adjusted = await adjust_chart(ChartAdjustRequest(rows=rows, instruction=kind, chart_plan=chart["chart_plan"], column_labels=chart["column_labels"]))
        # Assert
        assert chart["status"] == "success"
        assert chart["column_labels"]["region_code"] == "地区编码"
        assert chart["column_labels"]["sales_amount"] == "销售金额"
        assert adjusted["column_labels"] == chart["column_labels"]
        assert "region_code:" not in json.dumps(adjusted["option"], ensure_ascii=False)
        assert rows == original

    # 验证多指标要求不能被误解为把数值字段用作分组维度。
    # Args: self - 实例。
    # Returns: 无返回值。
    def test_multi_metric_prompt_distinguishes_metrics_and_facets(self) -> None:
        """用户要求多个单位的指标时，应全部保留并由渲染器分面。"""
        from src.llm.prompts import DATA_ANALYSIS_SYSTEM

        # Arrange / Act
        contract = DATA_ANALYSIS_SYSTEM

        # Assert
        assert "metrics 必须包含用户要求展示的全部数值指标" in contract
        assert "facets 仅填写分类维度" in contract

    # 验证重复坐标场景要求模型先判断组合唯一缺字段并显式决策。
    # Args: self - 实例。
    # Returns: 无返回值。
    def test_duplicate_coordinate_prompt_requires_explicit_decision(self) -> None:
        """同一坐标多行时，提示词必须要求模型识别组合唯一并给出明确口径。"""
        from src.llm.prompts import DATA_ANALYSIS_SYSTEM

        # Arrange / Act
        contract = DATA_ANALYSIS_SYSTEM

        # Assert
        assert "组合唯一" in contract
        assert "重复坐标" in contract
        # 缺维度分支必须禁止用聚合口径掩盖缺失的区分维度。
        assert "禁止用 sum/avg 把同名实体合并掩盖" in contract
        # 后端待确认是预期行为，不得为通过校验而改写聚合口径。
        assert "待确认状态" in contract

    # 验证当前问题、历史和界面语言的优先级进入实际分析提示词。
    # Args: self - 实例；monkeypatch - 模型边界补丁；query - 问题；history - 历史；expected - 语言。
    # Returns: 无返回值。
    @pytest.mark.parametrize(
        "query,history,expected",
        [
            ("123", [], "en"),
            ("最近销售趋势", [], "zh-CN"),
            ("123", [{"user_query": "Sales trend"}], "en-US"),
            ("请用英文分析销售趋势", [], "en-US"),
        ],
    )
    async def test_analysis_prompt_uses_display_language_fallback(
        self, monkeypatch, query, history, expected
    ) -> None:
        """语言不明确的问题使用界面兜底，明确指令和问题语言优先。"""
        import src.graph.nodes.analyze_result as analyze
        import src.llm.adapters.registry as registry
        import src.llm.knowledge_loop as loop

        # Arrange
        response = SimpleNamespace(
            content=json.dumps(
                {
                    "summary": "已完成",
                    "insights": [],
                    "recommended_chart_type": "table",
                    "follow_up_questions": [],
                }
            )
        )
        model = SimpleNamespace(ainvoke=AsyncMock(return_value=response))
        monkeypatch.setattr(analyze, "_get_analysis_llm", lambda **kwargs: model)
        monkeypatch.setattr(loop, "build_knowledge_tools_for_call", lambda prepared=None: [])
        monkeypatch.setattr(
            registry,
            "get_adapter",
            lambda model: SimpleNamespace(
                parse_response=lambda response: SimpleNamespace(
                    content=response.content, reasoning_content=""
                ),
            ),
        )

        # Act
        result = await analyze._llm_analyze(
            [{"sales": 1}],
            "SELECT sales FROM metrics",
            {"columns": {}},
            "",
            "",
            "",
            history,
            user_query=query,
            display_language="en",
        )

        # Assert
        assert result["summary"] == "已完成"
        messages = model.ainvoke.await_args.args[0]
        assert f"使用展示语言 {expected}。" in messages[1].content
