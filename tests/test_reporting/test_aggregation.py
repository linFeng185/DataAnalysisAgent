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


class TestCrossSourceLimitationWording:
    """缺口 B：限制文案是用户可见的降级说明，不能是内部术语。

    真实验收产物：deep 档 8+6=14 个单元超预算，文档级 limitations 因此多出一条
    「跨源分析资源边界无效」——该函数其余降级分支都用可读中文，只有这条是术语。
    """

    # 方法作用：构造指定数量的跨源单元及其成功结果。
    # Args: count - 单元数。
    # Returns: 单元定义与脱敏结果，交替落在两个来源上，维度与指标完全一致。
    def _units(self, count: int) -> tuple[list[dict], list[UnitResult]]:
        """全部单元声明同一维度与指标，避免预算判定之外的校验先触发。"""
        units = [
            {
                "unit_id": f"unit_{index}",
                "datasource": "sales" if index % 2 else "refunds",
                "dimensions": ["day"],
                "metrics": ["amount"],
                "authorized_columns": ["day", "amount"],
            }
            for index in range(count)
        ]
        results = [
            UnitResult(unit_id=unit["unit_id"], datasource=unit["datasource"], rows=[{"day": "2026-09-01", "amount": 5}])
            for unit in units
        ]
        return units, results

    # 方法作用：验证单元数超预算时产出的限制文案可读且含实际单元数。
    # Args: self - 测试实例。
    # Returns: 无返回值，断言失败时由 pytest 报告。
    def test_over_budget_limitation_states_count_and_budget(self) -> None:
        """用户要看到「多少个 / 预算多少 / 已如何降级」，而不是「资源边界无效」。"""
        from src.reporting.aggregation import aggregate_cross_source

        # Arrange
        units, results = self._units(13)

        # Act
        result = aggregate_cross_source(units, results)
        message = result["limitations"][0]

        # Assert
        assert result["status"] == "blocked"
        assert not result["rows"]
        assert "13" in message and "12" in message
        assert "分别分析" in message
        assert "边界" not in message
        assert message != "跨源分析资源边界无效"
        # 判据语义不变：12 个单元仍在预算内，超预算只影响 13 及以上
        assert aggregate_cross_source(*self._units(12))["status"] == "success"

    # 方法作用：验证资源参数非法走另一条文案，不与单元数超预算混用。
    # Args: self - 测试实例。
    # Returns: 无返回值，断言失败时由 pytest 报告。
    def test_invalid_resource_limits_use_distinct_message(self) -> None:
        """两种情形原因不同，用一句话覆盖会让用户拿着错误的线索去排查。"""
        from src.reporting.aggregation import aggregate_cross_source

        # Arrange
        units, results = self._units(2)
        budget_message = aggregate_cross_source(*self._units(13))["limitations"][0]

        # Act
        message = aggregate_cross_source(units, results, max_rows=0)["limitations"][0]

        # Assert
        assert message != budget_message
        assert "单元数" not in message
        assert "边界" not in message
        assert "分别分析" in message
