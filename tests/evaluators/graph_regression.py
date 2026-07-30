"""任务路由、安全和统一产物的离线全链路契约评估器。"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any


def load_graph_benchmark(path: str | Path) -> dict[str, Any]:
    """加载固定图回归数据集并验证顶层结构。"""
    data = json.loads(Path(path).read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        raise ValueError("图评测集必须是 JSON 对象")
    for key in ("routing_cases", "security_cases", "artifact_cases"):
        if not isinstance(data.get(key), list):
            raise ValueError(f"图评测集缺少数组: {key}")
    return data


def evaluate_graph_benchmark(benchmark: dict[str, Any]) -> dict[str, Any]:
    """离线验证能力路由、危险 SQL 和 AnalysisArtifact 语义。"""
    from src.graph.artifacts import build_analysis_artifact
    from src.graph.contracts import build_task_plan
    from src.graph.workflow import after_analyze_result, route_by_intent
    from src.security.sql_execution import validate_sql

    details: list[dict[str, Any]] = []
    passed = 0
    for case in benchmark.get("routing_cases", []):
        plan = build_task_plan(
            str(case.get("intent", "query")),
            query=str(case.get("query", "")),
            datasources=list(case.get("datasources", []) or []),
        )
        state = {
            "user_query": case.get("query", ""),
            "intent": plan.intent,
            "task_plan": plan.model_dump(),
            "selected_datasources": list(case.get("datasources", []) or []),
        }
        initial = route_by_intent(state)
        post = after_analyze_result(state)
        ok = (
            plan.capability == case.get("expected_capability")
            and initial == case.get("expected_initial_route")
            and post == case.get("expected_post_analysis")
        )
        passed += int(ok)
        details.append({"case_id": case.get("id"), "kind": "routing", "passed": ok})
    for case in benchmark.get("security_cases", []):
        valid = validate_sql(str(case.get("sql", "")), str(case.get("dialect", "postgres"))).valid
        ok = valid is bool(case.get("expected_valid"))
        passed += int(ok)
        details.append({"case_id": case.get("id"), "kind": "security", "passed": ok})
    for case in benchmark.get("artifact_cases", []):
        state = dict(case.get("state", {}) or {})
        response = dict(case.get("response", {}) or {})
        artifact = build_analysis_artifact(state, response)
        ok = artifact.get("kind") == case.get("expected_kind") and bool(artifact.get("evidence"))
        passed += int(ok)
        details.append({"case_id": case.get("id"), "kind": "artifact", "passed": ok})
    total = len(details)
    return {
        "case_count": total,
        "passed": passed,
        "failed": total - passed,
        "pass_rate": passed / total if total else 1.0,
        "details": details,
    }
