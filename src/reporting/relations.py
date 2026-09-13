"""报表分析单元关系证据与受控跨源分析。"""

from __future__ import annotations

import math
import re
from collections import Counter
from datetime import date, datetime
from decimal import Decimal, InvalidOperation
from itertools import combinations
from typing import Any

from pydantic import BaseModel, Field, model_validator

from src.logging_config import get_logger

logger = get_logger(__name__)

# 跨源分布级对比的最小观测点数：低于此值不给相关系数，避免用两三个点编故事。
_MIN_CROSS_SOURCE_OBSERVATIONS = 8


# 方法作用：把单个取值转换为浮点数。
# Args: value - 原始取值。
# Returns: 浮点数；无法转换或为布尔时返回 None。
def _as_float(value: Any) -> float | None:
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


# 方法作用：判断某列取值是否整体可作数值比较。
# Args: rows - 结果行；column - 列名。
# Returns: 存在数值且全部非空取值都能转成数值时返回 True。
def _is_numeric_series(rows: list[dict[str, Any]], column: str) -> bool:
    values = [_as_float(row.get(column)) for row in rows if row.get(column) is not None]
    return bool(values) and all(value is not None for value in values)


# 方法作用：计算两组等长序列的 Spearman 秩相关系数。
# Args: left/right - 等长的数值序列。
# Returns: 相关系数；方差为零或计算失败时返回 None。
def _spearman(left: list[float], right: list[float]) -> float | None:
    if len(left) != len(right) or len(left) < _MIN_CROSS_SOURCE_OBSERVATIONS:
        return None
    try:
        import pandas as pd

        correlation = pd.Series(left, dtype="float64").corr(pd.Series(right, dtype="float64"), method="spearman")
        if correlation is None or pd.isna(correlation):
            return None
        return float(correlation)
    except Exception:
        logger.warning("跨源秩相关计算失败", observations=len(left), exc_info=True)
        return None


class RelationEvidence(BaseModel):
    """两个分析单元之间可审计的关系证据。"""

    left_unit_id: str
    right_unit_id: str
    relation_type: str = "none"
    join_keys: list[str] = Field(default_factory=list, max_length=20)
    match_rate: float | None = Field(default=None, ge=0, le=1)
    left_rows: int = Field(default=0, ge=0)
    right_rows: int = Field(default=0, ge=0)
    cardinality: str = "unknown"
    correlation: float | None = Field(default=None, ge=-1, le=1)
    time_alignment: str = "unknown"
    evidence_level: str = "no_relation"
    limitations: list[str] = Field(default_factory=list, max_length=20)
    estimated_join_rows: int = Field(default=0, ge=0)
    sampled: bool = False


class RelationPolicy(BaseModel):
    """受控关系分析的资源和可信度边界。"""

    max_rows: int = Field(default=1000, ge=1, le=10_000)
    max_columns: int = Field(default=20, ge=1, le=100)
    max_join_rows: int = Field(default=10_000, ge=1, le=100_000)
    min_match_rate: float = Field(default=0.6, ge=0, le=1)
    max_join_keys: int = Field(default=3, ge=1, le=10)


class RelationDataset(BaseModel):
    """关系分析器接收的脱敏、限列和限行数据集。"""

    unit_id: str = Field(..., min_length=1, max_length=64)
    datasource: str = ""
    columns: list[str] = Field(default_factory=list, max_length=100)
    authorized_columns: list[str] = Field(default_factory=list, max_length=100)
    dimensions: list[str] = Field(default_factory=list, max_length=50)
    metrics: list[str] = Field(default_factory=list, max_length=50)
    rows: list[dict[str, Any]] = Field(default_factory=list, max_length=10_000)
    status: str = "success"

    # 方法作用：去重并清理关系分析使用的列名，避免客户端或执行器注入路径表达式。
    # Args: self - 待校验的数据集。
    # Returns: 规范化后的数据集。
    @model_validator(mode="after")
    def normalize(self) -> "RelationDataset":
        self.columns = _normalize_columns(self.columns)
        self.authorized_columns = _normalize_columns(self.authorized_columns)
        self.dimensions = _normalize_columns(self.dimensions)
        self.metrics = _normalize_columns(self.metrics)
        return self


_DATE_LIKE_RE = re.compile(r"^\d{4}[-/]\d{1,2}(?:[-/]\d{1,2})?(?:[T ]\d{1,2}:\d{2}(?::\d{2})?)?$")
# 仅把明确的实体键、时间键或业务维度作为候选，避免把 amount/value 等共同指标误当 Join Key。
_KEY_HINTS = (
    "id", "key", "date", "time", "week", "month", "period",
    "region", "category", "segment", "code", "日期", "时间", "周",
    "月", "季度", "地区", "区域", "品类", "分群", "编码",
)
_SAFE_TEXT_RE = re.compile(r"^[^\x00-\x1f\x7f]{0,500}$")


# 方法作用：清理并去重列名，保证关系证据只引用简单字段名。
# Args: values - 原始列名列表。
# Returns: 有界的规范列名列表。
def _normalize_columns(values: list[str]) -> list[str]:
    result: list[str] = []
    for value in values:
        name = str(value).strip()
        if not name or len(name) > 128 or not _SAFE_TEXT_RE.fullmatch(name):
            continue
        if name not in result:
            result.append(name)
    return result[:100]


# 方法作用：把结果值转换为关系分析允许的有限标量，拒绝嵌套对象和控制字符。
# Args: value - 结果中的原始值。
# Returns: 可比较的标量；不支持的值返回 None。
def _safe_scalar(value: Any) -> Any:
    if value is None or isinstance(value, bool):
        return value
    if isinstance(value, (int, str)):
        if isinstance(value, str) and not _SAFE_TEXT_RE.fullmatch(value):
            return None
        return value[:500] if isinstance(value, str) else value
    if isinstance(value, float):
        return value if math.isfinite(value) else None
    if isinstance(value, (datetime, date)):
        return value.isoformat()
    return None


# 方法作用：从输入字典构造受控数据集，并只保留授权投影列和有限样本。
# Args: value - 分析单元描述或 RelationDataset；policy - 资源边界。
# Returns: 规范化后的 RelationDataset。
def _coerce_dataset(value: dict[str, Any] | RelationDataset, policy: RelationPolicy) -> RelationDataset:
    dataset = value if isinstance(value, RelationDataset) else RelationDataset.model_validate(value)
    # authorized_columns 是安全边界；未显式提供时才回退到调用方声明的 columns。
    declared = dataset.authorized_columns or dataset.columns
    if not declared:
        declared = _normalize_columns(list(dataset.rows[0].keys()) if dataset.rows else [])
    columns = [column for column in _normalize_columns(declared) if column in set(declared)][:policy.max_columns]
    projected: list[dict[str, Any]] = []
    for raw in dataset.rows[:policy.max_rows]:
        if not isinstance(raw, dict):
            continue
        projected_row: dict[str, Any] = {}
        for column in columns:
            if column in raw:
                scalar = _safe_scalar(raw.get(column))
                if scalar is not None:
                    projected_row[column] = scalar
        if projected_row:
            projected.append(projected_row)
    return dataset.model_copy(update={"columns": columns, "authorized_columns": columns, "rows": projected})


# 方法作用：识别适合作为关系键的业务字段，优先实体键、时间键和共同维度。
# Args: column - 字段名；dimensions - 当前单元维度集合。
# Returns: 字段是否适合作为候选关系键。
def _is_join_candidate(column: str, dimensions: set[str]) -> bool:
    lowered = column.lower()
    return column in dimensions or any(hint in lowered for hint in _KEY_HINTS)


# 方法作用：推断单列样本类型，供跨源字段兼容校验使用。
# Args: values - 单列非空值。
# Returns: number/string/date/bool/unknown 类型名。
def _value_type(values: list[Any]) -> str:
    observed = [item for item in values if item is not None]
    if not observed:
        return "unknown"
    if all(isinstance(item, bool) for item in observed):
        return "bool"
    if all(isinstance(item, (int, float)) and not isinstance(item, bool) for item in observed):
        return "number"
    if all(isinstance(item, str) and _DATE_LIKE_RE.fullmatch(item.strip()) for item in observed):
        return "date"
    if all(isinstance(item, str) for item in observed):
        return "string"
    return "unknown"


# 方法作用：判断两个字段样本类型是否可以安全比较。
# Args: left_values - 左侧字段值；right_values - 右侧字段值。
# Returns: 类型兼容返回 True。
def _types_compatible(left_values: list[Any], right_values: list[Any]) -> bool:
    left_type = _value_type(left_values)
    right_type = _value_type(right_values)
    return left_type == "unknown" or right_type == "unknown" or left_type == right_type or {left_type, right_type} <= {"number"}


# 方法作用：生成可哈希的复合键，统一日期文本并跳过缺失键。
# Args: row - 投影后的数据行；keys - 关系键集合。
# Returns: 复合键或 None。
def _row_key(row: dict[str, Any], keys: list[str]) -> tuple[Any, ...] | None:
    values: list[Any] = []
    for key in keys:
        value = row.get(key)
        if value is None:
            return None
        if isinstance(value, str) and _DATE_LIKE_RE.fullmatch(value.strip()):
            value = value.strip().replace("/", "-")
        values.append(value)
    return tuple(values)


# 方法作用：根据复合键统计行数和可匹配覆盖率。
# Args: rows - 投影数据行；keys - 关系键集合。
# Returns: 复合键计数器和有效行数。
def _key_counts(rows: list[dict[str, Any]], keys: list[str]) -> tuple[Counter[tuple[Any, ...]], int]:
    counts: Counter[tuple[Any, ...]] = Counter()
    valid_rows = 0
    for row in rows:
        key = _row_key(row, keys)
        if key is not None:
            counts[key] += 1
            valid_rows += 1
    return counts, valid_rows


# 方法作用：根据双方同一关系键的重复程度确定连接基数。
# Args: left_counts - 左侧键计数；right_counts - 右侧键计数。
# Returns: one_to_one/one_to_many/many_to_one/many_to_many/unknown。
def _cardinality(left_counts: Counter[tuple[Any, ...]], right_counts: Counter[tuple[Any, ...]]) -> str:
    keys = set(left_counts) & set(right_counts)
    if not keys:
        return "unknown"
    left_many = any(left_counts[key] > 1 for key in keys)
    right_many = any(right_counts[key] > 1 for key in keys)
    if left_many and right_many:
        return "many_to_many"
    if left_many:
        return "many_to_one"
    if right_many:
        return "one_to_many"
    return "one_to_one"


# 方法作用：估算匹配键产生的连接行数，提前阻断结果膨胀。
# Args: left_counts - 左侧键计数；right_counts - 右侧键计数。
# Returns: 有界的估算连接行数。
def _estimated_join_rows(left_counts: Counter[tuple[Any, ...]], right_counts: Counter[tuple[Any, ...]], limit: int) -> int:
    total = 0
    for key in set(left_counts) & set(right_counts):
        total += left_counts[key] * right_counts[key]
        if total > limit:
            return total
    return total


# 方法作用：为同一关系键上的共同数值字段计算 Pearson 相关系数。
# Args: left_rows - 左侧投影行；right_rows - 右侧投影行；keys - 关系键集合；left_metrics/right_metrics - 指标集合。
# Returns: 相关系数或 None（样本不足/无共同指标时）。
def _correlation(
    left_rows: list[dict[str, Any]],
    right_rows: list[dict[str, Any]],
    keys: list[str],
    left_metrics: set[str],
    right_metrics: set[str],
) -> float | None:
    common_metrics = [item for item in left_metrics & right_metrics if item not in keys]
    if not common_metrics:
        return None
    left_counts, _ = _key_counts(left_rows, keys)
    right_counts, _ = _key_counts(right_rows, keys)
    left_by_key = {key: row for row in left_rows if (key := _row_key(row, keys)) is not None}
    right_by_key = {key: row for row in right_rows if (key := _row_key(row, keys)) is not None}
    pairs: list[tuple[float, float]] = []
    metric = common_metrics[0]
    for key in set(left_counts) & set(right_counts):
        left_value = left_by_key.get(key, {}).get(metric)
        right_value = right_by_key.get(key, {}).get(metric)
        if isinstance(left_value, (int, float)) and not isinstance(left_value, bool) and isinstance(right_value, (int, float)) and not isinstance(right_value, bool):
            pairs.append((float(left_value), float(right_value)))
    if len(pairs) < 2:
        return None
    left_mean = sum(item[0] for item in pairs) / len(pairs)
    right_mean = sum(item[1] for item in pairs) / len(pairs)
    numerator = sum((left - left_mean) * (right - right_mean) for left, right in pairs)
    left_var = sum((left - left_mean) ** 2 for left, _ in pairs)
    right_var = sum((right - right_mean) ** 2 for _, right in pairs)
    denominator = math.sqrt(left_var * right_var)
    return round(numerator / denominator, 6) if denominator else None


class ControlledRelationAnalyzer:
    """受控关系分析器，只消费授权投影结果，不接触任一数据源凭证。"""

    # 方法作用：初始化关系分析的采样、匹配率和连接膨胀边界。
    # Args: policy - 可选租户级关系策略。
    # Returns: 无返回值。
    def __init__(self, policy: RelationPolicy | None = None) -> None:
        self.policy = policy or RelationPolicy()

    # 方法作用：跨源关系分析。优先走受控键匹配，键不可用时退回分布级对比。
    # Args: left - 左侧单元描述和脱敏结果；right - 右侧单元描述和脱敏结果。
    # Returns: 跨源关系证据；无法支撑时如实返回 insufficient 或原有结构证据。
    def analyze_cross_source(self, left: dict[str, Any] | RelationDataset, right: dict[str, Any] | RelationDataset) -> RelationEvidence:
        left_dataset = _coerce_dataset(left, self.policy)
        right_dataset = _coerce_dataset(right, self.policy)
        # 键匹配能给出匹配率与基数，证据强于纯分布对比，因此优先采用；
        # 只有它降级为"无关系/跨源不支持"时，才轮到分布级对比补位。
        matched = self.analyze_same_source(left, right)
        if matched.evidence_level in {"verified_relation", "statistical_association", "blocked"}:
            return matched
        # 没有样本行时分布级也无从计算，保留原有的跨源结构证据。
        if not left_dataset.rows or not right_dataset.rows:
            return matched
        base = {
            "left_unit_id": left_dataset.unit_id,
            "right_unit_id": right_dataset.unit_id,
            "left_rows": len(left_dataset.rows),
            "right_rows": len(right_dataset.rows),
            "relation_type": "cross_source_distribution",
        }
        metric = self._shared_numeric_column(left_dataset, right_dataset)
        if not metric:
            return RelationEvidence(**base, evidence_level="insufficient", limitations=["未发现双方均有权限且同为数值的共同指标，无法做分布级对比"])
        correlation, aligned, overlap, note = self._cross_source_correlation(left_dataset, right_dataset, metric)
        if correlation is None:
            return RelationEvidence(**base, join_keys=[], evidence_level="insufficient", limitations=[note])
        # join_keys 必须留空：跨源没有做任何连接，把对比指标写进去等于伪造连接键。
        limitations = [
            f"对比指标：{metric}",
            "跨数据源未做行级连接，结论仅基于各自样本分布的秩相关，不能推断个体级关联，不构成因果",
        ]
        if not aligned:
            limitations.append("两侧未按时间对齐，仅比较分布形态")
        logger.info(
            "跨源分布级关联完成",
            left_unit_id=left_dataset.unit_id,
            right_unit_id=right_dataset.unit_id,
            metric=metric,
            correlation=round(correlation, 4),
            time_aligned=aligned,
            observations=overlap,
        )
        return RelationEvidence(
            **base,
            # match_rate 与 join_keys 对跨源分布级对比都不适用，保持空值而不是填占位。
            join_keys=[],
            match_rate=None,
            correlation=round(correlation, 4),
            time_alignment="same_period" if aligned else "unknown",
            evidence_level="cross_source_statistical",
            limitations=limitations,
            sampled=overlap < min(len(left_dataset.rows), len(right_dataset.rows)),
        )

    # 方法作用：找出两侧共有的、取值可作数值比较的列。
    # Args: left_dataset/right_dataset - 两侧数据集。
    # Returns: 共同数值列名；没有时返回 None。
    def _shared_numeric_column(self, left_dataset: RelationDataset, right_dataset: RelationDataset) -> str | None:
        left_columns = set(left_dataset.columns)
        common = [column for column in right_dataset.columns if column in left_columns]
        # 优先取双方都声明为指标的列，其次取任意共同数值列。
        declared = [column for column in common if column in left_dataset.metrics and column in right_dataset.metrics]
        for column in declared + [item for item in common if item not in declared]:
            if _is_numeric_series(left_dataset.rows, column) and _is_numeric_series(right_dataset.rows, column):
                return column
        return None

    # 方法作用：按周期标签或分布形态计算跨源秩相关。
    # Args: left_dataset/right_dataset - 两侧数据集；metric - 共同数值列。
    # Returns: (相关系数, 是否按时间对齐, 观测点数, 无法计算时的说明)。
    def _cross_source_correlation(
        self, left_dataset: RelationDataset, right_dataset: RelationDataset, metric: str
    ) -> tuple[float | None, bool, int, str]:
        from src.reporting.profiling import detect_time_column

        left_time = detect_time_column(left_dataset.rows, left_dataset.columns)
        right_time = detect_time_column(right_dataset.rows, right_dataset.columns)
        if left_time and right_time:
            # 周期标签精确匹配对齐：跨源同一周期通常用同样的标签写法，文本匹配比日期运算更保守。
            left_by_period = {
                str(row.get(left_time, "")).strip(): _as_float(row.get(metric))
                for row in left_dataset.rows
                if str(row.get(left_time, "")).strip()
            }
            right_by_period = {
                str(row.get(right_time, "")).strip(): _as_float(row.get(metric))
                for row in right_dataset.rows
                if str(row.get(right_time, "")).strip()
            }
            paired = [
                (left_by_period[label], right_by_period[label])
                for label in sorted(set(left_by_period) & set(right_by_period))
                if left_by_period[label] is not None and right_by_period[label] is not None
            ]
            if len(paired) >= _MIN_CROSS_SOURCE_OBSERVATIONS:
                correlation = _spearman([item[0] for item in paired], [item[1] for item in paired])
                if correlation is not None:
                    return correlation, True, len(paired), ""
            else:
                note = f"两侧周期标签重合不足 {_MIN_CROSS_SOURCE_OBSERVATIONS} 个，无法按时间对齐"
        else:
            note = "两侧未识别出时间列，改按分布形态比较"
        # 退回分布形态比较：对各自排序后的数值序列做秩相关，等价于比较分位数函数。
        left_values = sorted(value for value in (_as_float(row.get(metric)) for row in left_dataset.rows) if value is not None)
        right_values = sorted(value for value in (_as_float(row.get(metric)) for row in right_dataset.rows) if value is not None)
        overlap = min(len(left_values), len(right_values))
        if overlap < _MIN_CROSS_SOURCE_OBSERVATIONS:
            return None, False, overlap, f"两侧可比观测点不足 {_MIN_CROSS_SOURCE_OBSERVATIONS} 个，无法做分布级对比"
        correlation = _spearman(left_values[:overlap], right_values[:overlap])
        if correlation is None:
            return None, False, overlap, "数值列无有效变化，无法计算秩相关"
        return correlation, False, overlap, note

    # 方法作用：对两个分析单元执行授权投影后的同源或跨源关系验证。
    # Args: left - 左侧单元描述和脱敏结果；right - 右侧单元描述和脱敏结果。
    # Returns: 包含匹配率、基数和限制的 RelationEvidence。
    def analyze_same_source(self, left: dict[str, Any] | RelationDataset, right: dict[str, Any] | RelationDataset) -> RelationEvidence:
        left_dataset = _coerce_dataset(left, self.policy)
        right_dataset = _coerce_dataset(right, self.policy)
        left_id = left_dataset.unit_id
        right_id = right_dataset.unit_id
        left_source = str(left_dataset.datasource)
        right_source = str(right_dataset.datasource)
        common = [column for column in left_dataset.columns if column in set(right_dataset.columns)]
        dimensions = set(left_dataset.dimensions) & set(right_dataset.dimensions)
        candidates = [column for column in common if _is_join_candidate(column, dimensions)][: self.policy.max_join_keys]
        base = {"left_unit_id": left_id, "right_unit_id": right_id, "left_rows": len(left_dataset.rows), "right_rows": len(right_dataset.rows)}
        if left_dataset.status == "failed" or right_dataset.status == "failed":
            return RelationEvidence(
                **base,
                evidence_level="blocked",
                limitations=["联合分析依赖的分析单元执行失败，不能使用剩余数据推断关系"],
            )
        if not common:
            return RelationEvidence(
                **base,
                evidence_level="unsupported_cross_source" if left_source != right_source else "no_relation",
                limitations=["未发现双方均有权限且类型兼容的共同字段，分别分析"],
            )
        if not candidates:
            return RelationEvidence(
                **base,
                evidence_level="no_relation",
                limitations=["共同字段仅是指标或非关系维度，未发现可验证的实体键/时间键，分别分析"],
            )
        # 没有结果行时只允许同源结构证据，跨源不能把字段同名当作已验证关系。
        if not left_dataset.rows or not right_dataset.rows:
            if left_source != right_source:
                return RelationEvidence(
                    **base,
                    join_keys=candidates[: self.policy.max_join_keys],
                    evidence_level="unsupported_cross_source",
                    limitations=["受控分析器需要先提供授权列投影后的样本或聚合结果，当前仅有结构信息"],
                )
            return RelationEvidence(
                **base,
                relation_type="entity_time_join" if any(_is_time_key(key) for key in candidates) else "entity_join",
                join_keys=candidates[:20],
                match_rate=1.0,
                time_alignment="same_period" if any(_is_time_key(key) for key in candidates) else "unknown",
                evidence_level="verified_relation",
                limitations=["关系证据不代表因果关系，未控制的业务因素需要单独说明；当前为结构证据"],
            )
        compatible: list[str] = []
        incompatible: list[str] = []
        for column in candidates:
            left_values = [row.get(column) for row in left_dataset.rows]
            right_values = [row.get(column) for row in right_dataset.rows]
            (compatible if _types_compatible(left_values, right_values) else incompatible).append(column)
        if not compatible:
            return RelationEvidence(
                **base,
                relation_type="entity_join",
                join_keys=candidates[: self.policy.max_join_keys],
                evidence_level="blocked",
                limitations=["共同字段类型不兼容，已阻止跨源联合"] + ([f"类型不兼容字段：{','.join(incompatible[:5])}"] if incompatible else []),
            )
        keys = compatible[: self.policy.max_join_keys]
        left_counts, left_valid = _key_counts(left_dataset.rows, keys)
        right_counts, right_valid = _key_counts(right_dataset.rows, keys)
        matched = set(left_counts) & set(right_counts)
        matched_left = sum(left_counts[key] for key in matched)
        matched_right = sum(right_counts[key] for key in matched)
        match_rate = min(matched_left / max(1, left_valid), matched_right / max(1, right_valid))
        cardinality = _cardinality(left_counts, right_counts)
        estimated = _estimated_join_rows(left_counts, right_counts, self.policy.max_join_rows)
        limitations = ["关系证据不代表因果关系，未控制的业务因素需要单独说明"]
        if incompatible:
            limitations.append(f"已排除类型不兼容字段：{','.join(incompatible[:5])}")
        if len(left_dataset.rows) >= self.policy.max_rows or len(right_dataset.rows) >= self.policy.max_rows:
            limitations.append("关系分析使用有界样本，匹配率和基数是样本估计")
        if estimated > self.policy.max_join_rows:
            limitations.append(f"预计连接行数超过上限 {self.policy.max_join_rows}，已阻止联合结果")
        if match_rate < self.policy.min_match_rate:
            limitations.append(f"匹配率 {match_rate:.2f} 低于阈值 {self.policy.min_match_rate:.2f}，已降级为分别分析")
        blocked = estimated > self.policy.max_join_rows or match_rate < self.policy.min_match_rate or cardinality == "many_to_many"
        if cardinality == "many_to_many":
            limitations.append("多对多基数可能造成重复计数，首期不发布联合结果")
        return RelationEvidence(
            **base,
            relation_type="entity_time_join" if any(_is_time_key(key) for key in keys) else "entity_join",
            join_keys=keys,
            match_rate=round(match_rate, 6),
            cardinality=cardinality,
            correlation=_correlation(left_dataset.rows, right_dataset.rows, keys, set(left_dataset.metrics), set(right_dataset.metrics)),
            time_alignment="same_period" if any(_is_time_key(key) for key in keys) and matched else "unknown",
            evidence_level="blocked" if blocked else "verified_relation",
            limitations=limitations,
            estimated_join_rows=estimated,
            sampled=len(left_dataset.rows) >= self.policy.max_rows or len(right_dataset.rows) >= self.policy.max_rows,
        )


# 方法作用：判断字段是否表示日期或时间粒度，供关系类型和对齐说明使用。
# Args: column - 字段名。
# Returns: 时间字段返回 True。
def _is_time_key(column: str) -> bool:
    lowered = column.lower()
    return any(item in lowered for item in ("time", "date", "week", "month", "period", "日期", "时间", "周", "月", "季度"))


# 方法作用：为每一对分析单元生成受控关系或明确降级证据。
# Args: units - 含 unit_id/datasource/columns 的授权分析单元摘要；unit_results - 可选脱敏结果；policy - 可选关系策略。
# Returns: 关系证据列表。
def build_relations(
    units: list[Any],
    unit_results: list[Any] | None = None,
    policy: RelationPolicy | None = None,
) -> list[RelationEvidence]:
    logger.debug("报表关系分析入口", unit_count=len(units), result_count=len(unit_results or []))
    result_map: dict[str, Any] = {}
    for result in unit_results or []:
        result_id = result.get("unit_id", "") if isinstance(result, dict) else getattr(result, "unit_id", "")
        result_map[str(result_id)] = result
    datasets: list[dict[str, Any]] = []
    for unit in units[:12]:
        item = unit.model_dump(mode="python") if hasattr(unit, "model_dump") else dict(unit)
        result = result_map.get(str(item.get("unit_id", "")))
        if result is not None:
            result_rows = result.get("rows", []) if isinstance(result, dict) else getattr(result, "rows", [])
            result_status = result.get("status", "success") if isinstance(result, dict) else getattr(result, "status", "success")
            result_columns = result.get("columns", []) if isinstance(result, dict) else getattr(result, "columns", [])
            item["rows"] = list(result_rows or [])
            item["status"] = str(result_status or "success")
            item["columns"] = list(item.get("columns", []) or result_columns or [])
        item["authorized_columns"] = list(item.get("authorized_columns", []) or item.get("columns", []) or [])
        if item["authorized_columns"]:
            allowed = set(item["authorized_columns"])
            item["columns"] = [column for column in list(item.get("columns", []) or []) if column in allowed]
        datasets.append(item)
    analyzer = ControlledRelationAnalyzer(policy)
    # 同源走键匹配，跨源只能做分布级对比：SQL 层不支持跨源 join，不能把字段同名当已验证关系。
    relations = [
        analyzer.analyze_same_source(left, right)
        if str(left.get("datasource", "")) == str(right.get("datasource", ""))
        else analyzer.analyze_cross_source(left, right)
        for left, right in combinations(datasets, 2)
    ]
    logger.info(
        "报表关系分析完成",
        relation_count=len(relations),
        verified_count=sum(item.evidence_level == "verified_relation" for item in relations),
        blocked_count=sum(item.evidence_level == "blocked" for item in relations),
    )
    return relations
