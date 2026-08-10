"""可复现抽样与代表性审计工具。"""

from __future__ import annotations

import json
import random
from collections import defaultdict
from datetime import datetime, timezone
from typing import Any

from langchain_core.tools import BaseTool

from src.logging_config import get_logger

logger = get_logger(__name__)
_METHODS = {"random", "systematic", "stratified", "time_window"}


class SampleWithAuditTool(BaseTool):
    """使用固定种子抽样并量化分层代表性差异。"""

    name: str = "sample_with_audit"
    description: str = "执行随机、系统、分层或时间窗口抽样，并返回可复现参数和代表性报告。"

    def _run(self, *args: Any, **kwargs: Any) -> dict[str, Any]:
        """拒绝同步入口。"""
        del args, kwargs
        raise RuntimeError("sample_with_audit 只允许异步执行")

    async def _arun(
        self,
        rows: list[dict[str, Any]],
        sample_size: int,
        method: str = "random",
        seed: int = 0,
        strata_column: str | None = None,
        time_column: str | None = None,
        start_time: str | None = None,
        end_time: str | None = None,
        run_manager: Any = None,
    ) -> dict[str, Any]:
        """返回保持原始顺序的抽样结果及审计统计。"""
        del run_manager
        _validate_rows(rows)
        if method not in _METHODS:
            raise ValueError(f"不支持的抽样方法: {method}")
        if sample_size < 1:
            raise ValueError("sample_size 必须大于零")
        logger.info(
            "抽样审计开始",
            population_size=len(rows),
            requested_sample_size=sample_size,
            method=method,
            seed=seed,
        )
        warnings: list[str] = []
        if sample_size > len(rows) and rows:
            warnings.append("样本量超过总体行数，已返回全部可用记录")

        if method == "random":
            selected = _random_indices(len(rows), sample_size, seed)
        elif method == "systematic":
            selected = _systematic_indices(len(rows), sample_size, seed)
        elif method == "stratified":
            if not strata_column:
                raise ValueError("分层抽样必须提供 strata_column")
            selected = _stratified_indices(rows, sample_size, seed, strata_column)
        else:
            if not time_column:
                raise ValueError("时间窗口抽样必须提供 time_column")
            if start_time is None and end_time is None:
                raise ValueError("时间窗口抽样至少需要 start_time 或 end_time")
            selected = _time_window_indices(
                rows,
                sample_size,
                seed,
                time_column,
                start_time,
                end_time,
            )

        report = _representativeness_report(rows, selected, strata_column)
        if strata_column and report["max_absolute_difference"] > 0.1:
            warnings.append("样本与总体的最大分层占比差异超过 10%")
        if not rows:
            warnings.append("总体为空，未抽取任何记录")
        result = {
            "method": method,
            "seed": seed,
            "population_size": len(rows),
            "requested_sample_size": sample_size,
            "actual_sample_size": len(selected),
            "sampling_fraction": round(len(selected) / len(rows), 6) if rows else 0.0,
            "selected_indices": selected,
            "sample_rows": [rows[index].copy() for index in selected],
            "representativeness": report,
            "warnings": warnings,
        }
        logger.info(
            "抽样审计完成",
            actual_sample_size=len(selected),
            representative=report["representative"],
            warning_count=len(warnings),
        )
        return result


def _validate_rows(rows: list[dict[str, Any]]) -> None:
    """校验行式数据结构。"""
    if any(not isinstance(row, dict) for row in rows):
        raise ValueError("rows 中的每一项都必须是对象")


def _random_indices(population_size: int, sample_size: int, seed: int) -> list[int]:
    """使用固定种子执行简单随机抽样。"""
    target = min(population_size, sample_size)
    return sorted(random.Random(seed).sample(range(population_size), target))


def _systematic_indices(population_size: int, sample_size: int, seed: int) -> list[int]:
    """使用随机起点执行等距系统抽样。"""
    target = min(population_size, sample_size)
    if target == 0:
        return []
    interval = population_size / target
    start = random.Random(seed).random() * interval
    return sorted(
        {min(int(start + index * interval), population_size - 1) for index in range(target)}
    )


def _stratified_indices(
    rows: list[dict[str, Any]],
    sample_size: int,
    seed: int,
    column: str,
) -> list[int]:
    """按总体占比和最大余数法分配各层样本量。"""
    groups: dict[str, list[int]] = defaultdict(list)
    labels: dict[str, Any] = {}
    for index, row in enumerate(rows):
        if column not in row:
            raise ValueError(f"第 {index} 行缺少分层字段 {column}")
        token = _token(row[column])
        groups[token].append(index)
        labels[token] = row[column]
    target = min(len(rows), sample_size)
    if not rows:
        return []
    exact = {token: target * len(indices) / len(rows) for token, indices in groups.items()}
    quotas = {token: int(value) for token, value in exact.items()}
    remaining = target - sum(quotas.values())
    allocation_order = sorted(groups, key=lambda token: (-(exact[token] - quotas[token]), token))
    for token in allocation_order[:remaining]:
        quotas[token] += 1
    rng = random.Random(seed)
    selected: list[int] = []
    for token in sorted(groups):
        selected.extend(rng.sample(groups[token], quotas[token]))
    del labels
    return sorted(selected)


def _time_window_indices(
    rows: list[dict[str, Any]],
    sample_size: int,
    seed: int,
    column: str,
    start_time: str | None,
    end_time: str | None,
) -> list[int]:
    """筛选闭区间时间窗口，并在超限时使用固定种子抽样。"""
    start = _parse_time(start_time, "start_time") if start_time is not None else None
    end = _parse_time(end_time, "end_time") if end_time is not None else None
    if start is not None and end is not None and start > end:
        raise ValueError("start_time 不能晚于 end_time")
    candidates: list[int] = []
    for index, row in enumerate(rows):
        if column not in row:
            raise ValueError(f"第 {index} 行缺少时间字段 {column}")
        value = _parse_time(str(row[column]), f"第 {index} 行字段 {column}")
        if (start is None or value >= start) and (end is None or value <= end):
            candidates.append(index)
    if len(candidates) <= sample_size:
        return candidates
    return sorted(random.Random(seed).sample(candidates, sample_size))


def _parse_time(value: str, label: str) -> datetime:
    """将标准时间文本转换为 UTC 时间。"""
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ValueError(f"{label} 不是有效的标准时间") from exc
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _representativeness_report(
    rows: list[dict[str, Any]],
    selected: list[int],
    strata_column: str | None,
) -> dict[str, Any]:
    """比较样本和总体的分层占比。"""
    if not strata_column:
        return {
            "strata_column": None,
            "representative": True,
            "max_absolute_difference": 0.0,
            "strata": [],
        }
    population: dict[str, int] = defaultdict(int)
    sample: dict[str, int] = defaultdict(int)
    values: dict[str, Any] = {}
    for row in rows:
        token = _token(row.get(strata_column))
        population[token] += 1
        values[token] = row.get(strata_column)
    for index in selected:
        token = _token(rows[index].get(strata_column))
        sample[token] += 1
    strata: list[dict[str, Any]] = []
    differences: list[float] = []
    for token in sorted(population):
        population_share = population[token] / len(rows) if rows else 0.0
        sample_share = sample[token] / len(selected) if selected else 0.0
        difference = abs(population_share - sample_share)
        differences.append(difference)
        strata.append(
            {
                "value": values[token],
                "population_count": population[token],
                "sample_count": sample[token],
                "population_share": round(population_share, 6),
                "sample_share": round(sample_share, 6),
                "absolute_difference": round(difference, 6),
            }
        )
    maximum = max(differences, default=0.0)
    return {
        "strata_column": strata_column,
        "representative": maximum <= 0.1,
        "max_absolute_difference": round(maximum, 6),
        "strata": strata,
    }


def _token(value: Any) -> str:
    """把分层值转换为稳定标记。"""
    return json.dumps(value, ensure_ascii=False, sort_keys=True, default=str, separators=(",", ":"))


_TOOLS: dict[str, BaseTool] = {"sample_with_audit": SampleWithAuditTool()}


def get_tool(name: str) -> BaseTool | None:
    """按名称返回已注册工具。"""
    return _TOOLS.get(name)


def get_tools() -> list[BaseTool]:
    """返回该 Skill 暴露的全部工具。"""
    return list(_TOOLS.values())
