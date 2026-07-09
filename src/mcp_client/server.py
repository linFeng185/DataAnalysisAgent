"""8.2 MCP Server — 对外暴露数据分析能力。

依据: SPEC §3.9.2
"""

from __future__ import annotations

from src.logging_config import get_logger

logger = get_logger(__name__)


def create_mcp_server():
    """8.2.1 创建 FastMCP 实例。"""
    from mcp.server.fastmcp import FastMCP
    mcp = FastMCP("data-analysis-agent")

    @mcp.tool()
    async def query_database(question: str, datasource: str, chart: bool = True) -> dict:
        """8.2.2 自然语言查询数据库。"""
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
            return out
        except Exception as e:
            return {"error": str(e)}

    @mcp.tool()
    async def list_datasources() -> dict:
        """8.2.3 列出所有可用数据源。"""
        try:
            from src.datasource.registry import get_registry
            dss = await get_registry().list_all()
            return {"datasources": [
                {"name": d.name, "dialect": d.dialect, "description": d.description or ""}
                for d in dss
            ]}
        except Exception as e:
            return {"error": str(e)}

    @mcp.tool()
    async def get_table_schema(datasource: str, table_name: str) -> dict:
        """8.2.4 获取指定表结构。"""
        try:
            from src.knowledge.schema_manager import get_schema_manager
            schema = await get_schema_manager().get_or_fetch_schema(datasource)
            if schema:
                for t in schema.tables:
                    if t.name.lower() == table_name.lower():
                        return {
                            "table": t.name, "description": t.description,
                            "columns": [{"name": c.name, "type": c.type, "comment": c.comment}
                                        for c in t.columns],
                            "relations": [{"target": r.target_table, "key": r.join_key}
                                          for r in (t.relations or [])],
                        }
            return {"error": f"表 '{table_name}' 未找到"}
        except Exception as e:
            return {"error": str(e)}

    @mcp.tool()
    async def get_metrics(metric_name: str) -> dict:
        """8.2.5 查询业务指标口径。"""
        try:
            from src.knowledge.business_rules import get_business_rule_store
            rules = await get_business_rule_store().search_business_rules(metric_name, top_k=3)
            return {"metric_name": metric_name, "rules": [
                {"content": r.content, "category": r.category} for r in rules
            ]}
        except Exception as e:
            return {"error": str(e)}

    return mcp
