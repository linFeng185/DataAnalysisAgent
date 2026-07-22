"""长期记忆回退路径日志回归测试。"""

from __future__ import annotations

import logging
from unittest.mock import AsyncMock, MagicMock


logger = logging.getLogger(__name__)


class TestLongTermStoreFallback:
    """覆盖偏好查询双存储故障的可见回退。"""

    # 方法作用：验证 ChromaDB 偏好查询异常返回空字典时记录堆栈。
    # Args: self - pytest 测试类实例；monkeypatch - pytest 补丁工具。
    # Returns: 无返回值，断言失败时由 pytest 报告。
    async def test_chroma_preference_failure_logs_exception(self, monkeypatch) -> None:
        """向量存储故障不得伪装成用户没有偏好。"""
        logger.debug("test_chroma_preference_failure_logs_exception 入口")
        try:
            # Arrange
            import src.memory.long_term_store as store_module
            import src.memory.vector_store as vector_module

            captured_logger = MagicMock()
            monkeypatch.setattr(store_module, "logger", captured_logger)
            monkeypatch.setattr(
                vector_module,
                "get_vector_store",
                AsyncMock(side_effect=RuntimeError("vector unavailable")),
            )
            store = store_module.LongTermMemoryStore()

            # Act
            result = await store._get_prefs_from_chroma("user-1")  # noqa: SLF001

            # Assert
            assert result == {}
            captured_logger.error.assert_called_once()
            assert captured_logger.error.call_args.kwargs["exc_info"] is True
            logger.info("test_chroma_preference_failure_logs_exception 完成")
        except Exception as exc:
            logger.error(
                "test_chroma_preference_failure_logs_exception 异常: %s",
                exc,
                exc_info=True,
            )
            raise
