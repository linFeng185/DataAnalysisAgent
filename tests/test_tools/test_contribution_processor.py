"""确定性归因分析处理器测试。"""

from __future__ import annotations

from decimal import Decimal

import pytest


class TestContributionProcessor:
    """覆盖功能 4.8.5 的维度变化归因计算。"""

    # 方法作用：验证归因基于每个维度的当前值和上期值而非相邻行。
    # Args: self - pytest 测试类实例。
    # Returns: 无返回值，断言失败时由 pytest 报告。
    def test_process_uses_current_and_previous_values_per_dimension(self) -> None:
        """贡献率分母应为总体净变化，并保留负向抵消项。"""
        # Arrange
        from src.tools.processors import ContributionProcessor

        rows = [
            {"dimension": "华东", "current_value": 120, "previous_value": 100},
            {"dimension": "华南", "current_value": 80, "previous_value": 100},
            {"dimension": "华北", "current_value": 150, "previous_value": 100},
        ]

        # Act
        result = ContributionProcessor().process(rows, {
            "dimension_col": "dimension",
            "current_value_col": "current_value",
            "previous_value_col": "previous_value",
        })

        # Assert
        by_name = {item["name"]: item for item in result.data}
        assert by_name["华东"]["change"] == Decimal("20")
        assert by_name["华南"]["contribution_pct"] == Decimal("-40.0")
        assert by_name["华北"]["contribution_pct"] == Decimal("100.0")
        assert by_name["华北"]["impact_share_pct"] == Decimal("55.6")
        assert "净变化 50" in result.summary

    # 方法作用：验证总体净变化为零时不制造无意义贡献率。
    # Args: self - pytest 测试类实例。
    # Returns: 无返回值，断言失败时由 pytest 报告。
    def test_process_zero_net_change_returns_impact_share_only(self) -> None:
        """正负变化完全抵消时贡献率应为空，仍可返回影响份额。"""
        # Arrange
        from src.tools.processors import ContributionProcessor

        rows = [
            {"dimension": "A", "current_value": 120, "previous_value": 100},
            {"dimension": "B", "current_value": 80, "previous_value": 100},
        ]

        # Act
        result = ContributionProcessor().process(rows, {
            "dimension_col": "dimension",
            "current_value_col": "current_value",
            "previous_value_col": "previous_value",
        })

        # Assert
        assert "净变化为零" in result.summary
        assert all(item["contribution_pct"] is None for item in result.data)
        assert [item["impact_share_pct"] for item in result.data] == [Decimal("50.0"), Decimal("50.0")]

    # 方法作用：验证分析节点能按列语义给归因处理器传入三类字段。
    # Args: self - pytest 测试类实例；monkeypatch - pytest 补丁工具。
    # Returns: 无返回值，断言失败时由 pytest 报告。
    @pytest.mark.asyncio
    async def test_analysis_node_routes_attribution_columns(self, monkeypatch) -> None:
        """为什么类问题应产出真实归因数据而不是相邻行差值。"""
        # Arrange
        import src.graph.nodes.analyze_result as analyze_module

        monkeypatch.setattr(analyze_module, "_is_task_llm_available", lambda task: False)
        rows = [
            {"region": "华东", "current_sales": 120, "previous_sales": 100},
            {"region": "华南", "current_sales": 90, "previous_sales": 100},
        ]

        # Act
        result = await analyze_module.analyze_result_node({
            "user_query": "为什么总销售额变化，各地区贡献度是多少",
            "intent": "attribution",
            "query_result_sample": rows,
        })

        # Assert
        analysis = result["analysis_result"]
        assert analysis["processor_name"] == "contribution"
        assert "净变化 10" in analysis["summary"]
