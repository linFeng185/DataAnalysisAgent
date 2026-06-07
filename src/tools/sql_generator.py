"""5.1.2 SQLGeneratorTool — 封装 SQL 生成逻辑供 Agent 调用。"""

from __future__ import annotations

from typing import Any

from langchain_core.tools import BaseTool

from src.logging_config import get_logger

logger = get_logger(__name__)


class SQLGeneratorTool(BaseTool):
    """NL → SQL 生成工具 — 将自然语言查询转换为 SQL。"""

    name: str = "sql_generator"
    description: str = (
        "将自然语言查询转换为 SQL 语句。"
        "输入: {\"query\": \"用户问题\", \"datasource\": \"名称\"}。"
        "返回: 生成的 SQL 及推理过程。"
    )
    datasource: str = ""

    def _run(
        self,
        query: str,
        datasource: str = "",
        run_manager: Any = None,
    ) -> dict:
        datasource = datasource or self.datasource
        logger.info("SQL 生成工具调用", datasource=datasource, query=query[:120])

        try:
            import asyncio

            from src.graph.nodes.generate_sql import _llm_generate, format_schema_for_prompt
            from src.knowledge.schema_manager import get_schema_manager
            from src.llm.client import is_llm_available
            from src.llm.prompts import get_dialect_cheatsheet

            if not is_llm_available():
                return {"success": False, "error": "LLM 不可用"}

            async def _gen():
                manager = get_schema_manager()
                schema = await manager.get_or_fetch_schema(datasource, user_query=query)
                if not schema or not schema.tables:
                    return {"success": False, "error": "未找到表结构"}
                tables = [
                    {"name": t.name, "description": t.description,
                     "columns": [{"name": c.name, "type": c.type, "comment": c.comment}
                                 for c in t.columns]}
                    for t in schema.tables
                ]
                sql, reasoning = await _llm_generate(
                    format_schema_for_prompt(tables),
                    get_dialect_cheatsheet("mysql"), query,
                    "", "", None,
                )
                return {"success": True, "sql": sql, "reasoning": reasoning}

            return asyncio.run(_gen())
        except Exception as e:
            logger.error("SQL 生成失败", error=str(e))
            return {"success": False, "error": str(e)}
