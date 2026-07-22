"""DataAnalyzerTool 统计分析测试。"""

from __future__ import annotations

import logging


logger = logging.getLogger(__name__)


class TestDataAnalyzerTool:
    """覆盖趋势、异常值、集中度与错误输入。"""

    # 方法作用：验证完整数据产生统计、趋势、异常值和集中度。
    # Args: self - pytest 测试类实例。
    # Returns: 无返回值，断言失败时由 pytest 报告。
    def test_analyze_rows_returns_structured_sections(self) -> None:
        """8 行时间分类数据应触发全部确定性分析模块。"""
        logger.debug("test_analyze_rows_returns_structured_sections 入口")
        from src.tools.data_analyzer import DataAnalyzerTool

        rows = [
            {"date": f"2026-01-{index + 1:02d}", "category": "A" if index < 4 else "B", "amount": index + 1}
            for index in range(8)
        ]

        result = DataAnalyzerTool()._run(rows, intent="trend")  # noqa: SLF001

        assert "statistics" in result
        assert "trend" in result
        assert "outliers" in result
        assert "concentration" in result
        logger.info("test_analyze_rows_returns_structured_sections 完成")

    # 方法作用：验证 JSON 和空数据错误边界。
    # Args: self - pytest 测试类实例。
    # Returns: 无返回值，断言失败时由 pytest 报告。
    def test_invalid_and_empty_rows_return_error(self) -> None:
        """非法 JSON 与无法分析的空数组均返回 error。"""
        logger.debug("test_invalid_and_empty_rows_return_error 入口")
        from src.tools.data_analyzer import DataAnalyzerTool

        tool = DataAnalyzerTool()
        assert "error" in tool._run("not-json")  # noqa: SLF001
        assert "error" in tool._run([])  # noqa: SLF001
        logger.info("test_invalid_and_empty_rows_return_error 完成")
