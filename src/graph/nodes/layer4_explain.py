"""4.6 layer4_explain Node — EXPLAIN 空跑校验。"""

from __future__ import annotations

import time

from src.graph.state import AnalysisState
from src.logging_config import get_logger

logger = get_logger(__name__)


async def layer4_explain_node(state: AnalysisState) -> dict:
    """Phase 2 对接 Connector.explain()。Phase 1 跳过。"""
    _start = time.monotonic()
    logger.info("节点开始", node="layer4_explain")
    logger.info("节点完成", node="layer4_explain", elapsed_ms=round((time.monotonic() - _start) * 1000))
    return {"explain_errors": [], "sql_valid": True}
