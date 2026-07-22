"""Checkpointer 事件循环兼容性测试。"""

from __future__ import annotations

import logging
from types import SimpleNamespace
from unittest.mock import Mock


logger = logging.getLogger(__name__)


class TestPostgresIdentifierSafety:
    """覆盖功能 7.1.3：自动建库必须安全引用数据库标识符。"""

    # 方法作用：验证 PostgreSQL 标识符中的双引号会被转义。
    # Args: self - pytest 测试类实例。
    # Returns: 无返回值，断言失败时由 pytest 报告。
    def test_database_identifier_is_quoted(self) -> None:
        """数据库名不得逃逸 CREATE DATABASE 标识符边界。"""
        logger.debug("test_database_identifier_is_quoted 入口")
        try:
            from src.memory.checkpointer import _quote_postgres_identifier

            result = _quote_postgres_identifier('app"; DROP DATABASE prod; --')

            assert result == '"app""; DROP DATABASE prod; --"'
            logger.info("test_database_identifier_is_quoted 完成")
        except Exception as exc:
            logger.error("test_database_identifier_is_quoted 异常: %s", exc, exc_info=True)
            raise


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
