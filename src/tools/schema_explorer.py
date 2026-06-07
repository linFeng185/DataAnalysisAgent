"""5.1.1 SchemaExplorerTool — 封装 SchemaManager 供 Agent 调用。"""

from __future__ import annotations

from typing import Any

from langchain_core.tools import BaseTool

from src.logging_config import get_logger

logger = get_logger(__name__)


class SchemaExplorerTool(BaseTool):
    """数据表结构探索工具 — 查询指定数据源的表结构和字段信息。

    用于 Agent 场景，LLM 可调用此工具了解有哪些表可用、表结构什么样。
    """

    name: str = "schema_explorer"
    description: str = (
        "获取数据源的表结构信息，包括所有表名、字段名、字段类型和注释。"
        "输入: JSON 格式 {\"datasource\": \"数据源名称\", \"query\": \"用户查询\"}。"
        "query 参数用于语义筛选相关表，不传则返回全部表。"
        "返回: 表结构和字段的 Markdown 格式文本。"
    )
    datasource: str = ""

    def _run(
        self,
        datasource: str = "",
        query: str = "",
        run_manager: Any = None,
    ) -> str:
        datasource = datasource or self.datasource
        logger.info("Schema 探索工具调用", datasource=datasource, query=query[:80])

        try:
            from src.knowledge.schema_manager import get_schema_manager

            manager = get_schema_manager()
            import asyncio
            schema = asyncio.run(manager.get_or_fetch_schema(
                datasource, user_query=query,
            ))
            if schema and schema.tables:
                return schema.to_prompt_text()
            return "未找到表结构信息"
        except Exception as e:
            logger.error("Schema 探索失败", error=str(e))
            return f"获取表结构失败: {e}"
