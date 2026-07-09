"""数据处理脚本框架 — 精确计算 + LLM 仅做自然语言总结。

设计原则:
1. SQL 能做的优先 SQL（过滤/聚合/排序/窗口函数）
2. 跨周期复杂计算由脚本处理（同比/环比/趋势/异常/分布）
3. LLM 只接收结构化结果做润色，不做数值计算
4. 统一接口: process(rows, params) → ProcessorResult
"""

from __future__ import annotations

import math
from abc import ABC, abstractmethod
from dataclasses import dataclass

from src.logging_config import get_logger

logger = get_logger(__name__)


@dataclass
class ProcessorResult:
    """结构化输出——喂给 LLM 做自然语言润色，不需要 LLM 计算数值。"""
    summary: str
    insights: list[str]
    chart_type: str
    data: list[dict]
    confidence: str = "high"


class DataProcessor(ABC):
    """数据处理基类。纯计算，不调 LLM。"""

    @abstractmethod
    def process(self, rows: list[dict], params: dict) -> ProcessorResult:
        ...

    @property
    @abstractmethod
    def name(self) -> str: ...

    @property
    @abstractmethod
    def intents(self) -> list[str]: ...

    @property
    def prefer_sql(self) -> bool:
        """SQL 能否替代？True=SQL 优先。"""
        return False

    @staticmethod
    def _f(val) -> float:
        if val is None: return 0.0
        if isinstance(val, (int, float)) and not isinstance(val, bool): return float(val)
        try: return float(str(val))
        except (ValueError, TypeError): return 0.0

    @staticmethod
    def _s(val, d="") -> str: return str(val) if val is not None else d

    @staticmethod
    def _group(rows, kc, vc) -> dict[str, list[float]]:
        g: dict[str, list[float]] = {}
        for r in rows:
            k = DataProcessor._s(r.get(kc)); v = DataProcessor._f(r.get(vc))
            if k: g.setdefault(k, []).append(v)
        return g

    @staticmethod
    def _pct(sv, p): return sv[min(int(len(sv)*p), len(sv)-1)] if sv else 0.0

    @staticmethod
    def _std(vals):
        if len(vals) < 2: return 0.0
        m = sum(vals) / len(vals)
        return math.sqrt(sum((v - m) ** 2 for v in vals) / len(vals))


_registry: dict[str, DataProcessor] = {}


def register(p: DataProcessor) -> DataProcessor:
    _registry[p.name] = p; return p


def get_processor(intent: str) -> DataProcessor | None:
    """按意图匹配处理器——优先 script-first (prefer_sql=False)。"""
    best = None
    for p in _registry.values():
        if intent in p.intents:
            if not p.prefer_sql:
                return p  # 脚本优先，直接返回
            if best is None:
                best = p  # SQL 可替代的兜底
    return best


def list_processors() -> list[dict]:
    return [{"name": p.name, "intents": p.intents, "prefer_sql": p.prefer_sql}
            for p in _registry.values()]
