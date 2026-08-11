"""前端结果渲染契约回归测试。"""

from __future__ import annotations

import logging
from pathlib import Path

logger = logging.getLogger(__name__)


class TestResultRendering:
    """覆盖多源字段、SQL、table 图表和流式分组展示。"""

    # 方法作用：验证数据表使用所有行字段并集，避免后续行字段不可见。
    # Args: self - pytest 测试类实例。
    # Returns: 无返回值，断言失败时由 pytest 报告。
    def test_data_table_uses_all_row_keys(self) -> None:
        """DataTable 不得只读取第一行字段。"""
        logger.debug("test_data_table_uses_all_row_keys 入口")
        try:
            # Arrange
            source = Path("frontend/src/components/DataTable.tsx").read_text(encoding="utf-8")

            # Act / Assert
            assert "Object.keys(sliced[0]" not in source
            assert "new Set<string>()" in source
            assert "Object.keys(row)" in source
            logger.info("test_data_table_uses_all_row_keys 完成")
        except Exception as exc:
            logger.error("test_data_table_uses_all_row_keys 异常: %s", exc, exc_info=True)
            raise

    # 方法作用：验证结果卡展示多源 SQL，并隐藏 table 类型的空图表面板。
    # Args: self - pytest 测试类实例。
    # Returns: 无返回值，断言失败时由 pytest 报告。
    def test_result_card_uses_sql_statements_and_hides_table_chart(self) -> None:
        """table 结果只展示数据表，多源 SQL 按来源分别展示。"""
        logger.debug("test_result_card_uses_sql_statements_and_hides_table_chart 入口")
        try:
            # Arrange
            source = Path("frontend/src/components/ResultCard.tsx").read_text(encoding="utf-8")
            logger.info(
                "结果卡图表可见性边界 legacy=%s visible_set=%s table_visible=%s render_uses_result=%s",
                "chartConfig.type !== 'table'" in source,
                "visibleChartTypes" in source,
                "['line', 'bar', 'pie', 'scatter', 'heatmap', 'table']" in source,
                "shouldShowChart && chartConfig" in source,
            )

            # Act / Assert
            assert "sql_statements" in source
            assert (
                "const visibleChartTypes = new Set(['line', 'bar', 'pie', 'scatter', 'heatmap']);"
                in source
            )
            assert (
                "visibleChartTypes.has(chartConfig.type) || chartConfig.status === 'failed'"
                in source
            )
            logger.info("test_result_card_uses_sql_statements_and_hides_table_chart 完成")
        except Exception as exc:
            logger.error(
                "test_result_card_uses_sql_statements_and_hides_table_chart 异常: %s",
                exc,
                exc_info=True,
            )
            raise

    # 方法作用：验证指标目录走结构化结果卡并渲染主题与指标明细。
    # Args: self - pytest 测试类实例。
    # Returns: 无返回值，断言失败时由 pytest 报告。
    def test_indicator_catalog_renders_structured_groups(self) -> None:
        """指标目录不得退化为只显示数量摘要的简化直接回答卡。"""
        logger.debug("test_indicator_catalog_renders_structured_groups 入口")
        try:
            # Arrange
            page = Path("frontend/src/pages/ChatPage.tsx").read_text(encoding="utf-8")
            result_card = Path("frontend/src/components/ResultCard.tsx").read_text(
                encoding="utf-8",
            )

            # Act / Assert
            assert "const isLLMDirect = finalResult?.source === 'llm_direct';" in page
            assert "analysis?.indicator_groups" in result_card
            assert "indicatorGroups.map" in result_card
            assert "group.indicators.map" in result_card
            logger.info("test_indicator_catalog_renders_structured_groups 完成")
        except Exception as exc:
            logger.error(
                "test_indicator_catalog_renders_structured_groups 异常: %s",
                exc,
                exc_info=True,
            )
            raise

    # 方法作用：验证 SQL 面板为高亮根节点和未分类文本提供可读前景色。
    # Args: self - pytest 测试类实例。
    # Returns: 无返回值，断言失败时由 pytest 报告。
    def test_sql_panel_keeps_plain_sql_text_visible(self) -> None:
        """未被 Highlight.js 包裹的字段名和标点在深色背景上也必须可见。"""
        logger.debug("test_sql_panel_keeps_plain_sql_text_visible 入口")
        try:
            # Arrange / Act
            component = Path("frontend/src/components/SqlPanel.tsx").read_text(encoding="utf-8")
            stylesheet = Path("frontend/src/index.css").read_text(encoding="utf-8")

            # Assert
            assert '<code ref={codeRef} className="hljs" />' in component
            assert ".sql-block" in stylesheet
            assert "color: #c9d1d9" in stylesheet
            logger.info("test_sql_panel_keeps_plain_sql_text_visible 完成")
        except Exception as exc:
            logger.error(
                "test_sql_panel_keeps_plain_sql_text_visible 异常: %s",
                exc,
                exc_info=True,
            )
            raise

    # 方法作用：验证聊天 Hook 按 stream_id 隔离并行推理和内容流。
    # Args: self - pytest 测试类实例。
    # Returns: 无返回值，断言失败时由 pytest 报告。
    def test_use_chat_groups_parallel_streams(self) -> None:
        """并行模型调用不得继续直接拼接到单一 reasoning/tokens 字符串。"""
        logger.debug("test_use_chat_groups_parallel_streams 入口")
        try:
            # Arrange
            source = Path("frontend/src/hooks/useChat.ts").read_text(encoding="utf-8")

            # Act / Assert
            assert "streamBuffers" in source
            assert "e.stream_id" in source
            assert "reasoning: a.reasoning +" not in source
            assert "tokens: a.tokens +" not in source
            logger.info("test_use_chat_groups_parallel_streams 完成")
        except Exception as exc:
            logger.error("test_use_chat_groups_parallel_streams 异常: %s", exc, exc_info=True)
            raise

    # 方法作用：验证历史会话恢复逐轮使用各自的结构化响应。
    # Args: self - pytest 测试类实例。
    # Returns: 无返回值，断言失败时由 pytest 报告。
    def test_use_chat_restores_each_turn_final_result(self) -> None:
        """前端不得只给最后一轮注入富数据或把最后 SQL 注入所有轮次。"""
        logger.debug("test_use_chat_restores_each_turn_final_result 入口")
        try:
            # Arrange
            source = Path("frontend/src/hooks/useChat.ts").read_text(encoding="utf-8")

            # Act / Assert
            assert "d.final_result" in source
            assert "isLast && latest" not in source
            assert "sql: latest?.sql || d.sql" not in source
            assert "savedTurns.map((d, i)" not in source
            logger.info("test_use_chat_restores_each_turn_final_result 完成")
        except Exception as exc:
            logger.error(
                "test_use_chat_restores_each_turn_final_result 异常: %s", exc, exc_info=True,
            )
            raise

    # 方法作用：验证结果卡展示触点归因模型、资格和确定性明细。
    # Args: self - pytest 测试类实例。
    # Returns: 无返回值，断言失败时由 pytest 报告。
    def test_result_card_renders_touchpoint_attribution(self) -> None:
        """功能 18.2.13 不能退化为只显示模型生成的摘要。"""
        # Arrange
        result_card = Path("frontend/src/components/ResultCard.tsx").read_text(
            encoding="utf-8",
        )
        types = Path("frontend/src/types/index.ts").read_text(encoding="utf-8")

        # Act / Assert
        assert "TouchpointAttributionAnalysis" in types
        assert "analysis?.attribution" in result_card
        assert "attribution.selection_source" in result_card
        assert "归因证据不足" in result_card
        assert "attributionRows" in result_card
        assert "attributed_value" in result_card
        assert "conversion_credit" in result_card
        assert "chartRows" in result_card
