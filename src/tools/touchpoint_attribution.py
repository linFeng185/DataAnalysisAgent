"""触点归因确定性引擎：字段识别、资格复核和五种规则模型计算。"""

from __future__ import annotations

import math
import re
from dataclasses import dataclass
from datetime import date, datetime, time, timezone
from decimal import Decimal, InvalidOperation
from enum import StrEnum
from typing import Any

from pydantic import BaseModel, Field

from src.logging_config import get_logger

logger = get_logger(__name__)
_ZERO = Decimal("0")
_ONE = Decimal("1")
_HUNDRED = Decimal("100")
_MONEY_QUANTUM = Decimal("0.01")
_PERCENT_QUANTUM = Decimal("0.01")


class TouchpointAttributionModel(StrEnum):
    """当前规则引擎支持的触点归因模型。"""

    FIRST_TOUCH = "first_touch"
    LAST_TOUCH = "last_touch"
    LINEAR = "linear"
    TIME_DECAY = "time_decay"
    POSITION_BASED = "position_based"


_MODEL_DISPLAY_NAMES: dict[TouchpointAttributionModel, str] = {
    TouchpointAttributionModel.FIRST_TOUCH: "首次互动",
    TouchpointAttributionModel.LAST_TOUCH: "末次互动",
    TouchpointAttributionModel.LINEAR: "线性归因",
    TouchpointAttributionModel.TIME_DECAY: "时间衰减归因",
    TouchpointAttributionModel.POSITION_BASED: "U 型位置归因",
}


class AttributionColumnMapping(BaseModel):
    """触点路径数据的确定性字段角色映射。"""

    path_id: str = ""
    touchpoint: str = ""
    sequence: str = ""
    timestamp: str = ""
    converted: str = ""
    conversion_value: str = ""


class AttributionEligibility(BaseModel):
    """触点归因资格检查报告。"""

    eligible: bool = False
    row_count: int = 0
    total_paths: int = 0
    converted_paths: int = 0
    unconverted_paths: int = 0
    minimum_converted_paths: int = 2
    mapping: AttributionColumnMapping = Field(default_factory=AttributionColumnMapping)
    missing_requirements: list[str] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)


class TouchpointAttributionItem(BaseModel):
    """一个触点汇总后的确定性归因结果。"""

    touchpoint: str
    attributed_value: Decimal
    attribution_pct: Decimal
    conversion_credit: Decimal


class TouchpointAttributionResult(BaseModel):
    """一次触点归因计算的完整确定性结果。"""

    model: TouchpointAttributionModel
    model_display_name: str
    total_attributed_value: Decimal
    total_paths: int
    converted_paths: int
    unconverted_paths: int
    half_life_days: Decimal | None = None
    confidence: str = "medium"
    mapping: AttributionColumnMapping
    warnings: list[str] = Field(default_factory=list)
    results: list[TouchpointAttributionItem] = Field(default_factory=list)


@dataclass(slots=True)
class _PreparedTouch:
    """规则引擎内部使用的已解析触点。"""

    touchpoint: str
    sequence: Decimal | None
    timestamp: datetime | None
    converted: bool
    conversion_value: Decimal | None
    original_index: int


@dataclass(slots=True)
class _PreparedPath:
    """规则引擎内部使用的单条有序路径。"""

    path_id: str
    touches: list[_PreparedTouch]
    converted: bool
    conversion_index: int
    conversion_value: Decimal


_COLUMN_ALIASES: dict[str, tuple[str, ...]] = {
    "path_id": (
        "attribution_path_id", "conversion_path_id", "customer_journey_id",
        "journey_id", "path_id", "session_id", "order_id", "conversion_id",
        "归因路径编号", "转化路径编号", "旅程编号", "路径编号", "会话编号", "订单编号",
    ),
    "touchpoint": (
        "touchpoint", "touch_point", "touchpoint_name", "channel_name",
        "interaction_channel", "source_channel", "channel", "campaign",
        "触点名称", "触点", "互动渠道", "来源渠道", "渠道", "营销活动",
    ),
    "sequence": (
        "touchpoint_order", "touch_order", "event_order", "interaction_order",
        "sequence_no", "sequence", "step_no", "step", "position",
        "触点顺序", "互动顺序", "事件顺序", "路径顺序", "步骤序号",
    ),
    "timestamp": (
        "touchpoint_time", "touch_time", "event_time", "interaction_time",
        "occurred_at", "event_timestamp", "timestamp", "event_date", "created_at",
        "触点时间", "互动时间", "事件时间", "发生时间", "转化时间",
    ),
    "converted": (
        "is_conversion", "is_converted", "conversion_flag", "converted_flag",
        "has_converted", "converted", "是否转化", "转化标记", "已转化",
    ),
    "conversion_value": (
        "conversion_value", "conversion_amount", "converted_value", "order_amount",
        "revenue", "gmv", "amount", "转化价值", "转化金额", "订单金额", "收入金额",
    ),
}


_EXPLICIT_MODEL_MARKERS: tuple[tuple[TouchpointAttributionModel, tuple[str, ...]], ...] = (
    (
        TouchpointAttributionModel.TIME_DECAY,
        ("时间衰减", "衰减归因", "time decay", "time-decay"),
    ),
    (
        TouchpointAttributionModel.POSITION_BASED,
        ("u型", "u 型", "位置归因", "position based", "position-based"),
    ),
    (
        TouchpointAttributionModel.FIRST_TOUCH,
        ("首次互动", "首次触点", "首次归因", "首触归因", "first touch", "first-touch"),
    ),
    (
        TouchpointAttributionModel.LAST_TOUCH,
        ("末次互动", "末次触点", "末次归因", "末触归因", "last touch", "last-touch"),
    ),
    (
        TouchpointAttributionModel.LINEAR,
        ("线性归因", "平均分配", "平均归因", "linear attribution"),
    ),
)


# 方法作用：返回归因模型的中文展示名称。
# Args: model - 受支持的触点归因模型。
# Returns: 对应的中文名称。
def attribution_model_display_name(model: TouchpointAttributionModel) -> str:
    """展示名称只用于报告，不参与模型选择。"""
    return _MODEL_DISPLAY_NAMES[model]


# 方法作用：从用户原问题中确定性识别明确指定的归因模型。
# Args: query - 用户原始问题。
# Returns: 命中时返回模型，否则返回 None。
def detect_explicit_attribution_model(query: str) -> TouchpointAttributionModel | None:
    """用户明确指定的模型优先级高于任何模型推荐。"""
    normalized = str(query or "").strip().lower()
    for model, markers in _EXPLICIT_MODEL_MARKERS:
        if any(marker in normalized for marker in markers):
            return model
    return None


# 方法作用：判断当前问题是否明确属于路径级触点归因。
# Args: query - 用户原始问题。
# Returns: 触点归因问题返回 True。
def is_touchpoint_attribution_request(query: str) -> bool:
    """普通“为什么”或维度贡献问题继续使用原有变化贡献处理器。"""
    normalized = str(query or "").strip().lower()
    if detect_explicit_attribution_model(normalized) is not None:
        return True
    direct_markers = ("触点归因", "营销归因", "转化路径", "归因模型", "互动归因")
    return any(marker in normalized for marker in direct_markers) or (
        "渠道" in normalized and "归因" in normalized
    )


# 方法作用：根据真实列名推断触点路径字段角色。
# Args: columns - 查询结果或 Schema 中的真实列名。
# Returns: 不猜测不存在字段的角色映射。
def infer_attribution_column_mapping(columns: list[str]) -> AttributionColumnMapping:
    """字段识别只使用稳定语义别名，不让大模型声明字段角色。"""
    available = [str(column) for column in columns if str(column).strip()]
    normalized = {_normalize_column(column): column for column in available}
    selected: dict[str, str] = {}
    for role, aliases in _COLUMN_ALIASES.items():
        selected[role] = ""
        for alias in aliases:
            match = normalized.get(_normalize_column(alias))
            if match:
                selected[role] = match
                break
    return AttributionColumnMapping(**selected)


class TouchpointAttributionEngine:
    """对路径级触点数据执行资格复核和确定性归因计算。"""

    # 方法作用：初始化归因样本阈值和时间衰减半衰期。
    # Args: minimum_converted_paths - 最少转化路径数；half_life_days - 时间衰减半衰期天数。
    # Returns: 无返回值。
    def __init__(
        self,
        *,
        minimum_converted_paths: int = 2,
        half_life_days: Decimal | int | float | str = Decimal("7"),
    ) -> None:
        """初始化样本阈值和时间衰减半衰期。"""
        if minimum_converted_paths < 1:
            raise ValueError("最少已转化路径数必须大于零")
        parsed_half_life = _to_decimal(half_life_days)
        if parsed_half_life is None or parsed_half_life <= 0:
            raise ValueError("时间衰减半衰期必须大于零")
        self.minimum_converted_paths = int(minimum_converted_paths)
        self.half_life_days = parsed_half_life

    # 方法作用：检查选中模型能否在当前路径数据上安全计算。
    # Args: self - 引擎实例；rows - 路径级触点行；model - 已选归因模型。
    # Returns: 字段映射、路径计数、缺口和警告。
    def evaluate(
        self,
        rows: list[dict[str, Any]],
        model: TouchpointAttributionModel,
    ) -> AttributionEligibility:
        """任何资格缺口都会阻断计算，不能由模型解释绕过。"""
        mapping = infer_attribution_column_mapping(list(rows[0]) if rows else [])
        missing: list[str] = []
        warnings: list[str] = []
        if not rows:
            missing.append("缺少路径级触点数据")
        if not mapping.path_id:
            missing.append("缺少路径标识字段 path_id")
        if not mapping.touchpoint:
            missing.append("缺少触点字段 touchpoint")
        if not mapping.converted:
            missing.append("缺少转化标记字段 is_conversion")
        if not mapping.sequence and not mapping.timestamp:
            missing.append("缺少触点顺序 touch_order 或触点时间 touch_time")
        if model is TouchpointAttributionModel.TIME_DECAY and not mapping.timestamp:
            missing.append("时间衰减归因缺少触点时间 touch_time")

        prepared: list[_PreparedPath] = []
        if not missing:
            prepared, preparation_errors, preparation_warnings = _prepare_paths(rows, mapping)
            missing.extend(preparation_errors)
            warnings.extend(preparation_warnings)

        total_paths = len(prepared)
        converted_paths = sum(1 for path in prepared if path.converted)
        unconverted_paths = total_paths - converted_paths
        if prepared and converted_paths < self.minimum_converted_paths:
            missing.append(
                f"已转化路径仅 {converted_paths} 条，至少需要 "
                f"{self.minimum_converted_paths} 条",
            )
        if prepared and unconverted_paths == 0:
            warnings.append("当前数据未包含未转化路径；规则模型可计算，但不能用于数据驱动归因")
        if converted_paths and converted_paths < 30:
            warnings.append("已转化路径样本少于 30 条，归因分配可计算但业务外推置信度有限")
        if prepared and not mapping.conversion_value:
            warnings.append("未提供转化价值字段，按每条已转化路径一次转化计算")

        missing = list(dict.fromkeys(missing))
        warnings = list(dict.fromkeys(warnings))
        result = AttributionEligibility(
            eligible=not missing,
            row_count=len(rows),
            total_paths=total_paths,
            converted_paths=converted_paths,
            unconverted_paths=unconverted_paths,
            minimum_converted_paths=self.minimum_converted_paths,
            mapping=mapping,
            missing_requirements=missing,
            warnings=warnings,
        )
        logger.info(
            "触点归因资格复核完成",
            model=model.value,
            eligible=result.eligible,
            rows=result.row_count,
            total_paths=result.total_paths,
            converted_paths=result.converted_paths,
            unconverted_paths=result.unconverted_paths,
            missing=result.missing_requirements,
        )
        return result

    # 方法作用：按选中模型计算每个触点的归因价值、占比和转化信用。
    # Args: self - 引擎实例；rows - 完整路径级触点行；model - 已通过选择的模型。
    # Returns: 可直接进入分析响应的确定性结果。
    def calculate(
        self,
        rows: list[dict[str, Any]],
        model: TouchpointAttributionModel,
    ) -> TouchpointAttributionResult:
        """计算前再次执行资格复核，避免调用方绕过检查。"""
        eligibility = self.evaluate(rows, model)
        if not eligibility.eligible:
            raise ValueError("；".join(eligibility.missing_requirements))
        paths, errors, __ = _prepare_paths(rows, eligibility.mapping)
        if errors:
            raise ValueError("；".join(errors))

        channel_values: dict[str, Decimal] = {}
        channel_credits: dict[str, Decimal] = {}
        total_value = _ZERO
        for path in paths:
            if not path.converted:
                continue
            weights = self._weights(path, model)
            total_value += path.conversion_value
            for touch, weight in zip(path.touches, weights, strict=True):
                if weight == 0:
                    continue
                channel_values[touch.touchpoint] = (
                    channel_values.get(touch.touchpoint, _ZERO)
                    + path.conversion_value * weight
                )
                channel_credits[touch.touchpoint] = (
                    channel_credits.get(touch.touchpoint, _ZERO) + weight
                )

        if total_value <= 0:
            raise ValueError("已转化路径的总转化价值必须大于零")
        logger.info(
            "触点归因聚合写回边界",
            channel_count=len(channel_values),
            zero_value_channels=sum(1 for value in channel_values.values() if value == 0),
            raw_total=str(sum(channel_values.values(), _ZERO)),
            expected_total=str(total_value),
        )
        ordered_channels = sorted(
            channel_values,
            key=lambda channel: (-channel_values[channel], channel),
        )
        rounded_values = _rounded_distribution(
            {channel: channel_values[channel] for channel in ordered_channels},
            total_value.quantize(_MONEY_QUANTUM),
            _MONEY_QUANTUM,
        )
        raw_percentages = {
            channel: channel_values[channel] / total_value * _HUNDRED
            for channel in ordered_channels
        }
        rounded_percentages = _rounded_distribution(
            raw_percentages,
            _HUNDRED,
            _PERCENT_QUANTUM,
        )
        rounded_credits = _rounded_distribution(
            channel_credits,
            Decimal(eligibility.converted_paths).quantize(_MONEY_QUANTUM),
            _MONEY_QUANTUM,
        )
        items = [
            TouchpointAttributionItem(
                touchpoint=channel,
                attributed_value=rounded_values[channel],
                attribution_pct=rounded_percentages[channel],
                conversion_credit=rounded_credits[channel],
            )
            for channel in ordered_channels
        ]
        confidence = "high" if (
            eligibility.converted_paths >= 30 and eligibility.unconverted_paths > 0
        ) else "medium"
        result = TouchpointAttributionResult(
            model=model,
            model_display_name=attribution_model_display_name(model),
            total_attributed_value=total_value.quantize(_MONEY_QUANTUM),
            total_paths=eligibility.total_paths,
            converted_paths=eligibility.converted_paths,
            unconverted_paths=eligibility.unconverted_paths,
            half_life_days=(
                self.half_life_days
                if model is TouchpointAttributionModel.TIME_DECAY
                else None
            ),
            confidence=confidence,
            mapping=eligibility.mapping,
            warnings=eligibility.warnings,
            results=items,
        )
        logger.info(
            "触点归因确定性计算完成",
            model=model.value,
            converted_paths=result.converted_paths,
            touchpoint_count=len(result.results),
            total_value=str(result.total_attributed_value),
        )
        return result

    # 方法作用：计算单条已转化路径中每个触点的归一化权重。
    # Args: self - 引擎实例；path - 已准备路径；model - 归因模型。
    # Returns: 与路径触点一一对应且合计为一的 Decimal 权重。
    def _weights(
        self,
        path: _PreparedPath,
        model: TouchpointAttributionModel,
    ) -> list[Decimal]:
        count = len(path.touches)
        if model is TouchpointAttributionModel.FIRST_TOUCH:
            return [_ONE, *([_ZERO] * (count - 1))]
        if model is TouchpointAttributionModel.LAST_TOUCH:
            return [*([_ZERO] * (count - 1)), _ONE]
        if model is TouchpointAttributionModel.LINEAR:
            weight = _ONE / Decimal(count)
            return [weight] * count
        if model is TouchpointAttributionModel.POSITION_BASED:
            if count == 1:
                return [_ONE]
            if count == 2:
                return [Decimal("0.5"), Decimal("0.5")]
            middle_weight = Decimal("0.2") / Decimal(count - 2)
            return [Decimal("0.4"), *([middle_weight] * (count - 2)), Decimal("0.4")]
        conversion_time = path.touches[path.conversion_index].timestamp
        if conversion_time is None:
            raise ValueError("时间衰减归因缺少可解析的转化时间")
        raw_weights: list[Decimal] = []
        for touch in path.touches:
            if touch.timestamp is None:
                raise ValueError("时间衰减归因存在不可解析的触点时间")
            age_days = max(
                0.0,
                (conversion_time - touch.timestamp).total_seconds() / 86_400,
            )
            raw_weights.append(Decimal(str(math.pow(
                0.5,
                age_days / float(self.half_life_days),
            ))))
        total_weight = sum(raw_weights, _ZERO)
        return [weight / total_weight for weight in raw_weights]


# 方法作用：把真实路径行解析为有序、去重计价的内部路径。
# Args: rows - 路径级触点行；mapping - 已识别字段角色。
# Returns: 路径列表、阻断错误和非阻断警告。
def _prepare_paths(
    rows: list[dict[str, Any]],
    mapping: AttributionColumnMapping,
) -> tuple[list[_PreparedPath], list[str], list[str]]:
    grouped: dict[str, list[_PreparedTouch]] = {}
    errors: list[str] = []
    warnings: list[str] = []
    for index, row in enumerate(rows):
        path_value = row.get(mapping.path_id)
        touchpoint_value = row.get(mapping.touchpoint)
        path_id = str(path_value).strip() if path_value is not None else ""
        touchpoint = str(touchpoint_value).strip() if touchpoint_value is not None else ""
        if not path_id:
            errors.append("存在空路径标识")
            continue
        if not touchpoint:
            errors.append("存在空触点名称")
            continue
        converted = _parse_bool(row.get(mapping.converted))
        if converted is None:
            errors.append("转化标记存在无法识别的值")
            continue
        sequence = _to_decimal(row.get(mapping.sequence)) if mapping.sequence else None
        if mapping.sequence and sequence is None:
            errors.append("触点顺序存在无法解析的值")
            continue
        timestamp = _parse_timestamp(row.get(mapping.timestamp)) if mapping.timestamp else None
        if mapping.timestamp and timestamp is None:
            errors.append("触点时间存在无法解析的值")
            continue
        conversion_value = (
            _to_decimal(row.get(mapping.conversion_value))
            if mapping.conversion_value and row.get(mapping.conversion_value) is not None
            else None
        )
        if mapping.conversion_value and row.get(mapping.conversion_value) is not None \
                and conversion_value is None:
            errors.append("转化价值存在无法解析的值")
            continue
        grouped.setdefault(path_id, []).append(_PreparedTouch(
            touchpoint=touchpoint,
            sequence=sequence,
            timestamp=timestamp,
            converted=converted,
            conversion_value=conversion_value,
            original_index=index,
        ))

    prepared: list[_PreparedPath] = []
    for path_id, touches in grouped.items():
        touches.sort(key=_touch_sort_key)
        order_values = [
            touch.sequence if mapping.sequence else touch.timestamp
            for touch in touches
        ]
        if len(order_values) != len(set(order_values)):
            errors.append(f"路径 {path_id} 存在重复顺序，无法确定首次和末次触点")
            continue
        conversion_indexes = [
            index for index, touch in enumerate(touches) if touch.converted
        ]
        is_converted = bool(conversion_indexes)
        if is_converted and len(conversion_indexes) < len(touches):
            conversion_index = conversion_indexes[-1]
            if conversion_index != len(touches) - 1:
                errors.append(f"路径 {path_id} 在转化事件后仍有触点，转化窗口不明确")
                continue
        else:
            conversion_index = len(touches) - 1
        path_value, value_error = _path_conversion_value(
            touches,
            is_converted,
            require_value=bool(mapping.conversion_value),
        )
        if value_error:
            errors.append(f"路径 {path_id} {value_error}")
            continue
        prepared.append(_PreparedPath(
            path_id=path_id,
            touches=touches,
            converted=is_converted,
            conversion_index=conversion_index,
            conversion_value=path_value,
        ))
    return prepared, list(dict.fromkeys(errors)), list(dict.fromkeys(warnings))


# 方法作用：为单条路径选择一次转化价值，拒绝同一路径口径冲突。
# Args: touches - 路径触点；converted - 路径是否转化。
# Returns: 路径价值和可选错误说明。
def _path_conversion_value(
    touches: list[_PreparedTouch],
    converted: bool,
    *,
    require_value: bool,
) -> tuple[Decimal, str]:
    if not converted:
        return _ZERO, ""
    all_values = {
        touch.conversion_value
        for touch in touches
        if touch.conversion_value is not None
    }
    candidates = all_values
    if require_value and not candidates:
        return _ZERO, "缺少转化价值"
    if len(candidates) > 1:
        return _ZERO, "存在多个不同的转化价值"
    value = next(iter(candidates), _ONE)
    if value < 0:
        return _ZERO, "转化价值不能为负数"
    return value, ""


# 方法作用：把字段名规范化为稳定比较键。
# Args: value - 原始字段名。
# Returns: 去除分隔符后的小写键。
def _normalize_column(value: str) -> str:
    return re.sub(r"[\s_\-]+", "", str(value or "").strip().lower())


# 方法作用：安全解析 Decimal，不把非法值静默转换为零。
# Args: value - 待解析值。
# Returns: 合法 Decimal 或 None。
def _to_decimal(value: Any) -> Decimal | None:
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, Decimal):
        return value if value.is_finite() else None
    try:
        parsed = Decimal(str(value).strip())
        return parsed if parsed.is_finite() else None
    except (InvalidOperation, TypeError, ValueError):
        return None


# 方法作用：严格解析数据库常见布尔转化标记。
# Args: value - 原始标记值。
# Returns: 可识别时返回布尔值，否则返回 None。
def _parse_bool(value: Any) -> bool | None:
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, Decimal)) and value in (0, 1):
        return bool(value)
    normalized = str(value or "").strip().lower()
    if normalized in {"1", "true", "yes", "y", "是", "已转化", "转化", "成功"}:
        return True
    if normalized in {"0", "false", "no", "n", "否", "未转化", "失败"}:
        return False
    return None


# 方法作用：把日期时间值统一解析为 UTC 时间。
# Args: value - datetime/date/时间戳/ISO 字符串。
# Returns: 可比较的 UTC datetime 或 None。
def _parse_timestamp(value: Any) -> datetime | None:
    parsed: datetime | None = None
    if isinstance(value, datetime):
        parsed = value
    elif isinstance(value, date):
        parsed = datetime.combine(value, time.min)
    elif isinstance(value, (int, float, Decimal)) and not isinstance(value, bool):
        try:
            parsed = datetime.fromtimestamp(float(value), tz=timezone.utc)
        except (OSError, OverflowError, ValueError):
            return None
    else:
        text = str(value or "").strip()
        if not text:
            return None
        try:
            parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
        except ValueError:
            return None
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


# 方法作用：构造稳定触点排序键。
# Args: touch - 已解析触点。
# Returns: 优先顺序字段、其次时间、最后原始位置的元组。
def _touch_sort_key(touch: _PreparedTouch) -> tuple[Any, ...]:
    if touch.sequence is not None:
        return (0, touch.sequence, touch.timestamp or datetime.min.replace(tzinfo=timezone.utc))
    return (1, touch.timestamp or datetime.min.replace(tzinfo=timezone.utc), touch.original_index)


# 方法作用：量化分配并把舍入差额回补到最大项目。
# Args: values - 原始分配；expected_total - 期望合计；quantum - 量化精度。
# Returns: 合计严格等于期望值的分配。
def _rounded_distribution(
    values: dict[str, Decimal],
    expected_total: Decimal,
    quantum: Decimal,
) -> dict[str, Decimal]:
    rounded = {key: value.quantize(quantum) for key, value in values.items()}
    if not rounded:
        return rounded
    difference = expected_total - sum(rounded.values(), _ZERO)
    logger.info(
        "触点归因舍入补差边界",
        item_count=len(values),
        expected_total=str(expected_total),
        rounded_total=str(sum(rounded.values(), _ZERO)),
        difference=str(difference),
    )
    if difference:
        largest = max(values, key=values.get)
        rounded[largest] += difference
    return rounded
