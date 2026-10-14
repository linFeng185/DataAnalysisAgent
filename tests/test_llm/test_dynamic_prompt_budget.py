"""覆盖 spec/53：提示词预算按超管模型窗口动态推导。"""

from __future__ import annotations

import contextlib
from unittest.mock import patch

import pytest

from src.config import get_settings
from src.llm.prompt_budget import resolve_dynamic_budget
from src.llm.prompts import PROMPT_REGISTRY, PromptDefinition, register_prompt


@contextlib.contextmanager
def _fake_window(usable_tokens: int):
    """用假可用输入 token 替换会话层预算解析，隔离真实配置链。"""

    class _Budget:
        pass

    budget = _Budget()
    budget.usable_input_tokens = usable_tokens
    with patch("src.memory.context_builder.get_context_budget", return_value=budget):
        yield


class TestDynamicPromptBudget:
    """覆盖窗口阶梯、系数覆盖与缺失回退。"""

    # 方法作用：构造带设计上限的临时提示词定义。
    # Args: cap - 设计上限字符数。
    # Returns: 已注册的 PromptDefinition。
    def _definition(self, cap: int) -> PromptDefinition:
        definition = PromptDefinition(
            prompt_id="budget.dynamic",
            version="1.0.0",
            task="test",
            template="BASE",
            context_budget=cap,
            system_policy="S",
            capability_policy="C",
        )
        register_prompt(definition)
        return definition

    def teardown_method(self) -> None:
        PROMPT_REGISTRY.pop("budget.dynamic", None)

    # 方法作用：把全局预算参数钉死为默认值，隔离宿主环境配置。
    # Args: monkeypatch - pytest 补丁器。
    # Returns: 无返回值。
    def _pin_budget_settings(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """稳定断言期望值，避免环境自定义系数或下限改变推导结果。"""
        settings = get_settings()
        monkeypatch.setattr(settings, "prompt_budget_token_char_factor", 1.0)
        monkeypatch.setattr(settings, "prompt_budget_min_chars", 8000)

    # 验证 128K 窗口推导值大于设计上限时封顶到上限。
    def test_large_window_caps_at_registered_upper(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """窗口再大也不突破设计上限。"""
        # Arrange
        self._pin_budget_settings(monkeypatch)
        definition = self._definition(50_000)

        # Act
        with _fake_window(108_000):
            effective = resolve_dynamic_budget(definition)

        # Assert
        assert effective == 50_000

    # 验证 1M 窗口同样被设计上限封顶。
    def test_million_window_still_capped(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """1M 窗口推导值近百万字符，仍取设计上限。"""
        # Arrange
        self._pin_budget_settings(monkeypatch)
        definition = self._definition(50_000)

        # Act
        with _fake_window(979_808):
            effective = resolve_dynamic_budget(definition)

        # Assert
        assert effective == 50_000

    # 验证小窗口收缩到窗口推导值。
    def test_small_window_converges_below_cap(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """32K 窗口下预算自动收缩，不超窗。"""
        # Arrange
        self._pin_budget_settings(monkeypatch)
        definition = self._definition(85_000)

        # Act
        with _fake_window(12_576):
            effective = resolve_dynamic_budget(definition)

        # Assert
        assert effective == 12_576

    # 验证极小窗口命中全局下限。
    def test_tiny_window_hits_global_floor(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """可用输入不足时不得推导出负数或装不下固定提示词的预算。"""
        # Arrange
        self._pin_budget_settings(monkeypatch)
        definition = self._definition(85_000)

        # Act
        with _fake_window(1):
            effective = resolve_dynamic_budget(definition)

        # Assert
        assert effective == 8_000

    # 验证模型能力声明的换算系数优先于全局默认。
    def test_model_factor_overrides_global_default(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """中文费 token 的 tokenizer 供应商把系数调小后预算随之收缩。"""
        # Arrange
        self._pin_budget_settings(monkeypatch)
        definition = self._definition(500_000)

        # Act / Assert
        with _fake_window(30_000):
            with patch(
                "src.memory.context_builder.resolve_prompt_budget_factor",
                return_value=0.5,
            ):
                assert resolve_dynamic_budget(definition) == 15_000

    # 验证系数缺失时回退全局默认 1.0。
    def test_missing_factor_falls_back_to_default(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """能力链读不到系数时按 1 token = 1 字符保守折算。"""
        # Arrange
        self._pin_budget_settings(monkeypatch)
        definition = self._definition(500_000)

        # Act
        with _fake_window(30_000):
            effective = resolve_dynamic_budget(definition)

        # Assert
        assert effective == 30_000


class TestToolLoopBudgetPropagation:
    """覆盖 spec/53：知识工具循环的消息预算必须跟随本次调用的动态预算。"""

    # 方法作用：验证工具循环消息预算按动态推导值而不是注册上限生效。
    # Args: self - pytest 测试类实例；monkeypatch - 补丁器。
    # Returns: 无返回值，断言失败时由 pytest 报告。
    def test_message_budget_follows_dynamic_budget(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """32K 窗口收敛到 12576 字符时，工具循环不得仍按注册上限 85000 字符放行消息。"""
        # Arrange
        from types import SimpleNamespace

        from src.llm.invocation import prepare_invocation
        from src.llm.knowledge_loop import KnowledgeLoopConfig, _resolve_message_budget
        from src.llm.prompt_budget import PromptSection
        from src.llm.prompts import get_prompt_definition

        TestDynamicPromptBudget()._pin_budget_settings(monkeypatch)

        class _Budget:
            usable_input_tokens = 12_576

        monkeypatch.setattr(
            "src.memory.context_builder.get_context_budget", lambda **kwargs: _Budget()
        )
        monkeypatch.setattr(
            "src.memory.context_builder.resolve_prompt_budget_factor", lambda **kwargs: None
        )

        # Act
        prepared = prepare_invocation(
            "analysis.result",
            [PromptSection("query", "Q" * 500, priority=100, min_chars=100, max_chars=1000)],
            model=SimpleNamespace(),
        )
        effective = resolve_dynamic_budget(get_prompt_definition("analysis.result"))
        loop_budget = _resolve_message_budget(prepared, KnowledgeLoopConfig())

        # Assert
        assert effective == 12_576
        assert prepared.budget_chars == effective
        assert loop_budget == effective

    # 方法作用：验证旧构造方未携带预算快照时回退注册上限，不破坏既有协议。
    # Args: self - pytest 测试类实例。
    # Returns: 无返回值，断言失败时由 pytest 报告。
    def test_message_budget_falls_back_to_registered_cap(self) -> None:
        """直接构造的调用快照没有预算快照字段时，按注册上限推导以保持兼容。"""
        # Arrange
        from types import SimpleNamespace

        from src.llm.invocation import PreparedInvocation
        from src.llm.knowledge_loop import KnowledgeLoopConfig, _resolve_message_budget
        from src.llm.prompts import get_prompt_definition

        prepared = PreparedInvocation(
            prompt_id="analysis.result",
            prompt_version="1.0.0",
            task="analyze_result",
            model=SimpleNamespace(),
            messages=[],
            config={},
        )

        # Act
        loop_budget = _resolve_message_budget(prepared, KnowledgeLoopConfig())

        # Assert
        assert loop_budget == int(get_prompt_definition("analysis.result").context_budget)
