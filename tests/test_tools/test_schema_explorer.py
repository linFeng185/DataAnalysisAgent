"""SchemaExplorerTool 异步边界测试。"""

from __future__ import annotations

import logging
from types import SimpleNamespace
from unittest.mock import AsyncMock


logger = logging.getLogger(__name__)


class TestSchemaExplorerTool:
    """覆盖同步拒绝、Schema 成功、空结果和异常回退。"""

    # 方法作用：验证同步入口和异步成功路径。
    # Args: self - pytest 测试类实例；monkeypatch - pytest 补丁工具。
    # Returns: 无返回值，断言失败时由 pytest 报告。
    async def test_sync_rejection_and_async_schema(self, monkeypatch) -> None:
        """异步入口必须返回 Schema prompt，缺省数据源使用实例配置。"""
        logger.debug("test_sync_rejection_and_async_schema 入口")
        import src.knowledge.schema_manager as schema_module
        from src.tools.schema_explorer import SchemaExplorerTool

        schema = SimpleNamespace(tables=[object()], to_prompt_text=lambda: "orders(id INTEGER)")
        manager = SimpleNamespace(get_or_fetch_schema=AsyncMock(return_value=schema))
        monkeypatch.setattr(schema_module, "get_schema_manager", lambda: manager)
        tool = SchemaExplorerTool(datasource="demo")

        assert "仅支持异步" in tool._run()  # noqa: SLF001
        assert await tool._arun(query="订单") == "orders(id INTEGER)"  # noqa: SLF001
        manager.get_or_fetch_schema.assert_awaited_once_with("demo", user_query="订单")
        logger.info("test_sync_rejection_and_async_schema 完成")

    # 方法作用：验证空 Schema 与 Manager 异常返回明确文本。
    # Args: self - pytest 测试类实例；monkeypatch - pytest 补丁工具。
    # Returns: 无返回值，断言失败时由 pytest 报告。
    async def test_empty_and_failure_fallbacks(self, monkeypatch) -> None:
        """Agent 必须能区分无表和加载故障。"""
        logger.debug("test_empty_and_failure_fallbacks 入口")
        import src.knowledge.schema_manager as schema_module
        from src.tools.schema_explorer import SchemaExplorerTool

        manager = SimpleNamespace(get_or_fetch_schema=AsyncMock(return_value=None))
        monkeypatch.setattr(schema_module, "get_schema_manager", lambda: manager)
        tool = SchemaExplorerTool()
        assert await tool._arun("demo") == "未找到表结构信息"  # noqa: SLF001

        manager.get_or_fetch_schema.side_effect = RuntimeError("schema unavailable")
        assert "schema unavailable" in await tool._arun("demo")  # noqa: SLF001
        logger.info("test_empty_and_failure_fallbacks 完成")
