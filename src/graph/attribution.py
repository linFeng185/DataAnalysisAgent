"""触点归因编排：受约束模型推荐、数据集选择和证据资格路由。"""

from __future__ import annotations

import json
from typing import Any, Literal

from pydantic import BaseModel, Field

from src.graph.state import AnalysisState
from src.llm.client import is_task_llm_available
from src.llm.prompt_budget import PromptSection
from src.logging_config import get_logger
from src.tools.touchpoint_attribution import (
    AttributionColumnMapping,
    TouchpointAttributionEngine,
    TouchpointAttributionModel,
    attribution_model_display_name,
    detect_explicit_attribution_model,
    infer_attribution_column_mapping,
    is_touchpoint_attribution_request,
)

logger = get_logger(__name__)
_SELECTION_SOURCES = Literal["user_explicit", "llm_recommended", "rule_default"]


class AttributionResolution(BaseModel):
    """触点归因编排返回给分析节点的内部结构。"""

    applied: bool = False
    analysis: dict[str, Any] = Field(default_factory=dict)
    analysis_rows: list[dict[str, Any]] = Field(default_factory=list)
    deterministic_findings: str = ""
    selection: dict[str, str] = Field(default_factory=dict)


class _AttributionDataset(BaseModel):
    """主查询或补查提供的一组候选路径数据。"""

    source: Literal["primary", "supplemental"]
    rows: list[dict[str, Any]] = Field(default_factory=list)
    row_count: int = 0
    complete: bool = False
    question: str = ""


# 方法作用：在触点归因请求上完成模型选择、资格复核和确定性计算。
# Args: state - 当前分析状态；user_query - 用户原问题；relevant_tables - 当前授权 Schema。
# Returns: 非触点归因返回 None，否则返回可直接合并的分析结构。
async def resolve_touchpoint_attribution(
    state: AnalysisState,
    *,
    user_query: str,
    relevant_tables: list[dict[str, Any]],
) -> AttributionResolution | None:
    """大模型只推荐枚举，资格和归因数值全部由规则引擎决定。"""
    if not is_touchpoint_attribution_request(user_query):
        return None
    from src.config import get_settings

    settings = get_settings()
    engine = TouchpointAttributionEngine(
        minimum_converted_paths=settings.attribution_min_converted_paths,
        half_life_days=settings.attribution_time_decay_half_life_days,
    )
    dataset = _select_dataset(state)
    model, selection_source, selection_reason = await _select_model(
        user_query,
        dataset,
        engine,
        state.get("attribution_selection", {}) or {},
    )
    selection = {
        "model": model.value,
        "source": selection_source,
        "reason": selection_reason,
    }
    eligibility = engine.evaluate(dataset.rows, model)
    if not dataset.complete:
        eligibility.eligible = False
        if dataset.rows:
            eligibility.missing_requirements.append(
                f"路径明细不完整：仅获得 {len(dataset.rows)}/{dataset.row_count} 行",
            )
        else:
            eligibility.missing_requirements.append("未获得路径级触点行")
    eligibility.missing_requirements = list(dict.fromkeys(
        eligibility.missing_requirements,
    ))
    logger.info(
        "触点归因选择与资格边界",
        model=model.value,
        selection_source=selection_source,
        selection_reason=selection_reason[:120],
        dataset_source=dataset.source,
        dataset_rows=len(dataset.rows),
        dataset_complete=dataset.complete,
        eligible=eligibility.eligible,
    )
    if eligibility.eligible:
        calculated = engine.calculate(dataset.rows, model)
        attribution = {
            "status": "applied",
            "selected_model": model.value,
            "model_display_name": calculated.model_display_name,
            "selection_source": selection_source,
            "selection_reason": selection_reason,
            "dataset_source": dataset.source,
            "eligibility": eligibility.model_dump(mode="python"),
            "total_attributed_value": calculated.total_attributed_value,
            "total_paths": calculated.total_paths,
            "converted_paths": calculated.converted_paths,
            "unconverted_paths": calculated.unconverted_paths,
            "half_life_days": calculated.half_life_days,
            "results": [item.model_dump(mode="python") for item in calculated.results],
        }
        top = calculated.results[:3]
        summary = (
            f"已使用{calculated.model_display_name}分析 "
            f"{calculated.converted_paths} 条已转化路径，"
            f"归因总价值为 {calculated.total_attributed_value}。"
        )
        insights = [
            f"{item.touchpoint}：归因价值 {item.attributed_value}，"
            f"占比 {item.attribution_pct}%"
            for item in top
        ]
        deterministic_findings = "\n".join((summary, *insights))
        analysis = {
            "summary": summary,
            "insights": insights,
            "recommended_chart_type": "bar",
            "follow_up_questions": [],
            "processor_name": "touchpoint_attribution",
            "confidence": calculated.confidence,
            "data_quality": [
                f"路径共 {calculated.total_paths} 条，其中已转化 "
                f"{calculated.converted_paths} 条、未转化 {calculated.unconverted_paths} 条",
            ],
            "limitations": calculated.warnings,
            "evidence_status": "sufficient",
            "evidence_gaps": [],
            "supplemental_questions": [],
            "attribution": attribution,
        }
        return AttributionResolution(
            applied=True,
            analysis=analysis,
            analysis_rows=dataset.rows,
            deterministic_findings=deterministic_findings,
            selection=selection,
        )

    evidence = list(state.get("supplemental_evidence", []) or [])
    current_round = int(state.get("supplemental_query_round", 0) or 0)
    schema_mapping = _schema_mapping(relevant_tables)
    can_supplement = (
        dataset.complete
        and not evidence
        and current_round < settings.max_supplemental_query_rounds
        and _schema_can_supply_model(schema_mapping, model)
    ) or (
        not dataset.rows
        and not evidence
        and current_round < settings.max_supplemental_query_rounds
        and _schema_can_supply_model(schema_mapping, model)
    )
    evidence_status = "supplementable" if can_supplement else "needs_user_input"
    supplemental_questions = [
        _supplemental_question(model),
    ] if can_supplement else []
    gaps = eligibility.missing_requirements or ["当前数据不满足触点归因资格"]
    analysis = {
        "summary": (
            f"当前证据不足，不能执行{attribution_model_display_name(model)}："
            + "；".join(gaps[:3])
        ),
        "insights": [],
        "recommended_chart_type": "table",
        "follow_up_questions": (
            [] if can_supplement else ["请补充完整的路径级触点数据或缩小查询范围"]
        ),
        "processor_name": "touchpoint_attribution",
        "confidence": "low",
        "data_quality": eligibility.warnings,
        "limitations": gaps,
        "evidence_status": evidence_status,
        "evidence_gaps": gaps,
        "supplemental_questions": supplemental_questions,
        "attribution": {
            "status": "insufficient",
            "selected_model": model.value,
            "model_display_name": attribution_model_display_name(model),
            "selection_source": selection_source,
            "selection_reason": selection_reason,
            "dataset_source": dataset.source,
            "eligibility": eligibility.model_dump(mode="python"),
            "results": [],
        },
    }
    return AttributionResolution(applied=False, analysis=analysis, selection=selection)


# 方法作用：按用户显式要求、模型推荐和规则默认值选择归因模型。
# Args: user_query - 用户问题；dataset - 候选数据；engine - 资格引擎。
# Returns: 模型、选择来源和可公开理由。
async def _select_model(
    user_query: str,
    dataset: _AttributionDataset,
    engine: TouchpointAttributionEngine,
    persisted_selection: dict[str, str],
) -> tuple[TouchpointAttributionModel, str, str]:
    explicit = detect_explicit_attribution_model(user_query)
    if explicit is not None:
        return explicit, "user_explicit", "用户在问题中明确指定该归因模型"
    try:
        persisted_model = TouchpointAttributionModel(
            str(persisted_selection.get("model", "") or ""),
        )
    except ValueError:
        persisted_model = None
    if persisted_model is not None:
        source = str(persisted_selection.get("source", "") or "rule_default")
        if source not in {"user_explicit", "llm_recommended", "rule_default"}:
            source = "rule_default"
        reason = str(persisted_selection.get("reason", "") or "补查前已确定归因模型")
        return persisted_model, source, reason
    if is_task_llm_available("analyze_result"):
        try:
            from src.llm.invocation import invoke_structured
            from src.llm.output_contracts import AttributionModelRecommendationOutput

            profile = {
                "rows": len(dataset.rows),
                "row_count": dataset.row_count,
                "complete": dataset.complete,
                "columns": list(dataset.rows[0]) if dataset.rows else [],
                "model_eligibility": {
                    model.value: engine.evaluate(dataset.rows, model).eligible
                    for model in TouchpointAttributionModel
                },
            }
            recommendation = await invoke_structured(
                "attribution.select",
                [
                    PromptSection(
                        "query",
                        f"## 用户目标\n{user_query}",
                        priority=100,
                        min_chars=100,
                        max_chars=1200,
                    ),
                    PromptSection(
                        "profile",
                        "## 数据资格概览\n" + json.dumps(
                            profile,
                            ensure_ascii=False,
                            separators=(",", ":"),
                        ),
                        priority=90,
                        min_chars=300,
                        max_chars=1800,
                    ),
                ],
                output_model=AttributionModelRecommendationOutput,
                task="analyze_result",
                temperature=0.0,
                reasoning=None,
                metadata={"node": "analyze_result", "stage": "attribution_selection"},
            )
            model = TouchpointAttributionModel(recommendation.model)
            return model, "llm_recommended", recommendation.reason
        except Exception as exc:
            logger.error("触点归因模型推荐失败，使用中性默认值", error=str(exc), exc_info=True)
    return (
        TouchpointAttributionModel.LINEAR,
        "rule_default",
        "分析模型不可用或推荐失败，使用不预设首末触点重要性的中性线性归因",
    )


# 方法作用：在主查询和成功补查数据中选择最完整、字段最匹配的一组路径数据。
# Args: state - 当前分析状态。
# Returns: 候选路径数据及完整性来源。
def _select_dataset(state: AnalysisState) -> _AttributionDataset:
    primary_rows = list(
        state.get("attribution_candidate_rows", [])
        or state.get("query_result_sample", [])
        or [],
    )
    primary_count = int(state.get("query_result_full_count", 0) or len(primary_rows))
    candidates = [_AttributionDataset(
        source="primary",
        rows=primary_rows,
        row_count=primary_count,
        complete=(
            not bool(state.get("query_result_truncated", False))
            and primary_count <= len(primary_rows)
        ),
    )]
    for item in reversed(list(state.get("supplemental_evidence", []) or [])):
        if not isinstance(item, dict) or not item.get("success"):
            continue
        rows = list(item.get("data", []) or [])
        row_count = int(item.get("row_count", 0) or len(rows))
        candidates.append(_AttributionDataset(
            source="supplemental",
            rows=rows,
            row_count=row_count,
            complete=bool(item.get("complete", row_count <= len(rows))),
            question=str(item.get("question", "") or ""),
        ))
    selected = max(candidates, key=_dataset_score)
    logger.info(
        "触点归因候选数据选择完成",
        source=selected.source,
        rows=len(selected.rows),
        row_count=selected.row_count,
        complete=selected.complete,
        score=_dataset_score(selected),
    )
    return selected


# 方法作用：按字段匹配、完整性和行数给候选数据评分。
# Args: dataset - 主查询或补查候选。
# Returns: 可用于 max 的稳定评分元组。
def _dataset_score(dataset: _AttributionDataset) -> tuple[int, int, int, int]:
    mapping = infer_attribution_column_mapping(list(dataset.rows[0]) if dataset.rows else [])
    mapped_roles = sum(bool(value) for value in mapping.model_dump().values())
    return (
        mapped_roles,
        int(dataset.complete),
        int(dataset.source == "supplemental"),
        len(dataset.rows),
    )


# 方法作用：从授权 Schema 的真实字段名构造归因字段映射。
# Args: relevant_tables - Schema 检索结果。
# Returns: 字段语义映射。
def _schema_mapping(relevant_tables: list[dict[str, Any]]) -> AttributionColumnMapping:
    columns = [
        str(column.get("name", "") or "")
        for table in relevant_tables
        if isinstance(table, dict)
        for column in (table.get("columns", []) or [])
        if isinstance(column, dict) and column.get("name")
    ]
    return infer_attribution_column_mapping(columns)


# 方法作用：判断当前授权 Schema 是否可能补齐选中模型的字段证据。
# Args: mapping - Schema 字段映射；model - 选中归因模型。
# Returns: 必需角色齐全时返回 True。
def _schema_can_supply_model(
    mapping: AttributionColumnMapping,
    model: TouchpointAttributionModel,
) -> bool:
    base = bool(
        mapping.path_id
        and mapping.touchpoint
        and mapping.converted
        and (mapping.sequence or mapping.timestamp)
    )
    if model is TouchpointAttributionModel.TIME_DECAY:
        return base and bool(mapping.timestamp)
    return base


# 方法作用：为自主补查生成不含 SQL 的明确路径级数据问题。
# Args: model - 当前需要执行的归因模型。
# Returns: SQL 生成节点可理解的自然语言补查问题。
def _supplemental_question(model: TouchpointAttributionModel) -> str:
    order_requirement = (
        "触点时间" if model is TouchpointAttributionModel.TIME_DECAY
        else "触点顺序或触点时间"
    )
    return (
        f"查询执行{attribution_model_display_name(model)}所需的完整路径级触点明细，"
        "每行一个触点，包含路径标识、触点渠道、"
        f"{order_requirement}、转化标记和可选转化价值；"
        "输出别名使用 path_id、touchpoint、touch_order 或 touch_time、"
        "is_conversion、conversion_value"
    )
