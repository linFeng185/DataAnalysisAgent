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

    # 方法作用：拒绝同步调用，避免在异步应用中创建嵌套事件循环。
    # Args: query - 用户问题；datasource - 数据源名称；run_manager - LangChain 同步回调管理器。
    # Returns: 指示调用方使用异步接口的失败结果。
    def _run(
        self,
        query: str,
        datasource: str = "",
        run_manager: Any = None,
    ) -> dict:
        """同步入口只返回明确指引，生产链路统一使用 `_arun()`。"""
        datasource = datasource or self.datasource
        logger.debug("SQLGeneratorTool._run 入口", datasource=datasource, query=query[:120])
        result = {"success": False, "error": "SQLGeneratorTool 仅支持异步调用，请使用 ainvoke()"}
        logger.warning("SQLGeneratorTool._run 拒绝", datasource=datasource, reason="仅支持异步调用")
        return result

    # 方法作用：异步获取 Schema 并调用统一 LLM 工厂生成 SQL。
    # Args: query - 用户问题；datasource - 数据源名称；run_manager - LangChain 异步回调管理器。
    # Returns: 包含 SQL、推理和解释的结构化结果。
    async def _arun(
        self,
        query: str,
        datasource: str = "",
        run_manager: Any = None,
    ) -> dict:
        """使用项目原生异步链路执行 NL 到 SQL 转换。"""
        datasource = datasource or self.datasource
        logger.debug("SQLGeneratorTool._arun 入口", datasource=datasource, query=query[:120])
        try:
            from src.graph.nodes.generate_sql import _llm_generate, format_schema_for_prompt
            from src.knowledge.schema_manager import get_schema_manager
            from src.llm.client import is_task_llm_available
            from src.llm.prompts import get_dialect_cheatsheet

            if not is_task_llm_available("generate_sql"):
                logger.warning("SQLGeneratorTool._arun 回退", datasource=datasource, reason="任务模型不可用")
                return {"success": False, "error": "LLM 不可用"}

            manager = get_schema_manager()
            schema = await manager.get_or_fetch_schema(datasource, user_query=query)
            if not schema or not schema.tables:
                logger.warning("SQLGeneratorTool._arun 回退", datasource=datasource, reason="Schema 为空")
                return {"success": False, "error": "未找到表结构"}
            tables = [
                {
                    "name": table.name,
                    "description": table.description,
                    "columns": [
                        {"name": column.name, "type": column.type, "comment": column.comment}
                        for column in table.columns
                    ],
                }
                for table in schema.tables
            ]
            sql, reasoning, explanation = await _llm_generate(
                format_schema_for_prompt(tables),
                get_dialect_cheatsheet("mysql"),
                query,
                "",
                "",
                None,
            )
            result = {
                "success": True,
                "sql": sql,
                "reasoning": reasoning,
                "explanation": explanation,
            }
            logger.info("SQLGeneratorTool._arun 完成", datasource=datasource, sql_chars=len(sql))
            return result
        except Exception as exc:
            logger.error("SQLGeneratorTool._arun 失败", error=str(exc), exc_info=True)
            return {"success": False, "error": str(exc)}
