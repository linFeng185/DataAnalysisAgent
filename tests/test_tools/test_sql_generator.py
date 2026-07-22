"""SQLGeneratorTool 统一 LLM 工厂委托测试。"""

from __future__ import annotations

import logging
from types import SimpleNamespace
from unittest.mock import AsyncMock


logger = logging.getLogger(__name__)


class TestSQLGeneratorTool:
    """覆盖同步拒绝、模型不可用、Schema 空和生成成功。"""

    # 方法作用：验证同步入口和模型不可用回退。
    # Args: self - pytest 测试类实例；monkeypatch - pytest 补丁工具。
    # Returns: 无返回值，断言失败时由 pytest 报告。
    async def test_sync_and_llm_unavailable(self, monkeypatch) -> None:
        """模型不可用时不得尝试加载 Schema。"""
        logger.debug("test_sync_and_llm_unavailable 入口")
        import src.llm.client as llm_module
        from src.tools.sql_generator import SQLGeneratorTool

        monkeypatch.setattr(llm_module, "is_task_llm_available", lambda task: False)
        tool = SQLGeneratorTool(datasource="demo")

        assert tool._run("问题")["success"] is False  # noqa: SLF001
        result = await tool._arun("问题")  # noqa: SLF001
        assert result == {"success": False, "error": "LLM 不可用"}
        logger.info("test_sync_and_llm_unavailable 完成")

    # 方法作用：验证空 Schema 失败关闭。
    # Args: self - pytest 测试类实例；monkeypatch - pytest 补丁工具。
    # Returns: 无返回值，断言失败时由 pytest 报告。
    async def test_empty_schema_is_rejected(self, monkeypatch) -> None:
        """没有可信表结构时不能让 LLM 自由生成 SQL。"""
        logger.debug("test_empty_schema_is_rejected 入口")
        import src.knowledge.schema_manager as schema_module
        import src.llm.client as llm_module
        from src.tools.sql_generator import SQLGeneratorTool

        monkeypatch.setattr(llm_module, "is_task_llm_available", lambda task: True)
        monkeypatch.setattr(
            schema_module,
            "get_schema_manager",
            lambda: SimpleNamespace(get_or_fetch_schema=AsyncMock(return_value=None)),
        )

        result = await SQLGeneratorTool()._arun("问题", "demo")  # noqa: SLF001

        assert result == {"success": False, "error": "未找到表结构"}
        logger.info("test_empty_schema_is_rejected 完成")

    # 方法作用：验证 Schema 转换后委托统一生成节点并返回三段结果。
    # Args: self - pytest 测试类实例；monkeypatch - pytest 补丁工具。
    # Returns: 无返回值，断言失败时由 pytest 报告。
    async def test_successful_generation(self, monkeypatch) -> None:
        """工具不得创建独立 LLM 客户端，必须复用 `_llm_generate`。"""
        logger.debug("test_successful_generation 入口")
        import src.graph.nodes.generate_sql as generate_module
        import src.knowledge.schema_manager as schema_module
        import src.llm.client as llm_module
        from src.datasource.schema_snapshot import ColumnInfo, SchemaSnapshot, TableSchema
        from src.tools.sql_generator import SQLGeneratorTool

        schema = SchemaSnapshot(tables=[TableSchema(
            name="orders",
            description="订单",
            columns=[ColumnInfo(name="amount", type="REAL", comment="金额")],
        )])
        monkeypatch.setattr(llm_module, "is_task_llm_available", lambda task: True)
        monkeypatch.setattr(
            schema_module,
            "get_schema_manager",
            lambda: SimpleNamespace(get_or_fetch_schema=AsyncMock(return_value=schema)),
        )
        generate = AsyncMock(return_value=("SELECT SUM(amount) FROM orders", "推理", "解释"))
        monkeypatch.setattr(generate_module, "_llm_generate", generate)

        result = await SQLGeneratorTool()._arun("总金额", "demo")  # noqa: SLF001

        assert result == {
            "success": True,
            "sql": "SELECT SUM(amount) FROM orders",
            "reasoning": "推理",
            "explanation": "解释",
        }
        generate.assert_awaited_once()
        logger.info("test_successful_generation 完成")
