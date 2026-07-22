"""generate_chart 节点 Decimal 图表回归测试。"""

from __future__ import annotations

import logging
from decimal import Decimal

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
    async def test_non_numeric_data_falls_back_to_table(self):
        """没有数值列时应保持 table 回退，避免生成无效图表。"""
        logger.debug("test_non_numeric_data_falls_back_to_table 入口")
        # Arrange
        state = {
            "query_result_sample": [{"category_name": "食品饮料", "status": "正常"}],
            "analysis_result": {},
        }

        # Act
        result = await generate_chart_node(state)

        # Assert
        assert result["chart_config"] == {"type": "table", "option": {}}
        logger.info("test_non_numeric_data_falls_back_to_table 完成", extra={"chart_type": "table"})

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
