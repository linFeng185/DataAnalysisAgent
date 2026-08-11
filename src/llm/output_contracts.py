"""LLM 结构化输出契约与兼容解析工具。"""

from __future__ import annotations

import json
from typing import Any, Literal, TypeVar

from pydantic import BaseModel, ConfigDict, Field, field_validator


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
