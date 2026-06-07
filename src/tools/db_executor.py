"""5.1.4 DBExecutorTool — 封装 SQL 执行逻辑供 Agent 调用。

依据: SPEC §8.3 MCP Agent Node 工具调用设计
"""

from __future__ import annotations

from typing import Any

from langchain_core.tools import BaseTool

from src.logging_config import get_logger

logger = get_logger(__name__)


class DBExecutorTool(BaseTool):
    """数据库查询执行工具 — 执行 SQL 并返回结果。"""

    name: str = "db_executor"
    description: str = (
        "在指定数据源上执行只读 SQL 查询。"
        "输入: JSON 格式 {\"sql\": \"SELECT ...\", \"datasource\": \"名称\"}。"
        "仅允许 SELECT/SHOW/DESCRIBE/EXPLAIN 语句。"
        "返回: 包含 success/data/row_count 的字典。"
    )
    datasource: str = ""

    def _run(
        self,
        sql: str,
        datasource: str = "",
        run_manager: Any = None,
    ) -> dict:
        datasource = datasource or self.datasource
        logger.info("DB 执行工具调用", datasource=datasource, sql=sql[:120])

        try:
            import asyncio

            from src.connectors.base import create_connector
            from src.datasource.registry import get_registry
            from src.tools.sqlglot_validator import validate_with_sqlglot

            async def _exec():
                ds = await get_registry().resolve(datasource)
                if ds is None:
                    return {"success": False, "error": f"数据源 '{datasource}' 未找到"}
                connector = create_connector(ds)
                rows = await connector.execute(sql)
                return {"success": True, "data": rows, "row_count": len(rows)}

            return asyncio.run(_exec())
        except Exception as e:
            logger.error("DB 执行失败", error=str(e))
            return {"success": False, "error": str(e)}


class DBExplainTool(BaseTool):
    """5.1.5 SQL EXPLAIN 空跑校验工具。"""

    name: str = "db_explain"
    description: str = (
        "对 SQL 执行 EXPLAIN 分析，不实际查询数据。"
        "输入: JSON 格式 {\"sql\": \"SELECT ...\", \"datasource\": \"名称\"}。"
        "返回: 执行计划文本。"
    )
    datasource: str = ""

    def _run(
        self,
        sql: str,
        datasource: str = "",
        run_manager: Any = None,
    ) -> dict:
        datasource = datasource or self.datasource
        logger.info("EXPLAIN 工具调用", datasource=datasource, sql=sql[:120])

        try:
            import asyncio

            from src.connectors.base import create_connector
            from src.datasource.registry import get_registry

            async def _explain():
                ds = await get_registry().resolve(datasource)
                if ds is None:
                    return {"success": False, "error": f"数据源 '{datasource}' 未找到"}
                connector = create_connector(ds)
                plan = await connector.explain(sql)
                return {"success": True, "explain_plan": plan}

            return asyncio.run(_explain())
        except Exception as e:
            logger.error("EXPLAIN 执行失败", error=str(e))
            return {"success": False, "error": str(e)}
