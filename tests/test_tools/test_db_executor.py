"""数据库执行与 EXPLAIN Tool 测试。"""

from __future__ import annotations

import logging
from types import SimpleNamespace
from unittest.mock import AsyncMock


logger = logging.getLogger(__name__)


class TestDBTools:
    """覆盖同步拒绝、SQL 校验、执行和 EXPLAIN。"""

    # 方法作用：验证两个 Tool 的同步入口明确拒绝。
    # Args: self - pytest 测试类实例。
    # Returns: 无返回值，断言失败时由 pytest 报告。
    def test_sync_entrypoints_are_rejected(self) -> None:
        """同步调用不能创建嵌套事件循环。"""
        logger.debug("test_sync_entrypoints_are_rejected 入口")
        from src.tools.db_executor import DBExecutorTool, DBExplainTool

        assert DBExecutorTool()._run("SELECT 1")["success"] is False  # noqa: SLF001
        assert DBExplainTool()._run("SELECT 1")["success"] is False  # noqa: SLF001
        logger.info("test_sync_entrypoints_are_rejected 完成")

    # 方法作用：验证执行和 EXPLAIN 委托 Connector 并返回结构化结果。
    # Args: self - pytest 测试类实例；monkeypatch - pytest 补丁工具。
    # Returns: 无返回值，断言失败时由 pytest 报告。
    async def test_async_execution_and_explain(self, monkeypatch) -> None:
        """合法 SQL 必须解析数据源并调用统一 Connector。"""
        logger.debug("test_async_execution_and_explain 入口")
        import src.connectors.registry as connector_registry
        import src.datasource.registry as datasource_registry
        from src.tools.db_executor import DBExecutorTool, DBExplainTool

        datasource = SimpleNamespace(engine=object())
        registry = SimpleNamespace(resolve=AsyncMock(return_value=datasource))
        connector = SimpleNamespace(
            _engine=None,
            execute=AsyncMock(return_value=[{"value": 1}]),
            explain=AsyncMock(return_value={"valid": True, "plan": "ok"}),
        )
        monkeypatch.setattr(datasource_registry, "get_registry", lambda: registry)
        monkeypatch.setattr(connector_registry, "create_connector", lambda config: connector)

        execution = await DBExecutorTool()._arun("SELECT 1", "demo")  # noqa: SLF001
        explain = await DBExplainTool()._arun("SELECT 1", "demo")  # noqa: SLF001

        assert execution == {"success": True, "data": [{"value": 1}], "row_count": 1}
        assert explain["success"] is True
        connector.execute.assert_awaited_once_with("SELECT 1")
        connector.explain.assert_awaited_once_with("SELECT 1")
        logger.info("test_async_execution_and_explain 完成")

    # 方法作用：验证危险 SQL 在连接数据库前被拒绝。
    # Args: self - pytest 测试类实例。
    # Returns: 无返回值，断言失败时由 pytest 报告。
    async def test_invalid_sql_is_rejected(self) -> None:
        """DELETE 不能进入 Registry 或 Connector。"""
        logger.debug("test_invalid_sql_is_rejected 入口")
        from src.tools.db_executor import DBExecutorTool

        result = await DBExecutorTool()._arun("DELETE FROM users", "demo")  # noqa: SLF001

        assert result["success"] is False
        assert result["error"] == "SQL 校验失败"
        logger.info("test_invalid_sql_is_rejected 完成")
