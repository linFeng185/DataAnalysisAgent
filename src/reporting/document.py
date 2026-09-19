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
from src.reporting.charts import (
    COMPARISON_GROUP_LABELS,
    ChartNotRenderable,
    build_group_options,
    build_option_for_spec,
    fallback_chart_plan,
    profile_columns,
    translate_column_name,
)
from src.reporting.models import (
    _UNIT_TITLE_MAX_CHARS,
    ReportPlan,
    UnitResult,
    max_document_blocks,
    max_page_rows,
    max_plan_units,
)
from src.reporting.relations import RelationEvidence

logger = get_logger(__name__)

# visualMap 是热力图的必需件（颜色映射图例）：白名单缺它会让 heatmap 直接被判非法。
# grid 仍由前端模板基线统一负责，后端不持有视觉边距。
_ECHARTS_ROOT_KEYS = {"xAxis", "yAxis", "series", "tooltip", "legend", "dataZoom", "visualMap"}
_FORBIDDEN_OPTION_KEYS = {
    "formatter", "renderItem", "dataView", "graphic", "markPoint", "markLine", "markArea",
    "onClick", "onclick", "event", "url", "image", "html", "script",
}
_DANGEROUS_TEXT_RE = re.compile(r"(?:function\s*\(|=>|<\s*script|javascript:|data:text/html)", re.I)
_BLOCK_TYPES = {"kpi", "narrative", "chart", "table", "insight_list", "relationship", "quality", "source_citation", "callout", "findings"}
# 集合类字段的静态宽松上界只用于挡住畸形输入；真实的块数预算由
# max_document_blocks() 在 validate_document 里按配置校验，两者必须分开：
# 写进 Field 的值在类定义时就固定，改配置后不会跟着变。
_STATIC_LIST_MAX = 4000


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
    blocks: list[ReportBlock] = Field(default_factory=list, max_length=_STATIC_LIST_MAX)


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
    navigation: list[str] = Field(default_factory=list, max_length=_STATIC_LIST_MAX)
    sections: list[ReportSection] = Field(default_factory=list, max_length=_STATIC_LIST_MAX)
    sources: list[dict[str, str]] = Field(default_factory=list, max_length=50)
    limitations: list[str] = Field(default_factory=list, max_length=50)
    permissions: dict[str, bool] = Field(default_factory=lambda: {"download": False, "drilldown": False})
    drilldowns: list[dict[str, Any]] = Field(default_factory=list, max_length=_STATIC_LIST_MAX)

    # 方法作用：限制页面总块数和序列化大小，避免把大结果写入公开页面。
    # Args: self - 待校验报表文档。
    # Returns: 通过校验的报表文档。
    @model_validator(mode="after")
    def validate_document(self) -> "ReportDocument":
        block_count = sum(len(section.blocks) for section in self.sections)
        if block_count > max_document_blocks():
            raise ValueError("报表页面块数量超过限制")
        for section in self.sections:
            if len(section.blocks) != len({block.id for block in section.blocks}):
                raise ValueError("同一区段页面块 ID 不能重复")
        encoded = json.dumps(self.model_dump(mode="json"), ensure_ascii=False, separators=(",", ":"))
        if len(encoded.encode("utf-8")) > 2_097_152:
            raise ValueError("报表页面超过 2 MB 限制")
        return self


# 方法作用：按页面块预算裁剪超限文档，优先保留先出现的块。
# Args: document - 待裁剪的报表文档。
# Returns: 被裁掉的块数；未超预算时返回 0。
def truncate_document_to_block_budget(document: ReportDocument) -> int:
    """块是按区段顺序追加的，深挖发现与口径来源在末尾，裁剪天然从增量内容开始。"""
    budget = max_document_blocks()
    total = sum(len(section.blocks) for section in document.sections)
    if total <= budget:
        return 0
    removed = 0
    kept = 0
    for section in document.sections:
        # 按区段顺序填预算：前面区段优先占满，剩下的才轮到后面的增量区段。
        allowed = min(len(section.blocks), max(0, budget - kept))
        kept += allowed
        if allowed >= len(section.blocks):
            continue
        removed += len(section.blocks) - allowed
        del section.blocks[allowed:]
        if not section.blocks:
            # 裁空的区段留在页面里只会渲染成空壳，直接从导航与区段列表摘掉。
            logger.warning("报表区段因块预算被整体裁剪", section_id=section.id)
    document.sections = [section for section in document.sections if section.blocks]
    if removed:
        kept_ids = {section.id for section in document.sections}
        # overview 是页面固定首屏锚点，不挂在任何区段上，必须原样保留。
        document.navigation = [
            item for item in document.navigation if item in kept_ids or item == "overview"
        ]
        limitation = f"页面块预算限制已省略 {removed} 个末尾内容块，其余区段正常展示"
        if limitation not in document.limitations:
            document.limitations = [*document.limitations[:49], limitation]
        logger.warning("报表页面块数超出预算，已裁剪增量区段", removed=removed, budget=budget, total=total)
    return removed


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


# 方法作用：把带符号的相关系数解读为方向与强度分档。
# Args: correlation - 皮尔逊或斯皮尔曼相关系数，可能为空。
# Returns: 含 direction 与 strength 的字典；无有效系数时两者都是 unknown。
def correlation_reading(correlation: float | None) -> dict[str, str]:
    if correlation is None:
        return {"direction": "unknown", "strength": "unknown"}
    magnitude = abs(float(correlation))
    # 绝对值小于 0.05 视为无方向，避免把随机噪声写成"正相关"。
    direction = "positive" if correlation > 0.05 else "negative" if correlation < -0.05 else "unknown"
    if magnitude < 0.3:
        strength = "weak"
    elif magnitude <= 0.6:
        strength = "moderate"
    else:
        strength = "strong"
    return {"direction": direction, "strength": strength}


# 方法作用：为结果列生成带说明的显示标题。
# Args: columns - 结果列名；comments - 数据源列备注；descriptions - 分析模型给出的字段说明。
# Returns: 列名到显示标题的映射。取到说明时写成「列名（说明）」，否则保持原列名。
def column_labels(
    columns: list[str],
    comments: dict[str, str] | None,
    descriptions: dict[str, str] | None,
) -> dict[str, str]:
    lookup = {**(descriptions or {}), **(comments or {})}
    labels: dict[str, str] = {}
    for name in columns:
        note = str(lookup.get(name, "") or "").strip()
        labels[name] = f"{name}（{note}）" if note else name
    return labels


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


# 区段标题的字数上限：标题要能一眼读完，超长问题截断而不是撑满一行。
# 取值复用单元标题的契约常量，避免展示层与解析层各写一个数字后互相脱钩。
_SECTION_TITLE_MAX = _UNIT_TITLE_MAX_CHARS


# 方法作用：把单元问题压成区段标题。
# Args: question - 单元问题文本；unit_id - 问题为空时的兜底标识；title - 模型给出的精简标题，可为空。
# Returns: 短标题；模型给了标题就直接用；否则问题形如「销售基盘：按年月…」时取冒号前的业务名，
#          再否则截断问题文本。回退分支不是死代码：发散单元与模型降级时标题必然为空。
def unit_short_title(question: str, unit_id: str = "", title: str = "") -> str:
    logger.debug("单元标题生成入口", question_chars=len(str(question or "")), has_title=bool(str(title or "").strip()))
    model_title = " ".join(str(title or "").split())
    if model_title:
        # 模型标题已是面向读者的业务名，不再做冒号切分或截断，长度由契约与单元字段各自把关。
        logger.debug("单元标题生成完成", source="model", title_chars=len(model_title))
        return model_title
    text = " ".join(str(question or "").split())
    if not text:
        logger.debug("单元标题生成完成", source="unit_id", title_chars=len(unit_id))
        return unit_id
    # 用户书写习惯是「小标题：详细要求」，冒号前那截就是要展示的名字。
    head = re.split(r"[：:]", text, maxsplit=1)[0].strip()
    if head and len(head) <= _SECTION_TITLE_MAX:
        logger.debug("单元标题生成完成", source="question_head", title_chars=len(head))
        return head
    logger.debug("单元标题生成完成", source="question_truncated", title_chars=_SECTION_TITLE_MAX)
    return f"{text[:_SECTION_TITLE_MAX]}…"


# 图表类型到默认标题措辞的映射：柱状图没有"趋势"，写死一种说法必然误导。
_CHART_TITLE_SUFFIX = {"line": "趋势", "pie": "构成", "heatmap": "分布", "scatter": "相关", "bar": "对比"}


# 方法作用：方案没给标题时按图表类型与指标名生成一句可读标题。
# Args: spec - 图表方案；columns - 列清单。
# Returns: 形如「净销售额 趋势」的标题。
def _default_chart_title(spec: Any, columns: list[str], labels: dict[str, str] | None = None) -> str:
    series = [str(name) for name in (spec.series_fields or []) if name]
    value_column = series[0] if series else (columns[-1] if columns else "数值")
    # 标题里的指标名同样走中文标签：用户用中文提问，不该回一份英文字段名。
    display = _column_display(value_column, labels)
    suffix = _CHART_TITLE_SUFFIX.get(str(spec.chart_type or "bar"), "对比")
    title = f"{display} {suffix}"
    logger.debug("图表默认标题生成完成", title=title, chart_type=spec.chart_type)
    return title


# 方法作用：按图表方案为一个单元构建图表块；方案要求 table 时改出明细表块。
# Args: section_id - 区段标识；data_ref - 脱敏数据引用；drilldown_id - 受控钻取标识；
#       plan - 该单元的图表方案；rows - 已脱敏结果行；columns - 列清单；
#       profile - 与该 rows 配套的列画像，缺省时按行现场构建（同一单元多张图共用一份）。
# Returns: 页面块列表；单张图构建失败时跳过该张而不是让整份报表失败。
def _build_chart_blocks(
    *,
    section_id: str,
    data_ref: str,
    drilldown_id: str,
    plan: Any,
    rows: list[dict[str, Any]],
    columns: list[str],
    labels: dict[str, str] | None = None,
    warnings: list[str] | None = None,
    profile: Any = None,
) -> list[ReportBlock]:
    blocks: list[ReportBlock] = []
    # 列画像扫全量，一个单元只算一次：多张图 × 图内多次列分类共用同一份证据。
    if profile is None:
        profile = profile_columns(rows)
    for index, spec in enumerate(list(getattr(plan, "charts", []) or [])):
        chart_type = str(getattr(spec, "chart_type", "") or "bar")
        # 第一个图块沿用 chart_{section_id}：钻取的 source_block_id 指向它，
        # 后续图块才带序号后缀。多图单元共用同一次受控钻取。
        block_id = f"chart_{section_id}" if index == 0 else f"chart_{section_id}_{index}"
        if chart_type == "table":
            # 与图块同理：首个表块沿用 table_{section_id}，后续表块带序号后缀，
            # 否则同单元多个 table plan 会产出重复 block ID 导致报表校验失败。
            table_block_id = f"table_{section_id}" if index == 0 else f"table_{section_id}_{index}"
            blocks.append(ReportBlock(
                id=table_block_id,
                section_id=section_id,
                type="table",
                priority=90,
                data_ref=data_ref,
                title=str(getattr(spec, "title", "") or "数据一览"),
                content={"columns": columns[:50], "drilldown_id": drilldown_id},
                layout=layout_for_block("table"),
            ))
            continue
        try:
            # 降级原因（缺指标列、全空系列）要一路带到页面的质量块：
            # 静默少画一张图，用户只会以为"这个单元没数据"。
            # 分组透视在实机规模下是「全量行 × 候选分组数」的重活，而 line/bar 的
            # build_option_for_spec 内部已经完整算过一次。这里用出参容器把它取回来复用，
            # 不再对同一份输入重算——**不缓存任何 rows**，只是把同一次调用的结果往下传。
            group_view: dict[str, Any] = {}
            option = build_option_for_spec(
                spec, rows, labels, warnings=warnings, profile=profile, group_view=group_view
            )
            series = list(option.get("series") or [])
            if not series or not any(list(item.get("data") or []) for item in series if isinstance(item, dict)):
                raise ChartNotRenderable("图表没有可绘制数据")
            content: dict[str, Any] = {
                "description": "点击图表可查看当前版本允许的脱敏明细",
                "drilldown_id": drilldown_id,
                "chart_type": chart_type,
                "x_field": str(getattr(spec, "x_field", "") or ""),
                "series_fields": [str(name) for name in (getattr(spec, "series_fields", []) or []) if name][:10],
            }
            # 只有共享 category/value 轴语义的 line/bar 能安全地只替换 series。
            # pie/scatter/heatmap 的 data 形状各不相同，错误下发会覆盖掉正确 option。
            if chart_type in {"line", "bar"}:
                if group_view:
                    # 复用 build_option_for_spec 同一次调用算出的分组透视，避免二次全量计算。
                    group_field = str(group_view.get("group_field", ""))
                    group_options = group_view.get("group_options") or {}
                else:
                    # 理论上走不到：chart_type 为 line/bar 时上面那次调用必定填充 group_view。
                    # 真有偏差时宁可多算一遍，也不能静默丢掉整组切换视图。
                    logger.warning(
                        "图表分组视图未随 option 一并返回，回退为重新计算",
                        section_id=section_id,
                        chart_type=chart_type,
                    )
                    group_field, _default_series, group_options = build_group_options(
                        spec=spec, rows=rows, labels=labels, warnings=warnings, profile=profile
                    )
                options_payload = _prune_group_options(
                    {
                        field: [
                            {
                                "name": item.get("name"),
                                "type": str(item.get("type") or chart_type),
                                "data": item.get("data"),
                            }
                            for item in grouped_series
                        ]
                        for field, grouped_series in group_options.items()
                    },
                    group_field,
                )
                content.update({
                    "group_field": group_field,
                    "group_options": options_payload,
                    "group_labels": {
                        field: COMPARISON_GROUP_LABELS.get(field)
                        or (_column_display(field, labels) if field else "汇总值")
                        for field in options_payload
                    },
                })
            block = ReportBlock(
                id=block_id,
                section_id=section_id,
                type="chart",
                priority=90,
                data_ref=data_ref,
                title=str(getattr(spec, "title", "") or "") or _default_chart_title(spec, columns, labels),
                content=content,
                echarts_option=option,
                layout=layout_for_block("chart"),
            )
        except ChartNotRenderable as exc:
            # 确定性规则说"这张图不该画"：原因必须写进页面限制里，而不是只留一行日志。
            logger.warning(
                "图表按确定性规则降级为明细表",
                section_id=section_id,
                chart_type=chart_type,
                reason=str(exc),
            )
            message = f"该分析单元的图表未能生成（{exc}），已降级为明细表"
            if warnings is not None and message not in warnings:
                warnings.append(message)
            blocks.append(ReportBlock(
                id=f"table_{section_id}_{index}",
                section_id=section_id,
                type="table",
                priority=90,
                data_ref=data_ref,
                title="数据一览",
                content={"columns": columns[:50], "drilldown_id": drilldown_id},
                layout=layout_for_block("table"),
            ))
            continue
        except Exception:
            logger.error(
                "图表构建失败，降级为明细表",
                section_id=section_id,
                chart_type=chart_type,
                exc_info=True,
            )
            message = "该分析单元的图表未能生成，已降级为明细表"
            if warnings is not None and message not in warnings:
                warnings.append(message)
            blocks.append(ReportBlock(
                id=f"table_{section_id}_{index}",
                section_id=section_id,
                type="table",
                priority=90,
                data_ref=data_ref,
                title="数据一览",
                content={"columns": columns[:50], "drilldown_id": drilldown_id},
                layout=layout_for_block("table"),
            ))
            continue
        blocks.append(block)
    logger.info("单元图表块构建完成", section_id=section_id, block_count=len(blocks))
    return blocks


# 方法作用：把列名渲染成给用户看的标签。
# Args: column - 列名；labels - 列名到标签的映射。
# Returns: 有映射时返回映射值，否则返回列名本身。
def _column_display(column: str, labels: dict[str, str] | None) -> str:
    return str((labels or {}).get(column, "") or column)


# 方法作用：从该单元已批准的指标口径中解析 KPI 的计量单位。
# Args: column - KPI 数值列；citations - 指标口径快照列表。
# Returns: 精确匹配指标编码时返回单位，否则返回空串且不猜测。
def _kpi_unit(column: str, citations: list[dict[str, Any]] | None) -> str:
    for citation in list(citations or []):
        if not isinstance(citation, dict):
            continue
        if str(citation.get("code", "") or "") == column:
            return str(citation.get("unit", "") or "").strip()
    return ""


# 下发分组视图的体积预算：页面有 2 MB 硬上限，而每种分组视图都要整份下发，
# 多图单元（一个单元 6 张图 × 每种 4 个视图）会直接把页面撑爆（实机复现过）。
_MAX_GROUP_VIEWS = 4
_MAX_VIEW_POINTS = 800
# 页面正文块的体积预算：预留一部分给叙事、KPI 与质量块，切换视图用剩下的。
_PAGE_PAYLOAD_BUDGET = 1_600_000


# 方法作用：估算一段 JSON 负载的 UTF-8 字节数。
# Args: payload - 任意可序列化对象。
# Returns: 字节数；序列化失败时按 0 处理。
def _json_bytes(payload: Any) -> int:
    try:
        return len(json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8"))
    except (TypeError, ValueError):
        logger.warning("页面体积估算失败，按 0 处理", exc_info=True)
        return 0


# 方法作用：在构造 ReportDocument **之前**按体积预算剥离图表的分组切换视图。
# Args: sections - 已构建的区段列表；manifest - 脱敏数据清单。
# Returns: 实际被剥离视图的块数。
def _fit_payload_budget(sections: list[ReportSection], manifest: dict[str, list[dict[str, Any]]]) -> int:
    """必须在构造前裁剪：ReportDocument 的校验器会在构造期直接拒绝超限文档。

    实现要点：**不能每剥一个块就全量序列化一次**——文档可达数 MB，上百个图块会变成
    上百次全量 dump，内存与耗时都被放大（这正是"报表生成内存飙升"的一部分）。
    改为按**块体积估算**决定剥哪些，只在最后做一次总量核对。
    """
    base_size = _json_bytes(manifest) + sum(
        _json_bytes([block.model_dump(mode="json") for block in section.blocks]) for section in sections
    )
    if base_size <= _PAGE_PAYLOAD_BUDGET:
        return 0
    group_blocks: list[tuple[int, Any]] = []
    for section in sections:
        for block in section.blocks:
            if block.type != "chart" or not isinstance(block.content, dict):
                continue
            options = block.content.get("group_options")
            if not options:
                continue
            # 剥掉的是备选视图的增量：默认视图已固化在 echarts_option 里。
            group_blocks.append((_json_bytes(options), block))
    # 先剥最大的，收敛最快。
    group_blocks.sort(key=lambda item: -item[0])
    removed = 0
    freed = 0
    for size, block in group_blocks:
        if base_size - freed <= _PAGE_PAYLOAD_BUDGET:
            break
        block.content.pop("group_options", None)
        block.content.pop("group_labels", None)
        removed += 1
        freed += size
    logger.info(
        "页面体积预算裁剪完成",
        stripped_blocks=removed,
        estimated_before=base_size,
        estimated_after=base_size - freed,
    )
    return removed


# 方法作用：按体积预算裁剪下发的分组视图，默认视图始终保留。
# Args: options_payload - 分组字段到系列列表的映射；default_field - 默认分组字段。
# Returns: 裁剪后的分组视图映射。
def _prune_group_options(
    options_payload: dict[str, list[dict[str, Any]]],
    default_field: str,
) -> dict[str, list[dict[str, Any]]]:
    """被裁掉的视图在前端表现为"少一个切换项"，而不是整份报表发布失败。"""
    kept: dict[str, list[dict[str, Any]]] = {}
    if default_field in options_payload:
        kept[default_field] = options_payload[default_field]
    for field, series in options_payload.items():
        if field in kept:
            continue
        if len(kept) >= _MAX_GROUP_VIEWS:
            break
        points = sum(len(item.get("data") or []) for item in series)
        if points > _MAX_VIEW_POINTS:
            continue
        kept[field] = series
    if len(kept) != len(options_payload):
        logger.info(
            "分组视图按体积预算裁剪",
            total=len(options_payload),
            kept=len(kept),
            default_field=default_field,
        )
    return kept


# 方法作用：按分析单元构建可读页面、脱敏数据清单和快照 Drilldown 定义。
# Args: plan - 已确认报表计划；results - 各分析单元结果；report_id - 可选报表 ID；
#       chart_plans - 可选的 unit_id 到图表方案映射，缺省时按列类型确定性推断。
# Returns: 可持久化的 ReportArtifact。
def build_report_document(
    plan: ReportPlan,
    results: list[UnitResult],
    *,
    report_id: str | None = None,
    relations: list[RelationEvidence] | None = None,
    template_id: str = "insight",
    chart_plans: dict[str, Any] | None = None,
) -> ReportArtifact:
    logger.debug("报表页面构建入口", unit_count=len(results), has_chart_plans=bool(chart_plans))
    resolved_report_id = report_id or str(uuid4())
    manifest: dict[str, list[dict[str, Any]]] = {}
    drilldowns: list[dict[str, Any]] = []
    sections: list[ReportSection] = []
    navigation: list[str] = ["overview"]
    # 区段标题优先用模型在 report.plan 里顺带给出的 title（不额外起模型调用），
    # 缺失时才退回单元问题的业务名——绝不再拿 unit_id 充当标题，页面上整片英文。
    unit_titles = {
        str(unit.unit_id): unit_short_title(unit.question, str(unit.unit_id), str(getattr(unit, "title", "") or ""))
        for unit in plan.analysis_units
    }
    # 图表决策在异步节点里预先算好传入（本函数保持同步，20 处既有调用不受影响）；
    # 单元问题只在计划里有，结果对象不携带，故在此按 unit_id 建映射供兜底使用。
    unit_questions = {str(unit.unit_id): str(unit.question or "") for unit in plan.analysis_units}
    unit_limit = max_plan_units()
    document_limitations = list(plan.warnings)
    if len(results) > unit_limit:
        message = f"本次共有 {len(results)} 个分析单元结果，页面受当前上限限制仅展示前 {unit_limit} 个"
        if message not in document_limitations:
            document_limitations.append(message)
        logger.warning(
            "报表分析单元结果按页面上限裁剪",
            result_count=len(results),
            kept=unit_limit,
        )
    for result in results[:unit_limit]:
        section_id = result.unit_id
        navigation.append(section_id)
        # 图表用完整结果做透视：透视后每个 (x, 分组) 只留一个点，体积很小，
        # 但透视必须看到全量才能算对——只喂前若干行会让"月度趋势"只剩开头几个月。
        # 页面清单（钻取/表格数据源）另走 max_page_rows，避免撞 2 MB 页面体积上限。
        # 清单从同一份清洗结果切片：原先是两次独立遍历，等于把整个结果集复制两遍，
        # 22 单元同时驻留时是实打实的常驻内存翻倍。
        chart_rows = [sanitize_value(row) for row in result.rows]
        safe_rows = chart_rows[:max_page_rows()]
        if len(result.rows) > len(safe_rows):
            # 页面数据被裁必须留痕：图表基于被裁样本透视出来的趋势会失真，
            # 实机就出现过"单元返回 4704 行、图表只剩前 100 行的三个月"。
            logger.info(
                "页面数据清单按上限裁剪",
                unit_id=section_id,
                returned=len(result.rows),
                kept=len(safe_rows),
                limit=max_page_rows(),
            )
        data_ref = f"data:{section_id}"
        manifest[data_ref] = safe_rows
        blocks: list[ReportBlock] = []
        section_warnings = list(result.warnings)
        if safe_rows:
            # 列画像扫全量，一个单元只算一次：同一份结果会被多张图与图内多次列分类反复用到。
            chart_profile = profile_columns(chart_rows)
            numeric_column = next(
                (key for key, value in safe_rows[0].items() if isinstance(value, (int, float)) and not isinstance(value, bool)),
                None,
            )
            if numeric_column:
                kpi_labels = column_labels([numeric_column], getattr(result, "column_comments", None), getattr(result, "field_descriptions", None))
                blocks.append(ReportBlock(
                    section_id=section_id,
                    type="kpi",
                    priority=95,
                    data_ref=data_ref,
                    title=kpi_labels[numeric_column],
                    content={
                        "value": safe_rows[0].get(numeric_column),
                        "unit": _kpi_unit(numeric_column, result.metric_citations),
                        "column_labels": kpi_labels,
                    },
                    layout=layout_for_block("kpi"),
                ))
            columns = list(dict.fromkeys(column for row in safe_rows for column in row))
            label_column = columns[0]
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
            # 图表方案由模型按数据形态决策；缺省（旧调用方/模型不可用）时走确定性兜底，
            # 不再无条件画柱状图、也不再只画第一个数值列。
            plan_for_unit = (chart_plans or {}).get(section_id)
            if plan_for_unit is None:
                plan_for_unit = fallback_chart_plan(
                    question=unit_questions.get(section_id, ""),
                    columns=list(result.columns) or columns,
                    rows=chart_rows,
                    profile=chart_profile,
                )
            # 图表标签要**纯中文**，不能复用表格用的「列名（说明）」格式：
            # 图例里再带一遍英文列名会变成「region_code（地区）：CN-BJ」这种双重标注。
            # 说明与注释都没有时（模型的 SQL 别名不在数据源注释里，实机图例成片英文），
            # 用确定性词表翻译；连词表都没命中才回退列名本身。
            chart_labels = {
                name: (str(descriptions.get(name, "") or "").strip() or str(comments.get(name, "") or "").strip() or translate_column_name(name))
                for name in dict.fromkeys(column for row in safe_rows for column in row)
                for descriptions, comments in [(
                    getattr(result, "field_descriptions", None) or {},
                    getattr(result, "column_comments", None) or {},
                )]
            }
            blocks.extend(_build_chart_blocks(
                section_id=section_id,
                data_ref=data_ref,
                drilldown_id=drilldown_id,
                labels=chart_labels,
                plan=plan_for_unit,
                rows=chart_rows,
                columns=columns,
                warnings=section_warnings,
                profile=chart_profile,
            ))
            # 明细表格块已按实机反馈移除：5~18 列挤在页面里只会被 ellipsis 切成
            # "每格几个字加省略号"，既占版面又读不出信息。明细由使用方回数据源查，
            # 页面只保留结论；图表下方的受控钻取（drilldowns）仍可用。
        else:
            # 直答类单元（元数据说明等）的结论在摘要文字里，没有数据表是正常形态；
            # 用「暂无数据」会让整份报表看起来每个单元都缺数据。
            if result.answer_kind == "text":
                label, fallback_message = "该单元为说明类分析", "结论见上方分析摘要"
            else:
                label, fallback_message = "该分析单元暂无数据", "查询未返回可展示数据"
            blocks.append(ReportBlock(
                section_id=section_id,
                type="callout",
                priority=100,
                title=label,
                content={"status": result.status, "message": result.user_error_message or fallback_message},
                layout=layout_for_block("callout", False),
            ))
        summary = result.summary or f"已完成“{section_id}”分析，当前样本 {len(safe_rows)} 行。"
        blocks.insert(0, ReportBlock(
            section_id=section_id,
            type="narrative",
            priority=85,
            title="分析摘要",
            content={"summary": summary, "warnings": section_warnings[:20]},
            layout=layout_for_block("narrative"),
        ))
        if section_warnings:
            blocks.append(ReportBlock(
                section_id=section_id,
                type="quality",
                priority=60,
                title="数据质量与限制",
                content={"limitations": section_warnings[:20]},
                layout=layout_for_block("quality"),
            ))
        sections.append(ReportSection(id=section_id, title=unit_titles.get(section_id, section_id), blocks=blocks))
    if not sections:
        sections.append(ReportSection(
            id="overview",
            title="总览",
            blocks=[ReportBlock(section_id="overview", type="callout", title="暂无分析单元", content={"status": "empty"}, layout=layout_for_block("callout"))],
        ))
    # 只保留真正建立了关系的证据：实机页面上 20 块里多数是"未发现共同字段"，
    # 占版面却不提供任何信息；整段都没有有效证据时，区段本身也不出现。
    relation_items = [item for item in list(relations or []) if item.evidence_level != "no_relation"][:20]
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
                    # 方向与强度在文档层派生：证据层只存带符号的 correlation，不存冗余字段。
                    **correlation_reading(relation.correlation),
                    "cross_source": relation.evidence_level == "cross_source_statistical",
                },
                layout=layout_for_block("relationship"),
            )
            for relation in relation_items
        ]
        sections.append(ReportSection(id="relations", title="关系证据", subtitle="仅展示可验证关系和明确限制", blocks=relation_blocks))
        navigation.append("relations")
    # 必须在构造前裁剪：ReportDocument 的校验器在构造期就会拒绝超限文档。
    stripped_group_views = _fit_payload_budget(sections, manifest)
    if stripped_group_views:
        document_limitations = [
            *document_limitations[:49],
            f"页面体积受限，已省略 {stripped_group_views} 张图的分组切换视图，默认图表仍完整保留",
        ]
    document = ReportDocument(
        report_id=resolved_report_id,
        # 后端只承载模板标识，不承载任何颜色或密度取值：视觉真源在前端注册表。
        theme={"template_id": template_id or "insight"},
        title="经营分析报表",
        subtitle=f"共 {len(results)} 个分析单元，页面默认保留 {plan.page_ttl_days} 天" if not plan.permanent else "永久有效页面",
        navigation=navigation,
        sections=sections,
        limitations=document_limitations[:50],
        permissions={"download": False, "drilldown": bool(drilldowns)},
        drilldowns=drilldowns,
    )
    logger.info("报表页面构建完成", report_id=resolved_report_id, block_count=sum(len(item.blocks) for item in sections), data_refs=len(manifest))
    return ReportArtifact(document=document, data_manifest=manifest, drilldowns=drilldowns)
