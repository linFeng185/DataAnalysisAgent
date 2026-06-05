"""4.10 build_response Node — 组装最终响应。"""

from __future__ import annotations

from src.graph.state import AnalysisState


async def build_response_node(state: AnalysisState) -> dict:
    if state.get("validation_errors"):
        return {"final_response": {
            "success": False,
            "error_code": "VALIDATION_FAILED",
            "error_message": str(state["validation_errors"]),
            "user_query": state.get("user_query", ""),
        }}
    return {"final_response": {
        "success": True,
        "session_id": "",
        "user_query": state.get("user_query", ""),
        "sql": state.get("generated_sql", ""),
        "data": state.get("query_result_sample", []),
        "analysis": state.get("analysis_result", {}),
        "chart": state.get("chart_config", {}),
    }}
