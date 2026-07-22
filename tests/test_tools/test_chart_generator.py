"""ChartGeneratorTool 图表推荐和配置测试。"""

from __future__ import annotations

import logging

import pytest


logger = logging.getLogger(__name__)


class TestChartGeneratorTool:
    """覆盖时间、分类、散点、表格和输入错误。"""

    # 方法作用：验证 auto 模式按列组合选择图表类型。
    # Args: self - pytest 测试类实例；rows - 输入行；expected - 期望图表类型。
    # Returns: 无返回值，断言失败时由 pytest 报告。
    @pytest.mark.parametrize(
        ("rows", "expected"),
        [
            ([{"date": "2026-01-01", "amount": 1}], "line"),
            ([{"category": "A", "amount": 1}, {"category": "B", "amount": 2}], "pie"),
            ([{"category": f"C{i}", "amount": i} for i in range(9)], "bar"),
            ([{"x": 1, "y": 2}, {"x": 2, "y": 3}], "scatter"),
            ([{"name": "A"}], "table"),
        ],
    )
    def test_auto_chart_matrix(self, rows: list[dict], expected: str) -> None:
        """常见列组合必须映射为稳定图表类型。"""
        logger.debug("test_auto_chart_matrix 入口", extra={"expected": expected})
        from src.tools.chart_generator import ChartGeneratorTool

        result = ChartGeneratorTool()._run(rows, chart_type="auto")  # noqa: SLF001

        assert result["recommended_chart_type"] == expected
        assert result["option"]
        logger.info("test_auto_chart_matrix 完成", extra={"expected": expected})

    # 方法作用：验证 JSON 输入、空输入和非法结构回退。
    # Args: self - pytest 测试类实例。
    # Returns: 无返回值，断言失败时由 pytest 报告。
    def test_json_and_error_boundaries(self) -> None:
        """工具边界必须返回结构化 error，不能向 Agent 抛异常。"""
        logger.debug("test_json_and_error_boundaries 入口")
        from src.tools.chart_generator import ChartGeneratorTool

        tool = ChartGeneratorTool()
        valid = tool._run('[{"category":"A","amount":2}]')  # noqa: SLF001
        invalid = tool._run("not-json")  # noqa: SLF001
        empty = tool._run([])  # noqa: SLF001
        malformed = tool._run([1])  # noqa: SLF001

        assert valid["recommended_chart_type"] == "pie"
        assert "error" in invalid
        assert "error" in empty
        assert "error" in malformed
        logger.info("test_json_and_error_boundaries 完成")
