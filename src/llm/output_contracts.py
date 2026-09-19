"""LLM 结构化输出契约与兼容解析工具。"""

from __future__ import annotations

import json
from typing import Annotated, Any, Literal, TypeVar

from pydantic import BaseModel, ConfigDict, Field, field_validator

from src.logging_config import get_logger

logger = get_logger(__name__)

# 报表拆解单元数的理智上限：只拦模型失控输出，不承担业务限额。
# 业务限额由调用方 max_plan_units() 读配置后裁剪；该值不得小于
# reporting/models.py 的 _UNIT_COUNT_STATIC_MAX（有测试守住这条不变式）。
_MAX_PLAN_UNITS_SANITY = 200

# 报表单元精简标题的契约上限。report.plan / report.diverge / report.supplement
# 三个单元契约共用这一个符号，不各写一个 12——上一版只给拆解契约加了 title，
# 发散契约无处可给标题，真实验收里 6/14 个区段标题因此退回截断的问题原文。
# 这是给模型的硬闸（提示词里的「不超过 12 个字」）；单元字段侧另有更宽的
# reporting/models.py::_UNIT_TITLE_MAX_CHARS（20），解析层负责把两者接上：
# 模型超一点不致命，但不能原样灌进单元字段，否则整个单元会被拒收。
_REPORT_UNIT_TITLE_MAX_CHARS = 12


# 方法作用：把模型给的单元标题归一为单行并截断到契约长度，永不抛错。
# Args: value - 模型输出的原始标题值。
# Returns: 单行、去空白、已截断的标题字符串。
def _clip_unit_title(value: Any) -> str:
    """标题只是展示字段，超长必须截断而不是拒收。

    历史教训：给展示字段设硬上限（query_count 的 le=64、analysis_units 的
    max_length=12）会让一个字段的越界毁掉整次规划——模型多写一个字，整批单元
    在 parse_json_model 处校验失败，降级成规则拆分的标题残渣。故此处按 before
    模式先截断再校验。
    """
    return " ".join(str(value or "").split())[:_REPORT_UNIT_TITLE_MAX_CHARS]


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


# 模型给 priority 时用的同义词：只归一不拒收。
# 实机事故：模型对补充单元写 priority="supplementary"，Literal 校验把**整批 16 个单元**
# 打回，plan 整体降级成规则切分的 28 个垃圾单元——一个枚举值毁掉整份报表的规划。
_PRIORITY_SYNONYMS: dict[str, str] = {
    "primary": "primary",
    "main": "primary",
    "core": "primary",
    "secondary": "secondary",
    "supplementary": "secondary",
    "supplemental": "secondary",
    "supporting": "secondary",
    "extra": "secondary",
    "evidence": "evidence",
}


class ReportPlanUnitTimeRange(StrictOutputModel):
    """单元的时间范围建议；模型无法判断时留空，由服务端补默认值。"""

    kind: Literal["relative", "absolute", "all"] = "relative"
    value: str = Field(default="", max_length=64)

    # 方法作用：校验前把无法识别的 kind 归一到 relative。
    # Args: value - 模型给出的 kind。
    # Returns: 白名单内的 kind；无法识别时返回 relative。
    @field_validator("kind", mode="before")
    @classmethod
    def _normalize_kind(cls, value: Any) -> str:
        """时间范围是描述性字段，越界只降级，不能让它拖垮整批计划。"""
        normalized = str(value or "").strip().lower()
        return normalized if normalized in {"relative", "absolute", "all"} else "relative"


class ReportPlanUnit(StrictOutputModel):
    """大模型对报表需求拆解出的单个分析单元建议。"""

    unit_id: str = Field(default="", max_length=64)
    question: str = Field(min_length=1, max_length=2000)
    # 单元精简标题，随同一次拆解调用产出，用于页面区段标题。
    # 超长由 _clip_unit_title 截断，不抛错：展示字段越界不能拖垮整批单元。
    title: str = Field(default="", max_length=_REPORT_UNIT_TITLE_MAX_CHARS)
    datasource: str = Field(default="", max_length=64)
    priority: Literal["primary", "secondary", "evidence"] = "primary"
    dimensions: list[str] = Field(default_factory=list, max_length=30)
    metrics: list[str] = Field(default_factory=list, max_length=30)
    time_range: ReportPlanUnitTimeRange | None = None

    # 方法作用：校验前把 priority 的同义词与未知值归一到白名单取值。
    # Args: value - 模型给出的 priority。
    # Returns: primary / secondary / evidence 之一。
    @field_validator("priority", mode="before")
    @classmethod
    def _normalize_priority(cls, value: Any) -> str:
        """优先级只是排序提示：越界必须归一，不能让一个枚举值打回整批单元。"""
        normalized = _PRIORITY_SYNONYMS.get(str(value or "").strip().lower(), "")
        if not normalized:
            logger.debug("优先级取值未识别，归一到 secondary", raw=str(value or "")[:32])
            return "secondary"
        return normalized

    # 方法作用：校验前先归一标题，避免超长标题让整批拆解结果被拒收。
    # Args: value - 模型给出的标题原文。
    # Returns: 截断后的标题。
    @field_validator("title", mode="before")
    @classmethod
    def _normalize_title(cls, value: Any) -> str:
        return _clip_unit_title(value)


class ReportPlanUnitsOutput(StrictOutputModel):
    """报表需求拆解输出，只承载业务语义，不承载调度与权限字段。"""

    # 上一版在这里写死 12，而配置允许到 36：模型按用户要求正确拆出的 16 个单元
    # 在契约处被 too_long 拒收，异常降级成"模型规划不可用，已按规则拆分"，
    # 用户看到的是规则切出来的标题残渣。上限必须留给调用方按配置裁剪。
    analysis_units: list[ReportPlanUnit] = Field(default_factory=list, max_length=_MAX_PLAN_UNITS_SANITY)


class ChartSpec(StrictOutputModel):
    """单张图表的呈现方案；三种形态共用这一个契约。

    形态①一个单元多张图 = charts 里多条；形态②一张图多系列 = series_fields 多项；
    形态③可切换指标/维度 = switchable_fields 非空。
    """

    chart_type: Literal["line", "bar", "pie", "scatter", "heatmap", "table"] = "bar"
    title: str = Field(default="", max_length=40)
    x_field: str = Field(default="", max_length=64)
    # 指标列：决定"看什么数值"。多个指标 = 一张图多条系列。
    series_fields: list[str] = Field(default_factory=list, max_length=10)
    # 分组维度：决定"按什么拆开看"。x 轴取该列唯一值，分组列唯一值当系列名。
    # 空串表示不分组（单指标，或多个指标各一条系列）。
    group_field: str = Field(default="", max_length=64)
    # 可切换的分组维度候选列：服务端把每个候选的分组结果都算好下发，
    # 前端切换只替换 series，不重新请求后端。
    switchable_fields: list[str] = Field(default_factory=list, max_length=10)

    # 方法作用：校验前把空值归一为空串。
    # Args: value - 模型给出的字符串字段值。
    # Returns: 空值返回空串，其余原样转字符串。
    @field_validator("title", "x_field", "group_field", mode="before")
    @classmethod
    def _coerce_blank_text(cls, value: Any) -> str:
        """展示字段写 null 是常见形态，不能让一个 null 把整批图表方案打回（实机复现过）。"""
        return "" if value is None else str(value)

    # 方法作用：校验前把列表类字段收敛到契约上限，避免模型多给几项就整批被拒收。
    # Args: value - 模型给出的原始列表值。
    # Returns: 截断到上限且只保留非空字符串的列表。
    @field_validator("series_fields", "switchable_fields", mode="before")
    @classmethod
    def _clip_field_list(cls, value: Any) -> list[str]:
        if value is None:
            return []
        if isinstance(value, str):
            value = [value]
        try:
            items = list(value)
        except TypeError:
            return []
        return [str(item).strip() for item in items if str(item or "").strip()][:10]


class ReportChartPlan(StrictOutputModel):
    """一个单元的图表方案：可多张图，每张可多系列、可切换。"""

    charts: list[ChartSpec] = Field(default_factory=list, max_length=6)

    # 方法作用：校验前收敛图表张数，超出的方案被裁剪而不是让整批决策失败。
    # Args: value - 模型给出的图表列表。
    # Returns: 最多 6 条的图表列表。
    @field_validator("charts", mode="before")
    @classmethod
    def _clip_charts(cls, value: Any) -> list[Any]:
        if value is None:
            return []
        try:
            return list(value)[:6]
        except TypeError:
            return []


class ReportUnitRoute(StrictOutputModel):
    """大模型对单个分析单元的数据源命中判定。"""

    unit_id: str = Field(min_length=1, max_length=64)
    datasources: list[str] = Field(default_factory=list, max_length=20)


class ReportUnitRoutesOutput(StrictOutputModel):
    """分析单元到数据源的命中映射；空列表表示没有数据源能支撑该问题。"""

    routes: list[ReportUnitRoute] = Field(default_factory=list, max_length=200)


class ReportNodeRouteDecision(StrictOutputModel):
    """模型为一次报表单元执行选出的节点集合。

    只选"参与哪些节点"，**不排顺序**：顺序由服务端按 node_registry.CANONICAL_ORDER
    固定，否则模型把 execute_sql 排到安全校验之前就是漏洞（见 spec/50）。
    """

    nodes: list[str] = Field(default_factory=list, max_length=16)
    reason: str = Field(default="", max_length=300)

    # 方法作用：校验前收敛节点清单，越界只截断不拒收。
    # Args: value - 模型给出的节点列表。
    # Returns: 去空白、去空项、最多 16 条的节点列表。
    @field_validator("nodes", mode="before")
    @classmethod
    def _clip_nodes(cls, value: Any) -> list[str]:
        if value is None:
            return []
        if isinstance(value, str):
            value = [value]
        try:
            items = list(value)
        except TypeError:
            return []
        return [str(item).strip() for item in items if str(item or "").strip()][:16]


class ReportDivergeUnit(StrictOutputModel):
    """计划期发散建议。契约里不含 unit_id，由服务端分配，避免与用户单元冲突。"""

    question: str = Field(min_length=1, max_length=2000)
    # 与 ReportPlanUnit.title 同源同限（_REPORT_UNIT_TITLE_MAX_CHARS）：发散单元同样要
    # 出现在页面区段标题上，契约不给字段模型就没机会给，展示层只能截断问题原文。
    title: str = Field(default="", max_length=_REPORT_UNIT_TITLE_MAX_CHARS)
    datasource: str = Field(default="", max_length=64)
    rationale: str = Field(default="", max_length=500)
    expected_signal: str = Field(default="", max_length=200)

    # 方法作用：校验前先归一标题，避免超长标题让整批发散建议被拒收。
    # Args: value - 模型给出的标题原文。
    # Returns: 截断后的标题。
    @field_validator("title", mode="before")
    @classmethod
    def _normalize_title(cls, value: Any) -> str:
        return _clip_unit_title(value)


class ReportDivergeOutput(StrictOutputModel):
    """计划期发散输出，最多 8 条建议，实际采纳量由档位额度决定。"""

    proposed_units: list[ReportDivergeUnit] = Field(default_factory=list, max_length=8)


class ReportSupplementUnit(StrictOutputModel):
    """运行期补充建议。必须说明它解释或验证了哪一条已有发现。"""

    question: str = Field(min_length=1, max_length=2000)
    title: str = Field(default="", max_length=_REPORT_UNIT_TITLE_MAX_CHARS)
    datasource: str = Field(default="", max_length=64)
    rationale: str = Field(default="", max_length=500)
    supports_finding: str = Field(default="", max_length=200)

    # 方法作用：校验前先归一标题，避免超长标题让整批补查建议被拒收。
    # Args: value - 模型给出的标题原文。
    # Returns: 截断后的标题。
    @field_validator("title", mode="before")
    @classmethod
    def _normalize_title(cls, value: Any) -> str:
        return _clip_unit_title(value)


class ReportSupplementOutput(StrictOutputModel):
    """运行期补查输出；空数组是最常见也最正确的结果。"""

    proposed_units: list[ReportSupplementUnit] = Field(default_factory=list, max_length=6)


class ReportTitleOutput(StrictOutputModel):
    """报表摘要标题输出。上限给到 40 字，服务端仍按 20 字截断后再落库。"""

    title: str = Field(min_length=1, max_length=40)


class SampleDecision(BaseModel):
    """样本超限时的处置建议：聚合、缩小范围或原样保留，三选一。"""

    # 故意保留 extra="forbid"（不改成 StrictOutputModel 的 extra="ignore"）：
    # 处置建议是控制面输入，模型多给一个动作键时宁可显式失败降级为保守保留，
    # 也不能静默丢掉后按默认动作执行。契约放在本模块是为了让 prompts.py 注册
    # report.sample_guard 时能直接引用同一个类（见 reporting/sample_guard.py）。
    model_config = {"extra": "forbid"}

    action: Literal["aggregate", "narrow_range", "keep"] = "keep"
    reason: str = Field(default="", max_length=300)
    suggested_group_by: list[str] = Field(default_factory=list, max_length=10)
    suggested_range: str = Field(default="", max_length=64)


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
