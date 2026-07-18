"""Checkpointer 事件循环兼容性测试。"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import Mock


class TestCheckpointerEventLoop:
    """覆盖 Windows psycopg 异步事件循环适配。"""

    def test_windows_proactor_switches_to_selector(self, monkeypatch):
        """Windows Proactor 策略应切换为 Selector 策略。"""
        import src.memory.checkpointer as module

        class FakeProactor:
            """模拟当前 Proactor 策略。"""

        class FakeSelector:
            """模拟兼容 psycopg 的 Selector 策略。"""

        current = FakeProactor()
        setter = Mock()
        monkeypatch.setattr(module.sys, "platform", "win32")
        monkeypatch.setattr(module, "asyncio", SimpleNamespace(
            WindowsProactorEventLoopPolicy=FakeProactor,
            WindowsSelectorEventLoopPolicy=FakeSelector,
            get_event_loop_policy=lambda: current,
            set_event_loop_policy=setter,
        ))

        module.configure_asyncio_event_loop()

        setter.assert_called_once()
        assert isinstance(setter.call_args.args[0], FakeSelector)

    def test_non_windows_keeps_default_policy(self, monkeypatch):
        """非 Windows 环境不应修改事件循环策略。"""
        import src.memory.checkpointer as module

        setter = Mock()
        monkeypatch.setattr(module.sys, "platform", "linux")
        monkeypatch.setattr(module, "asyncio", SimpleNamespace(set_event_loop_policy=setter))

        module.configure_asyncio_event_loop()

        setter.assert_not_called()

    def test_main_factory_returns_selector_loop(self):
        """Uvicorn 工厂应显式创建 SelectorEventLoop。"""
        from src.main import selector_event_loop_factory

        loop = selector_event_loop_factory()
        try:
            assert "SelectorEventLoop" in type(loop).__name__
        finally:
            loop.close()
