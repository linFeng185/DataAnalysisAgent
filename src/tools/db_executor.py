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

    # 方法作用：拒绝同步数据库执行，避免嵌套事件循环。
    # Args: sql - 只读 SQL；datasource - 数据源名称；run_manager - LangChain 同步回调管理器。
    # Returns: 指示调用方使用异步接口的失败结果。
    def _run(
        self,
        sql: str,
        datasource: str = "",
        run_manager: Any = None,
    ) -> dict:
        """同步入口只返回明确指引，生产链路统一使用 `_arun()`。"""
        datasource = datasource or self.datasource
        logger.debug("DBExecutorTool._run 入口", datasource=datasource, sql=sql[:120])
        result = {"success": False, "error": "DBExecutorTool 仅支持异步调用，请使用 ainvoke()"}
        logger.warning("DBExecutorTool._run 拒绝", datasource=datasource, reason="仅支持异步调用")
        return result

    # 方法作用：通过连接器异步执行只读 SQL。
    # Args: sql - 只读 SQL；datasource - 数据源名称；run_manager - LangChain 异步回调管理器。
    # Returns: 包含结果行和行数的结构化结果。
    async def _arun(
        self,
        sql: str,
        datasource: str = "",
        run_manager: Any = None,
    ) -> dict:
        """使用 DataSourceRegistry 和 Connector 原生异步接口执行 SQL。"""
        datasource = datasource or self.datasource
        logger.debug("DBExecutorTool._arun 入口", datasource=datasource, sql=sql[:120])
        try:
            from src.connectors.registry import create_connector
            from src.datasource.registry import get_registry
            from src.graph.nodes.layer3_validate import validate_readonly_sql
            from src.tools.sqlglot_validator import validate_with_sqlglot

            validation = validate_with_sqlglot(sql, dialect="mysql")
            readonly_errors = validate_readonly_sql(sql, "mysql")
            if not validation.get("valid", False) or readonly_errors:
                logger.warning("DBExecutorTool._arun 拒绝", datasource=datasource, reason="SQL 校验失败")
                return {
                    "success": False,
                    "error": "SQL 校验失败",
                    "details": validation.get("errors", []) + readonly_errors,
                }
            ds = await get_registry().resolve(datasource)
            connector = create_connector(ds)
            connector._engine = ds.engine
            rows = await connector.execute(sql)
            result = {"success": True, "data": rows, "row_count": len(rows)}
            logger.info("DBExecutorTool._arun 完成", datasource=datasource, row_count=len(rows))
            return result
        except Exception as exc:
            logger.error("DBExecutorTool._arun 失败", error=str(exc), exc_info=True)
            return {"success": False, "error": str(exc)}


class DBExplainTool(BaseTool):
    """5.1.5 SQL EXPLAIN 空跑校验工具。"""

    name: str = "db_explain"
    description: str = (
        "对 SQL 执行 EXPLAIN 分析，不实际查询数据。"
        "输入: JSON 格式 {\"sql\": \"SELECT ...\", \"datasource\": \"名称\"}。"
        "返回: 执行计划文本。"
    )
    datasource: str = ""

    # 方法作用：拒绝同步 EXPLAIN，避免嵌套事件循环。
    # Args: sql - 待校验 SQL；datasource - 数据源名称；run_manager - LangChain 同步回调管理器。
    # Returns: 指示调用方使用异步接口的失败结果。
    def _run(
        self,
        sql: str,
        datasource: str = "",
        run_manager: Any = None,
    ) -> dict:
        """同步入口只返回明确指引，生产链路统一使用 `_arun()`。"""
        datasource = datasource or self.datasource
        logger.debug("DBExplainTool._run 入口", datasource=datasource, sql=sql[:120])
        result = {"success": False, "error": "DBExplainTool 仅支持异步调用，请使用 ainvoke()"}
        logger.warning("DBExplainTool._run 拒绝", datasource=datasource, reason="仅支持异步调用")
        return result

    # 方法作用：通过连接器异步执行 EXPLAIN 校验。
    # Args: sql - 待校验 SQL；datasource - 数据源名称；run_manager - LangChain 异步回调管理器。
    # Returns: 包含 EXPLAIN 计划或错误的结构化结果。
    async def _arun(
        self,
        sql: str,
        datasource: str = "",
        run_manager: Any = None,
    ) -> dict:
        """使用 DataSourceRegistry 和 Connector 原生异步接口执行 EXPLAIN。"""
        datasource = datasource or self.datasource
        logger.debug("DBExplainTool._arun 入口", datasource=datasource, sql=sql[:120])
        try:
            from src.connectors.registry import create_connector
            from src.datasource.registry import get_registry

            ds = await get_registry().resolve(datasource)
            connector = create_connector(ds)
            connector._engine = ds.engine
            plan = await connector.explain(sql)
            result = {"success": bool(plan.get("valid")), "explain_plan": plan}
            logger.info("DBExplainTool._arun 完成", datasource=datasource, valid=result["success"])
            return result
        except Exception as exc:
            logger.error("DBExplainTool._arun 失败", error=str(exc), exc_info=True)
            return {"success": False, "error": str(exc)}
