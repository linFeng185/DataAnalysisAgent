"""跨源授权样本按明确粒度独立聚合，不执行原始行连接。"""

from __future__ import annotations

import json
from decimal import Decimal, InvalidOperation
from typing import Any

from src.logging_config import get_logger
from src.reporting.models import UnitResult

logger = get_logger(__name__)


# 方法作用：在共同声明维度上分别聚合来源，避免连接膨胀与跨来源重复相加。
# Args: units - 授权单元；results - 已脱敏结果；max_rows/max_groups/max_bytes - 总资源上限。
# Returns: 样本统计表或有原因的降级结果，不声称全量或因果结论。
def aggregate_cross_source(units: list[dict[str, Any]], results: list[UnitResult], *, max_rows: int = 6000, max_groups: int = 200, max_bytes: int = 524_288) -> dict[str, Any]:
    """仅输出每个来源指标的样本和、均值、计数，不推断可加业务指标。"""
    base: dict[str, Any] = {"status": "blocked", "dimensions": [], "rows": [], "sampled": True, "limitations": []}
    sources = {str(unit.get("datasource", "")) for unit in units}
    if len(sources - {""}) < 2:
        return {**base, "status": "not_applicable"}
    logger.debug("跨源样本聚合入口", unit_count=len(units), max_rows=max_rows, max_groups=max_groups)
    try:
        # 单元数超预算与资源参数非法是两种不同的降级原因，必须各给一句如实文案：
        # 用前者的话术描述后者会把排查方向带偏。真实验收里 deep 档 8+6=14 个单元走的是
        # 第一条，用户看到的却是「跨源分析资源边界无效」这类内部术语。
        if not 2 <= len(units) <= 12:
            logger.info("跨源聚合单元数超出预算", unit_count=len(units), min_units=2, max_units=12)
            raise ValueError(f"本次分析单元数 {len(units)} 个超出跨源聚合预算（2–12 个），已分别分析各来源，不做跨源合并")
        if min(max_rows, max_groups, max_bytes) < 1:
            logger.info("跨源聚合资源上限非法", max_rows=max_rows, max_groups=max_groups, max_bytes=max_bytes)
            raise ValueError("跨源聚合的行数、分组数或字节数上限不是正整数，已跳过跨源合并，改为分别分析各来源")
        result_map = {result.unit_id: result for result in results}
        dimensions = list(dict.fromkeys(str(field) for field in units[0].get("dimensions", [])))
        if not dimensions or len(dimensions) > 3 or any(set(unit.get("dimensions", [])) != set(dimensions) for unit in units):
            raise ValueError("各来源必须声明完全一致的一至三个分组维度，不自动猜测业务粒度")
        selected = [result_map.get(str(unit.get("unit_id"))) for unit in units]
        if any(result is None or result.status != "success" or not result.rows for result in selected):
            raise ValueError("部分来源失败或为空，跨源聚合已降级为分别分析")
        if sum(len(result.rows) for result in selected if result is not None) > max_rows:
            raise ValueError("跨源输入行数超过预算，不发布截断聚合")
        groups: dict[tuple[Any, ...], dict[str, Any]] = {}
        key_types: dict[str, type] = {}
        output_columns: list[str] = []
        input_bytes = 0
        for unit, result in zip(units, selected, strict=True):
            assert result is not None
            metrics = list(dict.fromkeys(str(field) for field in unit.get("metrics", [])))
            allowed = set(unit.get("authorized_columns") or unit.get("columns") or result.columns)
            if not metrics or len(metrics) > 8 or not set(dimensions + metrics).issubset(allowed) or set(metrics) & set(dimensions):
                raise ValueError("分组维度和数值指标必须全部位于授权列中，且不能混用")
            totals: dict[tuple[Any, ...], dict[str, tuple[Decimal, int]]] = {}
            for row in result.rows:
                key = tuple(row.get(field) for field in dimensions)
                for field, value in zip(dimensions, key, strict=True):
                    if type(value) not in (str, int) or value == "" or isinstance(value, str) and len(value) > 200:
                        raise ValueError("分组维度包含空值或不受支持的键类型")
                    if field in key_types and key_types[field] is not type(value):
                        raise ValueError("跨源分组键类型不一致，不进行隐式类型转换")
                    key_types[field] = type(value)
                groups.setdefault(key, dict(zip(dimensions, key, strict=True)))
                if len(groups) > max_groups:
                    raise ValueError("跨源分组数超过预算，已保留独立来源分析")
                values = totals.setdefault(key, {})
                input_bytes += len(json.dumps({field: row.get(field) for field in dimensions + metrics}, ensure_ascii=False, default=str).encode("utf-8"))
                if input_bytes > max_bytes:
                    raise ValueError("跨源授权数据字节数超过预算")
                for metric in metrics:
                    raw = row.get(metric)
                    if raw is None:
                        continue
                    if isinstance(raw, bool) or len(str(raw)) > 100:
                        raise ValueError("聚合指标不是有限数值")
                    try:
                        number = Decimal(str(raw))
                    except (InvalidOperation, ValueError) as exc:
                        raise ValueError("聚合指标包含非数值，不将错误数据当作零") from exc
                    if not number.is_finite() or abs(number) > Decimal("1e100"):
                        raise ValueError("聚合指标超过有限数值范围")
                    total, count = values.get(metric, (Decimal(0), 0))
                    values[metric] = total + number, count + 1
            for metric in metrics:
                prefix = f"{result.unit_id}.{metric}"
                names = [f"{prefix}.sample_sum", f"{prefix}.sample_mean", f"{prefix}.sample_count"]
                output_columns.extend(names)
                for key, values in totals.items():
                    total, count = values.get(metric, (Decimal(0), 0))
                    groups[key].update(dict(zip(names, [float(total) if count else None, float(total / count) if count else None, count], strict=True)))
        rows = [{**group, **{column: group.get(column) for column in output_columns}} for group in groups.values()]
        if len(json.dumps(rows, ensure_ascii=False).encode("utf-8")) > max_bytes:
            raise ValueError("跨源聚合输出超过页面字节预算")
        logger.info("跨源样本聚合完成", source_count=len(sources), group_count=len(rows), input_bytes=input_bytes)
        return {"status": "success", "dimensions": dimensions, "rows": rows, "sampled": True, "limitations": ["仅对各来源已授权的脱敏样本进行分组描述，不代表全量业务总额", "样本和不代表指标具有业务可加性；不同来源的指标不相加、不推断因果", "缺失来源保留空值，不能视为零；时间键必须事先采用一致时区与粒度"]}
    except ValueError as exc:
        logger.info("跨源聚合安全降级", reason=str(exc))
        return {**base, "limitations": [str(exc)]}
