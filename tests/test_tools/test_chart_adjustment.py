"""图表类型调整和热力图生成测试。"""

from __future__ import annotations

import pytest


class TestChartAdjustment:
    """覆盖功能 14.6、14.8 的确定性图表重生成。"""

    # 方法作用：验证自然语言图表指令被限制到支持的类型集合。
    # Args: self - pytest 测试类实例。
    # Returns: 无返回值，断言失败时由 pytest 报告。
    @pytest.mark.parametrize(
        ("instruction", "expected"),
        [
            ("用饼图展示", "pie"),
            ("切换成柱状图", "bar"),
            ("改为趋势折线图", "line"),
            ("使用散点图", "scatter"),
            ("显示热力图", "heatmap"),
            ("恢复自动推荐", "auto"),
        ],
    )
    def test_resolve_chart_type_instruction(self, instruction: str, expected: str) -> None:
        """常用中英文类型指令应映射到稳定枚举。"""
        # Arrange
        from src.tools.chart_generator import resolve_chart_type_instruction

        # Act / Assert
        assert resolve_chart_type_instruction(instruction) == expected

    # 方法作用：验证不明确的图表指令不会触发猜测式重生成。
    # Args: self - pytest 测试类实例。
    # Returns: 无返回值，断言失败时由 pytest 报告。
    def test_rejects_unknown_chart_instruction(self) -> None:
        """后端只接受白名单图表类型。"""
        # Arrange
        from src.tools.chart_generator import resolve_chart_type_instruction

        # Act / Assert
        with pytest.raises(ValueError, match="无法识别"):
            resolve_chart_type_instruction("帮我美化一下")

    # 方法作用：验证两个分类维度和一个数值列可以生成 ECharts heatmap。
    # Args: self - pytest 测试类实例。
    # Returns: 无返回值，断言失败时由 pytest 报告。
    def test_heatmap_builds_coordinate_series(self) -> None:
        """热力图配置应包含双分类轴、视觉映射和三元坐标数据。"""
        # Arrange
        from src.tools.chart_generator import ChartGeneratorTool

        rows = [
            {"weekday": "周一", "hour": "09", "orders": 12},
            {"weekday": "周一", "hour": "10", "orders": 18},
            {"weekday": "周二", "hour": "09", "orders": 9},
        ]

        # Act
        result = ChartGeneratorTool()._run(rows, chart_type="heatmap")  # noqa: SLF001

        # Assert
        assert result["recommended_chart_type"] == "heatmap"
        option = result["option"]
        assert option["series"][0]["type"] == "heatmap"
        assert option["visualMap"]["max"] == 18.0
        assert option["series"][0]["data"][0] == [0, 0, 12.0]
