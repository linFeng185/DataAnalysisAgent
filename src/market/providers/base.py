"""市场数据 Provider 抽象契约。"""

from __future__ import annotations

from abc import ABC, abstractmethod
from datetime import datetime

from src.market.models import MarketBar, MarketFrequency
from src.logging_config import get_logger

logger = get_logger(__name__)


class MarketDataProvider(ABC):
    """跨市场行情数据源接口，具体 Provider 不应暴露原始响应给上层。"""

    # 方法作用：返回 Provider 稳定名称，用于审计、去重和模型卡。
    # Args: self - Provider 实例。
    # Returns: Provider 名称。
    @property
    @abstractmethod
    def name(self) -> str:
        logger.debug("读取市场 provider 名称入口")
        raise NotImplementedError

    # 方法作用：按证券和时间条件拉取行情，并在返回前完成持久化。
    # Args: symbol - 证券代码；start - 起始时间；end - 结束时间；frequency - 行情粒度。
    # Returns: 标准化行情记录列表。
    @abstractmethod
    async def fetch_bars(
        self,
        symbol: str,
        start: str = "",
        end: str = "",
        frequency: MarketFrequency = MarketFrequency.DAILY,
    ) -> list[MarketBar]:
        logger.debug("拉取市场行情入口", symbol=symbol, start=start, end=end, frequency=str(frequency))
        raise NotImplementedError

    # 方法作用：兼容已有 MarketDataProvider.fetch 调用，并转换为统一行情记录。
    # Args: self - Provider 实例；symbol - 证券代码；start - 起始时间；end - 结束时间。
    # Returns: 标准化行情记录列表。
    async def fetch(self, symbol: str, start: str = "", end: str = "") -> list[MarketBar]:
        logger.debug("兼容行情 fetch 入口", symbol=symbol, start=start, end=end)
        result = await self.fetch_bars(symbol, start, end, MarketFrequency.DAILY)
        logger.info("兼容行情 fetch 完成", symbol=symbol, count=len(result))
        return result
