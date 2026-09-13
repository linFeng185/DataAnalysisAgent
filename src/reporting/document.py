"""报表页面 DSL、ECharts 白名单和确定性布局器。"""

from __future__ import annotations

import json
import math
import re
from datetime import datetime, timezone
from decimal import Decimal
from typing import Any, Literal
from uuid import uuid4

from pydantic import BaseModel, Field, model_validator

from src.logging_config import get_logger
from src.reporting.models import ReportPlan, UnitResult
from src.reporting.relations import RelationEvidence

logger = get_logger(__name__)

_ECHARTS_ROOT_KEYS = {"xAxis", "yAxis", "series", "tooltip", "legend", "dataZoom"}
_FORBIDDEN_OPTION_KEYS = {
    "formatter", "renderItem", "dataView", "graphic", "markPoint", "markLine", "markArea",
    "onClick", "onclick", "event", "url", "image", "html", "script",
}
_DANGEROUS_TEXT_RE = re.compile(r"(?:function\s*\(|=>|<\s*script|javascript:|data:text/html)", re.I)
_BLOCK_TYPES = {"kpi", "narrative", "chart", "table", "insight_list", "relationship", "quality", "source_citation", "callout", "findings"}


# 方法作用：递归校验 ECharts option 只能包含普通 JSON 值和白名单节点。
# Args: value - 待校验的 option 值；path - 当前字段路径。
# Returns: 校验通过的原值；发现脚本、函数或非法字段时抛出 ValueError。
def _validate_option_value(value: Any, path: str = "option") -> Any:
    if callable(value):
        raise ValueError(f"{path} 不允许函数值")
    if value is None or isinstance(value, (bool, int, str)):
        if isinstance(value, str) and _DANGEROUS_TEXT_RE.search(value):
            raise ValueError(f"{path} 包含脚本或函数表达式")
        return value
    if isinstance(value, float):
        if not math.isfinite(value):
            raise ValueError(f"{path} 不允许非有限数字")
        return value
    if isinstance(value, list):
        if len(value) > 10_000:
            raise ValueError(f"{path} 数组过大")
        return [_validate_option_value(item, f"{path}[{index}]") for index, item in enumerate(value)]
    if isinstance(value, dict):
        result: dict[str, Any] = {}
        for key, item in value.items():
            name = str(key)
            if name in _FORBIDDEN_OPTION_KEYS or name.lower() in {item.lower() for item in _FORBIDDEN_OPTION_KEYS}:
                raise ValueError(f"{path}.{name} 是禁止字段")
            result[name] = _validate_option_value(item, f"{path}.{name}")
        return result
    raise ValueError(f"{path} 只允许普通 JSON 值")


# 方法作用：校验并复制服务端生成的 ECharts option 白名单子集。
# Args: option - 仅包含 axis/series/tooltip/legend/dataZoom 的图表配置。
# Returns: 通过校验的普通 JSON 字典。
def validate_echarts_option(option: dict[str, Any]) -> dict[str, Any]:
    if not isinstance(option, dict):
        raise ValueError("ECharts option 必须是 JSON 对象")
    unexpected = set(option) - _ECHARTS_ROOT_KEYS
    if unexpected:
        raise ValueError(f"ECharts option 包含白名单外字段: {sorted(unexpected)}")
    result = _validate_option_value(option)
    encoded = json.dumps(result, ensure_ascii=False, allow_nan=False, separators=(",", ":"))
    if len(encoded.encode("utf-8")) > 512_000:
        raise ValueError("ECharts option 超过大小限制")
    return result


class ReportLayout(BaseModel):
    """响应式 CSS Grid 布局意图。"""

    desktop: dict[str, int] = Field(default_factory=lambda: {"col_span": 12, "row_span": 1})
    tablet: dict[str, int] = Field(default_factory=lambda: {"col_span": 8, "row_span": 1})
    mobile: dict[str, int] = Field(default_factory=lambda: {"col_span": 4, "row_span": 1})


class ReportBlock(BaseModel):
    """页面中的一个白名单可渲染块。"""

    id: str = Field(default_factory=lambda: f"block_{uuid4().hex[:10]}", max_length=64)
    section_id: str = Field(..., min_length=1, max_length=64)
    type: Literal["kpi", "narrative", "chart", "table", "insight_list", "relationship", "quality", "source_citation", "callout", "findings"]
    priority: int = Field(default=50, ge=0, le=100)
    data_ref: str = ""
    evidence_refs: list[str] = Field(default_factory=list, max_length=20)
    layout: ReportLayout = Field(default_factory=ReportLayout)
    visibility: Literal["private", "shared", "public"] = "private"
    title: str = Field(default="", max_length=500)
    content: dict[str, Any] = Field(default_factory=dict)
    echarts_option: dict[str, Any] | None = None

    # 方法作用：校验块类型、文本和图表 option，阻止任意 HTML/脚本进入页面。
    # Args: self - 待校验页面块。
    # Returns: 通过校验的页面块。
    @model_validator(mode="after")
    def validate_block(self) -> "ReportBlock":
        if self.type not in _BLOCK_TYPES:
            raise ValueError("页面块类型不受支持")
        for value in self.content.values():
            if isinstance(value, str) and _DANGEROUS_TEXT_RE.search(value):
                raise ValueError("页面内容不允许脚本或函数表达式")
        if self.echarts_option is not None:
            self.echarts_option = validate_echarts_option(self.echarts_option)
        return self


class ReportSection(BaseModel):
    """报表页面的无框区段。"""

    id: str = Field(..., min_length=1, max_length=64)
    title: str = Field(default="", max_length=500)
    subtitle: str = Field(default="", max_length=2000)
    blocks: list[ReportBlock] = Field(default_factory=list, max_length=80)


class ReportDocument(BaseModel):
    """后端签发、前端按白名单渲染的报表文档。"""

    schema_version: Literal["report.document.v1"] = "report.document.v1"
    report_id: str
    version: int = Field(default=1, ge=1)
    title: str = Field(default="数据分析报表", max_length=500)
    subtitle: str = Field(default="", max_length=2000)
    generated_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    expires_at: datetime | None = None
    theme: dict[str, str] = Field(default_factory=lambda: {"accent": "#1677ff", "density": "comfortable"})
    navigation: list[str] = Field(default_factory=list, max_length=30)
    sections: list[ReportSection] = Field(default_factory=list, max_length=80)
    sources: list[dict[str, str]] = Field(default_factory=list, max_length=50)
    limitations: list[str] = Field(default_factory=list, max_length=50)
    permissions: dict[str, bool] = Field(default_factory=lambda: {"download": False, "drilldown": False})
    drilldowns: list[dict[str, Any]] = Field(default_factory=list, max_length=80)

    # 方法作用：限制页面总块数和序列化大小，避免把大结果写入公开页面。
    # Args: self - 待校验报表文档。
    # Returns: 通过校验的报表文档。
    @model_validator(mode="after")
    def validate_document(self) -> "ReportDocument":
        block_count = sum(len(section.blocks) for section in self.sections)
        if block_count > 80:
            raise ValueError("报表页面块数量超过限制")
        for section in self.sections:
            if len(section.blocks) != len({block.id for block in section.blocks}):
                raise ValueError("同一区段页面块 ID 不能重复")
        encoded = json.dumps(self.model_dump(mode="json"), ensure_ascii=False, separators=(",", ":"))
        if len(encoded.encode("utf-8")) > 2_097_152:
            raise ValueError("报表页面超过 2 MB 限制")
        return self


class ReportArtifact(BaseModel):
    """页面、脱敏数据清单和 Drilldown 定义的不可变产物。"""

    document: ReportDocument
    data_manifest: dict[str, list[dict[str, Any]]] = Field(default_factory=dict)
    drilldowns: list[dict[str, Any]] = Field(default_factory=list)


# 方法作用：把数据库值转换成公开页面允许的 JSON 标量或嵌套结构。
# Args: value - 查询结果中的原始值；depth - 当前嵌套深度。
# Returns: 脱敏后的 JSON 兼容值。
def sanitize_value(value: Any, depth: int = 0) -> Any:
    if depth > 4:
        return "[已省略]"
    if value is None or isinstance(value, (bool, int, str)):
        return value
    if isinstance(value, float):
        return value if math.isfinite(value) else None
    if isinstance(value, Decimal):
        return float(value)
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, dict):
        return {str(key): sanitize_value(item, depth + 1) for key, item in list(value.items())[:100]}
    if isinstance(value, (list, tuple)):
        return [sanitize_value(item, depth + 1) for item in list(value)[:100]]
    return str(value)[:500]


# 方法作用：为页面块计算稳定的响应式列跨度。
# Args: block_type - 页面块类型；has_data - 是否有可展示数据。
# Returns: ReportLayout 布局意图。
def layout_for_block(block_type: str, has_data: bool = True) -> ReportLayout:
    spans = {
        "kpi": (3, 2, 1),
        "chart": (8, 6, 4),
        "table": (12, 8, 4),
        "narrative": (8, 6, 4),
        "insight_list": (4, 4, 4),
        "relationship": (8, 6, 4),
        "quality": (4, 4, 4),
        "source_citation": (4, 4, 4),
        "findings": (12, 8, 4),
        "callout": (12, 8, 4),
    }
    desktop, tablet, mobile = spans.get(block_type, spans["narrative"])
    if not has_data and block_type == "chart":
        desktop, tablet, mobile = 12, 8, 4
    return ReportLayout(
        desktop={"col_span": desktop, "row_span": 1},
        tablet={"col_span": tablet, "row_span": 1},
        mobile={"col_span": mobile, "row_span": 1},
    )


# 方法作用：按分析单元构建可读页面、脱敏数据清单和快照 Drilldown 定义。
# Args: plan - 已确认报表计划；results - 各分析单元结果；report_id - 可选报表 ID。
# Returns: 可持久化的 ReportArtifact。
def build_report_document(
    plan: ReportPlan,
    results: list[UnitResult],
    *,
    report_id: str | None = None,
    relations: list[RelationEvidence] | None = None,
) -> ReportArtifact:
    logger.debug("报表页面构建入口", unit_count=len(results))
    resolved_report_id = report_id or str(uuid4())
    manifest: dict[str, list[dict[str, Any]]] = {}
    drilldowns: list[dict[str, Any]] = []
    sections: list[ReportSection] = []
    navigation: list[str] = ["overview"]
    for result in results[:12]:
        section_id = result.unit_id
        navigation.append(section_id)
        safe_rows = [sanitize_value(row) for row in result.rows[:100]]
        data_ref = f"data:{section_id}"
        manifest[data_ref] = safe_rows
        blocks: list[ReportBlock] = []
        if safe_rows:
            numeric_column = next(
                (key for key, value in safe_rows[0].items() if isinstance(value, (int, float)) and not isinstance(value, bool)),
                None,
            )
            if numeric_column:
                blocks.append(ReportBlock(
                    section_id=section_id,
                    type="kpi",
                    priority=95,
                    data_ref=data_ref,
                    title=numeric_column,
                    content={"value": safe_rows[0].get(numeric_column), "unit_id": section_id},
                    layout=layout_for_block("kpi"),
                ))
            columns = list(safe_rows[0].keys())
            label_column = columns[0]
            value_column = numeric_column or (columns[-1] if columns else "value")
            chart_option: dict[str, Any] = {
                "tooltip": {"trigger": "axis"},
                "xAxis": {"type": "category", "data": [str(row.get(label_column, "")) for row in safe_rows[:50]]},
                "yAxis": {"type": "value"},
                "series": [{"type": "bar", "name": value_column, "data": [row.get(value_column, 0) for row in safe_rows[:50]]}],
            }
            drilldown_id = f"dd-{uuid4()}"
            drilldown = {
                "drilldown_id": drilldown_id,
                "source_block_id": f"chart_{section_id}",
                "mode": "snapshot",
                "allowed_filters": [
                    {"field": label_column, "operator": "eq", "value_type": "string"},
                ],
                "columns": columns[:50],
                "max_rows": 100,
                "data_ref": data_ref,
            }
            drilldowns.append(drilldown)
            if result.live_query_id:
                drilldowns.append({**drilldown, "drilldown_id": f"live-{result.live_query_id}", "mode": "live", "query_ref": result.live_query_id})
            blocks.append(ReportBlock(
                id=f"chart_{section_id}",
                section_id=section_id,
                type="chart",
                priority=90,
                data_ref=data_ref,
                title="趋势与分布",
                content={"description": "点击图表可查看当前版本允许的脱敏明细", "drilldown_id": drilldown_id},
                echarts_option=chart_option,
                layout=layout_for_block("chart"),
            ))
            blocks.append(ReportBlock(
                section_id=section_id,
                type="table",
                priority=70,
                data_ref=data_ref,
                title="明细样本",
                content={"columns": columns[:50], "rows": safe_rows[:100], "page_size": 100},
                layout=layout_for_block("table"),
            ))
        else:
            blocks.append(ReportBlock(
                section_id=section_id,
                type="callout",
                priority=100,
                title="该分析单元暂无数据",
                content={"status": result.status, "message": result.user_error_message or "查询未返回可展示数据"},
                layout=layout_for_block("callout", False),
            ))
        summary = result.summary or f"已完成“{section_id}”分析，当前样本 {len(safe_rows)} 行。"
        blocks.insert(0, ReportBlock(
            section_id=section_id,
            type="narrative",
            priority=85,
            title="分析摘要",
            content={"summary": summary, "warnings": result.warnings[:20]},
            layout=layout_for_block("narrative"),
        ))
        if result.warnings:
            blocks.append(ReportBlock(
                section_id=section_id,
                type="quality",
                priority=60,
                title="数据质量与限制",
                content={"limitations": result.warnings[:20]},
                layout=layout_for_block("quality"),
            ))
        sections.append(ReportSection(id=section_id, title=section_id, blocks=blocks))
    if not sections:
        sections.append(ReportSection(
            id="overview",
            title="总览",
            blocks=[ReportBlock(section_id="overview", type="callout", title="暂无分析单元", content={"status": "empty"}, layout=layout_for_block("callout"))],
        ))
    relation_items = list(relations or [])[:20]
    if relation_items:
        relation_blocks = [
            ReportBlock(
                section_id="relations",
                type="relationship",
                priority=80 if relation.evidence_level == "verified_relation" else 55,
                title=f"{relation.left_unit_id} 与 {relation.right_unit_id}",
                content={
                    "relation_type": relation.relation_type,
                    "join_keys": relation.join_keys,
                    "match_rate": relation.match_rate,
                    "evidence_level": relation.evidence_level,
                    "limitations": relation.limitations,
                },
                layout=layout_for_block("relationship"),
            )
            for relation in relation_items
        ]
        sections.append(ReportSection(id="relations", title="关系证据", subtitle="仅展示可验证关系和明确限制", blocks=relation_blocks))
        navigation.append("relations")
    document = ReportDocument(
        report_id=resolved_report_id,
        title="经营分析报表",
        subtitle=f"共 {len(results)} 个分析单元，页面默认保留 {plan.page_ttl_days} 天" if not plan.permanent else "永久有效页面",
        navigation=navigation,
        sections=sections,
        limitations=list(plan.warnings),
        permissions={"download": False, "drilldown": bool(drilldowns)},
        drilldowns=drilldowns,
    )
    logger.info("报表页面构建完成", report_id=resolved_report_id, block_count=sum(len(item.blocks) for item in sections), data_refs=len(manifest))
    return ReportArtifact(document=document, data_manifest=manifest, drilldowns=drilldowns)
