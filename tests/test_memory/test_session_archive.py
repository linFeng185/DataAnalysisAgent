"""会话归档与记忆维护公共行为测试。"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock


logger = logging.getLogger(__name__)


class TestSessionArchive:
    """覆盖会话时间边界、轮次限制和摘要裁剪。"""

    # 方法作用：验证归档时间和最大轮次判断边界。
    # Args: self - pytest 测试类实例。
    # Returns: 无返回值，断言失败时由 pytest 报告。
    def test_archive_and_turn_limit_boundaries(self) -> None:
        """超时或达到 50 轮时返回 True，活跃短会话返回 False。"""
        logger.debug("test_archive_and_turn_limit_boundaries 入口")
        from src.memory.session_archive import check_archive_needed, check_turn_limit

        active = SimpleNamespace(
            last_active_at=datetime.now(),
            conversation_history=[object()] * 49,
        )
        stale = SimpleNamespace(
            last_active_at=datetime.now() - timedelta(minutes=31),
            conversation_history=[object()] * 50,
        )

        assert check_archive_needed(active) is False
        assert check_turn_limit(active) is False
        assert check_archive_needed(stale) is True
        assert check_turn_limit(stale) is True
        logger.info("test_archive_and_turn_limit_boundaries 完成")

    # 方法作用：验证模型不可用时规则摘要会移除指定旧轮次。
    # Args: self - pytest 测试类实例；monkeypatch - pytest 补丁工具。
    # Returns: 无返回值，断言失败时由 pytest 报告。
    async def test_summarize_old_turns_uses_rule_fallback(self, monkeypatch) -> None:
        """规则摘要必须保留成功计数并精确裁剪历史。"""
        logger.debug("test_summarize_old_turns_uses_rule_fallback 入口")
        import src.llm.client as llm_module
        from src.memory.models import ConversationTurn
        from src.memory.session_archive import summarize_old_turns

        monkeypatch.setattr(llm_module, "is_llm_available", lambda: False)
        context = SimpleNamespace(conversation_history=[
            ConversationTurn(1, "问题1", execution_success=True),
            ConversationTurn(2, "问题2", execution_success=False),
            ConversationTurn(3, "问题3", execution_success=True),
        ])

        result = await summarize_old_turns(context, count=2)

        assert "成功 1/2" in result
        assert [turn.turn_id for turn in context.conversation_history] == [3]
        assert await summarize_old_turns(SimpleNamespace(conversation_history=[])) == ""
        logger.info("test_summarize_old_turns_uses_rule_fallback 完成")

    # 方法作用：验证会话启动加载记忆成功和故障回退路径。
    # Args: self - pytest 测试类实例；monkeypatch - pytest 补丁工具。
    # Returns: 无返回值，断言失败时由 pytest 报告。
    async def test_on_session_start_success_and_failure_visibility(self, monkeypatch) -> None:
        """存储故障可回退空结果，但必须记录完整堆栈。"""
        logger.debug("test_on_session_start_success_and_failure_visibility 入口")
        import src.memory.session_archive as module

        store = SimpleNamespace(
            get_preferences=AsyncMock(return_value={"language": "zh"}),
            search=AsyncMock(return_value=["memory"]),
        )
        success = await module.on_session_start("u1", "query", store)
        assert success == {
            "preferences": {"language": "zh"},
            "related_memories": ["memory"],
        }

        captured_logger = MagicMock()
        monkeypatch.setattr(module, "logger", captured_logger)
        broken = SimpleNamespace(
            get_preferences=AsyncMock(side_effect=RuntimeError("store unavailable")),
            search=AsyncMock(),
        )
        fallback = await module.on_session_start("u1", "query", broken)

        assert fallback == {"preferences": {}, "related_memories": []}
        captured_logger.error.assert_called_once()
        assert captured_logger.error.call_args.kwargs["exc_info"] is True
        logger.info("test_on_session_start_success_and_failure_visibility 完成")


class TestSessionMaintenance:
    """覆盖归档 SQL、无数据库边界和维护任务编排。"""

    # 方法作用：验证归档计数和全部维护任务结果。
    # Args: self - pytest 测试类实例。
    # Returns: 无返回值，断言失败时由 pytest 报告。
    async def test_archive_and_run_all(self) -> None:
        """数据库状态计数应转成 int，维护结果包含三类任务。"""
        logger.debug("test_archive_and_run_all 入口")
        from src.memory.session_archive import SessionMaintenance

        pool = SimpleNamespace(execute=AsyncMock(return_value="INSERT 0 3"))
        store = SimpleNamespace(
            decay_old_templates=AsyncMock(return_value=2),
            prune_low_confidence=AsyncMock(return_value=1),
        )
        maintenance = SessionMaintenance(pool, store)

        assert await maintenance.archive_sessions() == 3
        assert await maintenance.run_all() == {"decayed": 2, "pruned": 1, "archived": 3}
        assert await SessionMaintenance().archive_sessions() == 0
        logger.info("test_archive_and_run_all 完成")
