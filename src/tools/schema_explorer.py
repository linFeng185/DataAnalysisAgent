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

    # 方法作用：拒绝同步调用，避免在异步应用中创建嵌套事件循环。
    # Args: datasource - 数据源名称；query - 用户查询；run_manager - LangChain 同步回调管理器。
    # Returns: 指示调用方使用异步接口的错误文本。
    def _run(
        self,
        datasource: str = "",
        query: str = "",
        run_manager: Any = None,
    ) -> str:
        """同步入口只返回明确指引，生产链路统一使用 `_arun()`。"""
        datasource = datasource or self.datasource
        logger.debug("SchemaExplorerTool._run 入口", datasource=datasource, query=query[:80])
        result = "SchemaExplorerTool 仅支持异步调用，请使用 ainvoke()"
        logger.warning("SchemaExplorerTool._run 拒绝", datasource=datasource, reason="仅支持异步调用")
        return result

    # 方法作用：异步获取指定数据源的 Schema 文本。
    # Args: datasource - 数据源名称；query - 用户查询；run_manager - LangChain 异步回调管理器。
    # Returns: Schema 的 Prompt 文本或明确错误信息。
    async def _arun(
        self,
        datasource: str = "",
        query: str = "",
        run_manager: Any = None,
    ) -> str:
        """通过 SchemaManager 原生异步接口探索表结构。"""
        datasource = datasource or self.datasource
        logger.debug("SchemaExplorerTool._arun 入口", datasource=datasource, query=query[:80])
        try:
            from src.knowledge.schema_manager import get_schema_manager

            manager = get_schema_manager()
            schema = await manager.get_or_fetch_schema(
                datasource, user_query=query,
            )
            if schema and schema.tables:
                result = schema.to_prompt_text()
            else:
                result = "未找到表结构信息"
            logger.info("SchemaExplorerTool._arun 完成", datasource=datasource, result_chars=len(result))
            return result
        except Exception as exc:
            logger.error("SchemaExplorerTool._arun 失败", error=str(exc), exc_info=True)
            return f"获取表结构失败: {exc}"
