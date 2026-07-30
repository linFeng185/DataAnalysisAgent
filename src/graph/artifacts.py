"""把节点状态转换为统一的可追溯分析产物。"""

from __future__ import annotations

import hashlib
from typing import Any

from src.graph.context import read_contexts
from src.knowledge.asset_models import AnalysisArtifact, Evidence
from src.logging_config import get_logger

logger = get_logger(__name__)


def build_analysis_artifact(
    state: dict[str, Any] | None,
    response: dict[str, Any] | None,
) -> dict[str, Any]:
    """生成兼容旧响应字段的统一 AnalysisArtifact。"""
    state = state or {}
    response = response or {}
    contexts = read_contexts(state)
    source = str(response.get("source") or "unknown")
    datasource = contexts.routing.datasource
    analysis = response.get("analysis") or {}
    if not isinstance(analysis, dict):
        analysis = {}
    status = str(response.get("status") or "failed")
    chart = response.get("chart") or {}
    chart_type = str(chart.get("type") or "table") if isinstance(chart, dict) else "table"
    rendered_report = str(analysis.get("rendered_report", "") or "")
    forecast = analysis.get("forecast") if isinstance(analysis, dict) else None
    if source in {"mcp_agent", "file_analysis", "market_research", "report"} or rendered_report:
        kind = "report"
    elif source == "external_action":
        kind = "recommendation"
    elif source == "forecast" or forecast:
        kind = "forecast"
    elif chart_type != "table":
        kind = "chart"
    else:
        kind = "table"

    raw_data = response.get("data", [])
    data: dict[str, Any] | list[Any]
    if forecast:
        data = forecast if isinstance(forecast, dict) else {"forecast": forecast}
    elif rendered_report:
        data = {"report": rendered_report, "rows": raw_data if isinstance(raw_data, list) else []}
    elif isinstance(raw_data, (dict, list)):
        data = raw_data
    else:
        data = []
    summary = str(
        analysis.get("summary")
        or response.get("decision_summary")
        or response.get("error_message")
        or "当前请求未生成可展示结论"
    )
    source_id = f"{source}:{datasource or 'current'}"
    version = contexts.request.session_id or "current"
    evidence = Evidence(
        content=f"结果来源：{source_id}，状态：{status}",
        source_id=source_id,
        version=version,
        locator={
            "row_count": int(response.get("row_count", 0) or 0),
            "truncated": bool(response.get("truncated", False)),
        },
        metadata={"datasource": datasource, "status": status},
    )
    limitations = [
        "产物仅基于当前请求返回的数据和已验证证据。",
    ]
    if response.get("truncated"):
        limitations.append("结果受到行数上限限制，不能直接代表完整数据集。")
    if status == "partial":
        limitations.append("部分数据源未完成，跨源结论需要谨慎解释。")
    confidence = str(analysis.get("confidence") or "low")
    if confidence not in {"high", "medium", "low"}:
        confidence = "low"
    sql = str(response.get("sql") or "")
    reproducibility = {
        "source": source,
        "datasource": datasource,
        "status": status,
        "task_plan": contexts.routing.task_plan,
        "sql_hash": hashlib.sha256(sql.encode("utf-8")).hexdigest() if sql else "",
    }
    artifact = AnalysisArtifact(
        kind=kind,
        data=data,
        narrative={
            "summary": summary,
            "insights": analysis.get("insights", []) or [],
            "chart_type": chart_type,
        },
        evidence=[evidence],
        limitations=limitations,
        confidence=confidence,
        reproducibility=reproducibility,
    )
    result = artifact.model_dump(mode="json")
    logger.info(
        "统一分析产物构建完成",
        kind=kind,
        source=source,
        status=status,
        evidence_count=len(result.get("evidence", [])),
    )
    return result
