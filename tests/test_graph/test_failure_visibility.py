"""Graph 可恢复异常日志可见性回归测试。"""

from __future__ import annotations

import logging
from unittest.mock import AsyncMock, MagicMock


logger = logging.getLogger(__name__)


class TestGraphFallbackLogging:
    """覆盖 LLM 分类和枚举检索的可恢复回退日志。"""

    # 方法作用：验证意图分类模型异常回退时记录完整堆栈。
    # Args: self - pytest 测试类实例；monkeypatch - pytest 补丁工具。
    # Returns: 无返回值，断言失败时由 pytest 报告。
    async def test_intent_classification_fallback_logs_exception(self, monkeypatch) -> None:
        """LLM 分类故障可以回退规则结果，但不得静默。"""
        logger.debug("test_intent_classification_fallback_logs_exception 入口")
        try:
            # Arrange
            import src.graph.nodes.classify_intent as classify_module
            import src.llm.client as llm_client

            captured_logger = MagicMock()
            monkeypatch.setattr(classify_module, "logger", captured_logger)
            monkeypatch.setattr(
                llm_client,
                "is_task_llm_available",
                MagicMock(side_effect=RuntimeError("llm unavailable")),
            )

            # Act
            result = await classify_module._llm_classify("查询订单")  # noqa: SLF001

            # Assert
            assert result is None
            captured_logger.error.assert_called_once()
            assert captured_logger.error.call_args.kwargs["exc_info"] is True
            logger.info("test_intent_classification_fallback_logs_exception 完成")
        except Exception as exc:
            logger.error(
                "test_intent_classification_fallback_logs_exception 异常: %s",
                exc,
                exc_info=True,
            )
            raise

    # 方法作用：验证枚举值向量检索失败回退空字典时记录完整堆栈。
    # Args: self - pytest 测试类实例；monkeypatch - pytest 补丁工具。
    # Returns: 无返回值，断言失败时由 pytest 报告。
    async def test_enum_dictionary_fallback_logs_exception(self, monkeypatch) -> None:
        """枚举增强失败可以降级，但日志必须区分真实空数据与存储故障。"""
        logger.debug("test_enum_dictionary_fallback_logs_exception 入口")
        try:
            # Arrange
            import src.graph.nodes.retrieve_schema as retrieve_module
            import src.memory.vector_store as vector_module

            captured_logger = MagicMock()
            monkeypatch.setattr(retrieve_module, "logger", captured_logger)
            monkeypatch.setattr(
                vector_module,
                "get_vector_store",
                AsyncMock(side_effect=RuntimeError("vector unavailable")),
            )

            # Act
            result = await retrieve_module._load_enum_dictionary("demo", [])  # noqa: SLF001

            # Assert
            assert result == {}
            captured_logger.error.assert_called_once()
            assert captured_logger.error.call_args.kwargs["exc_info"] is True
            logger.info("test_enum_dictionary_fallback_logs_exception 完成")
        except Exception as exc:
            logger.error(
                "test_enum_dictionary_fallback_logs_exception 异常: %s",
                exc,
                exc_info=True,
            )
            raise
