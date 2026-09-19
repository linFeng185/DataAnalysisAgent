"""报表图表方案：由模型决策形态，由**确定性代码**完成分组与聚合。

两条不可动摇的分工（2026-09-15 实机验收教训）：
- 模型只决定「看什么」：图表类型、x 轴列、分组维度、指标列。
- 分组、透视、聚合、排序**一律由本模块用代码完成**。把明细行直接铺成数据点会让
  「各区域月度趋势」出现 100 个重复的 2016-01（实机事故），而且模型算数容易幻觉。

与对话路径的关系：类型推断与单系列构型复用 `src.tools.chart_generator`；本模块在它
之上补透视、多维度切换与中文化，不另造第二套 ECharts 构型。
"""

from __future__ import annotations

import math
import re
from dataclasses import dataclass
from decimal import Decimal
from typing import Any

from src.llm.output_contracts import ChartSpec, ReportChartPlan
from src.logging_config import get_logger

logger = get_logger(__name__)

# 提示词标识与模型任务标识：项目约定 prompt_id 用点号、task 用下划线。
_PROMPT_ID = "report.chart"
_PROMPT_TASK = "report_chart"
# 探针只喂少量样例：图表决策看的是"数据形态"，不需要整份结果。
_PROBE_ROW_LIMIT = 3
_PROBE_COLUMN_LIMIT = 30
# 单张图最多画几条系列：再多图例就会压住画布。超出时按量级取前 N。
# 取 8 还有体积上的考虑：每种分组视图都要整份下发，系列多会成倍放大页面体积。
_MAX_SERIES = 8
# 时间列名字特征，用于兜底时确定 X 轴。
_TIME_COLUMN_HINTS = ("date", "time", "day", "month", "year", "dt", "period", "期间", "日期", "月份", "年")
# 比率/单价/深度类列的聚合语义是"求平均"，其余可加指标才求和。
# 用确定性词表判定，不让模型算，也不把比率列相加（那会得出无意义的和）。
_AVG_COLUMN_HINTS = (
    "rate", "ratio", "pct", "percent", "avg", "average", "mean",
    "price", "depth", "margin", "share", "unit_", "score", "index", "temp", "hour",
)


# 方法作用：判断单个值是否可当作数值绘制。
# Args: value - 待判断的值。
# Returns: 是数值或可转数值的字符串时返回 True。
def _is_number(value: Any) -> bool:
    return _as_float(value) is not None


# 方法作用：把值转成浮点数，非数值返回 None。
# Args: value - 待转换的值。
# Returns: 浮点数或 None。
def _as_float(value: Any) -> float | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float, Decimal)):
        number = float(value)
        return number if math.isfinite(number) else None
    if isinstance(value, str):
        try:
            number = float(value.strip().replace(",", ""))
            return number if math.isfinite(number) else None
        except ValueError:
            return None
    return None


# 方法作用：按列名语义决定该列的聚合方式。
# Args: column - 列名。
# Returns: "avg" 表示求平均，"sum" 表示求和。
def _aggregate_kind(column: str) -> str:
    name = str(column or "").lower()
    return "avg" if any(hint in name for hint in _AVG_COLUMN_HINTS) else "sum"


# 方法作用：对一组值按列语义求聚合值。
# Args: values - 数值列表；column - 列名。
# Returns: 聚合结果；无有效值时返回 None。
def _aggregate(values: list[float], column: str) -> float | None:
    if not values:
        return None
    if _aggregate_kind(column) == "avg":
        return round(sum(values) / len(values), 4)
    return round(sum(values), 4)


# 标识类列名的尾词：ID / 编码 / 键 / 编号是实体的名字，不是可加可平均的度量。
# 实机取证（分享 token 2gwR2fiF…）：store_id / product_id 在库里是数值类型，
# 按「取值全是数字」被判成指标列后，横轴失去类目，退化成 [''] 或 ['missed','summary']。
_IDENTIFIER_TAIL_TOKENS = frozenset({"id", "code", "key", "no", "uuid"})

@dataclass(slots=True)
class ColumnProfile:
    """结果集的列画像：全量列序 + 每列的非空/数值/脏值计数。

    `_split_columns` 原先只看前 50 行。真实 manifest 里 union 分支的指标列
    （`budget_tax.execution_rate_pct`、`hr_cost.engagement_response_count`）在前 200 行
    全为空、读数出现在 200 行之后，于是整列被判成维度，重建时直接抛 `ChartNotRenderable`。
    改为全量扫描后单次成本是 O(行 × 列)，而同一份结果会被一张图的多个构建步骤反复用到，
    因此把证据累计到这里由调用方复用，而不是让每个图各扫一遍全表。
    """

    columns: list[str]
    non_empty_counts: dict[str, int]
    numeric_counts: dict[str, int]
    dirty_counts: dict[str, int]


# 方法作用：对整份结果做一次线性扫描，累计每列的数值/脏值/非空证据与全量列序。
# Args: rows - 结果行。
# Returns: 列画像；空输入返回空画像。
def profile_columns(rows: list[dict[str, Any]]) -> ColumnProfile:
    """单次线性扫描，不做任何列级数据复制：内存只随「列数」增长，不随「行数」增长。"""
    columns: list[str] = []
    numeric_counts: dict[str, int] = {}
    dirty_counts: dict[str, int] = {}
    non_empty_counts: dict[str, int] = {}
    for row in rows:
        for name, value in row.items():
            # 首次见到该列时登记全量列序与计数槽：后段才出现的键因此不会消失。
            if name not in numeric_counts:
                columns.append(name)
                numeric_counts[name] = 0
                dirty_counts[name] = 0
                non_empty_counts[name] = 0
            if value is None or (isinstance(value, str) and not value.strip()):
                continue
            non_empty_counts[name] += 1
            if _is_number(value):
                numeric_counts[name] += 1
            else:
                dirty_counts[name] += 1
    logger.debug("图表列画像完成", row_count=len(rows), column_count=len(columns))
    return ColumnProfile(
        columns=columns,
        non_empty_counts=non_empty_counts,
        numeric_counts=numeric_counts,
        dirty_counts=dirty_counts,
    )


# 方法作用：判断列名是否属于 ID / 编码 / 键这类标识列。
# Args: column - 列名。
# Returns: 是标识列返回 True。
def _is_identifier_column(column: str) -> bool:
    """标识列即使取值全是数字也不是指标：门店编号、商品编号都没有"合计"的语义。

    时间列名不参与判定：`year_month`、`stat_date`、`period_no` 这类名字里带时间片段的列
    仍按原有规则分类，避免把真正的时间轴推成维度后又回退成空类目。
    """
    name = str(column or "").strip().lower()
    if not name:
        return False
    if any(hint in name for hint in _TIME_COLUMN_HINTS):
        return False
    tokens = [token for token in re.split(r"[^a-z0-9]+", name) if token]
    if not tokens:
        return False
    is_identifier = tokens[-1] in _IDENTIFIER_TAIL_TOKENS
    logger.debug("图表标识列判定完成", column=column, is_identifier=is_identifier)
    return is_identifier


# 方法作用：判断某列在结果里是否存在非空取值。
# Args: rows - 结果行；field - 列名；profile - 可复用的列画像，缺省时现场短路扫描。
# Returns: 至少有一个非空取值时返回 True。
def _column_has_values(
    rows: list[dict[str, Any]], field: str, *, profile: ColumnProfile | None = None
) -> bool:
    """全空列既不能当横轴也不能当分组维度：它画出来只会是一个空刻度或空系列。"""
    if not field:
        return False
    if profile is not None:
        return profile.non_empty_counts.get(field, 0) > 0
    for row in rows:
        value = row.get(field)
        if value is None:
            continue
        if isinstance(value, str) and not value.strip():
            continue
        return True
    return False


# 方法作用：把列拆成数值列与维度列，保持原始列序。
# Args: rows - 非空结果行；profile - 可复用的列画像，缺省时现场全量扫描。
# Returns: (数值列名列表, 维度列名列表)。
def _split_columns(
    rows: list[dict[str, Any]], *, profile: ColumnProfile | None = None
) -> tuple[list[str], list[str]]:
    """全量扫描：只扫前若干行会把后段才出现的指标判成维度，整列读数随之消失。"""
    data = profile if profile is not None else profile_columns(rows)
    numeric: list[str] = []
    dimensions: list[str] = []
    for name in data.columns:
        numeric_count = data.numeric_counts.get(name, 0)
        dirty_count = data.dirty_counts.get(name, 0)
        # 合计行、N/A 等单个脏值不能让整列降级；但至少要有两个数值证据，
        # 才允许在混合列上容忍非数值，避免把「编号 + 名称」误判成指标。
        tolerates_dirty = numeric_count >= 2 and dirty_count <= max(1, numeric_count // 4)
        # 标识列即使取值全是数字也归维度：它当指标画会让横轴失去类目。
        if (
            data.non_empty_counts.get(name, 0)
            and (dirty_count == 0 or tolerates_dirty)
            and not _is_identifier_column(name)
        ):
            numeric.append(name)
        else:
            dimensions.append(name)
    logger.debug(
        "图表列拆分完成",
        numeric_count=len(numeric),
        dimension_count=len(dimensions),
        reused_profile=profile is not None,
    )
    return numeric, dimensions


# 方法作用：把列名换成可读标签，供图例与提示框展示。
# Args: column - 列名；labels - 列名到中文标签的映射。
# Returns: 中文标签；没有映射时返回原名。
def _label(column: str, labels: dict[str, str] | None) -> str:
    return str((labels or {}).get(column, "") or translate_column_name(column))


# 常见业务指标的整词译名，命中即用——按词翻译会把 avg_order_value 拼成"平均订单值"。
_COLUMN_PHRASE_LABELS: dict[str, str] = {
    "avg_order_value": "客单价",
    "net_sales": "净销售额",
    "net_sales_amount": "净销售额",
    "net_sales_total": "净销售额合计",
    "net_amount": "净额",
    "order_count": "订单数",
    "customer_count": "客户数",
    "gross_profit_before_refund": "退款前毛利",
    "gross_margin_pct": "毛利率",
    "unit_price": "单价",
    "avg_unit_price": "件单价",
    "discount_depth_pct": "折扣深度",
    "refund_amount": "退款金额",
    "days_of_inventory": "库存天数",
    "turnover_ratio": "周转率",
    "execution_rate_pct": "费用执行率",
    "weighted_avg_unit_cost": "加权平均成本",
    "defect_rate_pct": "不良率",
    "spare_part_cnt": "备件消耗数",
    "spare_stock_units": "备件库存量",
    "labor_cost": "人力成本",
    "work_hours": "工时",
    "claim_amount": "报销金额",
    "salary_band_min": "薪酬带宽下限",
    "salary_band_mid": "薪酬带宽中位",
    "salary_band_max": "薪酬带宽上限",
    "avg_engagement_rating": "平均敬业度评分",
    "engagement_response_count": "敬业度调研人数",
    "variance_amount": "差异金额",
    "gap_amount": "缺口金额",
    "achieve_rate": "达成率",
    "target_amount": "目标金额",
    "attributed_revenue": "归因收入",
    "revenue_gap": "收入差额",
    "freight_to_revenue_pct": "运费占收入比",
    "avg_freight_per_order": "单均运费",
    "invoice_receipt_diff": "发票收款差额",
    "receipt_refund_diff": "收款退款差额",
    "difference_amount": "差额金额",
    "low_recency_pct": "低近度占比",
    "low_frequency_pct": "低频次占比",
    "low_monetary_pct": "低消费占比",
    "net_sales_per_customer": "人均净销售额",
    "annual_fee_per_customer": "人均年费",
    "net_sales_yoy_pct": "净销售额同比",
    "net_sales_mom_pct": "净销售额环比",
    "budget_amount": "预算金额",
    "actual_amount": "实际金额",
    "invoice_amount": "发票金额",
    "receipt_amount": "收款金额",
    "payable_amount": "应付金额",
    "freight_amount": "运费",
    "coupon_issued_total": "优惠券发放数",
    "coupon_redeemed_total": "优惠券核销数",
    "points_earned_total": "积分发放数",
    "points_redeemed_total": "积分核销数",
}

# 分词译名：整词表未命中时按 _ 拆词逐段翻译，未命中的片段保留原文。
_COLUMN_TOKEN_LABELS: dict[str, str] = {
    "net": "净", "sales": "销售", "amount": "金额", "cnt": "数", "count": "数",
    "qty": "数量", "quantity": "数量", "total": "合计", "sum": "合计",
    "avg": "平均", "average": "平均", "mean": "平均", "max": "最高", "min": "最低",
    "customer": "客户", "order": "订单", "product": "商品", "goods": "商品",
    "store": "门店", "shop": "门店", "region": "地区", "area": "区域",
    "dept": "部门", "department": "部门", "center": "中心", "employee": "员工",
    "labor": "人力", "staff": "人员", "cost": "成本", "price": "单价",
    "discount": "折扣", "depth": "深度", "rate": "率", "ratio": "比率",
    "pct": "占比", "percent": "占比", "margin": "毛利", "gross": "毛利",
    "profit": "利润", "invoice": "发票", "receipt": "收款", "refund": "退款",
    "freight": "运费", "shipping": "运费", "inventory": "库存", "stock": "库存",
    "turnover": "周转", "defect": "不良", "spare": "备件", "part": "件",
    "work": "工作", "hours": "工时", "claim": "报销", "salary": "薪酬",
    "band": "带宽", "engagement": "敬业度", "rating": "评分", "score": "得分",
    "budget": "预算", "actual": "实际", "variance": "差异", "gap": "缺口",
    "target": "目标", "achieve": "达成", "attributed": "归因", "revenue": "收入",
    "low": "低", "high": "高", "recency": "近度", "frequency": "频次",
    "monetary": "消费", "coupon": "优惠券", "issued": "发放", "redeemed": "核销",
    "points": "积分", "earned": "获得", "annual": "年", "fee": "费",
    "execution": "执行", "aging": "账龄", "bucket": "档", "payable": "应付",
    "receivable": "应收", "balance": "余额", "diff": "差额", "difference": "差额",
    "scenario": "情景", "segment": "分层", "level": "等级", "type": "类型",
    "name": "名称", "code": "编码", "record": "记录", "alert": "告警",
    "month": "月份", "year": "年份",
    "day": "日期", "week": "周", "quarter": "季度", "yoy": "同比", "mom": "环比",
    "growth": "增长", "to": "占", "value": "值", "gap_amount": "缺口",
}


# 方法作用：把英文列名/别名按业务词表翻成中文，供图例与提示框兜底展示。
# Args: column - 列名或 SQL 别名。
# Returns: 中文标签；不含任何已知业务词时原样返回列名。
def translate_column_name(column: str) -> str:
    text = str(column or "").strip()
    if not text:
        return ""
    phrase = _COLUMN_PHRASE_LABELS.get(text.lower())
    if phrase:
        return phrase
    # 只处理纯英文列名：含中文或非英文片段的列名保持原样，避免误翻。
    if not all(part.isascii() for part in text.split("_")):
        return text
    parts = [part for part in text.lower().split("_") if part]
    labels = [_COLUMN_TOKEN_LABELS.get(part) for part in parts]
    if not any(labels):
        return text
    # 未命中的片段保留原文，命中的按序拼接，保证信息不丢。
    return "".join(label if label else part for part, label in zip(parts, labels, strict=True))


class ChartNotRenderable(ValueError):
    """确定性规则判定这张图无法生成可读 option。

    调用方（页面构建）必须据此降级为明细表并**向用户披露原因**：静默画一张空图或
    换一个指标顶替，都会让用户把"画不出来"误读成"数据就是这样"。
    """


# 方法作用：把一条面向用户的图表说明追加进警告列表（去重）。
# Args: warnings - 警告列表，可为 None；message - 待追加的说明。
# Returns: 无返回值。
def _warn(warnings: list[str] | None, message: str) -> None:
    if warnings is None or not message:
        return
    if message not in warnings:
        warnings.append(message)


# 方法作用：把横轴回退写成用户可见的中文说明，含声明列、实际列与标题失真提示。
# Args: warnings - 披露列表，可为 None；declared - 模型声明的横轴列；used - 实际使用的横轴列；
#       title - 图表的显式标题，可为空。
# Returns: 无返回值。
def _disclose_axis_fallback(
    *, warnings: list[str] | None, declared: str, used: str, title: str = ""
) -> None:
    """回退保住了可读性，但「各区域折扣深度对比」配上告警等级轴就是一句错话。

    实机取证：`chart_price_promotion_1` 的 `region_code` 全 NULL，代码静默换成
    `margin_alert_level`，warnings 仍为空——用户读到的是「各区域」，看到的是「告警等级」。
    只打日志不够：页面质量块必须出现这层错位。
    """
    if not declared or not used or declared == used:
        return
    # 三种原因都要说到：声明列可能压根不存在、可能存在但类型不适合作横轴（数值列）、
    # 也可能存在却整列为空。只写「不存在或全空」会在类型不适用时对用户说假话。
    _warn(
        warnings,
        f"图表声明的横轴「{declared}」不可用（列不存在、类型不适合作横轴或整列为空），已改用「{used}」",
    )
    if title:
        _warn(warnings, f"图表标题「{title}」按原横轴撰写，实际横轴已改为「{used}」，标题与图形可能不符")


# 方法作用：在有值的维度列里挑一个适合当横轴的列。
# Args: rows - 结果行；dimensions - 维度列；exclude - 需要排除的列；
#       profile - 可复用的列画像，缺省时现场扫描。
# Returns: 列名；没有可用维度时返回空串。
def _pick_populated_x(
    *,
    rows: list[dict[str, Any]],
    dimensions: list[str],
    exclude: tuple[str, ...] = (),
    profile: ColumnProfile | None = None,
) -> str:
    """类目数适中的优先：实机出现过 488 个门店名挤在一根横轴上。

    全空列一律不选——实机取证：price_promotion 的 region_code 实际全为 NULL，
    画出来就是一个只有空刻度的框。
    """
    usable = [
        name
        for name in dimensions
        if name not in exclude and _column_has_values(rows, name, profile=profile)
    ]
    readable = [
        name
        for name in usable
        if 1 < len({row.get(name) for row in rows if row.get(name) not in (None, "")}) <= _MAX_X_CATEGORIES
    ]
    chosen = readable[0] if readable else (usable[0] if usable else "")
    if not chosen:
        logger.info("图表没有可用作横轴的维度列", dimension_count=len(dimensions))
    return chosen


# 指标列不可用的三种真实原因：文案必须逐列说准，混成一句「不存在」就是对用户说假话。
_UNAVAILABLE_REASON_ABSENT = "列不存在"
_UNAVAILABLE_REASON_EMPTY = "整列为空或无有效数值"
_UNAVAILABLE_REASON_MISTYPED = "列类型不适合作指标"


# 方法作用：把不可用作指标的列按真实原因分组描述，供用户可见文案使用。
# Args: names - 不可用的列名列表（保持声明顺序）；rows - 结果行；
#       profile - 可复用的列画像，缺省时现场全量扫描。
# Returns: 形如「ghost（列不存在）、blank（整列为空或无有效数值）」的中文描述串。
def _describe_unavailable_columns(
    names: list[str], *, rows: list[dict[str, Any]], profile: ColumnProfile | None = None
) -> str:
    """「不在数值列里」有三种成因，混成一句「不存在」会让用户去排查一个不存在的问题。

    实机语义：声明列名与结果列对得上、只是整列 NULL 或空串时，`_split_columns` 按「无非空取值」
    把它归入维度，`_usable_columns` 便按「不在 numeric 里」判为不可用；照此写「列不存在」，
    用户会去查 SQL 别名，而真正的问题是那一列根本没有读数。
    """
    data = profile if profile is not None else profile_columns(rows)
    available = set(data.columns)
    # 保持声明顺序：先出现的列，其原因分组也先出现（dict 保序）。
    grouped: dict[str, list[str]] = {}
    for name in names:
        if name not in available:
            reason = _UNAVAILABLE_REASON_ABSENT
        elif data.non_empty_counts.get(name, 0) <= 0:
            reason = _UNAVAILABLE_REASON_EMPTY
        else:
            # 有值却当不了指标：标识列（store_id）或混入过多非数值、无法求和的列。
            reason = _UNAVAILABLE_REASON_MISTYPED
        grouped.setdefault(reason, []).append(name)
    described = "、".join(f"{'、'.join(group)}（{reason}）" for reason, group in grouped.items())
    logger.debug(
        "图表不可用指标列已分类",
        absent_count=len(grouped.get(_UNAVAILABLE_REASON_ABSENT, [])),
        empty_count=len(grouped.get(_UNAVAILABLE_REASON_EMPTY, [])),
        mistyped_count=len(grouped.get(_UNAVAILABLE_REASON_MISTYPED, [])),
    )
    return described


# 方法作用：把模型给的列名对齐到真实存在的结果列，避免"列名对不上导致整张空图"。
# Args: spec - 图表方案；rows - 结果行；profile - 可复用的列画像；
#       warnings - 可选的用户可见说明列表，横轴回退原因会追加进去。
# Returns: (可用 x 列, 可用指标列列表, 可切换维度候选, 声明了但不可用作指标的列)。
# Raises: ChartNotRenderable - 声明的指标列一个都不可用时抛出，调用方应降级为明细表。
def _usable_columns(
    *,
    spec: ChartSpec,
    rows: list[dict[str, Any]],
    profile: ColumnProfile | None = None,
    warnings: list[str] | None = None,
) -> tuple[str, list[str], list[str], list[str]]:
    numeric, dimensions = _split_columns(rows, profile=profile)
    declared_x = str(spec.x_field or "")
    chart_type = str(spec.chart_type or "bar")
    # 散点图的两条轴都是数值列；沿用分类图的「x 只能是维度」规则会把点集恒清空。
    x_candidates = numeric if chart_type == "scatter" else dimensions
    # 列名对得上但整列全空时同样不能用：它会吃掉全部数据点，只剩一个空刻度。
    x_field = (
        declared_x
        if declared_x in x_candidates and _column_has_values(rows, declared_x, profile=profile)
        else ""
    )
    if not x_field:
        if chart_type == "scatter":
            x_field = numeric[0] if numeric else ""
        else:
            # 模型按 schema 猜的列名在结果里不存在（SQL 改了别名）时，退到有值的维度列。
            x_field = _pick_populated_x(rows=rows, dimensions=dimensions, profile=profile)
        if x_field and x_field != declared_x:
            logger.info("图表 x 轴列名对不上结果列，已回退", declared=declared_x, used=x_field)
            # 换轴不能只留日志：页面质量块要写明声明列与实际列，标题可能随之失真的也要说。
            _disclose_axis_fallback(
                warnings=warnings,
                declared=declared_x,
                used=x_field,
                title=str(getattr(spec, "title", "") or ""),
            )
        elif not x_field and declared_x:
            # 连替代列都没有时同样要点名：只说「横轴没有可用类目」，用户不知道是哪一列。
            _warn(
                warnings,
                f"图表声明的横轴「{declared_x}」不可用（列不存在、类型不适合作横轴或整列为空），"
                "且没有其它可用作横轴的维度列",
            )
    requested = [str(name) for name in spec.series_fields if name]
    resolved = [name for name in requested if name in numeric and name != x_field]
    # 列在结果里存在、只是被选作横轴而不能再当纵轴：这是「被排除」，不是「缺失」。
    # 实机语义：`scatter(x_field=price, series_fields=[price, quantity])` 里 price 既当 x
    # 又出现在 series_fields；把 x 从 resolved 排除后再用「不在 resolved」判缺失，
    # 就会向用户披露「price 不存在」——那一列明明就在结果里。
    used_as_axis = [name for name in requested if name == x_field and name in numeric]
    # 这里只判「不可用作指标」，不判「列不存在」：整列为空、类型不适合作指标的列同样落在
    # numeric 之外，具体原因由 _describe_unavailable_columns 逐列说准。
    # 非散点图的 x 取自维度列，与 numeric 天然互斥，因此 used_as_axis 豁免只影响散点图，
    # 既有的 requested/resolved 保护不变。
    missing = [name for name in requested if name not in numeric]
    if requested and not resolved and not used_as_axis:
        # 声明的指标一个都不可用：拿别的数值列顶上等于换了一个指标画，
        # 用户看到的是"长得像但问的不是它"的图——降级并说明，不静默替换。
        logger.warning("图表声明的指标列全部不可用，图表不可渲染", declared=requested)
        raise ChartNotRenderable(
            f"声明的指标列不可用：{_describe_unavailable_columns(requested, rows=rows, profile=profile)}"
        )
    if resolved:
        metrics = resolved
        if missing:
            logger.warning("图表部分指标列不可用，仅绘制可用指标", missing=missing, drawn=resolved)
    else:
        # 模型压根没声明指标（旧调用方或方案不完整）时才按数值列补齐。
        metrics = [name for name in numeric if name != x_field][:3]
        if metrics:
            logger.info("图表未声明指标列，按数值列补齐", used=metrics)
        elif chart_type == "scatter":
            # 散点图两条轴都必须是数值列：一个非 x 的数值列都没有时，唯一能画的点
            # 是 (x, x) 自比，等于画了一张没有信息量的图。如实降级，且**不能**把
            # 原因写成「声明的列不存在」——x 列就在结果里，缺的是纵轴指标。
            logger.warning("散点图没有可用作纵轴的数值列", x_field=x_field, declared=requested)
            raise ChartNotRenderable("散点图缺少可用作纵轴的数值列")
    # 全空的候选维度不生成切换项：切过去只会得到一张空图。
    candidates = [
        name
        for name in spec.switchable_fields
        if name in dimensions and name != x_field and _column_has_values(rows, name, profile=profile)
    ]
    return x_field, metrics, candidates, missing


# x 轴时间点超过该数量就自动上卷到更粗的粒度：一根轴上挤上百个点，图和标签都没法看。
_TIME_ROLLUP_THRESHOLD = 24
# 非时间 x 轴的类目上限：超过就说明这个维度不适合当横轴（实机出现过 488 个门店名）。
_MAX_X_CATEGORIES = 60
# 单张分类图真正保留多少个横轴类目：约 800px 图宽下再多就只剩糊成一片的色块。
# 与 _MAX_X_CATEGORIES 的分工：后者决定「哪一列适合当横轴」（回退选列时用），
# 前者决定「横轴上真的画几个类目」——声明合法的列也会超限（实机 198~200 个类目，
# axisLabel.hideOverlap 会把大部分标签藏掉，用户看到的就是「图里缺了什么」）。
_MAX_VISIBLE_CATEGORIES = 24
# 由细到粗的时间粒度序列，上卷时逐级放大。
_TIME_GRAINS = ("minute", "hour", "day", "month", "year")


# 方法作用：识别一组取值的时间粒度。
# Args: xs - x 轴取值列表。
# Returns: minute / hour / day / month / year；识别不出时返回 unknown。
def _time_grain(xs: list[str]) -> str:
    sample = next((value for value in xs if str(value or "").strip()), "")
    text = str(sample).strip()
    if not text:
        return "unknown"
    if len(text) >= 16 and text[10] == " " and text[13] == ":":
        return "minute"
    if len(text) == 13 and text[10] == " ":
        return "hour"
    if len(text) >= 10 and text[4] == "-" and text[7] == "-":
        return "day"
    if len(text) == 7 and text[4] == "-":
        return "month"
    if len(text) == 4 and text.isdigit():
        return "year"
    return "unknown"


# 方法作用：把一个时间值上卷一级（分钟→小时→日→月→年）。
# Args: value - 原始时间值；grain - 其当前粒度。
# Returns: 上卷后的时间值；无法上卷时原样返回。
def _coarsen_value(value: str, grain: str) -> str:
    text = str(value or "")
    if grain == "minute" and len(text) >= 13:
        return text[:13]
    if grain == "hour" and len(text) >= 10:
        return text[:10]
    if grain == "day" and len(text) >= 7:
        return text[:7]
    if grain == "month" and len(text) >= 4:
        return text[:4]
    return text


# 方法作用：把时间值从一种粒度上卷到另一种粒度。
# Args: value - 原始时间值；from_grain - 原粒度；to_grain - 目标粒度。
# Returns: 上卷后的时间值。
def _coarsen_value_to(value: str, from_grain: str, to_grain: str) -> str:
    if from_grain == "unknown" or to_grain == "unknown" or from_grain == to_grain:
        return str(value or "")
    current = str(value or "")
    grain = from_grain
    while grain != to_grain and grain in _TIME_GRAINS[:-1]:
        current = _coarsen_value(current, grain)
        grain = _TIME_GRAINS[_TIME_GRAINS.index(grain) + 1]
    return current


# 方法作用：按数据点数量决定实际使用的 x 轴粒度，必要时逐级上卷。
# Args: xs - 原始去重取值；x_field - 列名（仅用于日志）。
# Returns: (最终使用的 x 取值, 原粒度, 目标粒度)；非时间轴原粒度与目标粒度相同。
def _rollup_time_axis(xs: list[str], x_field: str) -> tuple[list[str], str, str]:
    source_grain = _time_grain(xs)
    if source_grain == "unknown":
        return xs, source_grain, source_grain
    target = source_grain
    coarsened = xs
    # 逐级放大直到点数进入阈值或已到年：跨度大自动按年、小则保留月/日/时。
    while len(coarsened) > _TIME_ROLLUP_THRESHOLD and target in _TIME_GRAINS[:-1]:
        target = _TIME_GRAINS[_TIME_GRAINS.index(target) + 1]
        coarsened = list(dict.fromkeys(_coarsen_value_to(value, source_grain, target) for value in xs))
    if target != source_grain:
        logger.info(
            "时间轴自动上卷",
            x_field=x_field,
            from_grain=source_grain,
            to_grain=target,
            point_count=len(coarsened),
        )
    return coarsened, source_grain, target


# 方法作用：确定 x 轴的取值序列，数值型按数值排序，其余按首次出现序。
# Args: rows - 结果行；x_field - x 轴列名。
# Returns: 去重后的 x 取值字符串列表。
def _x_values(rows: list[dict[str, Any]], x_field: str) -> list[str]:
    raw = [row.get(x_field) for row in rows]
    # NULL 与空白串都不是类目：让它们进轴会得到一个没有任何含义的空刻度，
    # 而对应行在分桶时也落不到任何轴上（键与轴取值对不上），数据不会因此丢失。
    non_null = [value for value in raw if value is not None and str(value).strip() != ""]
    if non_null and all(_as_float(value) is not None for value in non_null):
        # 数值型 x 轴按数值排序，避免字符串比较把 10 排到 2 前面。
        uniques = sorted({float(value) for value in non_null})
        return [str(int(value)) if float(value).is_integer() else str(value) for value in uniques]
    return list(dict.fromkeys(str(value) for value in non_null))


# 方法作用：把分桶键规范成与 _x_values 完全一致的字符串表示。
# Args: value - 原始横轴或维度值。
# Returns: 规范后的键；空值返回空字符串。
def _axis_key(value: Any) -> str:
    if value is None:
        return ""
    number = _as_float(value)
    if number is not None:
        return str(int(number)) if number.is_integer() else str(number)
    return str(value)


# 方法作用：按横轴类目累计所有可见指标的绝对量级，作为可读化保留的排序依据。
# Args: rows - 结果行；x_field - 横轴列；metrics - 参与打分的指标列；
#       source_grain - 原始时间粒度；target_grain - 上卷后时间粒度。
# Returns: {类目: 绝对量级合计}；没有任何非零读数的类目不出现在结果里。
def _category_magnitudes(
    *,
    rows: list[dict[str, Any]],
    x_field: str,
    metrics: list[str],
    source_grain: str,
    target_grain: str,
) -> dict[str, float]:
    """打分只看量级不看分组：保留集合必须与分组视图无关，否则切换后横轴会错位。"""
    magnitudes: dict[str, float] = {}
    for row in rows:
        raw_key = _axis_key(row.get(x_field))
        if not raw_key:
            continue
        total = 0.0
        for metric in metrics:
            value = _as_float(row.get(metric))
            if value is not None:
                total += abs(value)
        if total:
            key = _coarsen_value_to(raw_key, source_grain, target_grain)
            magnitudes[key] = magnitudes.get(key, 0.0) + total
    logger.debug("图表横轴类目量级统计完成", x_field=x_field, category_count=len(magnitudes))
    return magnitudes


# 方法作用：确定性地保留量级最大的前 N 个类目，其余不进入横轴。
# Args: xs - 全量类目（横轴原顺序）；magnitudes - 类目到绝对量级的映射；limit - 保留上限。
# Returns: 保留后的类目列表，保持横轴原顺序（不做量级重排）。
def _keep_top_categories(xs: list[str], magnitudes: dict[str, float], limit: int) -> list[str]:
    """并列时按原始顺序优先：排序键里带上下标，保证相同输入给出相同输出。"""
    ranked = sorted(range(len(xs)), key=lambda index: (-magnitudes.get(xs[index], 0.0), index))
    kept = set(ranked[:limit])
    return [value for index, value in enumerate(xs) if index in kept]


# 方法作用：确定横轴最终类目：时间轴按粒度上卷，分类轴超限时按量级保留前 N。
# Args: rows - 结果行；x_field - 横轴列；metrics - 指标列；labels - 列名到中文标签的映射；
#       warnings - 可选的用户可见说明列表。
# Returns: (最终类目列表, 原时间粒度, 目标时间粒度)。
# Raises: ChartNotRenderable - 分类轴超限且没有任何非零读数、无法可靠确定保留范围时抛出。
def _resolve_axis_categories(
    *,
    rows: list[dict[str, Any]],
    x_field: str,
    metrics: list[str],
    labels: dict[str, str] | None = None,
    warnings: list[str] | None = None,
) -> tuple[list[str], str, str]:
    """时间轴只上卷不裁剪——把趋势按量级重排会得出错误结论。

    散点图的数值轴与热力图的密度矩阵都在调用本函数之前各自返回，不经过这里。
    """
    raw_xs = _x_values(rows, x_field)
    xs, source_grain, target_grain = _rollup_time_axis(raw_xs, x_field)
    if source_grain != "unknown" or len(xs) <= _MAX_VISIBLE_CATEGORIES:
        return xs, source_grain, target_grain
    magnitudes = _category_magnitudes(
        rows=rows,
        x_field=x_field,
        metrics=metrics,
        source_grain=source_grain,
        target_grain=target_grain,
    )
    if sum(magnitudes.values()) <= 0:
        # 全零读数下保留哪几个类目纯属随机：宁可降级为明细表并说明，也不随意截断。
        logger.warning(
            "图表横轴类目过多且没有非零读数，无法确定保留范围",
            x_field=x_field,
            category_count=len(xs),
        )
        raise ChartNotRenderable(f"横轴类目过多（{len(xs)} 个）且所有读数均为零，无法确定保留哪些类目")
    kept = _keep_top_categories(xs, magnitudes, _MAX_VISIBLE_CATEGORIES)
    logger.info("图表横轴类目超限，按量级保留前 N", x_field=x_field, total=len(xs), kept=len(kept))
    # 裁剪绝不能静默：原始类目数、保留数与选择规则都要出现在页面质量说明里。
    _warn(
        warnings,
        f"横轴「{_label(x_field, labels)}」共 {len(xs)} 个类目，超出单图可读上限 "
        f"{_MAX_VISIBLE_CATEGORIES}，已按各系列绝对量级综合保留最大的 {len(kept)} 个"
        f"（其余 {len(xs) - len(kept)} 个未在图中展示，可按明细表查看）",
    )
    return kept, source_grain, target_grain


@dataclass(slots=True)
class _SeriesTrim:
    """一次系列裁剪的事实：原系列数、保留数与发生裁剪的视图名。

    不直接把文案写进 warnings 的原因：一张图有多个候选分组视图时，逐视图各写一条会在
    很短时间内把 `section_warnings[:20]` 挤满，真正的限制（如取数失败）反而被顶掉。
    事实先收集到这里，由 `_disclose_series_trims` 去重后聚合成**一张图一条**说明。
    """

    view: str
    total: int
    kept: int


# 方法作用：把一张图的系列裁剪事实聚合成一条用户可见说明。
# Args: warnings - 披露列表，可为 None；trims - 本次构建收集到的裁剪事实；
#       multi_metric - 本图是否按「指标 × 分组」出系列。
# Returns: 无返回值。
def _disclose_series_trims(
    warnings: list[str] | None, trims: list[_SeriesTrim], *, multi_metric: bool
) -> None:
    """文案必须含原系列数、保留数、未展示数与选择策略，否则用户只会以为「分组就这么少」。"""
    # 判空必须用 `is None`：空的 warnings 列表正是「可写入」的常态，用真假值判会静默丢掉披露。
    if warnings is None or not trims:
        return
    # 同一视图只可能裁剪一次；完全相同的 (视图, 总数, 保留数) 按序去重，保证输出确定。
    unique: list[_SeriesTrim] = []
    seen: set[tuple[str, int, int]] = set()
    for trim in trims:
        key = (trim.view, trim.total, trim.kept)
        if key in seen:
            continue
        seen.add(key)
        unique.append(trim)
    dropped = sum(item.total - item.kept for item in unique)
    # 取法必须与实际实现同源：单指标按各点绝对值合计；多指标按指标轮流、各指标内同规则。
    strategy = (
        "多指标按指标轮流，并在各指标内按各点绝对值合计"
        if multi_metric
        else "单指标按各点绝对值合计"
    )
    detail = "、".join(f"{item.view} {item.total}→{item.kept}" for item in unique)
    _warn(
        warnings,
        f"图表系列数超出单图可读上限 {_MAX_SERIES}：{detail}，"
        f"合计 {sum(item.total for item in unique)} 条系列中保留 "
        f"{sum(item.kept for item in unique)} 条、其余 {dropped} 条未在图中展示"
        f"（选择策略：{strategy}）",
    )
    logger.debug("图表系列裁剪已聚合披露", view_count=len(unique), dropped=dropped)


# 方法作用：按 (x, 分组) 透视出每个分组的一条系列，缺失点补 null。
# Args: rows - 结果行；x_field - x 轴列；group_field - 分组列，空串表示单指标；
#       metric - 指标列；labels - 列名到中文标签的映射；series_type - ECharts 系列类型；
#       name_prefix - 系列名前缀（多指标时放指标中文名）；limit - 本条指标的系列数上限，
#       None 表示不限（由调用方在指标之间统一切）；
#       axis - 调用方已解析好的横轴（类目, 原粒度, 目标粒度），缺省时现场解析；
#       trims - 可选的裁剪事实收集容器，发生截断时追加一条，供调用方聚合成用户可见说明；
#       view - 本视图在用户文案里的名字（默认分组 / 候选分组「X」）。
# Returns: ECharts series 列表，按各点绝对值合计降序、最多 limit 条。
def _pivot_series(
    *,
    rows: list[dict[str, Any]],
    x_field: str,
    group_field: str,
    metric: str,
    labels: dict[str, str] | None,
    series_type: str = "line",
    name_prefix: str = "",
    limit: int | None = _MAX_SERIES,
    axis: tuple[list[str], str, str] | None = None,
    trims: list[_SeriesTrim] | None = None,
    view: str = "默认分组",
) -> list[dict[str, Any]]:
    if axis is None:
        axis = _resolve_axis_categories(
            rows=rows,
            x_field=x_field,
            metrics=[metric] if metric else [],
            labels=labels,
        )
    xs, source_grain, target_grain = axis
    buckets: dict[str, dict[str, list[float]]] = {}
    order: list[str] = []
    for row in rows:
        group_value = row.get(group_field) if group_field else metric
        # 空分组值（明细里的汇总行或非当前分组维度的记录）不构成一条系列：
        # 让它成系列会画出「门店名称：None」这种既没信息又占图例的条目。
        if group_field and group_value in (None, ""):
            continue
        group = str(group_value) if group_field else metric
        value = _as_float(row.get(metric))
        if value is None:
            continue
        if group not in buckets:
            buckets[group] = {}
            order.append(group)
        # 时间轴可能已上卷，分桶键必须跟着用同一个粒度，否则点会落不到轴上。
        raw_key = _axis_key(row.get(x_field))
        key = _coarsen_value_to(raw_key, source_grain, target_grain)
        buckets[group].setdefault(key, []).append(value)
    built: list[dict[str, Any]] = []
    for group in order:
        points = [_aggregate(buckets[group].get(x, []), metric) for x in xs]
        # 量级必须是**各点绝对值合计**，不能先求和再取绝对值：正负读数会互相抵消，
        # 一条 +100/-100 的剧烈波动系列会被算成 0，输给平稳的 +2/+2 而被裁掉。
        magnitude = sum(abs(value) for value in points if value is not None)
        # 带指标前缀时系列名必须同时含指标名与分组值，否则多指标图的图例无法分辨。
        if group_field and name_prefix:
            name = f"{name_prefix} - {_label(group_field, labels)}：{group}"
        elif group_field:
            name = f"{_label(group_field, labels)}：{group}"
        else:
            name = name_prefix or _label(metric, labels)
        built.append({"name": name, "type": series_type, "data": points, "_magnitude": magnitude})
    # 系列过多时按各点绝对值合计取前 N：图例可读，且被省略的都是可解释的小量级分组。
    # list.sort 稳定，并列时保留原始分组出现顺序；reverse 不会打乱并列项的相对次序。
    built.sort(key=lambda item: item.get("_magnitude") or 0.0, reverse=True)
    for item in built:
        item.pop("_magnitude", None)
    if limit is not None and len(built) > limit:
        # 逐视图的诊断走 DEBUG：一张图有多个候选分组时，逐条 INFO 会把日志刷成噪声。
        # 用户可见的事实由调用方经 trims 聚合成一条披露。
        logger.debug(
            "图表系列数超限，按各点绝对值合计取前 N",
            total=len(built),
            kept=limit,
            group_field=group_field,
        )
        if trims is not None:
            trims.append(_SeriesTrim(view=view, total=len(built), kept=limit))
        built = built[:limit]
    logger.debug("图表透视完成", x_count=len(xs), series_count=len(built), group_field=group_field)
    return built


# 方法作用：在系列上限内按指标轮流取系列，保证每个指标都被画到。
# Args: per_metric - 每个指标的系列列表（各自已按量级降序）；limit - 系列总数上限。
# Returns: 交错后的系列列表。
def _interleave_series(per_metric: list[list[dict[str, Any]]], limit: int) -> list[dict[str, Any]]:
    picked: list[dict[str, Any]] = []
    index = 0
    while len(picked) < limit:
        advanced = False
        for series in per_metric:
            if index >= len(series) or len(picked) >= limit:
                continue
            picked.append(series[index])
            advanced = True
        if not advanced:
            break
        index += 1
    return picked


# 方法作用：按 (指标 × 分组) 组合生成系列，单指标时等价于原分组透视。
# Args: rows - 结果行；x_field - x 轴列；group_field - 分组列；
#       metrics - 指标列列表；labels - 标签映射；series_type - ECharts 系列类型；
#       axis - 调用方已解析好的横轴，缺省时现场解析；
#       trims - 可选的裁剪事实收集容器，供调用方聚合成用户可见说明；
#       view - 本视图在用户文案里的名字（默认分组 / 候选分组「X」）。
# Returns: ECharts series 列表，总条数不超过 _MAX_SERIES。
def _pivot_group_series(
    *,
    rows: list[dict[str, Any]],
    x_field: str,
    group_field: str,
    metrics: list[str],
    labels: dict[str, str] | None,
    series_type: str = "line",
    axis: tuple[list[str], str, str] | None = None,
    trims: list[_SeriesTrim] | None = None,
    view: str = "默认分组",
) -> list[dict[str, Any]]:
    """多个指标时按「指标 × 分组」出系列，而不是只用 metrics[0]。

    实机取证：ChartSpec 的 series_fields 声明了多个指标，图上有分组维度时却只画了
    第一个指标——用户以为"另一个指标没有数据"，实际上它只是被静默丢掉了。
    """
    if len(metrics) <= 1:
        return _pivot_series(
            rows=rows,
            x_field=x_field,
            group_field=group_field,
            metric=metrics[0] if metrics else "",
            labels=labels,
            series_type=series_type,
            axis=axis,
            trims=trims,
            view=view,
        )
    per_metric = [
        _pivot_series(
            rows=rows,
            x_field=x_field,
            group_field=group_field,
            metric=metric,
            labels=labels,
            series_type=series_type,
            name_prefix=_label(metric, labels),
            # 单条指标内先不截断：截断要放到指标之间统一做，否则第一个指标会吃满配额。
            limit=None,
            axis=axis,
        )
        for metric in metrics
    ]
    total = sum(len(item) for item in per_metric)
    built = _interleave_series(per_metric, _MAX_SERIES)
    if total > len(built):
        # 同上：逐视图诊断降为 DEBUG，用户事实交给调用方聚合披露。
        logger.debug("图表系列数超限，按指标轮流取前 N", total=total, kept=len(built))
        if trims is not None:
            trims.append(_SeriesTrim(view=view, total=total, kept=len(built)))
    logger.debug("图表多指标透视完成", metric_count=len(metrics), series_count=len(built), group_field=group_field)
    return built


# 方法作用：无分组维度时，把多个指标各画成一条系列。
# Args: rows - 结果行；x_field - x 轴列；metrics - 指标列列表；labels - 标签映射；
#       series_type - ECharts 系列类型；axis - 调用方已解析好的横轴，缺省时现场解析。
# Returns: ECharts series 列表。
def _pivot_metric_series(
    *,
    rows: list[dict[str, Any]],
    x_field: str,
    metrics: list[str],
    labels: dict[str, str] | None,
    series_type: str = "line",
    axis: tuple[list[str], str, str] | None = None,
) -> list[dict[str, Any]]:
    if axis is None:
        axis = _resolve_axis_categories(rows=rows, x_field=x_field, metrics=metrics, labels=labels)
    xs, source_grain, target_grain = axis
    buckets: dict[str, dict[str, list[float]]] = {metric: {} for metric in metrics}
    for row in rows:
        raw_key = _axis_key(row.get(x_field))
        key = _coarsen_value_to(raw_key, source_grain, target_grain)
        for metric in metrics:
            value = _as_float(row.get(metric))
            if value is not None:
                buckets[metric].setdefault(key, []).append(value)
    return [
        {
            "name": _label(metric, labels),
            "type": series_type,
            "data": [_aggregate(buckets[metric].get(x, []), metric) for x in xs],
        }
        for metric in metrics
    ]


# 同比/环比的切换键：与真实列名不可能冲突，供前端当作切换项使用。
_YOY_KEY = "__yoy__"
_MOM_KEY = "__mom__"
# 月度序列至少要有这么多点才可能算出一整年的同比。
_YOY_MIN_POINTS = 13
# 这两个视图是代码算出来的派生视图，不是数据里的真实列，页面按此显示中文名。
COMPARISON_GROUP_LABELS: dict[str, str] = {_YOY_KEY: "同比(%)", _MOM_KEY: "环比(%)"}


# 方法作用：用代码计算同比/环比，避免让模型做算术。
# Args: series - 原始系列列表；periods - 一个周期含多少个点（月度=12，季度=4）。
# Returns: 同比系列列表；无可比基期时对应点补 None。
def _comparison_series(series: list[dict[str, Any]], *, periods: int) -> list[dict[str, Any]]:
    """百分比由 (本期-基期)/|基期| 得出，基期为零时留空而不是编一个数。"""
    result: list[dict[str, Any]] = []
    for item in series:
        data = list(item.get("data") or [])
        values: list[float | None] = [None] * len(data)
        for index in range(periods, len(data)):
            base, current = data[index - periods], data[index]
            if base in (None, 0) or current is None:
                continue
            values[index] = round((current - base) / abs(base) * 100, 2)
        result.append({"name": item.get("name"), "type": item.get("type", "line"), "data": values})
    return result


# 各时间粒度下"同比"需要回看多少个点：年粒度与上年比即 1 个点。
# 日/时/分不提供同比——回看 365/24/60 个点得到的不是业务意义上的同比。
_YOY_PERIODS: dict[str, int] = {"year": 1, "quarter": 4, "month": 12}


# 方法作用：读取某时间粒度算同比需要回看的点数。
# Args: grain - 时间粒度。
# Returns: 回看点位数；不支持同比的粒度返回 0。
def _yoy_periods(grain: str) -> int:
    return _YOY_PERIODS.get(str(grain or ""), 0)


# 分组维度的基数上限：超过它就不可能画成可读的系列（图例挤爆、每系列只剩零星点）。
_MAX_GROUP_CARDINALITY = 20

# 已经是同比/环比的列名 token：对这类指标再算一次比较就是「二阶比较」。
_COMPARISON_TOKENS = frozenset({"mom", "yoy"})
# 中文标签里的比较写法，同样按整体出现判定。
_COMPARISON_LABEL_HINTS = ("同比", "环比")


# 方法作用：判断列名本身是否已经是同比/环比指标。
# Args: column - 列名或中文标签。
# Returns: 是同比/环比指标时返回 True。
def _is_comparison_metric(column: str) -> bool:
    """必须按 token 边界判定：`net_sales_mom_pct` 命中，`momentum_score` 不命中。

    实机取证（分享 token 2gwR2fiF… 的 `chart_sales_foundation_4`）：该图声明的
    `series_fields=[net_sales_yoy_pct, net_sales_mom_pct]`，旧逻辑又在其上生成「环比(%)」，
    得到的是「同比的环比」——业务上不存在这种读数。
    """
    name = str(column or "").strip().lower()
    if not name:
        return False
    if any(hint in name for hint in _COMPARISON_LABEL_HINTS):
        return True
    # 拉丁列名按非字母数字切词后逐个比对整体 token，避免子串包含造成的误判。
    return any(token in _COMPARISON_TOKENS for token in re.split(r"[^a-z0-9]+", name))


# 方法作用：在基数过大的分组维度之间做降级选择。
# Args: rows - 结果行；x_field - 最终横轴列；group_field - 首选分组列；candidates - 其余候选维度。
# Returns: 可读的分组列；都不可读时返回空串（退化为不分组）。
def _pick_readable_group(
    *,
    rows: list[dict[str, Any]],
    x_field: str,
    group_field: str,
    candidates: list[str],
) -> str:
    """基数过大就换一个候选维度，都大就不分组。

    实机案例：按 120 个门店分组、横轴 21 年，每系列只有一两个非空点，整张图近乎空白。
    不分组（把该指标按横轴总体聚合）至少能给出可读的趋势。
    """
    def _cardinality(field: str) -> int:
        return len({row.get(field) for row in rows if row.get(field) not in (None, "")})

    if not group_field:
        return group_field
    cardinality = _cardinality(group_field)
    if 0 < cardinality <= _MAX_GROUP_CARDINALITY:
        return group_field
    # 首选维度太大：在候选里挑基数最小的那个（可读性优先于模型的选择）。
    readable = [
        name for name in candidates
        if name != x_field and 0 < _cardinality(name) <= _MAX_GROUP_CARDINALITY
    ]
    if readable:
        chosen = min(readable, key=_cardinality)
        logger.info(
            "分组维度过大，改用低基数候选",
            original=group_field,
            original_cardinality=cardinality,
            chosen=chosen,
            chosen_cardinality=_cardinality(chosen),
        )
        return chosen
    logger.info(
        "分组维度均过大，退化为不分组",
        original=group_field,
        cardinality=cardinality,
    )
    return ""


# 方法作用：按可读性决定最终的横轴列与分组列。
# Args: rows - 结果行；x_field - 模型给的横轴列；group_field - 模型给的分组列。
# Returns: (实际使用的横轴列, 实际使用的分组列)。
def _effective_axes_after_swap(
    *,
    rows: list[dict[str, Any]],
    x_field: str,
    group_field: str,
) -> tuple[str, str]:
    """x 轴类目过多且分组维度是时间列时交换两者。

    实机出现 488 个门店名挤在一根横轴上：人眼读"按年月看各门店"远比"按门店看各年月"清楚。
    """
    if not group_field:
        return x_field, group_field
    xs = _x_values(rows, x_field)
    if (
        len(xs) > _MAX_X_CATEGORIES
        and _time_grain(xs) == "unknown"
        and _time_grain(_x_values(rows, group_field)) != "unknown"
    ):
        logger.info("图表 x 轴与分组维度交换", old_x=x_field, new_x=group_field)
        return group_field, x_field
    return x_field, group_field


# 方法作用：为一张图算出默认分组与全部可切换分组的系列数据。
# Args: spec - 图表方案；rows - 结果行；labels - 列名到中文标签的映射；
#       warnings - 可选的用户可见说明列表，缺指标等降级原因会追加进去；
#       profile - 可复用的列画像；axis - 已解析好的横轴（与 build_option_for_spec 共用）。
# Returns: (默认分组名, 默认系列, {分组名: 系列列表})。
# Raises: ChartNotRenderable - 声明的指标列一个都不可用时抛出。
def build_group_options(
    *,
    spec: ChartSpec,
    rows: list[dict[str, Any]],
    labels: dict[str, str] | None = None,
    warnings: list[str] | None = None,
    profile: ColumnProfile | None = None,
    axis: tuple[list[str], str, str] | None = None,
) -> tuple[str, list[dict[str, Any]], dict[str, list[dict[str, Any]]]]:
    """切换分组时前端只替换 series，不重新请求后端，也不会出现"点了标签数据清空"。"""
    x_field, metrics, candidates, missing = _usable_columns(
        spec=spec, rows=rows, profile=profile, warnings=warnings
    )
    if missing:
        _warn(
            warnings,
            "图表缺少可用指标列："
            f"{_describe_unavailable_columns(missing, rows=rows, profile=profile)}，已按存在的指标绘制",
        )
    series_type = str(spec.chart_type or "line")
    _, dimensions = _split_columns(rows, profile=profile)
    group_field = str(spec.group_field or "")
    # 全空的分组列不能当默认分组：分组时每一行都会被跳过，结果是一张空图。
    if (
        group_field not in dimensions
        or group_field == x_field
        or not _column_has_values(rows, group_field, profile=profile)
    ):
        group_field = candidates[0] if candidates else ""
    x_field, group_field = _effective_axes_after_swap(rows=rows, x_field=x_field, group_field=group_field)
    # 分组维度基数过大时必须换一个：实机出现按 120 个门店分组，每个门店在 21 年里
    # 只有一两个点，图上几乎全是空白——那不是"数据少"，是维度选得不可读。
    group_field = _pick_readable_group(rows=rows, x_field=x_field, group_field=group_field, candidates=candidates)
    # 横轴必须在 x/分组定稿后解析一次，并贯穿默认视图与全部切换视图：
    # 各视图各算一套类目，切换后就会出现 data 长度错位。
    if axis is None:
        axis = _resolve_axis_categories(
            rows=rows, x_field=x_field, metrics=metrics, labels=labels, warnings=warnings
        )
    xs = axis[0]
    target_grain = axis[2]
    # 指标多于一个且没有分组维度时，一张图同时看多个指标（多系列 = 多指标）。
    if not group_field and len(metrics) > 1:
        series = _pivot_metric_series(
            rows=rows,
            x_field=x_field,
            metrics=metrics,
            labels=labels,
            series_type=series_type,
            axis=axis,
        )
        # 无分组时系列数就等于指标数，而 series_fields 的契约上限是 10——这条路径
        # 原先完全不受 _MAX_SERIES 约束，10 条系列会把图例挤爆。按指标声明顺序截断
        # （顺序稳定，同一份输入永远给出同一批系列），并把省略事实写给用户看。
        if len(series) > _MAX_SERIES:
            logger.info("图表系列数超限，按指标顺序取前 N", total=len(series), kept=_MAX_SERIES)
            _warn(
                warnings,
                f"图表声明了 {len(series)} 个指标，超出单图可读上限 {_MAX_SERIES}，"
                f"已只绘制前 {_MAX_SERIES} 个（其余 {len(series) - _MAX_SERIES} 个未在图中展示）",
            )
            series = series[:_MAX_SERIES]
        return "", series, {"": series}
    options: dict[str, list[dict[str, Any]]] = {}
    # 同一张图的多次系列裁剪先收集事实，收尾时聚合成一条说明：逐视图各写一条会
    # 在多个候选分组时把 section_warnings[:20] 挤满，真正的限制反而被顶掉。
    trims: list[_SeriesTrim] = []
    default_series = _pivot_group_series(
        rows=rows,
        x_field=x_field,
        group_field=group_field,
        metrics=metrics,
        labels=labels,
        series_type=series_type,
        axis=axis,
        trims=trims,
        view=f"默认分组「{_label(group_field, labels)}」" if group_field else "汇总视图",
    )
    options[group_field] = default_series
    for candidate in candidates:
        if candidate == group_field:
            continue
        candidate_series = _pivot_group_series(
            rows=rows,
            x_field=x_field,
            group_field=candidate,
            metrics=metrics,
            labels=labels,
            series_type=series_type,
            axis=axis,
            trims=trims,
            view=f"候选分组「{_label(candidate, labels)}」",
        )
        # 该维度切过去没有任何可画的系列：不生成切换项，避免"点了标签数据清空"。
        if not candidate_series:
            logger.info("图表候选分组无可绘制系列，已略过该切换项", candidate=candidate)
            continue
        options[candidate] = candidate_series
    _disclose_series_trims(warnings, trims, multi_metric=len(metrics) > 1)
    if not group_field:
        options.setdefault("", default_series)
    # 时间轴且点数足够时，附加同比/环比视图：这两个指标一律由代码算，
    # 不让模型做算术（模型算同比幻觉率很高，且基期取错无人察觉）。
    # **但本图指标本身已是同比/环比时一律不附加**：那是「同比的环比」这种二阶比较，
    # 业务上不存在。逐指标元数据在当前契约里不可靠，因此只要声明或实际绘制的指标里
    # 有一个是比较指标，整张图都不做自动比较——宁可少一个派生视图，也不生成错误读数。
    # 字段名与实际绘制的指标都要判一次，且**字段名与展示标签各判一次**：
    # 真实 ChartSpec 的字段名多是数据库英文列名（`growth_rate`），比较语义只落在
    # `labels` 映射的中文标签（「净销售额同比」）里。只看字段名会让这类图照旧生成
    # 「同比的环比」。标签一律经 `_label` 取，缺映射时退化为列名兜底翻译，不新增契约。
    declared_metrics = [str(name) for name in (spec.series_fields or []) if name]
    comparison_metric = next(
        (
            name
            for name in [*declared_metrics, *metrics]
            if _is_comparison_metric(name) or _is_comparison_metric(_label(name, labels))
        ),
        "",
    )
    if comparison_metric:
        logger.debug("图表指标已是同比环比，不附加自动比较视图", metric=comparison_metric, grain=target_grain)
    yoy_periods = _yoy_periods(target_grain)
    if yoy_periods and len(xs) > yoy_periods and not comparison_metric:
        options[_YOY_KEY] = _comparison_series(default_series, periods=yoy_periods)
    # 环比只在有上一期时才成立；年粒度下与同比等价，不再重复出一个同名视图。
    # **分类轴（target_grain == unknown）同样不得生成**：那里的读数会变成「相邻类别
    # 之间的变化率」，业务语义完全错误。实机取证（分享 token 2gwR2fiF…，version 3）：
    # region_code / store_id / rfm_segment / aging_bucket / experiment_id 等 13 张
    # 非时间图都带着「环比(%)」切换项。同比已由 _yoy_periods 对 unknown 返回 0 拦住。
    if len(xs) > 1 and target_grain not in ("unknown", "year") and not comparison_metric:
        options[_MOM_KEY] = _comparison_series(default_series, periods=1)
    if _YOY_KEY in options or _MOM_KEY in options:
        logger.info(
            "图表附加同比环比视图",
            grain=target_grain,
            point_count=len(xs),
            has_yoy=_YOY_KEY in options,
            has_mom=_MOM_KEY in options,
        )
    logger.info(
        "图表分组方案构建完成",
        x_field=x_field,
        group_field=group_field,
        group_count=len(options),
        series_count=len(default_series),
    )
    return group_field, default_series, options


# 方法作用：把一张图表的方案渲染成 ECharts option。
# Args: spec - 图表方案；rows - 已脱敏结果行；labels - 列名到中文标签的映射；
#       warnings - 可选的用户可见说明列表，缺指标/全空系列/横轴回退等降级原因会追加进去；
#       profile - 可复用的列画像，缺省时现场扫描全量结果；
#       group_view - 可选的出参容器：line/bar 分支会把本次已算好的分组透视结果写进去，
#       供调用方复用，避免对同一份输入再算一遍（**不会**污染返回的 option）。
# Returns: ECharts option；散点/热力图走各自的构型分支，其余走透视后的多系列。
# Raises: ChartNotRenderable - 横轴无类目或所有系列都为空时抛出，调用方应降级为明细表。
def build_option_for_spec(
    spec: ChartSpec,
    rows: list[dict[str, Any]],
    labels: dict[str, str] | None = None,
    warnings: list[str] | None = None,
    profile: ColumnProfile | None = None,
    *,
    group_view: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """透视由代码做，模型只给语义；散点与热力图的取值列也必须按指标列取。"""
    chart_type = str(spec.chart_type or "bar")
    # 横轴决策必须与分组视图共用同一处逻辑：只在一侧交换 x/分组，会让"轴"和"系列"
    # 各自按不同列计算，图上直接错位（自测时抓到过这个）。
    x_field, metrics, candidates, missing = _usable_columns(
        spec=spec, rows=rows, profile=profile, warnings=warnings
    )
    if missing:
        _warn(
            warnings,
            "图表缺少可用指标列："
            f"{_describe_unavailable_columns(missing, rows=rows, profile=profile)}，已按存在的指标绘制",
        )
    metric = metrics[0] if metrics else ""
    numeric, dimensions = _split_columns(rows, profile=profile)
    if chart_type == "scatter":
        # 散点图两条轴都是数值轴：既不走类目回退，也不受类目可读上限裁剪——
        # 按类目截断会让点集凭空消失，而那正是"量级分布"这张图要讲的事。
        y_field = metric or (numeric[0] if numeric else "")
        points = [
            [value, _as_float(row.get(y_field))]
            for row in rows
            if (value := _as_float(row.get(x_field))) is not None and _as_float(row.get(y_field)) is not None
        ]
        option = {
            "tooltip": {"trigger": "item"},
            "xAxis": {"type": "value", "name": _label(x_field, labels)},
            "yAxis": {"type": "value", "name": _label(y_field, labels)},
            "series": [{"name": _label(metric, labels), "type": "scatter", "data": points}],
        }
        logger.info("图表配置构建完成", chart_type=chart_type, series_count=1, point_count=len(points))
        return option
    declared_group = str(spec.group_field or "")
    # 全空的分组列不得当默认分组：分组时每一行都会被跳过，结果是一张空图。
    if (
        declared_group not in dimensions
        or declared_group == x_field
        or not _column_has_values(rows, declared_group, profile=profile)
    ):
        declared_group = candidates[0] if candidates else ""
    x_field, declared_group = _effective_axes_after_swap(rows=rows, x_field=x_field, group_field=declared_group)
    declared_group = _pick_readable_group(
        rows=rows, x_field=x_field, group_field=declared_group, candidates=candidates
    )
    if chart_type == "heatmap":
        return _build_heatmap_option(
            spec=spec, rows=rows, labels=labels, x_field=x_field, metric=metric, profile=profile
        )
    # 类目解析要在 x/分组定稿后做一次，并传给分组视图：两边各算一套会让轴与系列错位。
    axis = _resolve_axis_categories(
        rows=rows, x_field=x_field, metrics=metrics, labels=labels, warnings=warnings
    )
    xs = axis[0]
    if not xs:
        # 横轴一个有效类目都没有：画出来只是一个空框，必须让调用方降级并披露原因。
        logger.warning("图表横轴没有可用类目，图表不可渲染", x_field=x_field, declared_x=spec.x_field)
        raise ChartNotRenderable("横轴列没有任何非空取值，图表没有可用类目")
    if chart_type == "pie":
        series = _pivot_series(
            rows=rows, x_field=x_field, group_field="", metric=metric, labels=labels, axis=axis
        )
        data = [
            {"name": x, "value": value}
            for x, value in zip(xs, (series[0]["data"] if series else []), strict=False)
            if value is not None
        ]
        option = {
            "tooltip": {"trigger": "item"},
            "legend": {"type": "scroll"},
            "series": [{"name": _label(metric, labels), "type": "pie", "data": data}],
        }
        logger.info("图表配置构建完成", chart_type=chart_type, slice_count=len(data))
        return option
    group_field, default_series, group_options = build_group_options(
        spec=spec, rows=rows, labels=labels, warnings=warnings, profile=profile, axis=axis
    )
    if group_view is not None:
        # 分组透视在实机规模下是「按全量行 × 候选分组数」的重活：页面构建随后还要同一份
        # 结果去下发切换视图，这里就地透出，调用方不必对相同输入再算一遍。
        group_view.update({
            "group_field": group_field,
            "default_series": default_series,
            "group_options": group_options,
        })
    # 全空系列不渲染：一条没有任何读数的系列只占图例，还会让用户以为"这个维度没数据"。
    drawn = [
        item
        for item in default_series
        if any(value is not None for value in list(item.get("data") or []))
    ]
    if len(drawn) != len(default_series):
        _warn(warnings, f"图表有 {len(default_series) - len(drawn)} 条系列全部为空，已略去")
    if not drawn:
        logger.warning("图表所有系列都没有可绘制数据", chart_type=chart_type, x_field=x_field)
        raise ChartNotRenderable("图表的所有系列都没有可绘制的数据")
    for item in drawn:
        item["type"] = chart_type
    option = {
        # 柱状图按数据项触发：单柱悬停只看该系列该维度。按轴触发会把同一横轴上
        # 全部系列一起弹出来（实机取证：悬停一根柱子弹出 8 条系列的读数）。
        # 折线图的读法本来就是"看某一期的全部系列"，保持按轴触发。
        "tooltip": {"trigger": "axis" if chart_type == "line" else "item"},
        "legend": {"type": "scroll"},
        "xAxis": {"type": "category", "data": xs, "axisLabel": {"hideOverlap": True, "rotate": 0}},
        "yAxis": {"type": "value"},
        "series": drawn,
    }
    logger.info("图表配置构建完成", chart_type=chart_type, series_count=len(drawn), x_count=len(xs))
    return option


# 方法作用：把热力图方案渲染成二维交叉的 ECharts option。
# Args: spec - 图表方案；rows - 结果行；labels - 标签映射；x_field - x 轴列；metric - 指标列；
#       profile - 可复用的列画像。
# Returns: ECharts option。
def _build_heatmap_option(
    *,
    spec: ChartSpec,
    rows: list[dict[str, Any]],
    labels: dict[str, str] | None,
    x_field: str,
    metric: str,
    profile: ColumnProfile | None = None,
) -> dict[str, Any]:
    """值必须取指标列本身：曾经误取 numeric[-1]，把"排名"画成了折扣深度。

    热力图不走 `_resolve_axis_categories` 的类目裁剪：它是密度矩阵，可读性由
    `label.show` 的格子数阈值保证；截断轴会让格子索引与分桶键对不上。
    """
    _, dimensions = _split_columns(rows, profile=profile)
    y_candidates = [name for name in spec.switchable_fields if name and name in dimensions and name != x_field]
    y_field = str(spec.group_field or "") or (y_candidates[0] if y_candidates else "")
    xs = _x_values(rows, x_field)
    ys = _x_values(rows, y_field) if y_field else []
    x_index = {value: index for index, value in enumerate(xs)}
    y_index = {value: index for index, value in enumerate(ys)}
    buckets: dict[tuple[str, str], list[float]] = {}
    for row in rows:
        value = _as_float(row.get(metric))
        if value is None:
            continue
        key = (_axis_key(row.get(x_field)), _axis_key(row.get(y_field)))
        buckets.setdefault(key, []).append(value)
    data = [
        [x_index[x], y_index[y], _aggregate(values, metric)]
        for (x, y), values in buckets.items()
        if x in x_index and y in y_index
    ]
    values_only = [item[2] for item in data if item[2] is not None]
    option = {
        "tooltip": {"position": "top"},
        "xAxis": {"type": "category", "data": xs, "splitArea": {"show": True}, "axisLabel": {"hideOverlap": True}},
        "yAxis": {"type": "category", "data": ys, "splitArea": {"show": True}, "axisLabel": {"hideOverlap": True}},
        "visualMap": {
            "min": min(values_only) if values_only else 0,
            "max": max(values_only) if values_only else 1,
            "calculable": True,
            "orient": "horizontal",
            "left": "center",
            "bottom": 0,
        },
        # 格子多时逐格标数字会糊成一片（实机：8 区 × 5 渠道就已经重叠），
        # 超过 24 格只保留颜色映射，数值交给 tooltip。
        "series": [{
            "name": _label(metric, labels),
            "type": "heatmap",
            "data": data,
            "label": {"show": len(data) <= 24},
        }],
    }
    logger.info("图表配置构建完成", chart_type="heatmap", cell_count=len(data), labeled=len(data) <= 24)
    return option


# 方法作用：无模型或模型失败时按列类型确定性推断图表方案。
# Args: question - 单元问题；columns - 列名清单；rows - 结果行；profile - 可复用的列画像。
# Returns: 单张图的图表方案；类型推断复用对话路径的 classify_chart_type。
def fallback_chart_plan(
    *,
    question: str,
    columns: list[str] | None = None,
    rows: list[dict[str, Any]],
    profile: ColumnProfile | None = None,
) -> ReportChartPlan:
    from src.tools.chart_generator import classify_chart_type

    del question
    if not rows:
        logger.info("图表兜底方案完成", chart_type="table", reason="无数据")
        return ReportChartPlan(charts=[ChartSpec(chart_type="table")])
    chart_type = classify_chart_type(rows)
    numeric, dimensions = _split_columns(rows, profile=profile)
    declared = [str(name) for name in (columns or [])]
    x_field = ""
    for name in [*dimensions, *declared]:
        # 时间列优先当横轴，但全空的时间列同样不能用：它会退化成只有一个空刻度。
        if any(hint in name.lower() for hint in _TIME_COLUMN_HINTS) and _column_has_values(
            rows, name, profile=profile
        ):
            x_field = name
            break
    if not x_field:
        x_field = _pick_populated_x(rows=rows, dimensions=dimensions, profile=profile)
    # 分组维度默认取第一个非 x 的有值维度列，让首屏就是"按维度拆分"的可读形态。
    group_candidates = [
        name
        for name in dimensions
        if name != x_field and _column_has_values(rows, name, profile=profile)
    ]
    spec = ChartSpec(
        chart_type=chart_type,
        title="",
        x_field=x_field,
        group_field=group_candidates[0] if group_candidates else "",
        series_fields=numeric[:1],
        switchable_fields=group_candidates[:6],
    )
    logger.info("图表兜底方案完成", chart_type=spec.chart_type, x_field=x_field, group_field=spec.group_field)
    return ReportChartPlan(charts=[spec])


# 方法作用：把数据形态渲染成提示词探针。
# Args: question - 单元问题；columns - 列名清单；rows - 结果行；row_count - 总行数。
# Returns: 探针文本。
def _render_probe(*, question: str, columns: list[str], rows: list[dict[str, Any]], row_count: int) -> str:
    shown = list(columns)[:_PROBE_COLUMN_LIMIT]
    omitted = len(columns) - len(shown)
    lines = [
        f"## 单元问题\n{question}",
        f"## 列清单\n{', '.join(str(name) for name in shown) or '（无）'}",
        f"## 返回行数\n{int(row_count or 0)}",
    ]
    if omitted > 0:
        lines.append(f"（另有 {omitted} 列未展示）")
    sample = list(rows)[:_PROBE_ROW_LIMIT]
    if sample:
        lines.append("## 样例行\n" + "\n".join(str(row) for row in sample))
    text = "\n".join(lines)
    logger.debug("图表探针渲染完成", probe_chars=len(text))
    return text


# 方法作用：请求模型按数据形态给出图表方案。
# Args: question - 单元问题；columns - 列名清单；rows - 结果行；row_count - 总行数。
# Returns: 模型给出的图表方案；模型不可用或调用失败时返回 None。
async def _invoke_chart_plan(
    *,
    question: str,
    columns: list[str],
    rows: list[dict[str, Any]],
    row_count: int,
) -> ReportChartPlan | None:
    from src.llm.client import is_task_llm_available

    if not is_task_llm_available(_PROMPT_TASK):
        logger.info("图表决策模型不可用，走确定性兜底", task=_PROMPT_TASK)
        return None
    from src.llm.invocation import invoke_structured
    from src.llm.prompt_budget import PromptSection

    probe = _render_probe(question=question, columns=columns, rows=rows, row_count=row_count)
    return await invoke_structured(
        _PROMPT_ID,
        [PromptSection("data", f"## 数据形态\n{probe}", priority=100, min_chars=50, max_chars=8000)],
        output_model=ReportChartPlan,
        task=_PROMPT_TASK,
        temperature=0.0,
        metadata={"node": "report_charts"},
    )


# 方法作用：为一个单元决定图表方案，失败时退回确定性推断。
# Args: question - 单元问题；columns - 列名清单；rows - 结果行。
# Returns: 图表方案；本函数永不抛错、永不返回 None。
async def plan_charts(
    *,
    question: str,
    columns: list[str] | None = None,
    rows: list[dict[str, Any]],
) -> ReportChartPlan:
    # 兜底列清单同样要全量：只取前几行的键会让后段才出现的列连探针都进不去，
    # 模型看不见列自然也不会声明它（_render_probe 自带 30 列展示上限）。
    declared = [str(name) for name in (columns or [])] or profile_columns(rows).columns
    try:
        decided = await _invoke_chart_plan(
            question=question,
            columns=declared,
            rows=rows,
            row_count=len(rows),
        )
    except Exception:
        # 决策失败不影响报表产出，退回确定性推断并留堆栈以便与"模型没给"区分。
        logger.error("图表决策失败，走确定性兜底", exc_info=True)
        decided = None
    if decided is not None and decided.charts:
        logger.info("图表决策生效", chart_count=len(decided.charts), chart_type=decided.charts[0].chart_type)
        return decided
    return fallback_chart_plan(question=question, columns=declared, rows=rows)


# 方法作用：为一批单元结果批量决定图表方案。
# Args: units - (unit_id, question, columns, rows) 四元组列表。
# Returns: unit_id 到图表方案的映射；单个单元失败不影响其它单元。
async def plan_unit_charts(
    units: list[tuple[str, str, list[str], list[dict[str, Any]]]],
) -> dict[str, ReportChartPlan]:
    logger.debug("批量图表决策入口", unit_count=len(units))
    plans: dict[str, ReportChartPlan] = {}
    for unit_id, question, columns, rows in units:
        try:
            plans[str(unit_id)] = await plan_charts(question=question, columns=columns, rows=rows)
        except Exception:
            logger.error("单元图表决策异常", unit_id=unit_id, exc_info=True)
    logger.info("批量图表决策完成", unit_count=len(units), planned=len(plans))
    return plans
