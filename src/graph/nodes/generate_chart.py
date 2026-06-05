"""4.9 generate_chart Node — 图表生成。"""

from __future__ import annotations

from src.graph.state import AnalysisState


async def generate_chart_node(state: AnalysisState) -> dict:
    return {"chart_config": {"type": "bar", "echarts_option": {}}}
