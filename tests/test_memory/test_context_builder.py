"""LLM 上下文热温冷裁剪测试。"""

from __future__ import annotations

import logging
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

logger = logging.getLogger(__name__)


class TestContextBuilder:
    """覆盖上下文分层、预算裁剪和可恢复检索故障。"""

    # 方法作用：验证热温冷三层内容按配置组合。
    # Args: self - pytest 测试类实例；monkeypatch - pytest 补丁工具。
    # Returns: 无返回值，断言失败时由 pytest 报告。
    async def test_build_llm_context_combines_three_tiers(self, monkeypatch) -> None:
        """长历史必须包含温摘要、最近完整轮次和相关经验。"""
        logger.debug("test_build_llm_context_combines_three_tiers 入口")
        import src.memory.context_builder as module

        monkeypatch.setattr(module, "get_settings", lambda: SimpleNamespace(
            context_hot_turns=2,
            context_warm_turns=4,
            context_max_tokens=10_000,
        ))
        monkeypatch.setattr(module, "_summarize_turns", AsyncMock(return_value="温摘要"))
        store = SimpleNamespace(search=AsyncMock(return_value=[SimpleNamespace(
            payload={"question": "历史问题"},
            content="历史内容",
        )]))
        history = [
            {"user_query": f"问题{i}", "generated_sql": f"SELECT {i}", "analysis_summary": f"结论{i}"}
            for i in range(5)
        ]

        result = await module.build_llm_context(
            history,
            user_query="当前问题",
            long_term_store=store,
            node_name="generate_sql",
        )

        assert "[前序对话摘要] 温摘要" in result
        assert "用户: 问题4" in result
        assert "[历史相关经验] 历史问题" in result
        store.search.assert_awaited_once_with("当前问题", top_k=3)
        logger.info("test_build_llm_context_combines_three_tiers 完成")

    # 方法作用：验证冷数据检索故障可回退且记录完整堆栈。
    # Args: self - pytest 测试类实例；monkeypatch - pytest 补丁工具。
    # Returns: 无返回值，断言失败时由 pytest 报告。
    async def test_long_term_failure_logs_traceback_and_keeps_hot_context(self, monkeypatch) -> None:
        """向量库故障不能丢掉热上下文，也不能静默。"""
        logger.debug("test_long_term_failure_logs_traceback_and_keeps_hot_context 入口")
        import src.memory.context_builder as module

        monkeypatch.setattr(module, "get_settings", lambda: SimpleNamespace(
            context_hot_turns=1,
            context_warm_turns=2,
            context_max_tokens=10_000,
        ))
        monkeypatch.setattr(module, "_summarize_turns", AsyncMock(return_value="摘要"))
        captured_logger = MagicMock()
        monkeypatch.setattr(module, "logger", captured_logger)
        store = SimpleNamespace(search=AsyncMock(side_effect=RuntimeError("vector unavailable")))

        result = await module.build_llm_context(
            [{"user_query": "旧"}, {"user_query": "新"}, {"user_query": "最新"}],
            user_query="当前",
            long_term_store=store,
        )

        assert "用户: 最新" in result
        captured_logger.error.assert_called_once()
        assert captured_logger.error.call_args.kwargs["exc_info"] is True
        logger.info("test_long_term_failure_logs_traceback_and_keeps_hot_context 完成")

    # 方法作用：验证 Token 估算和超预算裁剪边界。
    # Args: self - pytest 测试类实例；monkeypatch - pytest 补丁工具。
    # Returns: 无返回值，断言失败时由 pytest 报告。
    async def test_token_budget_keeps_only_latest_turn(self, monkeypatch) -> None:
        """超预算后必须至少保留最新问题，不能返回无限上下文。"""
        logger.debug("test_token_budget_keeps_only_latest_turn 入口")
        import src.memory.context_builder as module

        monkeypatch.setattr(module, "get_settings", lambda: SimpleNamespace(
            context_hot_turns=2,
            context_warm_turns=3,
            context_max_tokens=1,
        ))
        monkeypatch.setattr(module, "_summarize_turns", AsyncMock(return_value="摘要"))

        result = await module.build_llm_context([
            {"user_query": "第一轮"},
            {"user_query": "第二轮"},
            {"user_query": "最新一轮", "generated_sql": "SELECT 1"},
        ])

        assert "用户: 最新一轮" in result
        assert "用户: 第二轮" not in result
        assert module.estimate_tokens("") == 0
        assert module.estimate_tokens("测试 abc") > 0
        logger.info("test_token_budget_keeps_only_latest_turn 完成")

    # 方法作用：验证动态预算读取模型窗口并扣除输入输出预留。
    # Args: self - pytest 测试类实例；monkeypatch - pytest 补丁工具。
    # Returns: 无返回值，断言失败时由 pytest 报告。
    def test_context_budget_uses_model_window_and_hysteresis(self, monkeypatch) -> None:
        """DeepSeek 1M 窗口应按 80% 触发、55% 回落并保留输出空间。"""
        # Arrange
        import src.memory.context_builder as module

        monkeypatch.setattr(module, "get_settings", lambda: SimpleNamespace(
            context_max_tokens=0,
            context_prompt_reserve_tokens=12_000,
            context_output_reserve_tokens=8_192,
            context_summary_reserve_tokens=1_024,
            context_compression_trigger_ratio=0.80,
            context_compression_target_ratio=0.55,
            context_hard_limit_ratio=0.92,
        ))

        # Act
        budget = module.get_context_budget(1_000_000, model_id="deepseek-v4-pro")

        # Assert
        assert budget.context_window == 1_000_000
        assert budget.usable_input_tokens == 979_808
        assert budget.trigger_tokens == 783_846
        assert budget.target_tokens == 538_894
        assert budget.hard_limit_tokens == 901_424

    # 方法作用：验证模型注册表中的上下文能力可以作为动态预算来源。
    # Args: self - pytest 测试类实例；monkeypatch - pytest 补丁工具。
    # Returns: 无返回值，断言失败时由 pytest 报告。
    def test_context_budget_reads_registered_model_window(self, monkeypatch) -> None:
        """未显式传窗口时，DeepSeek V4 Pro 应解析为 1M。"""
        # Arrange
        import src.memory.context_builder as module

        monkeypatch.setattr(module, "get_settings", lambda: SimpleNamespace(
            context_max_tokens=0,
            context_default_window=128_000,
            context_prompt_reserve_tokens=12_000,
            context_output_reserve_tokens=8_192,
            context_summary_reserve_tokens=1_024,
            context_compression_trigger_ratio=0.80,
            context_compression_target_ratio=0.55,
            context_hard_limit_ratio=0.92,
        ))
        monkeypatch.setattr(
            module,
            "_resolve_model_identity",
            lambda model_id=None, model=None: ("deepseek-v4-pro", {
                "context_window": 1_000_000,
                "max_tokens_limit": 8_192,
            }),
        )

        # Act
        budget = module.get_context_budget(model_id="deepseek-v4-pro")

        # Assert
        assert budget.context_window == 1_000_000
        assert budget.model_id == "deepseek-v4-pro"
