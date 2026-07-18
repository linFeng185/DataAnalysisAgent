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

            # Act / Assert
            assert "sql_statements" in source
            assert "chartConfig.type !== 'table'" in source
            logger.info("test_result_card_uses_sql_statements_and_hides_table_chart 完成")
        except Exception as exc:
            logger.error(
                "test_result_card_uses_sql_statements_and_hides_table_chart 异常: %s",
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
