"""分析节点 Skill 工具执行回归测试。"""

from __future__ import annotations

from decimal import Decimal
from unittest.mock import AsyncMock


class _FakeTool:
    """最小异步工具替身，不调用真实模型或外部服务。"""

    # 方法作用：构造带固定异步返回值的测试工具。
    # Args: self - 工具实例；name - 工具名；value - 调用返回值。
    # Returns: 无返回值。
    def __init__(self, name: str, value):
        self.name = name
        self.ainvoke = AsyncMock(return_value=value)


class TestAnalyzeResultSkillTools:
    """覆盖分析后工具可达性和请求级预算。"""

    # 方法作用：验证报告 Skill 能消费分析负载并写回渲染结果。
    # Args: self - pytest 测试类实例。
    # Returns: 无返回值，断言失败时由 pytest 报告。
    async def test_report_tool_is_executed_with_analysis_payload(self) -> None:
        """custom-report 的渲染工具应在分析完成后真实执行。"""
        # Arrange
        from src.graph.nodes.analyze_result import _execute_post_analysis_skill_tools

        report_tool = _FakeTool("render_report", "# 周报")
        result = {"summary": "销售上升", "insights": ["趋势向上"]}
        rows = [{"amount": 12}]
        state = {
            "user_query": "生成周报",
            "skill_tools": [report_tool],
            "skill_tool_budget": 2,
            "skill_tool_calls": 0,
        }

        # Act
        enriched, calls = await _execute_post_analysis_skill_tools(result, rows, state)

        # Assert
        assert calls == 1
        assert enriched["rendered_report"] == "# 周报"
        report_tool.ainvoke.assert_awaited_once()
        payload = report_tool.ainvoke.await_args.args[0]
        assert payload["template"] == "weekly_report"
        assert payload["data"]["summary"] == "销售上升"

    # 方法作用：验证已用调用数达到预算后不再执行额外工具。
    # Args: self - pytest 测试类实例。
    # Returns: 无返回值，断言失败时由 pytest 报告。
    async def test_skill_tool_budget_blocks_extra_calls(self) -> None:
        """预算耗尽后不得继续调用已授权工具。"""
        # Arrange
        from src.graph.nodes.analyze_result import _execute_post_analysis_skill_tools

        null_tool = _FakeTool("check_null_rate", {"null_rate": 0})
        result = {"summary": "ok"}
        rows = [{"a": 1, "b": 2, "c": 3}]
        state = {
            "skill_tools": [null_tool],
            "skill_tool_budget": 1,
            "skill_tool_calls": 1,
        }

        # Act
        enriched, calls = await _execute_post_analysis_skill_tools(result, rows, state)

        # Assert
        assert calls == 1
        assert "skill_outputs" not in enriched
        null_tool.ainvoke.assert_not_awaited()

    # 方法作用：验证显式零预算不会被解释为默认工具额度。
    # Args: self - pytest 测试类实例。
    # Returns: 无返回值，断言失败时由 pytest 报告。
    async def test_zero_skill_tool_budget_blocks_all_calls(self) -> None:
        """Manifest 将工具预算设为零时必须完全禁止调用。"""
        # Arrange
        from src.graph.nodes.analyze_result import _execute_post_analysis_skill_tools

        report_tool = _FakeTool("render_report", "不应生成")
        state = {
            "user_query": "生成周报",
            "skill_tools": [report_tool],
            "skill_tool_budget": 0,
            "skill_tool_calls": 0,
        }

        # Act
        enriched, calls = await _execute_post_analysis_skill_tools(
            {"summary": "原始摘要"},
            [{"amount": 12}],
            state,
        )

        # Assert
        assert calls == 0
        assert enriched == {"summary": "原始摘要"}
        report_tool.ainvoke.assert_not_awaited()


class TestAnalyzeResultDecimal:
    """覆盖数据库 Decimal 在规则分析分组中的数值识别。"""

    # 方法作用：验证规则分组正确识别并累计 Decimal 指标。
    # Args: self - pytest 测试类实例。
    # Returns: 无返回值，断言失败时由 pytest 报告。
    def test_group_by_includes_decimal_values(self) -> None:
        """Decimal 指标不能被静默当成非数值并汇总为零。"""
        # Arrange
        from src.graph.nodes.analyze_result import _group_by

        rows = [
            {"category": "A", "amount": Decimal("1.25")},
            {"category": "A", "amount": Decimal("2.75")},
            {"category": "B", "amount": Decimal("3.00")},
        ]

        # Act
        grouped = _group_by(rows, "category", "amount")

        # Assert
        assert grouped == [("A", 4.0), ("B", 3.0)]


class TestAnalyzeResultFallback:
    """覆盖 LLM 分析失败后的规则回退上下文。"""

    # 方法作用：验证模型失败时规则分析保留调用方计算出的图表类型和真实意图。
    # Args: self - pytest 测试类实例；monkeypatch - pytest 补丁工具。
    # Returns: 无返回值，断言失败时由 pytest 报告。
    async def test_llm_failure_preserves_chart_type_and_intent(self, monkeypatch) -> None:
        """趋势问题失败回退不能静默降级为普通表格查询。"""
        # Arrange
        import src.graph.nodes.analyze_result as analyze_module

        captured: dict[str, str] = {}

        # 方法作用：记录规则回退实际收到的图表类型和意图。
        # Args: rows/stats/trend/outlier/conc - 规则输入；chart_type - 图表类型；intent - 真实意图。
        # Returns: 包含推荐图表类型的最小分析结果。
        def fallback(rows, stats, trend, outlier, conc, chart_type, intent):
            del rows, stats, trend, outlier, conc
            captured.update(chart_type=chart_type, intent=intent)
            return {"recommended_chart_type": chart_type}

        monkeypatch.setattr(
            analyze_module,
            "get_llm",
            lambda temperature=0.3: (_ for _ in ()).throw(RuntimeError("model down")),
        )
        monkeypatch.setattr(analyze_module, "_rule_analyze", fallback)

        # Act
        result = await analyze_module._llm_analyze(
            [{"date": "2026-07-01", "amount": 1}],
            "SELECT date, amount FROM metrics",
            {"columns": {}},
            "趋势上升",
            "",
            "",
            chart_type="line",
            intent="trend",
        )

        # Assert
        assert captured == {"chart_type": "line", "intent": "trend"}
        assert result["recommended_chart_type"] == "line"
