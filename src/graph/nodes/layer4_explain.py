"""4.6 layer4_explain Node — EXPLAIN 空跑校验。"""

from __future__ import annotations

from src.graph.state import AnalysisState


async def layer4_explain_node(state: AnalysisState) -> dict:
    """Phase 2 对接 Connector.explain()。Phase 1 跳过。"""
    return {"explain_errors": [], "sql_valid": True}
