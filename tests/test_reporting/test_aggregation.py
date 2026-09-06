"""报表跨源有界聚合回归，覆盖 23.14。"""

import pytest

from src.reporting.models import AnalysisUnit, ReportPlan, UnitResult


class TestReportAggregation:
    """验证 23.14 的粒度、资源预算、授权投影和失败降级。"""

    # 方法作用：构造带明确共同维度与指标的数据集。
    # Args: self - 测试实例。
    # Returns: 单元定义和脱敏结果。
    def datasets(self) -> tuple[list[dict], list[UnitResult]]:
        """两个来源同一天多行，聚合后不做原始行连接。"""
        units = [{"unit_id": source, "datasource": source, "dimensions": ["day"], "metrics": ["amount"], "authorized_columns": ["day", "amount"]} for source in ("sales", "refunds")]
        results = [UnitResult(unit_id=source, datasource=source, rows=[{"day": "2026-09-01", "amount": amount, "secret": "不可输出"} for amount in (2, 3)]) for source in ("sales", "refunds")]
        return units, results

    # 方法作用：验证每个来源独立聚合，不因重复连接键倍增金额。
    # Args: self - 测试实例。
    # Returns: 无返回值。
    def test_grouped_samples_do_not_multiply(self) -> None:
        """结果注明样本统计，保留独立指标名称与匹配覆盖。"""
        from src.reporting.aggregation import aggregate_cross_source

        units, results = self.datasets()
        result = aggregate_cross_source(units, results)

        assert len(result["rows"]) == 1
        assert result["rows"][0]["sales.amount.sample_sum"] == 5
        assert result["rows"][0]["refunds.amount.sample_mean"] == 2.5
        assert "secret" not in str(result)
        assert result["sampled"] is True
        assert result["dimensions"] == ["day"]

    # 方法作用：验证无明确粒度、无授权指标和失败来源均安全降级。
    # Args: self - 测试实例；scenario - 边界场景。
    # Returns: 无返回值。
    @pytest.mark.parametrize("scenario", ["dimension", "permission", "failed", "invalid_number", "missing_key", "empty"])
    def test_aggregation_fails_closed(self, scenario: str) -> None:
        """不猜测字段语义，也不把缺失或异常值当作零。"""
        from src.reporting.aggregation import aggregate_cross_source

        units, results = self.datasets()
        if scenario == "dimension":
            units[0]["dimensions"] = []
        elif scenario == "permission":
            units[0]["authorized_columns"] = ["day"]
        elif scenario == "failed":
            results[0].status = "failed"
        elif scenario == "invalid_number":
            results[0].rows[0]["amount"] = float("inf")
        elif scenario == "missing_key":
            results[0].rows[0]["day"] = None
        else:
            results[0].rows = []

        result = aggregate_cross_source(units, results)

        assert result["rows"] == []
        assert result["status"] == "blocked"
        assert result["limitations"]

    # 方法作用：验证行数和分组预算越界时不发布截断总量。
    # Args: self - 测试实例。
    # Returns: 无返回值。
    def test_aggregation_budget_rejects_partial_totals(self) -> None:
        """超过预算整个聚合降级，源单元仍可独立展示。"""
        from src.reporting.aggregation import aggregate_cross_source

        units, results = self.datasets()
        result = aggregate_cross_source(units, results, max_rows=1)

        assert result["status"] == "blocked"
        assert not result["rows"]

    # 方法作用：验证跨源聚合产物最终进入可浏览页面而非只存在节点状态。
    # Args: self - 测试实例。
    # Returns: 无返回值。
    async def test_aggregation_reaches_report_document(self) -> None:
        """聚合节点输出经布局构建保留在数据清单与表格。"""
        from src.graph.subgraphs.report_workbench import (
            report_plan_layout,
            report_synthesize_insights,
        )

        units, results = self.datasets()
        plan = ReportPlan(analysis_units=[AnalysisUnit(**{key: value for key, value in unit.items() if key != "authorized_columns"}, question="按天分析") for unit in units])
        state = {"plan": plan, "resolved_units": units, "unit_results": results, "relations": []}
        state.update(await report_synthesize_insights(state))
        artifact = (await report_plan_layout(state))["artifact"]

        assert "cross_source_aggregate" in artifact.data_manifest
        assert any(section.id == "cross_source_aggregate" for section in artifact.document.sections)
