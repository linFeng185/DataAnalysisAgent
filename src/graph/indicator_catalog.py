"""指标目录问法识别、存储形态规划与确定性 SQL 构造。"""

from __future__ import annotations

import json
import re
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from src.logging_config import get_logger

logger = get_logger(__name__)

IndicatorCatalogMode = Literal[
    "schema_catalog",
    "row_catalog",
    "ambiguous",
    "unavailable",
]
IndicatorStorageMode = Literal["table_metric", "wide_columns", "row_values"]

_CATALOG_PATTERNS = (
    re.compile(r"有(?:哪些|什么)(?:数据|业务|核心|可用)?指标"),
    re.compile(r"(?:数据)?指标(?:有(?:哪些|什么)(?!字段|列)|清单|列表|目录)"),
    re.compile(r"(?:支持|提供|包含|列出).{0,8}(?:数据)?指标"),
)
_VALUE_QUERY_MARKERS = (
    "下降",
    "上升",
    "增长",
    "减少",
    "趋势",
    "走势",
    "同比",
    "环比",
    "排名",
    "top",
    "最高",
    "最低",
    "平均",
    "总和",
    "占比",
    "变化",
    "异常",
    "预测",
    "归因",
    "值是多少",
)
_CODE_COLUMNS = (
    "metric_code",
    "indicator_code",
    "kpi_code",
    "index_code",
    "target_code",
)
_NAME_COLUMNS = (
    "metric_name",
    "indicator_name",
    "kpi_name",
    "index_name",
    "target_name",
)
_VALUE_COLUMNS = (
    "metric_value",
    "indicator_value",
    "kpi_value",
    "index_value",
    "target_value",
)
_NUMERIC_TYPE_MARKERS = (
    "int",
    "decimal",
    "numeric",
    "number",
    "float",
    "double",
    "real",
    "money",
)
_DIMENSION_SUFFIXES = (
    "_id",
    "_code",
    "_name",
    "_date",
    "_time",
    "_year",
    "_month",
    "_day",
    "_type",
    "_status",
)
_DIMENSION_NAMES = {
    "id",
    "date",
    "time",
    "year",
    "month",
    "day",
    "code",
    "name",
    "type",
    "status",
}
_NON_METRIC_NAMES = {
    "id",
    "sort",
    "sequence",
    "seq",
    "latitude",
    "longitude",
    "lat",
    "lon",
    "lng",
    "coordinate",
}
_NON_METRIC_COMMENT_MARKERS = (
    "主键",
    "外键",
    "编号",
    "编码",
    "排序",
    "序号",
    "经度",
    "纬度",
    "坐标",
    "状态",
    "记录创建时间",
    "记录更新时间",
)
_THEME_RULES = (
    ("销售与订单", ("sale", "sales", "order", "contract", "ship", "销售", "订单", "合同", "出货")),
    ("生产与工单", ("production", "output", "workorder", "work_order", "workshop", "生产", "产量", "工单", "车间")),
    ("库存与供应链", ("stock", "inventory", "warehouse", "purchase", "supplier", "库存", "仓库", "采购", "供应商")),
    ("设备运维", ("device", "alarm", "shutdown", "operation", "设备", "报警", "停机", "运行")),
    ("人员与组织", ("attendance", "personnel", "staff", "talent", "人员", "出勤", "人才")),
    ("质量与客户", ("quality", "pass", "satisfaction", "customer", "质量", "合格", "满意度", "客户")),
    ("能源与环境", ("electricity", "temperature", "humidity", "energy", "用电", "温度", "湿度", "能耗")),
    ("经营与目标", ("income", "target", "progress", "收入", "目标", "进度")),
)


class IndicatorTablePlan(BaseModel):
    """单张表在指标目录中的确定性分类。"""

    model_config = ConfigDict(extra="forbid")

    name: str = Field(min_length=1)
    description: str = ""
    storage_mode: IndicatorStorageMode
    columns: list[str] = Field(default_factory=list)
    numeric_columns: list[str] = Field(default_factory=list)
    metric_columns: list[str] = Field(default_factory=list)
    column_comments: dict[str, str] = Field(default_factory=dict)
    code_column: str = ""
    name_column: str = ""
    value_column: str = ""


class IndicatorCatalogPlan(BaseModel):
    """一次指标目录请求的存储形态与执行决策。"""

    model_config = ConfigDict(extra="forbid")

    mode: IndicatorCatalogMode
    tables: list[IndicatorTablePlan] = Field(default_factory=list)
    selected_table: str = ""
    code_column: str = ""
    name_column: str = ""
    requires_data_query: bool = False
    needs_input: bool = False


class IndicatorDisplayItem(BaseModel):
    """前端展示的一项已复检业务指标。"""

    model_config = ConfigDict(extra="forbid")

    name: str
    description: str = ""
    table: str
    column: str


class IndicatorDisplayGroup(BaseModel):
    """按业务主题组织的一组指标。"""

    model_config = ConfigDict(extra="forbid")

    theme: str
    indicators: list[IndicatorDisplayItem] = Field(default_factory=list)


# 方法作用：识别纯指标目录问法，并排除趋势、排名等指标值分析请求。
# Args: query - 用户自然语言问题。
# Returns: 仅当问题询问指标清单或可用指标时返回 True。
def is_indicator_catalog_query(query: str) -> bool:
    """目录识别只决定操作类型，不依赖时间词或 LLM 自然语言说明。"""
    normalized = str(query or "").strip().lower()
    if not normalized:
        return False
    catalog_match = any(pattern.search(normalized) for pattern in _CATALOG_PATTERNS)
    value_match = any(marker in normalized for marker in _VALUE_QUERY_MARKERS)
    return catalog_match and not value_match


# 方法作用：从列定义中查找第一个匹配的行式指标角色列。
# Args: column_names - 小写列名到原始列名的映射；candidates - 允许的角色列别名。
# Returns: 命中的原始列名；无匹配时返回空字符串。
def _find_role_column(
    column_names: dict[str, str],
    candidates: tuple[str, ...],
) -> str:
    """角色列只接受明确别名，避免把普通业务值表误判为指标目录。"""
    return next((column_names[name] for name in candidates if name in column_names), "")


# 方法作用：判断数据库类型是否属于可作为指标值的数值类型。
# Args: column_type - Schema 返回的字段类型文本。
# Returns: 类型包含受支持数值标记时返回 True。
def _is_numeric_type(column_type: object) -> bool:
    """兼容带精度和方言修饰的类型，如 Decimal(18,2) 与 UInt64。"""
    normalized = str(column_type or "").strip().lower()
    return any(marker in normalized for marker in _NUMERIC_TYPE_MARKERS)


# 方法作用：判断数值列是否更可能是维度键而不是业务指标。
# Args: column_name - 字段名称。
# Returns: ID、编码、日期和状态类列返回 True。
def _is_dimension_name(column_name: str) -> bool:
    """使用保守名称规则，避免把时间键和编码键列入指标字段。"""
    normalized = column_name.strip().lower()
    semantic_name = normalized.removeprefix("f_")
    return (
        semantic_name in _DIMENSION_NAMES
        or semantic_name in _NON_METRIC_NAMES
        or semantic_name.endswith(_DIMENSION_SUFFIXES)
    )


# 方法作用：结合字段名和注释判断数值列是否明显属于维度或技术字段。
# Args: column_name - 字段名；comment - Schema 字段注释。
# Returns: 明显不能作为业务指标时返回 True。
def _is_dimension_column(column_name: str, comment: str) -> bool:
    """字段注释用于补足带业务前缀的主键、坐标和排序字段识别。"""
    return _is_dimension_name(column_name) or any(
        marker in comment for marker in _NON_METRIC_COMMENT_MARKERS
    )


# 方法作用：把一张 Schema 表分类为行式、宽表字段式或表指标域。
# Args: table - relevant_tables 中的单张表结构。
# Returns: 包含字段角色和存储形态的 IndicatorTablePlan；无表名时返回 None。
def _classify_table(table: dict) -> IndicatorTablePlan | None:
    """行式识别优先于数值字段数量判断。"""
    name = str(table.get("name", "") or "").strip()
    if not name:
        return None
    columns = [column for column in (table.get("columns", []) or []) if isinstance(column, dict)]
    column_names = {
        str(column.get("name", "") or "").strip().lower(): str(column.get("name", "") or "").strip()
        for column in columns
        if str(column.get("name", "") or "").strip()
    }
    code_column = _find_role_column(column_names, _CODE_COLUMNS)
    name_column = _find_role_column(column_names, _NAME_COLUMNS)
    value_column = _find_role_column(column_names, _VALUE_COLUMNS)
    numeric_columns = [
        str(column.get("name", "") or "").strip()
        for column in columns
        if str(column.get("name", "") or "").strip()
        and _is_numeric_type(column.get("type"))
    ]
    metric_columns = [
        str(column.get("name", "") or "").strip()
        for column in columns
        if str(column.get("name", "") or "").strip()
        and _is_numeric_type(column.get("type"))
        and not _is_dimension_column(
            str(column.get("name", "") or ""),
            str(column.get("comment", "") or ""),
        )
    ]
    logger.info(
        "指标目录表分类边界",
        table=name,
        column_count=len(columns),
        numeric_candidates=numeric_columns,
        accepted_metrics=metric_columns,
        code_column=code_column,
        name_column=name_column,
        value_column=value_column,
    )
    if value_column and (code_column or name_column):
        storage_mode: IndicatorStorageMode = "row_values"
    elif len(metric_columns) >= 2:
        storage_mode = "wide_columns"
    else:
        storage_mode = "table_metric"
    return IndicatorTablePlan(
        name=name,
        description=str(table.get("description", "") or "").strip(),
        storage_mode=storage_mode,
        columns=list(column_names.values()),
        numeric_columns=numeric_columns,
        metric_columns=metric_columns,
        column_comments={
            str(column.get("name", "") or "").strip(): str(column.get("comment", "") or "").strip()
            for column in columns
            if str(column.get("name", "") or "").strip()
            and str(column.get("comment", "") or "").strip()
        },
        code_column=code_column,
        name_column=name_column,
        value_column=value_column,
    )


# 方法作用：基于真实 Schema 规划指标目录读取方式。
# Args: tables - retrieve_schema 写回的相关表结构列表。
# Returns: 唯一行式表走受限查询，其他形态只读 Schema，多候选或空结构请求补充输入。
def build_indicator_catalog_plan(tables: list[dict]) -> IndicatorCatalogPlan:
    """规划器不读取业务数据，也不根据时间范围改变目录语义。"""
    table_plans = [
        plan
        for table in tables
        if isinstance(table, dict) and (plan := _classify_table(table)) is not None
    ]
    if not table_plans:
        return IndicatorCatalogPlan(mode="unavailable", needs_input=True)
    row_tables = [table for table in table_plans if table.storage_mode == "row_values"]
    if len(row_tables) > 1:
        return IndicatorCatalogPlan(
            mode="ambiguous",
            tables=table_plans,
            needs_input=True,
        )
    if len(row_tables) == 1:
        selected = row_tables[0]
        return IndicatorCatalogPlan(
            mode="row_catalog",
            tables=table_plans,
            selected_table=selected.name,
            code_column=selected.code_column,
            name_column=selected.name_column,
            requires_data_query=True,
        )
    return IndicatorCatalogPlan(mode="schema_catalog", tables=table_plans)


# 方法作用：为不同 SQL 方言安全引用 Schema 已确认的标识符。
# Args: identifier - 表名或字段名；dialect - 规范化前的数据库方言。
# Returns: 按方言引用的标识符，限定名逐段引用。
def _quote_identifier(identifier: str, dialect: str) -> str:
    """MySQL/ClickHouse 使用反引号，其余已支持方言使用双引号。"""
    quote = "`" if dialect.strip().lower() in {"mysql", "clickhouse"} else '"'
    return ".".join(
        f"{quote}{part.replace(quote, quote + quote)}{quote}"
        for part in identifier.split(".")
        if part
    )


# 方法作用：为唯一行式指标表生成受限 DISTINCT 目录 SQL。
# Args: plan - build_indicator_catalog_plan 的规划结果；dialect - 当前数据源方言。
# Returns: 只选择指标编码/名称且限制 1000 行的 SQL；无需或不能查询时返回空字符串。
def build_indicator_catalog_sql(plan: IndicatorCatalogPlan, dialect: str) -> str:
    """SQL 不读取 metric_value，也不附加时间条件。"""
    if plan.mode != "row_catalog" or not plan.selected_table:
        return ""
    columns = [column for column in (plan.code_column, plan.name_column) if column]
    if not columns:
        return ""
    selected = ", ".join(_quote_identifier(column, dialect) for column in columns)
    non_null = " OR ".join(
        f"{_quote_identifier(column, dialect)} IS NOT NULL" for column in columns
    )
    order_by = ", ".join(_quote_identifier(column, dialect) for column in columns)
    table = _quote_identifier(plan.selected_table, dialect)
    return (
        f"SELECT DISTINCT {selected} FROM {table} WHERE {non_null} ORDER BY {order_by} LIMIT 1000"
    )


# 方法作用：根据真实表名、字段名和注释确定默认业务主题。
# Args: table - 表名；column - 字段名；description - 字段注释。
# Returns: 命中的业务主题，无法识别时返回“其他”。
def _infer_theme(table: str, column: str, description: str) -> str:
    """主题规则只影响展示，不改变指标口径和查询行为。"""
    evidence = f"{table} {column} {description}".lower()
    for theme, markers in _THEME_RULES:
        if any(marker in evidence for marker in markers):
            return theme
    return "其他"


# 方法作用：把 Schema 注释或字段名整理成可读指标名称。
# Args: column - 真实字段名；comment - 字段注释；reviewed_name - 模型复检名称。
# Returns: 用于前端展示的指标名称。
def _display_name(column: str, comment: str, reviewed_name: str = "") -> str:
    """优先采用模型在证据内整理的名称，其次使用 Schema 注释。"""
    candidate = reviewed_name.strip() or comment.strip()
    if candidate:
        return re.sub(r"[（(](?:保留|单位[:：]?).*?[）)]", "", candidate).strip()
    return column


# 方法作用：从确定性候选生成可展示的主题分组。
# Args: plan - 指标目录规划；reviewed - 可选模型复检条目。
# Returns: 仅包含真实白名单字段的主题分组。
def build_indicator_display_groups(
    plan: IndicatorCatalogPlan,
    reviewed: list[dict] | None = None,
) -> list[IndicatorDisplayGroup]:
    """模型只能筛选和命名真实数值字段，未知表字段会被丢弃。"""
    table_map = {table.name: table for table in plan.tables}
    selected: list[tuple[str, str, str, str]] = []
    if reviewed is None:
        for table in plan.tables:
            for column in table.metric_columns:
                comment = table.column_comments.get(column, "")
                selected.append((table.name, column, _display_name(column, comment), ""))
    else:
        seen: set[tuple[str, str]] = set()
        for item in reviewed:
            table_name = str(item.get("table", "") or "").strip()
            column = str(item.get("column", "") or "").strip()
            table = table_map.get(table_name)
            key = (table_name, column)
            if (
                not item.get("include")
                or table is None
                or column not in table.numeric_columns
                or key in seen
            ):
                continue
            seen.add(key)
            comment = table.column_comments.get(column, "")
            selected.append((
                table_name,
                column,
                _display_name(column, comment, str(item.get("display_name", "") or "")),
                str(item.get("theme", "") or "").strip(),
            ))

    grouped: dict[str, list[IndicatorDisplayItem]] = {}
    for table_name, column, name, reviewed_theme in selected:
        table = table_map[table_name]
        description = table.column_comments.get(column, "")
        theme = reviewed_theme or _infer_theme(table_name, column, description)
        grouped.setdefault(theme, []).append(IndicatorDisplayItem(
            name=name,
            description=description,
            table=table_name,
            column=column,
        ))
    return [
        IndicatorDisplayGroup(theme=theme, indicators=indicators)
        for theme, indicators in grouped.items()
        if indicators
    ]


# 方法作用：调用模型复检真实数值字段的指标属性，并在失败时回退确定性过滤。
# Args: plan - 基于 Schema 生成的指标目录规划。
# Returns: 已完成白名单校验的主题化指标分组。
async def review_indicator_catalog(
    plan: IndicatorCatalogPlan,
) -> list[IndicatorDisplayGroup]:
    """模型不读取业务数据，也不能越过 Schema 字段白名单。"""
    fallback = build_indicator_display_groups(plan)
    candidates = [
        {
            "table": table.name,
            "table_description": table.description,
            "column": column,
            "comment": table.column_comments.get(column, ""),
            "deterministic_include": column in table.metric_columns,
        }
        for table in plan.tables
        for column in table.numeric_columns
    ]
    if not candidates:
        logger.info("指标目录模型复检跳过", reason="无数值候选")
        return fallback
    try:
        from src.llm.client import is_task_llm_available

        if not is_task_llm_available("direct_answer"):
            logger.info("指标目录模型复检跳过", reason="模型不可用", candidate_count=len(candidates))
            return fallback
        from src.llm.invocation import invoke_structured
        from src.llm.output_contracts import IndicatorCatalogReviewOutput
        from src.llm.prompt_budget import PromptSection

        result = await invoke_structured(
            "indicator_catalog.review",
            [PromptSection(
                "candidates",
                "## 待复检数值字段\n" + json.dumps(candidates, ensure_ascii=False),
                priority=100,
                min_chars=1000,
                max_chars=6000,
            )],
            output_model=IndicatorCatalogReviewOutput,
            task="direct_answer",
            temperature=0.0,
            reasoning=False,
        )
        reviewed = [item.model_dump() for item in result.indicators]
        groups = build_indicator_display_groups(plan, reviewed)
        logger.info(
            "指标目录模型复检完成",
            candidate_count=len(candidates),
            returned_count=len(reviewed),
            accepted_count=sum(len(group.indicators) for group in groups),
            theme_count=len(groups),
        )
        return groups
    except Exception as exc:
        logger.error("指标目录模型复检失败，回退确定性过滤", error=str(exc), exc_info=True)
        return fallback


# 方法作用：把 Schema 目录和可选行式查询结果整理为确定性中文摘要。
# Args: plan - 指标目录规划；rows - 行式目录查询返回的编码/名称行。
# Returns: 不编造指标、不要求时间范围的可展示摘要。
def build_indicator_catalog_summary(
    plan: IndicatorCatalogPlan,
    rows: list[dict] | None = None,
    groups: list[IndicatorDisplayGroup] | None = None,
) -> str:
    """摘要明确标注信息来自 Schema 或受限目录枚举。"""
    if plan.mode == "unavailable":
        return "当前 Schema 中没有可用于识别指标目录的表结构，请指定业务域或目标表。"
    logger.info(
        "指标目录展示组装边界",
        mode=plan.mode,
        table_count=len(plan.tables),
        metric_count=sum(len(table.metric_columns) for table in plan.tables),
        rows_count=len(rows or []),
    )
    display_groups = groups if groups is not None else build_indicator_display_groups(plan)
    metric_count = sum(len(group.indicators) for group in display_groups)
    logger.info(
        "指标目录摘要输入边界",
        metric_count=metric_count,
        group_count=len(display_groups),
        themes=[group.theme for group in display_groups],
        indicator_names=[
            item.name
            for group in display_groups
            for item in group.indicators
        ][:50],
    )
    lines = [
        f"当前数据源确认 {metric_count} 项业务指标，"
        f"已按 {len(display_groups)} 个业务主题整理。"
    ]
    if plan.mode == "schema_catalog":
        for group in display_groups:
            names = "、".join(item.name for item in group.indicators if item.name)
            if names:
                lines.append(f"- {group.theme}（{len(group.indicators)} 项）：{names}")
    elif plan.mode == "ambiguous":
        candidates = "、".join(
            table.name for table in plan.tables if table.storage_mode == "row_values"
        )
        lines.append(f"检测到多个行式指标目录表：{candidates}。请指定业务域或目标表。")
    elif plan.mode == "row_catalog":
        row_table = next(
            (table for table in plan.tables if table.name == plan.selected_table),
            None,
        )
        if rows:
            labels: list[str] = []
            for row in rows:
                code = str(row.get(plan.code_column, "") or "").strip()
                name = str(row.get(plan.name_column, "") or "").strip()
                label = f"{code}（{name}）" if code and name else code or name
                if label and label not in labels:
                    labels.append(label)
            lines.append(f"- {plan.selected_table}：行式指标目录，" + "、".join(labels))
        elif row_table is not None:
            lines.append(
                f"- {plan.selected_table}：行式指标目录，将仅枚举 "
                f"{row_table.code_column or row_table.name_column}"
                + (
                    f" / {row_table.name_column}"
                    if row_table.code_column and row_table.name_column
                    else ""
                )
            )
    return "\n".join(lines)
