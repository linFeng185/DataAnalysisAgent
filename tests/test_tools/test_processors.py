"""确定性数据处理器 Decimal 运算回归测试。"""

from __future__ import annotations

import logging
from decimal import Decimal

logger = logging.getLogger(__name__)


class TestDecimalProcessors:
    """覆盖功能 4.12.2：处理器全链路保持 Decimal 数值兼容。"""

    # 方法作用：验证趋势处理器使用 Decimal 阈值判断方向。
    # Args: self - pytest 测试类实例。
    # Returns: 无返回值，断言处理器输出上升趋势。
    def test_trend_processor_decimal_values(self) -> None:
        """Decimal 趋势均值不得与 float 阈值混算。"""
        # Arrange
        from src.tools.processors import TrendProcessor

        rows = [
            {"date": "2026-01", "value": Decimal("10")},
            {"date": "2026-02", "value": Decimal("20")},
            {"date": "2026-03", "value": Decimal("30")},
        ]

        # Act
        result = TrendProcessor().process(rows, {"time_col": "date", "value_col": "value"})

        # Assert
        assert "上升趋势" in result.summary
        assert result.data[-1]["moving_avg"] == Decimal("20.00")

    # 方法作用：验证分析节点可完整调用 Decimal 趋势处理器并写回处理器名称。
    # Args: self - pytest 测试类实例；monkeypatch - pytest 运行时替换工具。
    # Returns: 无返回值，断言分析结果来自 trend 处理器。
    async def test_analyze_result_node_decimal_trend(self, monkeypatch) -> None:
        """Decimal 查询结果应通过分析节点进入确定性趋势处理器。"""
        # Arrange
        import src.graph.nodes.analyze_result as analyze_module

        monkeypatch.setattr(analyze_module, "_is_task_llm_available", lambda task: False)
        state = {
            "user_query": "分析每日用电量趋势",
            "intent": "trend",
            "query_result_sample": [
                {"date": "2026-01", "value": Decimal("10")},
                {"date": "2026-02", "value": Decimal("20")},
                {"date": "2026-03", "value": Decimal("30")},
            ],
        }

        # Act
        result = await analyze_module.analyze_result_node(state)

        # Assert
        assert result["analysis_result"]["processor_name"] == "trend"
        assert "上升趋势" in result["analysis_result"]["summary"]

    # 方法作用：验证增长率处理器使用 Decimal 指数计算 CAGR。
    # Args: self - pytest 测试类实例。
    # Returns: 无返回值，断言两期复合增长率正确。
    def test_growth_rate_processor_decimal_exponent(self) -> None:
        """Decimal 底数不得与 float 指数执行幂运算。"""
        # Arrange
        from src.tools.processors import GrowthRateProcessor

        rows = [
            {"date": "2024", "value": Decimal("1")},
            {"date": "2025", "value": Decimal("2")},
            {"date": "2026", "value": Decimal("4")},
        ]

        # Act
        result = GrowthRateProcessor().process(
            rows, {"time_col": "date", "value_col": "value"},
        )

        # Assert
        assert "CAGR 100.00%" in result.summary

    # 方法作用：验证季节分解处理器统一使用 Decimal 方向和波动阈值。
    # Args: self - pytest 测试类实例。
    # Returns: 无返回值，断言处理器生成完整趋势数据。
    def test_seasonal_processor_decimal_thresholds(self) -> None:
        """Decimal 趋势和标准差不得与 float 阈值混算。"""
        # Arrange
        from src.tools.processors import SeasonalDecompositionProcessor

        rows = [
            {"date": str(index), "value": Decimal(str(index + 1))}
            for index in range(6)
        ]

        # Act
        result = SeasonalDecompositionProcessor().process(
            rows, {"time_col": "date", "value_col": "value"},
        )

        # Assert
        assert len(result.data) == 6
        assert "趋势" in result.summary

    # 方法作用：验证 A/B 测试处理器使用 Decimal 平方根计算标准误。
    # Args: self - pytest 测试类实例。
    # Returns: 无返回值，断言两组统计结果正常生成。
    def test_ab_test_processor_decimal_square_root(self) -> None:
        """Decimal 方差不得使用 float 指数求平方根。"""
        # Arrange
        from src.tools.processors import ABTestProcessor

        rows = [
            {"group": "A", "value": Decimal("10")},
            {"group": "A", "value": Decimal("12")},
            {"group": "B", "value": Decimal("20")},
            {"group": "B", "value": Decimal("22")},
        ]

        # Act
        result = ABTestProcessor().process(
            rows, {"group_col": "group", "value_col": "value"},
        )

        # Assert
        assert len(result.data) == 2
        assert "A vs B" in result.summary

    # 方法作用：验证简单预测处理器在线性回归核中保持 Decimal 类型一致。
    # Args: self - pytest 测试类实例。
    # Returns: 无返回值，断言预测数据和拟合优度正常生成。
    def test_prediction_processor_decimal_regression(self) -> None:
        """回归横轴、协方差和平方根必须处于同一 Decimal 数值域。"""
        # Arrange
        from src.tools.processors import SimplePredictionProcessor

        rows = [
            {"date": "2026-01", "value": Decimal("10")},
            {"date": "2026-02", "value": Decimal("20")},
            {"date": "2026-03", "value": Decimal("30")},
        ]

        # Act
        result = SimplePredictionProcessor().process(
            rows,
            {"time_col": "date", "value_col": "value", "forecast_steps": 2},
        )

        # Assert
        assert len(result.data) == 5
        assert result.data[-1]["predicted"] is True
        assert "R²=1.00" in result.summary
