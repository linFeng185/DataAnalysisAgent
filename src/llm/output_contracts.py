"""LLM 结构化输出契约与兼容解析工具。"""

from __future__ import annotations

import json
from typing import Annotated, Any, Literal, TypeVar

from pydantic import BaseModel, ConfigDict, Field, field_validator

# 报表拆解单元数的理智上限：只拦模型失控输出，不承担业务限额。
# 业务限额由调用方 max_plan_units() 读配置后裁剪；该值不得小于
# reporting/models.py 的 _UNIT_COUNT_STATIC_MAX（有测试守住这条不变式）。
_MAX_PLAN_UNITS_SANITY = 200


class StrictOutputModel(BaseModel):
    """允许模型扩展字段，但只把契约字段暴露给业务节点。"""

    model_config = ConfigDict(extra="ignore", str_strip_whitespace=True)


class IntentOutput(StrictOutputModel):
    """意图分类输出。"""

    intent: Literal[
        "query", "aggregation", "trend", "attribution", "metadata", "chat",
        "file_analysis", "meta",
    ]


class TaskPlanOutput(StrictOutputModel):
    """LLM 路由阶段的结构化任务计划。"""

    intent: Literal[
        "query", "aggregation", "trend", "attribution", "metadata", "chat",
        "file_analysis", "meta",
    ] = "query"
    operation: str = "query"
    confidence: float = Field(default=0.5, ge=0.0, le=1.0)


class DatasourceSelectionOutput(StrictOutputModel):
    """数据源选择输出。"""

    datasource: str = Field(min_length=1)


class DecomposeStep(StrictOutputModel):
    """查询分解中的单个子步骤。"""

    step: int = Field(ge=1)
    question: str = Field(min_length=1)
    depends_on: list[int] = Field(default_factory=list)
    output_columns: list[str] = Field(default_factory=list)


class DecomposeOutput(StrictOutputModel):
    """查询分解输出。"""

    needs_decompose: bool = False
    steps: list[DecomposeStep] = Field(default_factory=list, max_length=5)

    @field_validator("steps")
    @classmethod
    # 方法作用：校验查询分解只能依赖已经定义的更早步骤。
    # Args: cls - Pydantic 模型类；steps - 待校验的分解步骤。
    # Returns: 依赖关系合法的原步骤列表。
    def validate_dependencies(cls, steps: list[DecomposeStep]) -> list[DecomposeStep]:
        """拒绝引用未来步骤或不存在步骤，避免形成不可执行计划。"""
        known = {item.step for item in steps}
        for item in steps:
            if any(dependency not in known or dependency >= item.step for dependency in item.depends_on):
                raise ValueError("查询分解步骤依赖关系无效")
        return steps


class SQLGenerationOutput(StrictOutputModel):
    """SQL 生成输出。"""

    sql: str = ""
    explanation: str = ""
    assumptions: list[str] = Field(default_factory=list)
    confidence: Literal["high", "medium", "low"] = "medium"


class AnalysisOutput(StrictOutputModel):
    """分析结果输出。"""

    summary: str = ""
    # 结果字段的中文说明，键为结果列名。聚合或表达式计算出的字段尤其需要，供报表页做字段标注。
    field_descriptions: dict[str, str] = Field(default_factory=dict)
    insights: list[str] = Field(default_factory=list)
    data_quality: list[str] = Field(default_factory=list)
    limitations: list[str] = Field(default_factory=list)
    confidence: Literal["high", "medium", "low"] = "low"
    related_metrics: list[str] = Field(default_factory=list)
    business_impacts: list[str] = Field(default_factory=list)
    possible_causes: list[str] = Field(default_factory=list)
    recommended_actions: list[str] = Field(default_factory=list)
    evidence_status: Literal[
        "sufficient", "supplementable", "needs_user_input",
    ] = "sufficient"
    evidence_gaps: list[str] = Field(default_factory=list, max_length=5)
    supplemental_questions: list[str] = Field(default_factory=list, max_length=3)
    recommended_chart_type: Literal[
        "bar", "line", "pie", "scatter", "table", "heatmap",
    ] = "table"
    follow_up_questions: list[str] = Field(default_factory=list)


class AttributionModelRecommendationOutput(StrictOutputModel):
    """大模型对未指定触点归因模型的受约束推荐。"""

    model: Literal[
        "first_touch", "last_touch", "linear", "time_decay", "position_based",
    ]
    reason: str = Field(min_length=1, max_length=300)


class IndicatorReviewItem(StrictOutputModel):
    """模型对一个真实数值字段的指标属性复检结果。"""

    table: str = Field(min_length=1)
    column: str = Field(min_length=1)
    include: bool = False
    display_name: str = ""
    theme: str = "其他"


class IndicatorCatalogReviewOutput(StrictOutputModel):
    """指标目录模型复检输出。"""

    indicators: list[IndicatorReviewItem] = Field(default_factory=list)


class PolishOutput(StrictOutputModel):
    """结果润色输出。"""

    summary: str = ""


class TextAnswerOutput(StrictOutputModel):
    """直接回答或工具 Agent 的文本输出。"""

    answer: str = ""


class ImageAnalysisOutput(StrictOutputModel):
    """视觉识别只输出有界文字观察，不生成可执行动作。"""

    summary: str = Field(min_length=1, max_length=2000)
    observations: list[Annotated[str, Field(max_length=500)]] = Field(default_factory=list, max_length=20)
    extracted_text: str = Field(default="", max_length=8000)
    limitations: list[Annotated[str, Field(max_length=500)]] = Field(default_factory=list, max_length=10)


class ReportPlanUnit(StrictOutputModel):
    """大模型对报表需求拆解出的单个分析单元建议。"""

    unit_id: str = Field(default="", max_length=64)
    question: str = Field(min_length=1, max_length=2000)
    datasource: str = Field(default="", max_length=64)
    priority: Literal["primary", "secondary", "evidence"] = "primary"
    dimensions: list[str] = Field(default_factory=list, max_length=30)
    metrics: list[str] = Field(default_factory=list, max_length=30)


class ReportPlanUnitsOutput(StrictOutputModel):
    """报表需求拆解输出，只承载业务语义，不承载调度与权限字段。"""

    # 上一版在这里写死 12，而配置允许到 36：模型按用户要求正确拆出的 16 个单元
    # 在契约处被 too_long 拒收，异常降级成"模型规划不可用，已按规则拆分"，
    # 用户看到的是规则切出来的标题残渣。上限必须留给调用方按配置裁剪。
    analysis_units: list[ReportPlanUnit] = Field(default_factory=list, max_length=_MAX_PLAN_UNITS_SANITY)


class ReportUnitRoute(StrictOutputModel):
    """大模型对单个分析单元的数据源命中判定。"""

    unit_id: str = Field(min_length=1, max_length=64)
    datasources: list[str] = Field(default_factory=list, max_length=20)


class ReportUnitRoutesOutput(StrictOutputModel):
    """分析单元到数据源的命中映射；空列表表示没有数据源能支撑该问题。"""

    routes: list[ReportUnitRoute] = Field(default_factory=list, max_length=200)


class ReportDivergeUnit(StrictOutputModel):
    """计划期发散建议。契约里不含 unit_id，由服务端分配，避免与用户单元冲突。"""

    question: str = Field(min_length=1, max_length=2000)
    datasource: str = Field(default="", max_length=64)
    rationale: str = Field(default="", max_length=500)
    expected_signal: str = Field(default="", max_length=200)


class ReportDivergeOutput(StrictOutputModel):
    """计划期发散输出，最多 8 条建议，实际采纳量由档位额度决定。"""

    proposed_units: list[ReportDivergeUnit] = Field(default_factory=list, max_length=8)


class ReportSupplementUnit(StrictOutputModel):
    """运行期补充建议。必须说明它解释或验证了哪一条已有发现。"""

    question: str = Field(min_length=1, max_length=2000)
    datasource: str = Field(default="", max_length=64)
    rationale: str = Field(default="", max_length=500)
    supports_finding: str = Field(default="", max_length=200)


class ReportSupplementOutput(StrictOutputModel):
    """运行期补查输出；空数组是最常见也最正确的结果。"""

    proposed_units: list[ReportSupplementUnit] = Field(default_factory=list, max_length=6)


class ReportTitleOutput(StrictOutputModel):
    """报表摘要标题输出。上限给到 40 字，服务端仍按 20 字截断后再落库。"""

    title: str = Field(min_length=1, max_length=40)


T = TypeVar("T", bound=BaseModel)


# 方法作用：从纯 JSON 或 fenced JSON 解析并验证指定输出模型。
# Args: content - 模型原始输出；model - 目标 Pydantic 模型类型。
# Returns: 已完成结构校验的模型实例。
def parse_json_model(content: Any, model: type[T]) -> T:
    """从纯 JSON 或 Markdown fenced JSON 中解析并校验模型输出。"""
    if isinstance(content, BaseModel):
        return model.model_validate(content.model_dump())
    text = str(content or "").strip()
    if text.startswith("```"):
        text = text.removeprefix("```").removeprefix("json").removesuffix("```").strip()
    try:
        payload = json.loads(text)
    except json.JSONDecodeError:
        start = text.find("{")
        end = text.rfind("}")
        if start < 0 or end <= start:
            raise
        payload = json.loads(text[start : end + 1])
    if not isinstance(payload, dict):
        raise ValueError("LLM 输出不是 JSON 对象")
    return model.model_validate(payload)
