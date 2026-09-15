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

# 运行期补查任务标识，与计划期发散分离，便于独立路由与独立降级。
_SUPPLEMENT_LLM_TASK = "report_supplement"


class ReportWorkbenchState(TypedDict, total=False):
    """报表子图显式状态契约，不保存凭证、Prompt 原文或完整 SQL。"""

    plan: ReportPlan
    identity: dict[str, Any]
    authorized_datasources: dict[str, dict[str, Any]]
    unit_datasources: dict[str, list[str]]
    resolved_units: list[dict[str, Any]]
    query_plans: list[dict[str, Any]]
    unit_results: list[UnitResult]
    relations: list[RelationEvidence]
    cross_business_analysis: dict[str, Any]
    metric_citations: list[dict[str, Any]]
    unit_findings: list[Any]
    # 运行期发散追加的单元，不进 ReportPlan（计划已确认且已落库）。
    runtime_units: list[Any]
    # 运行期补查已走过的轮数，用于与档位轮数上限比较。
    divergence_round: int
    # 本轮补查的确定性触发来源与理由（缺基期 / 显著发现），由补查节点写入状态，
    # 供补查提示词与页面质量块消费：理由只进日志，用户和模型都看不到。
    supplement_context: dict[str, Any]
    artifact: ReportArtifact
    run_status: str
    execute_unit: Any
    step_logger: Any
    report_id: str
    job_id: str
    run_id: str
    # 任务创建时选定的视觉模板标识；后端只透传，不解释其取值。
    template_id: str


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
    from src.reporting.models import max_plan_units
    from src.reporting.profiling import is_runtime_quality_unit

    await _log_step(state, "report_plan_queries", "running")
    plans: list[dict[str, Any]] = []
    units_by_id = _units_by_id(state)
    grouped: dict[tuple, dict[str, Any]] = {}
    for unit in state.get("resolved_units", [])[:max_plan_units()]:
        # 运行说明不与空维度/指标的数据单元合并，否则顺序变化会吞掉真正的查询。
        if is_runtime_quality_unit(units_by_id[unit["unit_id"]]):
            plans.append({"unit_id": unit["unit_id"], "unit_ids": [unit["unit_id"]], "datasource": unit.get("datasource", ""), "query_kind": "runtime_quality"})
            continue
        time_range = unit.get("time_range", {}) or {}
        if hasattr(time_range, "model_dump"):
            time_range = time_range.model_dump(mode="json")
        time_signature = (str(time_range.get("kind", "relative")), str(time_range.get("value", "90d"))) if isinstance(time_range, dict) else ("relative", "90d")
        signature = (
            str(unit.get("datasource", "")),
            " ".join(str(unit.get("question", "")).split()),
            tuple((state.get("unit_datasources") or {}).get(str(unit["unit_id"]), [])),
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
# 方法作用：按重试契约执行单个分析单元，供主执行节点与运行期补查节点共用。
# Args: state - 当前状态；unit - 待执行单元；datasource - 命中的数据源；resolved - 该单元的解析摘要；executor - 执行器。
# Returns: 规范化后的 UnitResult。
async def _execute_single_unit(state: ReportWorkbenchState, unit: Any, datasource: str, resolved: dict[str, Any], executor: Any) -> UnitResult:
    if resolved.get("authorization_status") == "denied":
        return UnitResult(unit_id=unit.unit_id, datasource=datasource, status="failed", error_code="DATASOURCE_FORBIDDEN", user_error_message="该分析单元使用的数据源未授权，已阻止执行")
    from src.reporting.profiling import is_runtime_quality_unit

    if is_runtime_quality_unit(unit):
        return UnitResult(unit_id=unit.unit_id, answer_kind="text", summary="等待本轮查询与关系证据汇总。")
    if executor is None:
        return UnitResult(unit_id=unit.unit_id, datasource=datasource, status="partial", warnings=["首期执行器未绑定，等待现有 SQL 工作流注入"], user_error_message="当前报表暂时没有可用的查询执行器")
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
                return normalized
            if isinstance(result, dict):
                return UnitResult.model_validate({"unit_id": unit.unit_id, "datasource": datasource, **result})
            raise TypeError("分析执行器返回类型无效")
        except Exception as exc:
            if attempt < 3:
                await _log_step(state, "report_execute_units", "retry", "分析单元遇到瞬态错误，准备重试")
                continue
            logger.error("报表分析单元执行失败", unit_id=unit.unit_id, error=str(exc), exc_info=True)
            return UnitResult(unit_id=unit.unit_id, datasource=datasource, status="failed", error_code="UNIT_EXECUTION_FAILED", user_error_message="该分析单元执行失败，请查看运行日志后重试")
    return UnitResult(unit_id=unit.unit_id, datasource=datasource, status="failed", error_code="UNIT_EXECUTION_FAILED", user_error_message="该分析单元执行失败，请查看运行日志后重试")


# 方法作用：执行数据单元并汇总状态，运行质量说明不掩盖数据查询失败。
# Args: state - 报表执行状态。
# Returns: 单元结果和本轮状态。
async def report_execute_units(state: ReportWorkbenchState) -> dict[str, Any]:
    await _log_step(state, "report_execute_units", "running")
    plan = state["plan"]
    units_by_id = _units_by_id(state)
    resolved_by_id = {str(item.get("unit_id", "")): item for item in state.get("resolved_units", [])}
    executor = state.get("execute_unit")

    # 方法作用：执行去重后的查询计划并回填共享该查询的单元。
    # Args: query_plan - 有界查询计划。
    # Returns: 对应各单元的结果。
    async def execute_one(query_plan: dict[str, Any]) -> list[UnitResult]:
        unit = units_by_id[query_plan["unit_id"]]
        unit_ids = [str(item) for item in query_plan.get("unit_ids", [unit.unit_id]) if str(item) in units_by_id]
        if not unit_ids:
            unit_ids = [unit.unit_id]
        datasource = query_plan.get("datasource", "")
        resolved = resolved_by_id.get(unit.unit_id, {})
        normalized = await _execute_single_unit(state, unit, datasource, resolved, executor)
        return [normalized.model_copy(update={"unit_id": item}) for item in unit_ids]

    grouped_results = await asyncio.gather(*(execute_one(item) for item in state.get("query_plans", [])))
    results = [result for group in grouped_results for result in group]
    from src.reporting.profiling import is_runtime_quality_unit

    execution_units = {unit_id for unit_id, unit in units_by_id.items() if not is_runtime_quality_unit(unit)}
    execution_units.update(item.unit_id for item in results if item.status == "failed")
    outcome_results = [item for item in results if item.unit_id in execution_units] if execution_units else results
    # 数据类单元要求有数据行；直答类单元（元数据说明等）产出是文字结论，
    # 无数据行是正常形态，据「有无数据行」判定会把它们误算成部分完成。
    success_count = sum(
        item.status == "success" and (bool(item.rows) or item.answer_kind == "text")
        for item in outcome_results
    )
    failed_count = sum(item.status == "failed" for item in outcome_results)
    # 运行期补查追加的单元同样计入期望数，否则部分成功会被误判为成功。
    expected_count = len(execution_units) if execution_units else len(plan.analysis_units) + len(state.get("runtime_units", []) or [])
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
        if not rows and result.answer_kind != "text" and "暂无数据" not in warnings:
            warnings.append("当前查询没有返回数据，无法进行完整趋势分析")
        profiled.append(result.model_copy(update={"summary": summary, "warnings": warnings}))
    await _log_step(state, "report_profile_results", "succeeded")
    return {"unit_results": profiled}


# 方法作用：构建实际关系证据，并在所有查询完成后回填运行质量说明。
# Args: state - 含 resolved_units 的状态。
# Returns: RelationEvidence 列表和已回填质量说明的单元结果。
async def report_build_relations(state: ReportWorkbenchState) -> dict[str, Any]:
    await _log_step(state, "report_build_relations", "running")
    from src.reporting.profiling import is_runtime_quality_unit, summarize_runtime_quality

    quality_units = {unit_id: unit for unit_id, unit in _units_by_id(state).items() if is_runtime_quality_unit(unit)}
    results = list(state.get("unit_results", []))
    evidence_results = [item for item in results if item.unit_id not in quality_units]
    resolved_units = list(state.get("resolved_units", []))
    relations = build_relations(
        [item for item in resolved_units if item.get("unit_id") not in quality_units],
        evidence_results,
    )
    results = [
        summarize_runtime_quality(quality_units[item.unit_id], evidence_results, relations, resolved_units)
        if item.unit_id in quality_units and item.status != "failed" else item
        for item in results
    ]
    await _log_step(state, "report_build_relations", "succeeded")
    return {"relations": relations, "unit_results": results}


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
    artifact = build_report_document(state["plan"], list(state.get("unit_results", [])), report_id=state.get("report_id"), relations=list(state.get("relations", [])), template_id=str(state.get("template_id") or "insight"))
    if state.get("cross_business_analysis", {}).get("limitations"):
        artifact.document.limitations.extend(state["cross_business_analysis"]["limitations"])
    _append_metric_citations(artifact, list(state.get("metric_citations", []) or []))
    _append_findings(artifact, list(state.get("unit_findings", []) or []), list(state.get("resolved_units", []) or []))
    runtime_units = list(state.get("runtime_units", []) or [])
    if runtime_units:
        # 用户确认的是档位额度而非逐项内容，运行期追加必须显式披露。
        level = str(getattr(state["plan"], "divergence", "standard") or "standard")
        artifact.document.limitations.append(
            f"本次运行包含 {len(runtime_units)} 个由系统基于实际数据自动追加的分析单元（档位：{level}）"
        )
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


# 方法作用：合并计划单元与运行期追加单元，供执行与解析统一查找。
# Args: state - 当前状态。
# Returns: unit_id 到单元的映射。
def _units_by_id(state: ReportWorkbenchState) -> dict[str, Any]:
    units = {unit.unit_id: unit for unit in state["plan"].analysis_units}
    for unit in state.get("runtime_units", []) or []:
        units[unit.unit_id] = unit
    return units


# 方法作用：计算档位额度中尚未使用的部分。
# Args: state - 含 plan 与 runtime_units 的状态。
# Returns: (档位, 剩余额度)。
def _remaining_budget(state: ReportWorkbenchState) -> tuple[str, int]:
    from src.reporting.models import divergence_unit_budget

    plan = state["plan"]
    level = str(getattr(plan, "divergence", "standard") or "standard")
    # 档位是计划期与运行期共享的总授权，计划期已用的发散单元必须扣减。
    plan_used = sum(1 for unit in plan.analysis_units if getattr(unit, "origin", "user") == "diverged")
    runtime_used = len(state.get("runtime_units", []) or [])
    return level, max(0, divergence_unit_budget(level) - plan_used - runtime_used)


# 方法作用：取本次运行里每个单元已过授权过滤的指标列，判定侧与产出侧共用这一份派生。
# Args: state - 含 resolved_units 的状态。
# Returns: unit_id 到指标列列表的映射。
def _metrics_by_unit(state: ReportWorkbenchState) -> dict[str, list[str]]:
    return {
        str(item.get("unit_id", "")): [str(name) for name in (item.get("metrics", []) or [])]
        for item in state.get("resolved_units", [])
    }


# 方法作用：在结果样本里找出因缺少同环比基期而必须补查的单元。
# Args: state - 含计划、单元结果与发现的状态。
# Returns: (单元标识, 补查理由)；没有缺基期的单元时返回 None。
def _missing_baseline_reason(state: ReportWorkbenchState) -> tuple[str, str] | None:
    from src.reporting.profiling import needs_baseline_supplement, profile_inputs

    units = _units_by_id(state)
    findings = list(state.get("unit_findings", []) or [])
    metrics_by_unit = _metrics_by_unit(state)
    for result in state.get("unit_results", []) or []:
        unit_id = str(getattr(result, "unit_id", "") or "")
        unit = units.get(unit_id)
        if unit is None:
            continue
        # 与产出侧（report_analyze_units）走同一处派生：列取结果自身的列集合，
        # 指标取已过授权过滤的那份。判定侧自带一套列/指标口径时会与产出侧选到
        # 不同的数值列，重新打开「判足够但产出方产不出」的单向死区。
        columns, metrics = profile_inputs(result, metrics_by_unit.get(unit_id, []))
        reason = needs_baseline_supplement(
            unit, list(getattr(result, "rows", []) or []), findings, columns=columns, metrics=metrics
        )
        if reason:
            return unit_id, reason
    return None


# 方法作用：确定性判定本轮补查的触发来源与理由，供决策、提示词与页面共用同一份结论。
# Args: state - 含计划、单元结果与发现的状态。
# Returns: {"trigger", "unit_id", "reason"}；两个触发条件都不成立时返回 None。
def _supplement_context(state: ReportWorkbenchState) -> dict[str, Any] | None:
    """缺基期优先：它同时是「为什么补查」与「拿什么依据提建议」的唯一来源。"""
    missing_baseline = _missing_baseline_reason(state)
    if missing_baseline is not None:
        return {"trigger": "missing_baseline", "unit_id": missing_baseline[0], "reason": missing_baseline[1]}
    # 深挖没有显著发现说明数据正常，无需补查，且这条筛选不消耗 LLM 调用。
    significant = any(
        item.significance in {"high", "moderate"}
        for finding in state.get("unit_findings", []) or []
        for item in finding.items
    )
    if not significant:
        return None
    return {"trigger": "significant_finding", "unit_id": "", "reason": ""}


# 方法作用：判断是否进入运行期补查回路。三条硬前置条件全满足，且两条进入条件
# （显著发现 / 缺同环比基期）任一成立时，才允许调用 LLM。
# Args: state - 含结果、发现与档位的状态。
# Returns: "supplement" 表示进入回路，"continue" 表示继续后续节点。
def report_decide_supplement(state: ReportWorkbenchState) -> str:
    from src.reporting.models import divergence_max_rounds, max_plan_units

    level, remaining = _remaining_budget(state)
    if remaining <= 0:
        return "continue"
    if int(state.get("divergence_round", 0) or 0) >= divergence_max_rounds(level):
        return "continue"
    if len(state["plan"].analysis_units) + len(state.get("runtime_units", []) or []) >= max_plan_units():
        return "continue"
    # 缺同环比基期是与"已有发现"并列的独立触发条件：事故场景只拿回单月数据时
    # 恰好产不出任何发现，若耦合在发现上，回路永不触发、同比永远为 null。
    # 这条判定同样是确定性的，不消耗 LLM 调用。
    context = _supplement_context(state)
    if context is None:
        return "continue"
    if context["trigger"] == "missing_baseline":
        logger.info(
            "报表运行期补查因缺少同环比基期触发",
            unit_id=context["unit_id"],
            reason=context["reason"],
            level=level,
            divergence_round=int(state.get("divergence_round", 0) or 0),
        )
    return "supplement"


# 方法作用：把缺基期理由写进触发单元的 warnings，页面 quality 块据此向用户披露。
# Args: results - 当前单元结果列表；context - 补查上下文。
# Returns: 追加了披露文案的单元结果列表；非缺基期触发时原样返回。
def _disclose_supplement_reason(results: list[UnitResult], context: dict[str, Any] | None) -> list[UnitResult]:
    if not context or str(context.get("trigger", "")) != "missing_baseline":
        return results
    unit_id = str(context.get("unit_id", "") or "")
    text = f"数据不足以产出同环比：{context.get('reason', '')}；系统已据此追加补查单元"
    annotated: list[UnitResult] = []
    for result in results:
        if result.unit_id != unit_id or text in result.warnings:
            annotated.append(result)
            continue
        # warnings 上限与 UnitResult 契约一致，写在最后不挤掉前面的样本处置披露。
        annotated.append(result.model_copy(update={"warnings": [*result.warnings, text][:20]}))
    logger.info("报表缺基期理由已写入单元披露", unit_id=unit_id, matched=any(item.unit_id == unit_id for item in results))
    return annotated


# 方法作用：基于实际数据产出的发现提出补充单元，执行后并回状态。
# Args: state - 含 unit_findings、resolved_units 与授权数据源的状态。
# Returns: 追加的单元、解析摘要、执行结果与轮数。
async def report_plan_supplement(state: ReportWorkbenchState) -> dict[str, Any]:
    from src.reporting.models import divergence_units_per_round, max_plan_units

    await _log_step(state, "report_plan_supplement", "running")
    level, remaining = _remaining_budget(state)
    plan = state["plan"]
    round_index = int(state.get("divergence_round", 0) or 0) + 1
    limit = min(
        divergence_units_per_round(level),
        remaining,
        max_plan_units() - len(plan.analysis_units) - len(state.get("runtime_units", []) or []),
    )
    # 条件边函数不能写状态，触发理由必须由本节点重算一次并落进 state：下游的补查
    # 提示词与页面披露都从 supplement_context 取，保证同一份结论被两处复用。
    context = _supplement_context(state)
    update: dict[str, Any] = {
        "divergence_round": round_index,
        "unit_results": _disclose_supplement_reason(list(state.get("unit_results", []) or []), context),
    }
    if context is not None:
        update["supplement_context"] = context
        logger.info(
            "报表运行期补查触发来源已登记",
            trigger=context["trigger"],
            unit_id=context["unit_id"],
            round=round_index,
        )
    if limit <= 0:
        await _log_step(state, "report_plan_supplement", "succeeded")
        return update
    authorized = state.get("authorized_datasources", {}) or {}
    new_units = await _propose_runtime_units(state, limit=limit, authorized=authorized, context=context)
    if not new_units:
        await _log_step(state, "report_plan_supplement", "succeeded")
        return update
    resolved_entries: list[dict[str, Any]] = []
    results: list[UnitResult] = []
    executor = state.get("execute_unit")
    for unit in new_units:
        access = authorized.get(unit.datasource, {}) if isinstance(authorized, dict) else {}
        columns = list(access.get("allowed_columns", []) or [])
        entry = {
            "unit_id": unit.unit_id,
            "question": unit.question,
            "datasource": unit.datasource,
            "authorization_status": "granted",
            "columns": columns,
            "dimensions": [],
            "metrics": [name for name in unit.metrics if name in columns],
            "origin": "diverged",
            "decided_by": "runtime",
        }
        resolved_entries.append(entry)
        results.append(await _execute_single_unit(state, unit, unit.datasource, entry, executor))
    await _log_step(state, "report_plan_supplement", "succeeded")
    logger.info("报表运行期发散追加单元", round=round_index, added=len(new_units), level=level)
    return {
        **update,
        "runtime_units": [*(state.get("runtime_units", []) or []), *new_units],
        "resolved_units": [*(state.get("resolved_units", []) or []), *resolved_entries],
        "unit_results": [*update["unit_results"], *results],
    }


# 方法作用：调用补查模型提出运行期补充单元，失败时降级为空。
# Args: state - 含 unit_findings 的状态；limit - 本轮采纳上限；authorized - 已授权数据源映射；
#       context - 本轮补查的触发来源与理由（缺基期时作为模型的唯一依据）。
# Returns: 通过契约校验的单元列表；模型不可用或无建议时返回空列表。
async def _propose_runtime_units(
    state: ReportWorkbenchState,
    *,
    limit: int,
    authorized: dict[str, Any],
    context: dict[str, Any] | None = None,
) -> list[Any]:
    from src.llm.client import is_task_llm_available

    if not is_task_llm_available(_SUPPLEMENT_LLM_TASK):
        logger.info("报表运行期补查跳过", reason="无可用模型")
        return []
    findings = list(state.get("unit_findings", []) or [])
    summary = "\n".join(
        f"- [{finding.category}] {finding.title}：" + "；".join(
            f"{item.label}={item.value}" for item in finding.items[:3]
        )
        for finding in findings[:12]
    )
    baseline_triggered = str((context or {}).get("trigger", "") or "") == "missing_baseline"
    reason = str((context or {}).get("reason", "") or "")
    try:
        from src.llm.invocation import invoke_structured
        from src.llm.output_contracts import ReportSupplementOutput
        from src.llm.prompt_budget import PromptSection

        sections = [
            PromptSection("findings", f"实际数据算出的显著发现：\n{summary}", priority=100, min_chars=20, max_chars=6000),
        ]
        if baseline_triggered:
            # 零发现时 findings 段是空的，模型没有任何依据可提建议，只能按提示词
            # 合规地返回空数组——回路进了、模型调了，却结构性地产不出补查单元。
            # 缺基期理由是确定性判定，必须原样进提示词作为提建议的依据。
            sections.append(PromptSection(
                "baseline_reason",
                "本轮补查由「缺少同环比基期」这一确定性判定触发（与上方的显著发现无关）：\n"
                f"{reason}\n"
                "请围绕补齐该基期提出建议；这类建议不需要关联某一条已有发现，"
                "supports_finding 允许留空。",
                priority=95,
                min_chars=20,
                max_chars=2000,
            ))
        sections.append(PromptSection("limit", f"最多提出 {limit} 个建议。", priority=90, min_chars=10, max_chars=200))
        output = await invoke_structured(
            "report.supplement",
            sections,
            output_model=ReportSupplementOutput,
            task=_SUPPLEMENT_LLM_TASK,
            temperature=0.0,
            metadata={"node": "report_plan_supplement"},
        )
    except Exception:
        # 补查失败只留痕，已有结论与页面必须继续产出。
        logger.warning("报表运行期补查失败，按已有结果继续", exc_info=True)
        return []
    from src.reporting.planner import ReportPlanner

    raw: list[dict[str, Any]] = []
    for item in output.proposed_units:
        support = item.supports_finding.strip()
        # 过滤强度按触发来源区分：由显著发现触发时必须说清解释哪条发现，否则属无依据
        # 补查；由缺基期触发时「补齐基期」本身就是依据，再要求 supports_finding 非空
        # 会把这条路径永远卡死（事故里的同比因此一直是 null）。
        if not support and not baseline_triggered:
            logger.info("报表运行期补查建议缺少 supports_finding，已丢弃", question=item.question[:60])
            continue
        rationale = item.rationale or (f"解释已有发现：{support}" if support else f"补齐缺失基期：{reason}")
        raw.append({**item.model_dump(mode="python"), "rationale": rationale[:500]})
    return ReportPlanner._coerce_diverge_units(
        raw,
        allowed_datasources=set(authorized.keys()) if isinstance(authorized, dict) else set(),
        decided_by="runtime",
        limit=limit,
        reference_units=list(state["plan"].analysis_units),
    )


# 方法作用：对每个成功单元做确定性深挖，产出可披露的发现条目。
# Args: state - 含 unit_results 与 resolved_units 的状态。
# Returns: 全部单元的发现条目列表。
async def report_analyze_units(state: ReportWorkbenchState) -> dict[str, Any]:
    await _log_step(state, "report_analyze_units", "running")
    from src.reporting.profiling import profile_inputs, profile_unit

    metrics_by_unit = _metrics_by_unit(state)
    collected: list[Any] = []
    for result in state.get("unit_results", []):
        # 失败单元没有数据可加工；无命中数据源的单元行集为空，自然不产出。
        if getattr(result, "status", "") == "failed":
            continue
        # 与判定侧（_missing_baseline_reason）共用同一处输入派生，两侧不可能选到不同数值列。
        columns, metrics = profile_inputs(result, metrics_by_unit.get(result.unit_id, []))
        collected.extend(profile_unit(
            unit_id=result.unit_id,
            rows=list(result.rows),
            columns=columns,
            metrics=metrics,
            truncated=bool(getattr(result, "truncated", False)),
        ))
    await _log_step(state, "report_analyze_units", "succeeded")
    logger.info("报表确定性深挖汇总完成", unit_count=len(state.get("unit_results", [])), finding_count=len(collected))
    return {"unit_findings": collected}


# 方法作用：把确定性深挖发现追加为页面区段。
# Args: artifact - 已构建的页面产物；findings - 发现条目；units - 已解析单元摘要。
# Returns: 无返回值；无发现或区段校验失败时保持页面不变。
def _append_findings(artifact: ReportArtifact, findings: list[Any], units: list[dict[str, Any]]) -> None:
    if not findings:
        return
    from src.reporting.document import ReportBlock, ReportSection, layout_for_block, unit_short_title

    questions = {str(item.get("unit_id", "")): str(item.get("question", "") or "") for item in units}
    blocks: list[ReportBlock] = []
    for index, finding in enumerate(findings):
        # 标题用业务短标题：整段 question（60+ 字）铺在每条发现上又长又难读。
        by_unit = unit_short_title(questions.get(finding.unit_id, ""), finding.unit_id)
        blocks.append(ReportBlock(
            id=f"finding-{index}-{finding.category}",
            section_id="findings",
            type="findings",
            title=f"{by_unit} · {finding.title}",
            data_ref="",
            content={
                "category": finding.category,
                "method": finding.method,
                "truncated": bool(finding.truncated),
                "limitations": list(finding.limitations),
                "items": [item.model_dump(mode="json") for item in finding.items],
            },
            layout=layout_for_block("findings"),
        ))
    try:
        section = ReportSection(
            id="findings",
            title="确定性深挖发现",
            subtitle="由已取回数据直接计算，不经过模型转述；每条附计算方法",
            blocks=blocks,
        )
    except ValueError:
        logger.warning(
            "深挖发现区段被页面校验拒绝，已降级为不展示",
            finding_count=len(findings),
            exc_info=True,
        )
        return
    artifact.document.sections.append(section)


# 方法作用：把已批准指标口径追加为页面来源区段。
# Args: artifact - 已构建的页面产物；citations - 指标口径快照列表。
# Returns: 无返回值；无可引用口径或区段校验失败时保持页面不变。
def _append_metric_citations(artifact: ReportArtifact, citations: list[dict[str, Any]]) -> None:
    if not citations:
        return
    from src.reporting.document import ReportBlock, ReportSection, layout_for_block

    summary = "本报表按租户指标目录的已批准口径计算：" + "；".join(
        f"{item.get('name') or item.get('code')} v{item.get('version')}" for item in citations
    )
    try:
        # 页面块校验会拒绝含脚本特征的文本；口径表达式来自指标目录，
        # 但装饰性来源块无权让整份报表构建失败，失败时降级为不展示。
        section = ReportSection(
            id="metric_refs",
            title="指标口径来源",
            subtitle="口径取自租户指标目录的已批准版本，可追溯",
            blocks=[ReportBlock(
                section_id="metric_refs",
                type="source_citation",
                title="已批准口径",
                content={"citation_kind": "metric_catalog", "items": citations, "summary": summary},
                layout=layout_for_block("source_citation"),
            )],
        )
    except ValueError:
        logger.warning(
            "指标口径来源区段被页面校验拒绝，已降级为不展示",
            citation_codes=[str(item.get("code", "")) for item in citations],
            exc_info=True,
        )
        return
    artifact.document.sections.append(section)


# 方法作用：汇总各单元解析出的指标口径引用，供页面做口径溯源。
# Args: state - 含 unit_results 的状态。
# Returns: 按指标编码去重后的口径引用列表。
async def report_attach_metric_refs(state: ReportWorkbenchState) -> dict[str, Any]:
    await _log_step(state, "report_attach_metric_refs", "running")
    # 保守档保持"完全不加料"的语义。divergence 字段由 23.17 引入，
    # 此处按 getattr 读取，使该字段落地后闸门自动生效，无需二次改动。
    if str(getattr(state.get("plan"), "divergence", "standard") or "standard") == "conservative":
        await _log_step(state, "report_attach_metric_refs", "succeeded")
        return {"metric_citations": []}
    collected: dict[str, dict[str, Any]] = {}
    for result in state.get("unit_results", []):
        for citation in list(getattr(result, "metric_citations", []) or []):
            code = str(citation.get("code", "") or "")
            if code:
                collected[code] = dict(citation)
    await _log_step(state, "report_attach_metric_refs", "succeeded")
    return {"metric_citations": list(collected.values())}


# 方法作用：再次执行 DSL 和大小约束校验，防止自定义执行器写入非法页面。
# Args: state - 含 ReportArtifact 的状态。
# Returns: 校验后的 ReportArtifact。
async def report_validate_document(state: ReportWorkbenchState) -> dict[str, Any]:
    await _log_step(state, "report_validate_document", "running")
    artifact = state.get("artifact")
    if not isinstance(artifact, ReportArtifact):
        raise ValueError("报表页面产物缺失")
    # 块预算超限时按追加顺序裁剪增量区段（深挖发现、口径来源排在末尾）。
    # 这里绝不能直接抛错：单元已经成功取数，多出几块就丢弃整份报表的代价远大于少渲染几段。
    from src.reporting.document import truncate_document_to_block_budget

    truncated = truncate_document_to_block_budget(artifact.document)
    try:
        artifact.document = artifact.document.model_validate(artifact.document.model_dump(mode="json"))
    except ValueError:
        logger.error("报表页面校验失败", truncated_blocks=truncated, exc_info=True)
        raise
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
    graph.add_node("report_analyze_units", report_analyze_units)
    graph.add_node("report_plan_supplement", report_plan_supplement)
    graph.add_node("report_build_relations", report_build_relations)
    graph.add_node("report_synthesize_insights", report_synthesize_insights)
    graph.add_node("report_attach_metric_refs", report_attach_metric_refs)
    graph.add_node("report_plan_layout", report_plan_layout)
    graph.add_node("report_validate_document", report_validate_document)
    graph.set_entry_point("report_load_policy")
    graph.add_edge("report_load_policy", "report_resolve_units")
    graph.add_edge("report_resolve_units", "report_retrieve_schema")
    graph.add_edge("report_retrieve_schema", "report_plan_queries")
    graph.add_edge("report_plan_queries", "report_execute_units")
    graph.add_edge("report_execute_units", "report_profile_results")
    graph.add_edge("report_profile_results", "report_analyze_units")
    # 受控运行期回路：只有决策函数返回 supplement 时才补查，且补查后回到深挖重算发现。
    graph.add_conditional_edges(
        "report_analyze_units",
        report_decide_supplement,
        {"supplement": "report_plan_supplement", "continue": "report_build_relations"},
    )
    graph.add_edge("report_plan_supplement", "report_analyze_units")
    graph.add_edge("report_build_relations", "report_synthesize_insights")
    graph.add_edge("report_synthesize_insights", "report_attach_metric_refs")
    graph.add_edge("report_attach_metric_refs", "report_plan_layout")
    graph.add_edge("report_plan_layout", "report_validate_document")
    graph.add_edge("report_validate_document", END)
    return graph.compile()
