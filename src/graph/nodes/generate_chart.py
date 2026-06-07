"""4.9 generate_chart Node — 图表生成。"""

from __future__ import annotations

import time

from src.graph.state import AnalysisState
from src.logging_config import get_logger

logger = get_logger(__name__)


async def generate_chart_node(state: AnalysisState) -> dict:
    _start = time.monotonic()
    logger.info("节点开始", node="generate_chart")
    logger.info("节点完成", node="generate_chart", elapsed_ms=round((time.monotonic() - _start) * 1000))
    return {"chart_config": {"type": "bar", "echarts_option": {}}}
