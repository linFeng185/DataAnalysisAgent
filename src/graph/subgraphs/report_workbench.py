"""报表工作台 Phase 1 LangGraph 子图。"""

from __future__ import annotations

import asyncio
from typing import Any

from langgraph.graph import END, StateGraph
from typing_extensions import TypedDict

from src.logging_config import get_logger
from src.reporting.aggregation import aggregate_cross_source
from src.reporting.document import ReportArtifact, build_report_document
from src.reporting.models import ReportPlan, UnitResult
from src.reporting.relations import RelationEvidence, build_relations

logger = get_logger(__name__)


class ReportWorkbenchState(TypedDict, total=False):
    """报表子图显式状态契约，不保存凭证、Prompt 原文或完整 SQL。"""

    plan: ReportPlan
    identity: dict[str, Any]
    authorized_datasources: dict[str, dict[str, Any]]
    resolved_units: list[dict[str, Any]]
    query_plans: list[dict[str, Any]]
    unit_results: list[UnitResult]
    relations: list[RelationEvidence]
    cross_business_analysis: dict[str, Any]
    artifact: ReportArtifact
    run_status: str
    execute_unit: Any
    step_logger: Any
    report_id: str
    job_id: str
    run_id: str


# 方法作用：在节点边界记录可审计的节点状态，不写入敏感输入。
# Args: state - 当前子图状态；node - 节点名；status - 节点状态；error - 用户可见错误摘要。
# Returns: 无返回值。
async def _log_step(state: ReportWorkbenchState, node: str, status: str, error: str = "") -> None:
    callback = state.get("step_logger")
    if callback is None:
        return
    result = callback(node, status, error)
    if hasattr(result, "__await__"):
        await result


# 方法作用：读取当前身份和计划，建立报表执行权限摘要。
# Args: state - 报表子图状态。
# Returns: permission 摘要和下一节点所需状态。
async def report_load_policy(state: ReportWorkbenchState) -> dict[str, Any]:
    await _log_step(state, "report_load_policy", "running")
    plan = state.get("plan")
    if not isinstance(plan, ReportPlan):
        raise ValueError("报表计划缺失")
    identity = dict(state.get("identity", {}) or {})
    logger.debug("报表权限节点入口", tenant_id=identity.get("tenant_id", 0), user_id=identity.get("user_id", 0), unit_count=len(plan.analysis_units))
    await _log_step(state, "report_load_policy", "succeeded")
    return {"permission_context": {"tenant_id": identity.get("tenant_id", 0), "user_id": identity.get("user_id", 0), "role": identity.get("role", "")}}


# 方法作用：把计划分析单元绑定到当前授权数据源，不自动探测无权来源。
# Args: state - 含 ReportPlan 和授权数据源的状态。
# Returns: resolved_units 单元摘要。
async def report_resolve_units(state: ReportWorkbenchState) -> dict[str, Any]:
    await _log_step(state, "report_resolve_units", "running")
    plan = state["plan"]
    requested = list(plan.requested_datasources)
    authorized = state.get("authorized_datasources", {}) or {}
    # 运行器显式传入授权映射时，缺失数据源必须阻断，不能把空映射当作“全部可用”。
    authorization_supplied = "authorized_datasources" in state
    logger.info(
        "报表分析单元授权边界探针",
        authorization_supplied=authorization_supplied,
        authorized_datasource_names=sorted(authorized.keys()) if isinstance(authorized, dict) else [],
        requested_datasources=requested,
    )
    default_datasource = requested[0] if requested else next(iter(authorized), "")
    units: list[dict[str, Any]] = []
    for unit in plan.analysis_units:
        datasource = unit.datasource or default_datasource
        authorized_entry = authorized.get(datasource) if isinstance(authorized, dict) else None
        is_authorized = not authorization_supplied or (bool(datasource) and authorized_entry is not None)
        units.append({
            "unit_id": unit.unit_id,
            "question": unit.question,
            "datasource": datasource,
            "time_range": unit.time_range.model_dump(mode="json"),
            "dimensions": list(unit.dimensions),
            "metrics": list(unit.metrics),
            "columns": list(dict.fromkeys(unit.dimensions + unit.metrics)),
            "authorization_status": "authorized" if is_authorized else "denied",
            "authorization_error": "分析单元数据源未通过当前用户授权" if not is_authorized else "",
        })
    denied_count = sum(item["authorization_status"] == "denied" for item in units)
    if denied_count:
        logger.warning("报表分析单元授权阻断", denied_count=denied_count, unit_count=len(units))
    await _log_step(state, "report_resolve_units", "succeeded")
    return {"resolved_units": units}


# 方法作用：按分析单元生成授权字段证据，首期不将完整 Schema 写入报表状态。
# Args: state - 含 resolved_units 的状态。
# Returns: 补充 columns 的单元摘要。
async def report_retrieve_schema(state: ReportWorkbenchState) -> dict[str, Any]:
    await _log_step(state, "report_retrieve_schema", "running")
    authorized = state.get("authorized_datasources", {}) or {}
    resolved: list[dict[str, Any]] = []
    for unit in state.get("resolved_units", []):
        if unit.get("authorization_status") == "denied":
            resolved.append({**unit, "columns": [], "dimensions": [], "metrics": []})
            continue
        access = authorized.get(unit.get("datasource", ""), {}) if isinstance(authorized, dict) else {}
        allowed = list(access.get("allowed_columns", []) or [])
        columns = list(unit.get("columns", []) or [])
        if allowed:
            columns = [column for column in columns if column in allowed]
        resolved.append({
            **unit,
            "columns": columns,
            "dimensions": [column for column in unit.get("dimensions", []) if column in columns or not allowed],
            "metrics": [column for column in unit.get("metrics", []) if column in columns or not allowed],
        })
    await _log_step(state, "report_retrieve_schema", "succeeded")
    return {"resolved_units": resolved}


# 方法作用：为每个分析单元生成有界查询计划，合并相同粒度缺口。
# Args: state - 含已解析单元的状态。
# Returns: query_plans 列表，不包含可直接执行的任意 SQL。
async def report_plan_queries(state: ReportWorkbenchState) -> dict[str, Any]:
    await _log_step(state, "report_plan_queries", "running")
    plans: list[dict[str, Any]] = []
    grouped: dict[tuple[str, tuple[str, ...], tuple[str, ...], tuple[str, str]], dict[str, Any]] = {}
    for unit in state.get("resolved_units", [])[:16]:
        time_range = unit.get("time_range", {}) or {}
        if hasattr(time_range, "model_dump"):
            time_range = time_range.model_dump(mode="json")
        time_signature = (str(time_range.get("kind", "relative")), str(time_range.get("value", "90d"))) if isinstance(time_range, dict) else ("relative", "90d")
        signature = (
            str(unit.get("datasource", "")),
            tuple(unit.get("dimensions", []) or []),
            tuple(unit.get("metrics", []) or []),
            time_signature,
        )
        existing = grouped.get(signature)
        if existing is not None:
            # 同一取数粒度只执行一次，但保留所有业务单元，后续将结果复制回各单元。
            existing.setdefault("unit_ids", []).append(str(unit["unit_id"]))
            continue
        query_plan = {
            "unit_id": unit["unit_id"],
            "unit_ids": [str(unit["unit_id"])],
            "datasource": unit.get("datasource", ""),
            "question": unit.get("question", ""),
            "query_kind": "readonly_aggregate",
            "max_rows": 1000,
        }
        grouped[signature] = query_plan
        plans.append(query_plan)
    await _log_step(state, "report_plan_queries", "succeeded")
    return {"query_plans": plans}


# 方法作用：并行执行各分析单元并隔离单元错误。
# Args: state - 含 query_plans 和可注入 execute_unit 回调的状态。
# Returns: UnitResult 列表和运行状态。
async def report_execute_units(state: ReportWorkbenchState) -> dict[str, Any]:
    await _log_step(state, "report_execute_units", "running")
    plan = state["plan"]
    units_by_id = {unit.unit_id: unit for unit in plan.analysis_units}
    resolved_by_id = {str(item.get("unit_id", "")): item for item in state.get("resolved_units", [])}
    executor = state.get("execute_unit")

    async def execute_one(query_plan: dict[str, Any]) -> list[UnitResult]:
        unit = units_by_id[query_plan["unit_id"]]
        unit_ids = [str(item) for item in query_plan.get("unit_ids", [unit.unit_id]) if str(item) in units_by_id]
        if not unit_ids:
            unit_ids = [unit.unit_id]
        resolved = resolved_by_id.get(unit.unit_id, {})
        if resolved.get("authorization_status") == "denied":
            return [UnitResult(unit_id=item, datasource=query_plan.get("datasource", ""), status="failed", error_code="DATASOURCE_FORBIDDEN", user_error_message="该分析单元使用的数据源未授权，已阻止执行") for item in unit_ids]
        if executor is None:
            return [UnitResult(unit_id=item, datasource=query_plan.get("datasource", ""), status="partial", warnings=["首期执行器未绑定，等待现有 SQL 工作流注入"], user_error_message="当前报表暂时没有可用的查询执行器") for item in unit_ids]
        for attempt in range(1, 4):
            try:
                result = executor(unit, state)
                if hasattr(result, "__await__"):
                    result = await result
                if isinstance(result, UnitResult):
                    normalized = result if result.unit_id == unit.unit_id else result.model_copy(update={"unit_id": unit.unit_id})
                    if normalized.status == "failed" and normalized.error_code in {"TRANSIENT_DATABASE", "LLM_TRANSIENT", "TIMEOUT"} and attempt < 3:
                        await _log_step(state, "report_execute_units", "retry", normalized.user_error_message)
                        continue
                    return [normalized.model_copy(update={"unit_id": item}) for item in unit_ids]
                if isinstance(result, dict):
                    normalized = UnitResult.model_validate({"unit_id": unit.unit_id, "datasource": query_plan.get("datasource", ""), **result})
                    return [normalized.model_copy(update={"unit_id": item}) for item in unit_ids]
                raise TypeError("分析执行器返回类型无效")
            except Exception as exc:
                if attempt < 3:
                    await _log_step(state, "report_execute_units", "retry", "分析单元遇到瞬态错误，准备重试")
                    continue
                logger.error("报表分析单元执行失败", unit_id=unit.unit_id, error=str(exc), exc_info=True)
                return [UnitResult(unit_id=item, datasource=query_plan.get("datasource", ""), status="failed", error_code="UNIT_EXECUTION_FAILED", user_error_message="该分析单元执行失败，请查看运行日志后重试") for item in unit_ids]

    grouped_results = await asyncio.gather(*(execute_one(item) for item in state.get("query_plans", [])))
    results = [result for group in grouped_results for result in group]
    success_count = sum(item.status == "success" and bool(item.rows) for item in results)
    failed_count = sum(item.status == "failed" for item in results)
    expected_count = len(plan.analysis_units)
    run_status = "succeeded" if expected_count > 0 and failed_count == 0 and success_count == expected_count else ("partial" if success_count > 0 or failed_count < max(1, expected_count) else "failed")
    await _log_step(state, "report_execute_units", "partial" if run_status == "partial" else ("succeeded" if run_status == "succeeded" else "failed"))
    return {"unit_results": results, "run_status": run_status}


# 方法作用：补充确定性结果画像，避免让 LLM 伪造统计值。
# Args: state - 含 unit_results 的状态。
# Returns: 更新后的 UnitResult 列表。
async def report_profile_results(state: ReportWorkbenchState) -> dict[str, Any]:
    await _log_step(state, "report_profile_results", "running")
    profiled: list[UnitResult] = []
    for result in state.get("unit_results", []):
        rows = result.rows
        summary = result.summary or f"分析单元返回 {len(rows)} 行样本。"
        warnings = list(result.warnings)
        if not rows and "暂无数据" not in warnings:
            warnings.append("当前查询没有返回数据，无法进行完整趋势分析")
        profiled.append(result.model_copy(update={"summary": summary, "warnings": warnings}))
    await _log_step(state, "report_profile_results", "succeeded")
    return {"unit_results": profiled}


# 方法作用：基于单元授权列集合构建同源关系证据，跨源仅返回降级说明。
# Args: state - 含 resolved_units 的状态。
# Returns: RelationEvidence 列表。
async def report_build_relations(state: ReportWorkbenchState) -> dict[str, Any]:
    await _log_step(state, "report_build_relations", "running")
    relations = build_relations(
        list(state.get("resolved_units", [])),
        list(state.get("unit_results", [])),
    )
    await _log_step(state, "report_build_relations", "succeeded")
    return {"relations": relations}


# 方法作用：汇总事实、关系证据和限制，不把统计关系升级为因果结论。
# Args: state - 含结果和关系证据的状态。
# Returns: CrossBusinessAnalysis 字典。
async def report_synthesize_insights(state: ReportWorkbenchState) -> dict[str, Any]:
    await _log_step(state, "report_synthesize_insights", "running")
    relations = list(state.get("relations", []))
    verified = [item for item in relations if item.evidence_level == "verified_relation"]
    blocked = [item for item in relations if item.evidence_level in {"unsupported_cross_source", "blocked"}]
    analysis = {
        "facts": [f"已完成 {len(state.get('unit_results', []))} 个分析单元"],
        "verified_relations": [item.model_dump(mode="json") for item in verified],
        "limitations": ["关系证据不代表因果关系"] + (["跨数据源关系证据不足或被安全边界阻断，已分别分析"] if blocked else []),
    }
    aggregation = aggregate_cross_source(list(state.get("resolved_units", [])), list(state.get("unit_results", [])))
    analysis["aggregation"] = aggregation
    analysis["limitations"].extend(aggregation["limitations"])
    await _log_step(state, "report_synthesize_insights", "succeeded")
    return {"cross_business_analysis": analysis}


# 方法作用：根据分析单元内容生成动态页面布局和快照 Drilldown。
# Args: state - 含计划、结果和报表 ID 的状态。
# Returns: ReportArtifact。
async def report_plan_layout(state: ReportWorkbenchState) -> dict[str, Any]:
    await _log_step(state, "report_plan_layout", "running")
    artifact = build_report_document(state["plan"], list(state.get("unit_results", [])), report_id=state.get("report_id"), relations=list(state.get("relations", [])))
    if state.get("cross_business_analysis", {}).get("limitations"):
        artifact.document.limitations.extend(state["cross_business_analysis"]["limitations"])
    aggregation = state.get("cross_business_analysis", {}).get("aggregation", {})
    if aggregation.get("status") == "success":
        from src.reporting.document import (
            ReportBlock,
            ReportSection,
            layout_for_block,
            sanitize_value,
        )

        data_ref = "cross_source_aggregate"
        rows = sanitize_value(aggregation["rows"])
        artifact.data_manifest[data_ref] = rows
        artifact.document.navigation.append(data_ref)
        artifact.document.sections.append(ReportSection(id=data_ref, title="跨源样本聚合", subtitle="同一明确粒度，按来源分别统计；不代表全量业务总额", blocks=[ReportBlock(section_id=data_ref, type="table", title="授权样本的分组统计", data_ref=data_ref, content={"columns": list(rows[0]) if rows else [], "rows": rows}, layout=layout_for_block("table"))]))
    await _log_step(state, "report_plan_layout", "succeeded")
    return {"artifact": artifact}


# 方法作用：再次执行 DSL 和大小约束校验，防止自定义执行器写入非法页面。
# Args: state - 含 ReportArtifact 的状态。
# Returns: 校验后的 ReportArtifact。
async def report_validate_document(state: ReportWorkbenchState) -> dict[str, Any]:
    await _log_step(state, "report_validate_document", "running")
    artifact = state.get("artifact")
    if not isinstance(artifact, ReportArtifact):
        raise ValueError("报表页面产物缺失")
    artifact.document = artifact.document.model_validate(artifact.document.model_dump(mode="json"))
    await _log_step(state, "report_validate_document", "succeeded")
    return {"artifact": artifact}


# 方法作用：构建并编译报表工作台子图。
# Args: 无。
# Returns: 可通过 ainvoke 执行的 LangGraph 编译图。
def build_report_workbench_subgraph():
    graph = StateGraph(ReportWorkbenchState)
    graph.add_node("report_load_policy", report_load_policy)
    graph.add_node("report_resolve_units", report_resolve_units)
    graph.add_node("report_retrieve_schema", report_retrieve_schema)
    graph.add_node("report_plan_queries", report_plan_queries)
    graph.add_node("report_execute_units", report_execute_units)
    graph.add_node("report_profile_results", report_profile_results)
    graph.add_node("report_build_relations", report_build_relations)
    graph.add_node("report_synthesize_insights", report_synthesize_insights)
    graph.add_node("report_plan_layout", report_plan_layout)
    graph.add_node("report_validate_document", report_validate_document)
    graph.set_entry_point("report_load_policy")
    graph.add_edge("report_load_policy", "report_resolve_units")
    graph.add_edge("report_resolve_units", "report_retrieve_schema")
    graph.add_edge("report_retrieve_schema", "report_plan_queries")
    graph.add_edge("report_plan_queries", "report_execute_units")
    graph.add_edge("report_execute_units", "report_profile_results")
    graph.add_edge("report_profile_results", "report_build_relations")
    graph.add_edge("report_build_relations", "report_synthesize_insights")
    graph.add_edge("report_synthesize_insights", "report_plan_layout")
    graph.add_edge("report_plan_layout", "report_validate_document")
    graph.add_edge("report_validate_document", END)
    return graph.compile()
