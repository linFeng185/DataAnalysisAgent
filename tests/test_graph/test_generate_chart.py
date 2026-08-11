"""generate_chart 节点 Decimal 图表回归测试。"""

from __future__ import annotations

import logging
from decimal import Decimal

import pytest

from src.graph.nodes.generate_chart import generate_chart_node

logger = logging.getLogger(__name__)


class TestGenerateChartDecimal:
    """覆盖功能 4.9：Decimal 查询结果应生成完整 ECharts 配置。"""

    # 验证普通分类聚合数据可生成柱状图配置。
    # Args: self - pytest 测试类实例。
    # Returns: 无返回值，断言柱状图系列包含 Decimal 数值。
    async def test_category_decimal_builds_bar_option(self):
        """分类销售额为 Decimal 时应生成非空柱状图 option。"""
        logger.debug("test_category_decimal_builds_bar_option 入口")
        # Arrange
        state = {
            "query_result_sample": [
                {"category_name": "食品饮料", "total_sales": Decimal("645000000.25")},
                {"category_name": "图书文娱", "total_sales": Decimal("612000000.75")},
            ],
            "analysis_result": {"recommended_chart_type": "bar"},
        }

        # Act
        result = await generate_chart_node(state)

        # Assert
        chart = result["chart_config"]
        assert chart["type"] == "bar"
        assert chart["option"]["series"][0]["data"] == [645000000.25, 612000000.75]
        logger.info("test_category_decimal_builds_bar_option 完成", extra={"series_count": len(chart["option"]["series"])})

    # 验证月份与分类交叉数据可生成多系列图表。
    # Args: self - pytest 测试类实例。
    # Returns: 无返回值，断言交叉数据按分类拆分系列。
    async def test_cross_decimal_builds_multiple_series(self):
        """月份分类销售额为 Decimal 时应按分类生成多个系列。"""
        logger.debug("test_cross_decimal_builds_multiple_series 入口")
        # Arrange
        state = {
            "query_result_sample": [
                {"month": "2026-01", "category_name": "食品饮料", "total_sales": Decimal("10.5")},
                {"month": "2026-01", "category_name": "图书文娱", "total_sales": Decimal("8.5")},
                {"month": "2026-02", "category_name": "食品饮料", "total_sales": Decimal("11.5")},
                {"month": "2026-02", "category_name": "图书文娱", "total_sales": Decimal("9.5")},
            ],
            "analysis_result": {"recommended_chart_type": "bar"},
        }

        # Act
        result = await generate_chart_node(state)

        # Assert
        chart = result["chart_config"]
        assert chart["option"]["xAxis"]["data"] == ["2026-01", "2026-02"]
        assert [series["name"] for series in chart["option"]["series"]] == ["图书文娱", "食品饮料"]
        logger.info("test_cross_decimal_builds_multiple_series 完成", extra={"series_count": len(chart["option"]["series"])})

    # 验证非数值数据仍回退到表格展示。
    # Args: self - pytest 测试类实例。
    # Returns: 无返回值，断言非法图表输入不会生成坐标轴配置。
    async def test_non_numeric_data_returns_not_applicable(self):
        """没有数值列时应明确说明图表不适用，避免生成无效图表。"""
        logger.debug("test_non_numeric_data_falls_back_to_table 入口")
        # Arrange
        state = {
            "query_result_sample": [{"category_name": "食品饮料", "status": "正常"}],
            "analysis_result": {},
        }

        # Act
        result = await generate_chart_node(state)

        # Assert
        assert result["chart_config"]["type"] == "none"
        assert result["chart_config"]["status"] == "not_applicable"
        assert result["chart_config"]["reason"]
        logger.info("test_non_numeric_data_returns_not_applicable 完成", extra={"chart_type": "none"})

    # 验证首行为空值时仍会扫描后续行识别数值列。
    # Args: self - pytest 测试类实例。
    # Returns: 无返回值，断言后续有效数值能生成坐标轴图表。
    async def test_first_row_none_uses_later_numeric_value(self):
        """首行 None 不得把实际数值列误判为非数值列。"""
        logger.debug("test_first_row_none_uses_later_numeric_value 入口")
        state = {
            "query_result_sample": [
                {"category": "未知", "amount": None},
                {"category": "食品", "amount": Decimal("12.5")},
            ],
            "analysis_result": {},
        }

        result = await generate_chart_node(state)

        assert result["chart_config"]["type"] == "bar"
        assert result["chart_config"]["option"]["series"][0]["data"] == [0, 12.5]
        logger.info("test_first_row_none_uses_later_numeric_value 完成")


class TestGenerateChartEligibility:
    """覆盖功能 4.9.7：确定性图表资格复核、核心数值和失败可见性。"""

    async def test_model_table_with_time_dimension_builds_line(self) -> None:
        """模型推荐表格时，两行以上的时间维度加数值仍应生成折线图。"""
        # Arrange
        state = {
            "query_result_sample": [
                {"date": "2026-08-01", "order_count": 10},
                {"date": "2026-08-02", "order_count": 12},
            ],
            "analysis_result": {"recommended_chart_type": "table"},
        }

        # Act
        result = await generate_chart_node(state)

        # Assert
        chart = result["chart_config"]
        assert chart["type"] == "line"
        assert chart["status"] == "success"
        assert chart["option"]["xAxis"]["data"] == ["2026-08-01", "2026-08-02"]

    async def test_model_table_with_category_dimension_builds_bar(self) -> None:
        """模型推荐表格时，两行以上的分类维度加数值仍应生成柱状图。"""
        # Arrange
        state = {
            "query_result_sample": [
                {"workshop": "一车间", "order_count": 10},
                {"workshop": "二车间", "order_count": 12},
            ],
            "analysis_result": {"recommended_chart_type": "table"},
        }

        # Act
        result = await generate_chart_node(state)

        # Assert
        chart = result["chart_config"]
        assert chart["type"] == "bar"
        assert chart["status"] == "success"
        assert chart["option"]["series"][0]["data"] == [10.0, 12.0]

    async def test_single_row_single_metric_returns_core_value(self) -> None:
        """单行单指标结果应返回核心数值，不强制构造坐标轴图表。"""
        # Arrange
        state = {
            "query_result_sample": [{"total_orders": 42}],
            "analysis_result": {"recommended_chart_type": "bar"},
        }

        # Act
        result = await generate_chart_node(state)

        # Assert
        chart = result["chart_config"]
        assert chart["type"] == "none"
        assert chart["status"] == "not_applicable"
        assert chart["core_value"] == {
            "label": "total_orders",
            "value": 42,
            "column": "total_orders",
        }

    @pytest.mark.parametrize("intent", ["metadata", "chat"])
    async def test_non_query_intents_do_not_build_chart(self, intent: str) -> None:
        """元数据与直接问答即使携带数据也不得生成图表。"""
        # Arrange
        state = {
            "intent": intent,
            "query_result_sample": [
                {"category": "甲", "amount": 1},
                {"category": "乙", "amount": 2},
            ],
            "analysis_result": {"recommended_chart_type": "bar"},
        }

        # Act
        result = await generate_chart_node(state)

        # Assert
        chart = result["chart_config"]
        assert chart["type"] == "none"
        assert chart["status"] == "not_applicable"
        assert chart["reason"]

    async def test_empty_data_returns_explicit_no_chart_reason(self) -> None:
        """查询无数据时应返回不适用状态和明确原因。"""
        # Arrange
        state = {"query_result_sample": []}

        # Act
        result = await generate_chart_node(state)

        # Assert
        chart = result["chart_config"]
        assert chart["type"] == "none"
        assert chart["status"] == "not_applicable"
        assert "没有返回数据" in chart["reason"]

    async def test_chart_build_exception_returns_failed_reason(self, monkeypatch) -> None:
        """图表配置构建异常时应保留查询结果并公开失败原因。"""
        # Arrange
        import src.graph.nodes.generate_chart as chart_module

        def raise_build_error(rows: list[dict], chart_type: str) -> dict:
            del rows, chart_type
            raise ValueError("测试图表构建异常")

        monkeypatch.setattr(chart_module, "_build_echarts_option", raise_build_error)
        state = {
            "query_result_sample": [
                {"category": "甲", "amount": 1},
                {"category": "乙", "amount": 2},
            ],
            "analysis_result": {"recommended_chart_type": "bar"},
        }

        # Act
        result = await chart_module.generate_chart_node(state)

        # Assert
        chart = result["chart_config"]
        assert chart["type"] == "bar"
        assert chart["status"] == "failed"
        assert "测试图表构建异常" in chart["reason"]

    # 方法作用：验证触点归因图表只使用确定性归因结果而不是原始路径行。
    # Args: self - pytest 测试类实例。
    # Returns: 无返回值，断言失败时由 pytest 报告。
    async def test_touchpoint_attribution_builds_deterministic_bar_chart(self) -> None:
        """图表类目和值必须直接来自 analysis.attribution.results。"""
        # Arrange
        from decimal import Decimal

        from src.graph.nodes.generate_chart import generate_chart_node

        state = {
            "intent": "attribution",
            "query_result_sample": [{"path_id": "p1", "touchpoint": "搜索"}],
            "analysis_result": {
                "recommended_chart_type": "pie",
                "attribution": {
                    "status": "applied",
                    "results": [
                        {"touchpoint": "搜索", "attributed_value": Decimal("60"),
                         "attribution_pct": Decimal("60")},
                        {"touchpoint": "直播", "attributed_value": Decimal("40"),
                         "attribution_pct": Decimal("40")},
                    ],
                },
            },
        }

        # Act
        result = await generate_chart_node(state)

        # Assert
        chart = result["chart_config"]
        assert chart["type"] == "bar"
        assert chart["status"] == "success"
        assert chart["option"]["xAxis"]["data"] == ["搜索", "直播"]
        assert chart["option"]["series"][0]["data"] == [60.0, 40.0]

    # 方法作用：验证归因资格不足时明确不生成图表。
    # Args: self - pytest 测试类实例。
    # Returns: 无返回值，断言失败时由 pytest 报告。
    async def test_insufficient_attribution_does_not_build_chart(self) -> None:
        """原始行存在也不能掩盖归因证据不足状态。"""
        # Arrange
        from src.graph.nodes.generate_chart import generate_chart_node

        state = {
            "intent": "attribution",
            "query_result_sample": [
                {"channel": "搜索", "conversions": 10},
                {"channel": "直播", "conversions": 5},
            ],
            "analysis_result": {
                "attribution": {"status": "insufficient", "results": []},
            },
        }

        # Act
        result = await generate_chart_node(state)

        # Assert
        chart = result["chart_config"]
        assert chart["type"] == "none"
        assert "资格不足" in chart["reason"]
