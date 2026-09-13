"""报表确定性深挖适配层（覆盖 23.16）。

职责边界：**统计逻辑一律复用 `src/tools/processors.py`，本模块不重写**。
这里只做两件事：

1. 参数推导 —— processors 要求调用方给出 `time_col` / `value_col` / `name_col`，
   报表侧必须从真实数据确定性推导，而不是靠列名瞎猜。
2. 报表级严谨性补强 —— 双判据异常、R² 门槛、样本截断披露、`method` 必填、
   时间跨度不足时不做同比。这些不进 processors 内部，避免影响聊天链路。

纯函数、无 LLM、无 IO。统计数字直接进页面块，不经过模型转述。
"""

from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal, InvalidOperation
from typing import Any, Literal

from pydantic import BaseModel, Field

from src.logging_config import get_logger
from src.reporting.models import AnalysisUnit, UnitResult
from src.reporting.relations import RelationEvidence

logger = get_logger(__name__)

FindingCategory = Literal["period_over_period", "trend", "anomaly", "contribution", "distribution"]

# 率/比/单价类指标按均值聚合，其余按求和。这是启发式规则，必须写进 method 披露。
_RATE_LIKE_HINTS = ("率", "比", "单价", "均值", "平均", "avg", "rate", "ratio", "price", "pct", "percent")
# 各加工的观测点下限：低于此值不给结论，避免用一两个点编故事。
_MIN_OBSERVATIONS: dict[str, int] = {"period_over_period": 3, "trend": 5, "anomaly": 8, "distribution": 10}
# 贡献度对高基维度无意义，超过此基数不产出。
_MAX_CONTRIBUTION_CARDINALITY = 50
# 趋势拟合优度门槛：低于此值只报"不显著"，不给方向性措辞。
_R_SQUARED_THRESHOLD = 0.3
# 异常双判据参数。IQR 系数 1.5 与 MAD 修正 z-score 阈值 3.5 都是通用取值。
_IQR_FACTOR = 1.5
_MAD_Z_THRESHOLD = 3.5
_MAD_SCALE = 1.4826
# 异常点占比超过此值时整体放弃：说明分布本身就这样，不是异常。
_MAX_OUTLIER_RATIO = 0.2
# 同比至少需要回看一个完整年度。
_MIN_YOY_MONTHS = 13

_DATE_FORMATS = ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d", "%Y/%m/%d", "%Y-%m", "%Y/%m", "%Y")


# 方法作用：识别同时要求多项运行证据的说明单元，不改变普通 Schema 或数据查询路由。
# Args: unit - 报表分析单元。
# Returns: 是否应在查询与关系分析结束后汇总本轮证据。
def is_runtime_quality_unit(unit: AnalysisUnit) -> bool:
    """识别报表级覆盖、截断、权限和关系说明。"""
    question = unit.question.strip()
    explaining = question.startswith(("说明", "披露")) or "本次报表" in question or "本报表" in question
    evidence_groups = (("时间覆盖", "覆盖范围"), ("截断",), ("权限过滤", "权限限制"), ("未能建立", "未建立"))
    return explaining and sum(any(term in question for term in group) for group in evidence_groups) >= 2


# 方法作用：从本轮有界样本、执行状态和关系证据生成质量说明，不调用模型或再次查询。
# Args: unit - 说明单元；results - 其他单元结果；relations - 实际关系证据；resolved_units - 授权解析摘要。
# Returns: 可写回页面与运行状态的文字结果。
def summarize_runtime_quality(
    unit: AnalysisUnit,
    results: list[UnitResult],
    relations: list[RelationEvidence],
    resolved_units: list[dict[str, Any]],
) -> UnitResult:
    """只报告已观测证据，严格区分样本范围、全库范围和未知权限影响。"""
    data_results = [result for result in results if result.answer_kind == "data"]
    populated = [result for result in data_results if result.rows]
    failed = sum(result.status == "failed" for result in data_results)
    partial = sum(result.status == "partial" for result in data_results)
    lines = [
        f"本轮收到 {len(data_results)} 个数据分析单元的执行结果，其中 {len(populated)} 个返回样本，失败 {failed} 个，部分完成 {partial} 个。",
        "时间覆盖仅按返回样本计算，不代表数据库完整范围；年月粒度不能证明最近 90 天逐日完整覆盖。",
    ]
    if not populated:
        lines.append("本轮没有可用的查询结果样本，无法确认实际时间覆盖；这不等于没有尝试执行查询。")
    for result in data_results:
        if not result.rows:
            lines.append(f"{result.unit_id}：状态 {result.status}，没有可用样本。")
            continue
        # 多源数据按来源分别披露，不能把不同来源的最早/最晚值拼成完整覆盖。
        source_rows: dict[str, list[dict[str, Any]]] = {}
        for row in result.rows:
            source = str(row.get("_datasource") or result.datasource or "来源未标注")
            source_rows.setdefault(source, []).append(row)
        for source, rows in source_rows.items():
            time_column = detect_time_column(rows, result.columns)
            dated = [(parsed, str(row[time_column])) for row in rows if time_column and (parsed := _parse_date(row.get(time_column))) is not None]
            coverage = f"{time_column} 样本值 {min(dated)[1]} 至 {max(dated)[1]}" if dated else "没有可解析的时间列，无法确认覆盖"
            lines.append(f"{result.unit_id} / {source}：{len(rows)} 行样本；{coverage}。")
    truncated_units = [result.unit_id for result in data_results if result.truncated]
    lines.append(
        "结果样本截断：" + "、".join(truncated_units) + "；被裁掉行的具体内容不能从保留样本恢复。"
        if truncated_units else "结果样本未收到截断标记；这不等于已确认全量返回。"
    )
    lines.append("Schema 或 Prompt 文本截断与查询结果行截断是不同证据，不能混用。")
    denied_units = {str(item.get("unit_id")) for item in resolved_units if item.get("authorization_status") == "denied"}
    denied_units.update(result.unit_id for result in data_results if result.error_code == "DATASOURCE_FORBIDDEN")
    if denied_units:
        lines.append(f"权限阻断 {len(denied_units)} 个分析单元，未执行其受限查询。")
    lines.append("权限：仅使用当前身份可见结果；没有被过滤表、列或行的完整清单与数量，不能据此认定没有过滤。")
    verified = [relation for relation in relations if relation.evidence_level == "verified_relation"]
    statistical = [relation for relation in relations if relation.evidence_level in {"statistical_association", "cross_source_statistical"}]
    unresolved = [relation for relation in relations if relation.evidence_level not in {"verified_relation", "statistical_association", "cross_source_statistical"}]
    lines.append(f"关系证据：样本关系校验通过 {len(verified)} 条，仅统计关联 {len(statistical)} 条，未建立或被阻断 {len(unresolved)} 条；不代表 Schema 外键或因果关系。")
    for relation in unresolved[:20]:
        reason = "；".join(relation.limitations[:3]) or "未取得可验证关系证据"
        lines.append(f"{relation.left_unit_id} ↔ {relation.right_unit_id}：{reason}。")
    if len(unresolved) > 20:
        lines.append(f"其余 {len(unresolved) - 20} 条未建立关系未逐项展开。")
    warnings = list(dict.fromkeys(warning for result in results for warning in result.warnings))[:20]
    if warnings:
        lines.append("单元已披露的限制：" + "；".join(warnings))
    return UnitResult(unit_id=unit.unit_id, answer_kind="text", summary="\n\n".join(lines), warnings=warnings)


class FindingItem(BaseModel):
    """单条发现明细。"""

    label: str = Field(..., max_length=200)
    value: float | None = None
    unit: str = Field(default="", max_length=32)
    change_ratio: float | None = None
    baseline: str = Field(default="", max_length=64)
    significance: Literal["high", "moderate", "low", "unknown"] = "unknown"
    caveats: list[str] = Field(default_factory=list, max_length=10)


class UnitFinding(BaseModel):
    """单个分析单元的一条确定性发现。"""

    unit_id: str
    category: FindingCategory
    title: str = Field(..., max_length=200)
    items: list[FindingItem] = Field(default_factory=list, max_length=50)
    # 必填：数字怎么算出来的。这是可解释性与零幻觉的底线。
    method: str = Field(..., max_length=500)
    chart_type: str = Field(default="table", max_length=16)
    limitations: list[str] = Field(default_factory=list, max_length=10)
    truncated: bool = False


# 方法作用：把单个取值解析为日期，支持常见日期与年月写法。
# Args: value - 原始取值。
# Returns: 解析出的日期；无法识别时返回 None。
def _parse_date(value: Any) -> date | None:
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    text = str(value or "").strip()
    if not text:
        return None
    for fmt in _DATE_FORMATS:
        try:
            return datetime.strptime(text, fmt).date()
        except ValueError:
            continue
    return None


# 方法作用：把单个取值转换为数值。
# Args: value - 原始取值。
# Returns: 数值；无法转换或为布尔时返回 None。
def _to_float(value: Any) -> float | None:
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, (int, float, Decimal)):
        try:
            return float(value)
        except (InvalidOperation, ValueError):
            return None
    try:
        return float(str(value).strip())
    except (TypeError, ValueError):
        return None


# 方法作用：识别时间列。优先按列值能否解析为日期判断，其次用既有时间键命名启发。
# Args: rows - 结果行；columns - 授权列名。
# Returns: 时间列名；无法识别时返回 None。
def detect_time_column(rows: list[dict[str, Any]], columns: list[str]) -> str | None:
    if not rows:
        return None
    from src.reporting.relations import _is_time_key

    best: tuple[int, str] | None = None
    for column in columns:
        values = [row.get(column) for row in rows[:50]]
        parsed = sum(1 for value in values if _parse_date(value) is not None)
        # 需要绝大多数取值都能解析成日期，才认作时间列。
        if parsed >= max(2, int(len(values) * 0.8)) and (best is None or parsed > best[0]):
            best = (parsed, column)
    if best is not None:
        return best[1]
    for column in columns:
        if _is_time_key(column):
            logger.info("时间列按命名启发识别", column=column)
            return column
    return None


# 方法作用：识别用于统计的数值列，优先取单元声明的指标列。
# Args: rows - 结果行；columns - 授权列名；metrics - 单元声明的指标列。
# Returns: 数值列名；无法识别时返回 None。
def detect_value_column(rows: list[dict[str, Any]], columns: list[str], metrics: list[str]) -> str | None:
    if not rows:
        return None
    declared = [column for column in metrics if column in columns]
    ordered = declared + [column for column in columns if column not in declared]
    for column in ordered:
        present = [_to_float(row.get(column)) for row in rows if row.get(column) is not None]
        if present and all(value is not None for value in present):
            return column
    return None


# 方法作用：识别贡献度使用的维度列。
# Args: rows - 结果行；columns - 授权列名；exclude - 已用作时间/数值的列。
# Returns: 基数在可解释范围内的维度列名；没有合适列时返回 None。
def detect_dimension_column(rows: list[dict[str, Any]], columns: list[str], exclude: set[str]) -> str | None:
    for column in columns:
        if column in exclude:
            continue
        values = [str(row.get(column, "")) for row in rows if row.get(column) not in (None, "")]
        distinct = {value for value in values if _parse_date(value) is None and _to_float(value) is None}
        if not distinct or len(distinct) > _MAX_CONTRIBUTION_CARDINALITY:
            continue
        return column
    return None


# 方法作用：按指标名判断聚合方式。
# Args: column - 数值列名。
# Returns: mean 或 sum。
def aggregation_for(column: str) -> str:
    lowered = str(column or "").lower()
    return "mean" if any(hint in lowered for hint in _RATE_LIKE_HINTS) else "sum"


# 方法作用：按时间列升序排列行，使环比与趋势按真实时间顺序计算。
# Args: rows - 结果行；time_column - 时间列名。
# Returns: 升序排列后的行；时间列不可解析的行被丢弃。
def sort_by_time(rows: list[dict[str, Any]], time_column: str) -> list[dict[str, Any]]:
    dated = [(_parse_date(row.get(time_column)), row) for row in rows]
    resolvable = [item for item in dated if item[0] is not None]
    resolvable.sort(key=lambda pair: pair[0])
    return [row for _, row in resolvable]


# 方法作用：按时间列归并重复周期，使每个周期只有一个观测值。
# Args: rows - 已按时间排序的行；time_column - 时间列名；value_column - 数值列名；how - sum 或 mean。
# Returns: 每周期一行的聚合结果。
def aggregate_by_period(
    rows: list[dict[str, Any]], time_column: str, value_column: str, how: str
) -> list[dict[str, Any]]:
    # 用原始标签文本作为输出周期名，保留用户看到的写法（如 "2026-02" 而非 "2026-02-01"）。
    grouped: dict[date, tuple[str, list[float]]] = {}
    for row in rows:
        parsed = _parse_date(row.get(time_column))
        value = _to_float(row.get(value_column))
        if parsed is None or value is None:
            continue
        label, values = grouped.setdefault(parsed, (str(row.get(time_column, "")).strip() or parsed.isoformat(), []))
        values.append(value)
    return [
        {
            "period": label,
            value_column: (sum(values) / len(values) if how == "mean" else sum(values)),
        }
        for _, (label, values) in sorted(grouped.items())
    ]


# 方法作用：计算 IQR 上界与 MAD 修正 z-score 上界，作为异常的双判据。
# Args: values - 数值序列。
# Returns: (IQR 上界, MAD 上界)；任一无法计算时为 None。
def _outlier_bounds(values: list[float]) -> tuple[float | None, float | None]:
    if len(values) < 4:
        return None, None
    ordered = sorted(values)
    q1 = ordered[int(len(ordered) * 0.25)]
    q3 = ordered[min(int(len(ordered) * 0.75), len(ordered) - 1)]
    iqr = q3 - q1
    iqr_bound = q3 + _IQR_FACTOR * iqr if iqr > 0 else None
    median = ordered[len(ordered) // 2]
    deviations = sorted(abs(value - median) for value in values)
    mad = deviations[len(deviations) // 2]
    mad_bound = median + _MAD_Z_THRESHOLD * _MAD_SCALE * mad if mad > 0 else None
    return iqr_bound, mad_bound


# 方法作用：计算数值序列的线性趋势斜率与拟合优度 R²。
# Args: values - 按时间排序的数值序列。
# Returns: (斜率, R²)；观测点不足或方差为零时返回 (None, None)。
def _trend_fit(values: list[float]) -> tuple[float | None, float | None]:
    if len(values) < 3:
        return None, None
    try:
        import numpy as np

        xs = np.arange(len(values), dtype=float)
        ys = np.asarray(values, dtype=float)
        slope, intercept = np.polyfit(xs, ys, 1)
        predicted = slope * xs + intercept
        residual = float(((ys - predicted) ** 2).sum())
        total = float(((ys - ys.mean()) ** 2).sum())
        if total <= 0:
            return float(slope), None
        return float(slope), max(0.0, 1.0 - residual / total)
    except Exception:
        logger.warning("趋势拟合失败", obs=len(values), exc_info=True)
        return None, None


# 方法作用：计算分位数与偏度，刻画数值分布形态。
# Args: values - 数值序列。
# Returns: 含 p25/p50/p75/skew 的字典；计算失败时返回空字典。
def _distribution_shape(values: list[float]) -> dict[str, float]:
    if len(values) < 4:
        return {}
    try:
        import numpy as np

        array = np.asarray(values, dtype=float)
        p25, p50, p75 = (float(item) for item in np.percentile(array, [25, 50, 75]))
        std = float(array.std())
        skew = float(((array - array.mean()) ** 3).mean() / (std**3)) if std > 0 else 0.0
        return {"p25": p25, "p50": p50, "p75": p75, "skew": round(skew, 3)}
    except Exception:
        logger.warning("分布形态计算失败", obs=len(values), exc_info=True)
        return {}


# 方法作用：构建同环比发现，按 processors 的既有出口判断数据是否足够。
# Args: unit_id - 单元标识；rows - 结果行；time_column - 时间列；value_column - 数值列；sample_caveat - 样本披露文本；truncated - 是否截断。
# Returns: 环比与（时间跨度足够时的）同比发现。
def _period_findings(
    unit_id: str,
    rows: list[dict[str, Any]],
    time_column: str | None,
    value_column: str,
    sample_caveat: str,
    truncated: bool,
) -> list[UnitFinding]:
    if not time_column:
        return []
    from src.tools.processors import MoMProcessor, YoYProcessor

    how = aggregation_for(value_column)
    ordered = sort_by_time(rows, time_column)
    aggregated = aggregate_by_period(ordered, time_column, value_column, how)
    if len(aggregated) < _MIN_OBSERVATIONS["period_over_period"]:
        return []
    params = {"value_col": value_column, "time_col": "period"}
    findings: list[UnitFinding] = []
    method = f"按{time_column}分组{how == 'mean' and '求均值' or '求和'}后，与上一期对比"
    result = MoMProcessor().process(aggregated, params)
    if result.confidence != "low":
        # 处理器的 data[i] 对应 aggregated[i+1]，上一期标签从上一条聚合行取。
        findings.append(UnitFinding(
            unit_id=unit_id,
            category="period_over_period",
            title="环比变化",
            chart_type=result.chart_type,
            truncated=truncated,
            method=method,
            items=[
                FindingItem(
                    label=str(item.get("period", "")),
                    value=_to_float(item.get("value")),
                    change_ratio=_to_float(item.get("change_pct")),
                    baseline=f"上一期 {aggregated[index].get('period', '')}".strip(),
                    significance="moderate",
                    caveats=[sample_caveat] if sample_caveat else [],
                )
                for index, item in enumerate(result.data[:12])
                if index < len(aggregated)
            ],
        ))
    # 同比至少需要一个完整年度；跨度不足时不做，也不写同比数字。
    span_days = (_parse_date(aggregated[-1]["period"]) - _parse_date(aggregated[0]["period"])).days if len(aggregated) > 1 else 0
    if span_days < _MIN_YOY_MONTHS * 30:
        return findings
    yoy = YoYProcessor().process(aggregated, params)
    if yoy.confidence != "low":
        findings.append(UnitFinding(
            unit_id=unit_id,
            category="period_over_period",
            title="同比变化",
            chart_type=yoy.chart_type,
            truncated=truncated,
            method=f"按{time_column}分组{how == 'mean' and '求均值' or '求和'}后，与去年同期对比",
            items=[
                FindingItem(
                    label=str(item.get("period", "")),
                    value=_to_float(item.get("value")),
                    change_ratio=_to_float(item.get("change_pct")),
                    baseline=f"去年同期 {item.get('prev_period', '')}".strip(),
                    significance="moderate",
                    caveats=[sample_caveat] if sample_caveat else [],
                )
                for item in yoy.data[:12]
            ],
        ))
    return findings


# 方法作用：构建趋势发现，拟合优度不足时只报不显著。
# Args: unit_id - 单元标识；rows - 结果行；value_column - 数值列；sample_caveat - 样本披露文本；truncated - 是否截断。
# Returns: 趋势发现列表。
def _trend_findings(
    unit_id: str, rows: list[dict[str, Any]], value_column: str, sample_caveat: str, truncated: bool
) -> list[UnitFinding]:
    from src.tools.processors import TrendProcessor

    values = [_to_float(row.get(value_column)) for row in rows]
    series = [value for value in values if value is not None]
    if len(series) < _MIN_OBSERVATIONS["trend"]:
        return []
    result = TrendProcessor().process(rows, {"value_col": value_column, "window": 3})
    if result.confidence == "low":
        return []
    slope, r_squared = _trend_fit(series)
    if slope is None:
        return []
    significant = r_squared is not None and r_squared >= _R_SQUARED_THRESHOLD
    limitations = [] if significant else ["拟合优度低于门槛，趋势不显著，不给出方向性结论"]
    return [UnitFinding(
        unit_id=unit_id,
        category="trend",
        title="趋势方向",
        chart_type=result.chart_type,
        truncated=truncated,
        method=f"对{value_column}做一次线性拟合，斜率={round(slope, 4)}，R²={None if r_squared is None else round(r_squared, 3)}；方向判断取自处理器",
        limitations=limitations,
        items=[FindingItem(
            label=str(result.summary),
            value=round(slope, 4),
            baseline="后段均值对比前段均值",
            significance="moderate" if significant else "low",
            caveats=[sample_caveat] if sample_caveat else [],
        )],
    )]


# 方法作用：构建异常发现，IQR 与 MAD 双判据同时越界才算异常。
# Args: unit_id - 单元标识；rows - 结果行；value_column - 数值列；sample_caveat - 样本披露文本；truncated - 是否截断。
# Returns: 异常发现列表。
def _anomaly_findings(
    unit_id: str, rows: list[dict[str, Any]], value_column: str, sample_caveat: str, truncated: bool
) -> list[UnitFinding]:
    from src.tools.processors import AnomalyProcessor

    values = [_to_float(row.get(value_column)) for row in rows]
    series = [value for value in values if value is not None]
    if len(series) < _MIN_OBSERVATIONS["anomaly"]:
        return []
    result = AnomalyProcessor().process(rows, {"value_col": value_column})
    if result.confidence == "low":
        return []
    iqr_bound, mad_bound = _outlier_bounds(series)
    if iqr_bound is None or mad_bound is None:
        return []
    # 双判据：处理器的 z-score 候选还必须同时越过 IQR 上界，避免长尾误报。
    confirmed = [
        item for item in result.data
        if (_to_float(item.get("value")) or 0.0) > iqr_bound
    ]
    if not confirmed or len(confirmed) > len(series) * _MAX_OUTLIER_RATIO:
        return []
    return [UnitFinding(
        unit_id=unit_id,
        category="anomaly",
        title="异常点",
        chart_type=result.chart_type,
        truncated=truncated,
        method=(
            f"对{value_column}先用处理器 z-score 初筛，再要求同时越过 IQR 上界"
            f"（Q3+{_IQR_FACTOR}×IQR={round(iqr_bound, 2)}）与 MAD 上界"
            f"（中位数+{_MAD_Z_THRESHOLD}×{_MAD_SCALE}×MAD={round(mad_bound, 2)}）"
        ),
        items=[
            FindingItem(
                label=f"第 {int(item.get('index', 0)) + 1} 行",
                value=_to_float(item.get("value")),
                baseline=f"z-score={item.get('z_score')}",
                significance="high",
                caveats=[sample_caveat] if sample_caveat else [],
            )
            for item in confirmed[:10]
        ],
    )]


# 方法作用：构建贡献度发现，高基维度不产出。
# Args: unit_id - 单元标识；rows - 结果行；dimension_column - 维度列；value_column - 数值列；sample_caveat - 样本披露文本；truncated - 是否截断。
# Returns: 贡献度发现列表。
def _contribution_findings(
    unit_id: str,
    rows: list[dict[str, Any]],
    dimension_column: str | None,
    value_column: str,
    sample_caveat: str,
    truncated: bool,
) -> list[UnitFinding]:
    if not dimension_column:
        return []
    from src.tools.processors import RankingProcessor

    if len({str(row.get(dimension_column, "")) for row in rows}) > _MAX_CONTRIBUTION_CARDINALITY:
        return []
    result = RankingProcessor().process(rows, {"value_col": value_column, "name_col": dimension_column, "top_n": 8})
    if result.confidence == "low":
        return []
    return [UnitFinding(
        unit_id=unit_id,
        category="contribution",
        title="头部贡献集中度",
        chart_type=result.chart_type,
        truncated=truncated,
        method=f"按{dimension_column}分组对{value_column}求和后降序取前 N，集中度取自处理器",
        items=[
            FindingItem(
                label=str(item.get("name", "")),
                value=_to_float(item.get("value")),
                baseline=f"排名第 {item.get('rank')}",
                significance="moderate",
                caveats=[sample_caveat] if sample_caveat else [],
            )
            for item in result.data
        ],
    )]


# 方法作用：构建分布形态发现，给出分位数与偏度。
# Args: unit_id - 单元标识；rows - 结果行；value_column - 数值列；sample_caveat - 样本披露文本；truncated - 是否截断。
# Returns: 分布发现列表。
def _distribution_findings(
    unit_id: str, rows: list[dict[str, Any]], value_column: str, sample_caveat: str, truncated: bool
) -> list[UnitFinding]:
    from src.tools.processors import DistributionProcessor

    values = [_to_float(row.get(value_column)) for row in rows]
    series = [value for value in values if value is not None]
    if len(series) < _MIN_OBSERVATIONS["distribution"]:
        return []
    result = DistributionProcessor().process(rows, {"value_col": value_column})
    if result.confidence == "low":
        return []
    shape = _distribution_shape(series)
    if not shape:
        return []
    return [UnitFinding(
        unit_id=unit_id,
        category="distribution",
        title="数值分布形态",
        chart_type=result.chart_type,
        truncated=truncated,
        method=f"对{value_column}求分位数与偏度；取值区间与中位数取自处理器（{result.summary}）",
        items=[
            FindingItem(label="P25", value=round(shape["p25"], 2), significance="low", caveats=[sample_caveat] if sample_caveat else []),
            FindingItem(label="中位数 P50", value=round(shape["p50"], 2), significance="low", caveats=[sample_caveat] if sample_caveat else []),
            FindingItem(label="P75", value=round(shape["p75"], 2), significance="low", caveats=[sample_caveat] if sample_caveat else []),
            FindingItem(
                label="偏度",
                value=shape["skew"],
                baseline="正为右偏、负为左偏、0 为对称",
                significance="moderate",
                caveats=[sample_caveat] if sample_caveat else [],
            ),
        ],
    )]


# 方法作用：对单个分析单元做确定性深挖，产出可披露的发现条目。
# Args: unit_id - 单元标识；rows - 结果行；columns - 授权列名；metrics - 单元声明的指标列；truncated - 样本是否被截断。
# Returns: 发现条目列表；数据不足或语义不成立时返回空列表而不是猜测。
def profile_unit(
    *,
    unit_id: str,
    rows: list[dict[str, Any]],
    columns: list[str],
    metrics: list[str],
    truncated: bool = False,
) -> list[UnitFinding]:
    if not rows or not columns:
        return []
    sample_caveat = f"样本被截断：结论仅基于返回的 {len(rows)} 行计算" if truncated else ""
    findings: list[UnitFinding] = []
    time_column = detect_time_column(rows, columns)
    value_column = detect_value_column(rows, columns, metrics)
    if value_column:
        findings.extend(_period_findings(unit_id, rows, time_column, value_column, sample_caveat, truncated))
        findings.extend(_trend_findings(unit_id, rows, value_column, sample_caveat, truncated))
        findings.extend(_anomaly_findings(unit_id, rows, value_column, sample_caveat, truncated))
        findings.extend(_distribution_findings(unit_id, rows, value_column, sample_caveat, truncated))
        excluded = {value_column} | ({time_column} if time_column else set())
        dimension_column = detect_dimension_column(rows, columns, excluded)
        findings.extend(_contribution_findings(unit_id, rows, dimension_column, value_column, sample_caveat, truncated))
    logger.info(
        "报表确定性深挖完成",
        unit_id=unit_id,
        row_count=len(rows),
        time_column=time_column or "",
        value_column=value_column or "",
        finding_count=len(findings),
        truncated=truncated,
    )
    return findings
