"""8.2 MCP Server — 对外暴露数据分析能力。

依据: SPEC §3.9.2
"""

from __future__ import annotations

from src.logging_config import get_logger

logger = get_logger(__name__)


# 方法作用：创建并注册数据分析工具的 FastMCP Server。
# Args: 无。
# Returns: 注册完成的 FastMCP 实例。
def create_mcp_server():
    """8.2.1 创建 FastMCP 实例。"""
    logger.debug("创建 MCP Server 入口")
    from mcp.server.fastmcp import FastMCP
    mcp = FastMCP("data-analysis-agent")

    # 方法作用：通过工作流执行自然语言数据库查询。
    # Args: question - 用户问题；datasource - 数据源名；chart - 是否返回图表。
    # Returns: 查询结果或带 error 的协议响应。
    @mcp.tool()
    async def query_database(question: str, datasource: str, chart: bool = True) -> dict:
        """8.2.2 自然语言查询数据库。"""
        logger.debug("MCP 数据库查询入口", datasource=datasource, chart=chart)
        try:
            from src.graph.workflow import app
            result = await app.ainvoke({"user_query": question, "datasource": datasource})
            final = result.get("final_response", {})
            out = {"success": final.get("success", True), "sql": final.get("sql", ""),
                   "analysis": final.get("analysis", {})}
            if chart:
                out["chart"] = final.get("chart", {})
            if final.get("data"):
                out["data"] = final["data"][:100]
            logger.info("MCP 数据库查询完成", datasource=datasource, success=out["success"])
            return out
        except Exception as e:
            logger.error("MCP 数据库查询失败", datasource=datasource, error=str(e), exc_info=True)
            return {"error": str(e)}

    # 方法作用：列出当前应用可用的数据源。
    # Args: 无。
    # Returns: 数据源摘要或带 error 的协议响应。
    @mcp.tool()
    async def list_datasources() -> dict:
        """8.2.3 列出所有可用数据源。"""
        logger.debug("MCP 数据源列表入口")
        try:
            from src.datasource.registry import get_registry
            dss = await get_registry().list_all()
            result = {"datasources": [
                {
                    "name": d.get("name", "") if isinstance(d, dict) else d.name,
                    "dialect": d.get("dialect", "") if isinstance(d, dict) else d.dialect,
                    "description": (
                        d.get("description", "")
                        if isinstance(d, dict)
                        else d.description or ""
                    ),
                }
                for d in dss
            ]}
            logger.info("MCP 数据源列表完成", count=len(result["datasources"]))
            return result
        except Exception as e:
            logger.error("MCP 数据源列表失败", error=str(e), exc_info=True)
            return {"error": str(e)}

    # 方法作用：读取指定数据源的单表结构。
    # Args: datasource - 数据源名；table_name - 表名。
    # Returns: 表结构、未找到错误或带 error 的协议响应。
    @mcp.tool()
    async def get_table_schema(datasource: str, table_name: str) -> dict:
        """8.2.4 获取指定表结构。"""
        logger.debug("MCP 表结构查询入口", datasource=datasource, table=table_name)
        try:
            from src.knowledge.schema_manager import get_schema_manager
            schema = await get_schema_manager().get_or_fetch_schema(datasource)
            if schema:
                for t in schema.tables:
                    if t.name.lower() == table_name.lower():
                        result = {
                            "table": t.name, "description": t.description,
                            "columns": [{"name": c.name, "type": c.type, "comment": c.comment}
                                        for c in t.columns],
                            "relations": [{"target": r.target_table, "key": r.join_key}
                                          for r in (t.relations or [])],
                        }
                        logger.info("MCP 表结构查询完成", datasource=datasource, table=table_name)
                        return result
            logger.info("MCP 表结构查询完成", datasource=datasource, table=table_name, found=False)
            return {"error": f"表 '{table_name}' 未找到"}
        except Exception as e:
            logger.error(
                "MCP 表结构查询失败",
                datasource=datasource,
                table=table_name,
                error=str(e),
                exc_info=True,
            )
            return {"error": str(e)}

    # 方法作用：检索指定业务指标的知识规则。
    # Args: metric_name - 业务指标名。
    # Returns: 匹配规则或带 error 的协议响应。
    @mcp.tool()
    async def get_metrics(metric_name: str) -> dict:
        """8.2.5 查询业务指标口径。"""
        logger.debug("MCP 指标查询入口", metric=metric_name)
        try:
            from src.knowledge.business_rules import BusinessRuleStore
            from src.memory.vector_store import get_vector_store

            store = BusinessRuleStore(await get_vector_store())
            rules = await store.search_business_rules(metric_name, top_k=3)
            result = {"metric_name": metric_name, "rules": [
                {"content": r.content, "category": r.category} for r in rules
            ]}
            logger.info("MCP 指标查询完成", metric=metric_name, count=len(result["rules"]))
            return result
        except Exception as e:
            logger.error("MCP 指标查询失败", metric=metric_name, error=str(e), exc_info=True)
            return {"error": str(e)}

    logger.info("创建 MCP Server 完成")
    return mcp
