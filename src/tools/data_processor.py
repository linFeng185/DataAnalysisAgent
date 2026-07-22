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
from decimal import Decimal, InvalidOperation

from src.logging_config import get_logger

logger = get_logger(__name__)
D = Decimal


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
        except (InvalidOperation, TypeError, ValueError) as exc:
            logger.warning("数值转换失败，回退为零", value_type=type(val).__name__, error=str(exc))
            return D(0)

    @staticmethod
    def _s(val, d="") -> str: return str(val) if val is not None else d

    @staticmethod
    def _group(rows, kc, vc) -> dict[str, list[Decimal]]:
        g: dict[str, list[Decimal]] = {}
        for r in rows:
            k = DataProcessor._s(r.get(kc))
            v = DataProcessor._f(r.get(vc))
            if k:
                g.setdefault(k, []).append(v)
        return g

    @staticmethod
    def _pct(sv, p):
        if not sv:
            return D(0)
        sv_sorted = sorted(sv)
        idx = int(len(sv_sorted) * p)
        return sv_sorted[min(idx, len(sv_sorted) - 1)]

    @staticmethod
    def _std(vals):
        if len(vals) < 2:
            return D(0)
        m = sum(vals) / len(vals)
        return D(str(math.sqrt(float(sum((v - m) ** 2 for v in vals)) / len(vals))))


_registry: dict[str, DataProcessor] = {}


# 方法作用：实例化并登记处理器类，避免注册阶段只保留类对象而丢失运行结果。
# Args: p - 实现 DataProcessor 契约的处理器类。
# Returns: 原处理器类，保持装饰器替换兼容。
def register(p: DataProcessor) -> DataProcessor:
    """注册处理器类并保存单例实例，供分析节点复用。"""
    logger.debug("注册数据处理器入口", processor=getattr(p, "name", ""))
    instance = p()
    _registry[instance.name] = instance
    logger.info("注册数据处理器完成", processor=instance.name, total=len(_registry))
    return p


_QUERY_PROCESSOR_HINTS: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("yoy", ("同比", "year-over-year", "yoy")),
    ("mom", ("环比", "month-over-month", "mom")),
    ("ab_test", ("a/b", "ab测试", "广告版本", "不同版本")),
    ("correlation", ("相关性", "相关系数", "相关关系", "correlation")),
    ("rfm", ("rfm", "最近消费", "频次和金额")),
    ("funnel", ("漏斗", "转化漏斗", "转化率")),
    ("market_basket", ("一起购买", "关联规则", "购物篮", "共现")),
    ("budget_variance", ("预算", "预算执行", "预算偏差")),
    ("geo_distribution", ("地域分布", "各省", "全国各省", "地区分布")),
    ("cross_pivot", ("交叉分析", "交叉透视", "透视表")),
    ("pareto", ("帕累托", "80/20", "贡献了80", "累计占比")),
    ("retention", ("留存", "回访比例")),
    ("contribution", ("贡献度", "贡献百分比", "贡献")),
    ("seasonal", ("季节性", "季节规律", "季节分解")),
    ("growth_rate", ("增长率", "复合增长", "cagr")),
    ("prediction", ("预测", "预测未来", "外推")),
    ("distribution", ("分布", "分桶", "区间")),
    ("ranking", ("排名", "top", "最高的", "最低的")),
    ("proportion", ("占比", "比例")),
    ("anomaly", ("异常", "异常值", "离群")),
    ("trend", ("趋势", "走势", "变化")),
    ("aggregation", ("汇总", "统计", "合计", "总数", "平均")),
)


# 方法作用：按专用业务关键词和粗粒度意图选择确定性处理器。
# Args: intent - classify_intent 输出的粗粒度意图；query - 原始用户问题。
# Returns: 匹配的处理器实例；没有候选时返回 None。
def get_processor(intent: str, *, query: str = "") -> DataProcessor | None:
    """优先按用户问题选择专用处理器，再回退到 intent 的脚本优先策略。"""
    normalized_intent = str(intent or "").strip().lower()
    normalized_query = str(query or "").strip().lower()
    logger.debug(
        "获取数据处理器入口",
        intent=normalized_intent,
        query=normalized_query[:100],
    )

    if normalized_intent in _registry and not normalized_query:
        result = _registry[normalized_intent]
        logger.info("按处理器名称命中", processor=result.name)
        return result

    if normalized_query:
        for processor_name, hints in _QUERY_PROCESSOR_HINTS:
            if processor_name in _registry and any(hint in normalized_query for hint in hints):
                result = _registry[processor_name]
                logger.info(
                    "按问题关键词命中处理器",
                    processor=result.name,
                    intent=normalized_intent,
                )
                return result

    # 分解 intent 中的关键词，保留旧调用方的 script-first 回退行为。
    intent_keywords = set(normalized_intent.replace(",", " ").split())
    candidates: list[tuple[int, DataProcessor]] = []

    for p in _registry.values():
        if normalized_intent in p.intents:
            # 计算匹配得分：processor.intents 中与 intent 重叠的关键词数
            p_keywords = set()
            for pi in p.intents:
                p_keywords.update(pi.lower().replace(",", " ").split())
            overlap = len(intent_keywords & p_keywords)
            # script-first 加分，使其优先于 SQL 兜底
            priority_bonus = 100 if not p.prefer_sql else 0
            candidates.append((overlap + priority_bonus, p))

    if not candidates:
        logger.info("获取数据处理器未命中", intent=normalized_intent)
        return None
    # 按得分降序排列，取最优
    candidates.sort(key=lambda x: x[0], reverse=True)
    result = candidates[0][1]
    logger.info("按意图回退处理器完成", processor=result.name, intent=normalized_intent)
    return result


def list_processors() -> list[dict]:
    return [{"name": p.name, "intents": p.intents, "prefer_sql": p.prefer_sql}
            for p in _registry.values()]
