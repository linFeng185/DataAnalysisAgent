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
from decimal import Decimal
D = Decimal

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
    def _f(val) -> Decimal:
        if val is None:
            return D(0)
        if isinstance(val, Decimal):
            return val
        if isinstance(val, bool):
            return D(1) if val else D(0)
        if isinstance(val, (int, float)):
            return D(str(val))
        try:
            return D(str(val))
        except Exception:
            return D(0)

    @staticmethod
    def _s(val, d="") -> str: return str(val) if val is not None else d

    @staticmethod
    def _group(rows, kc, vc) -> dict[str, list[Decimal]]:
        g: dict[str, list[Decimal]] = {}
        for r in rows:
            k = DataProcessor._s(r.get(kc)); v = DataProcessor._f(r.get(vc))
            if k: g.setdefault(k, []).append(v)
        return g

    @staticmethod
    def _pct(sv, p):
        if not sv: return D(0)
        sv_sorted = sorted(sv)
        idx = int(len(sv_sorted) * p)
        return sv_sorted[min(idx, len(sv_sorted) - 1)]

    @staticmethod
    def _std(vals):
        if len(vals) < 2: return D(0)
        m = sum(vals) / len(vals)
        return D(str(math.sqrt(float(sum((v - m) ** 2 for v in vals)) / len(vals))))


_registry: dict[str, DataProcessor] = {}


def register(p: DataProcessor) -> DataProcessor:
    _registry[p.name] = p(); return p


def get_processor(intent: str) -> DataProcessor | None:
    """按意图匹配处理器——优先 script-first (prefer_sql=False)，同优先级按关键词匹配度排序。"""
    # 分解 intent 中的关键词
    intent_keywords = set(intent.lower().replace(",", " ").split())
    candidates: list[tuple[int, DataProcessor]] = []

    for p in _registry.values():
        if intent in p.intents:
            # 计算匹配得分：processor.intents 中与 intent 重叠的关键词数
            p_keywords = set()
            for pi in p.intents:
                p_keywords.update(pi.lower().replace(",", " ").split())
            overlap = len(intent_keywords & p_keywords)
            # script-first 加分，使其优先于 SQL 兜底
            priority_bonus = 100 if not p.prefer_sql else 0
            candidates.append((overlap + priority_bonus, p))

    if not candidates:
        return None
    # 按得分降序排列，取最优
    candidates.sort(key=lambda x: x[0], reverse=True)
    return candidates[0][1]


def list_processors() -> list[dict]:
    return [{"name": p.name, "intents": p.intents, "prefer_sql": p.prefer_sql}
            for p in _registry.values()]
