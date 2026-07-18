"""行情数据契约和基础风险指标。"""

from __future__ import annotations

import math
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any

from src.logging_config import get_logger

logger = get_logger(__name__)


class MarketAnalysisError(ValueError):
    """行情数据不满足时序或价格约束时抛出的异常。"""


@dataclass(frozen=True)
class MarketPoint:
    """带 provider 可追溯信息的单个行情点。"""

    symbol: str
    timestamp: str
    close: float
    volume: float | None = None
    exchange: str = ""
    timezone: str = "UTC"


@dataclass
class MarketMetrics:
    """行情描述性指标和数据许可/复权元数据。"""

    symbol: str
    provider: str
    as_of: str
    total_return: float
    volatility_annualized: float
    max_drawdown: float
    sharpe: float
    adjustment: str
    observations: int
    warnings: list[str] = field(default_factory=list)

    # 方法作用：将行情指标转换成可写入分析产物的字典。
    # Args: self - 行情指标对象。
    # Returns: JSON 兼容的指标字典。
    def to_dict(self) -> dict[str, Any]:
        logger.debug("行情指标序列化入口", symbol=self.symbol)
        result = {
            "symbol": self.symbol,
            "provider": self.provider,
            "as_of": self.as_of,
            "total_return": self.total_return,
            "volatility_annualized": self.volatility_annualized,
            "max_drawdown": self.max_drawdown,
            "sharpe": self.sharpe,
            "adjustment": self.adjustment,
            "observations": self.observations,
            "warnings": list(self.warnings),
        }
        logger.info("行情指标序列化完成", symbol=self.symbol)
        return result


class MarketDataProvider(ABC):
    """行情 provider 抽象，强制调用方显式处理时点和复权信息。"""

    # 方法作用：返回 provider 的稳定名称用于审计和模型卡。
    # Args: self - provider 实例。
    # Returns: provider 名称。
    @property
    @abstractmethod
    def name(self) -> str:
        """返回 provider 名称。"""
        ...

    # 方法作用：按证券和时间范围读取行情点。
    # Args: symbol - 证券代码；start - 起始时间；end - 结束时间。
    # Returns: 按时间升序排列的 MarketPoint 列表。
    @abstractmethod
    async def fetch(self, symbol: str, start: str = "", end: str = "") -> list[MarketPoint]:
        """读取行情数据。"""
        ...


class InMemoryMarketDataProvider(MarketDataProvider):
    """用于测试和离线回放的确定性行情 provider。"""

    # 方法作用：初始化离线行情点并按 symbol 分组。
    # Args: points - 行情点列表。
    # Returns: 无返回值。
    def __init__(self, points: list[MarketPoint], provider_name: str = "memory") -> None:
        logger.debug("初始化内存行情 provider 入口", points=len(points), provider=provider_name)
        self._points = list(points)
        self._provider_name = provider_name
        logger.info("初始化内存行情 provider 完成", points=len(self._points))

    # 方法作用：返回内存 provider 名称。
    # Args: self - provider 实例。
    # Returns: provider 名称。
    @property
    def name(self) -> str:
        logger.debug("读取行情 provider 名称入口")
        logger.info("读取行情 provider 名称完成", provider=self._provider_name)
        return self._provider_name

    # 方法作用：按 symbol 和可选时间边界过滤离线行情点。
    # Args: symbol - 证券代码；start - 起始 ISO 时间；end - 结束 ISO 时间。
    # Returns: 过滤后的升序行情点列表。
    async def fetch(self, symbol: str, start: str = "", end: str = "") -> list[MarketPoint]:
        logger.debug("读取内存行情入口", symbol=symbol, start=start, end=end)
        points = [point for point in self._points if point.symbol == symbol
                  and (not start or point.timestamp >= start)
                  and (not end or point.timestamp <= end)]
        points.sort(key=lambda point: point.timestamp)
        logger.info("读取内存行情完成", symbol=symbol, count=len(points))
        return points


# 方法作用：计算价格收益、年化波动率、最大回撤和 Sharpe 等基础指标。
# Args: points - 同一 symbol 且按时间升序的 MarketPoint；provider - 数据提供方名称；adjustment - 复权方式。
# Returns: 带 as_of 和限制说明的 MarketMetrics。
def compute_market_metrics(points: list[MarketPoint], provider: str = "unknown",
                           adjustment: str = "unadjusted") -> MarketMetrics:
    logger.debug("行情指标计算入口", points=len(points), provider=provider, adjustment=adjustment)
    if not points:
        raise MarketAnalysisError("行情数据不能为空")
    symbols = {point.symbol for point in points}
    if len(symbols) != 1:
        raise MarketAnalysisError("行情指标必须来自同一 symbol")
    ordered = sorted(points, key=lambda point: point.timestamp)
    if any(point.close <= 0 for point in ordered):
        raise MarketAnalysisError("收盘价必须为正数")
    if any(ordered[index].timestamp == ordered[index + 1].timestamp
           for index in range(len(ordered) - 1)):
        raise MarketAnalysisError("行情时间戳不能重复")
    returns = [ordered[index].close / ordered[index - 1].close - 1
               for index in range(1, len(ordered))]
    total_return = ordered[-1].close / ordered[0].close - 1
    volatility = _std(returns) * math.sqrt(252) if len(returns) > 1 else 0.0
    mean_return = sum(returns) / len(returns) if returns else 0.0
    sharpe = mean_return / _std(returns) * math.sqrt(252) if _std(returns) else 0.0
    peak = ordered[0].close
    drawdowns: list[float] = []
    for point in ordered:
        peak = max(peak, point.close)
        drawdowns.append(point.close / peak - 1)
    result = MarketMetrics(
        symbol=ordered[0].symbol,
        provider=provider,
        as_of=ordered[-1].timestamp,
        total_return=round(total_return, 8),
        volatility_annualized=round(volatility, 8),
        max_drawdown=round(min(drawdowns), 8),
        sharpe=round(sharpe, 8),
        adjustment=adjustment,
        observations=len(ordered),
        warnings=["分析信息不构成投资建议", "指标依赖 provider 数据许可、延迟和复权口径"],
    )
    logger.info("行情指标计算完成", symbol=result.symbol, as_of=result.as_of,
                observations=result.observations)
    return result


# 方法作用：计算样本标准差。
# Args: values - 数值列表。
# Returns: 样本标准差；样本不足时返回零。
def _std(values: list[float]) -> float:
    if len(values) < 2:
        return 0.0
    mean = sum(values) / len(values)
    return math.sqrt(sum((value - mean) ** 2 for value in values) / (len(values) - 1))
