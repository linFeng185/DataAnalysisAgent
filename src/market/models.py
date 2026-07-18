"""统一市场行情数据模型。"""

from __future__ import annotations

from datetime import datetime, timezone
from enum import StrEnum
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, field_validator

from src.logging_config import get_logger

logger = get_logger(__name__)


class MarketFrequency(StrEnum):
    """行情时间粒度，保留字符串值方便 Provider 和数据库扩展。"""

    DAILY = "1d"
    MINUTE_1 = "1m"
    ONE_MINUTE = "1m"
    MINUTE_5 = "5m"
    FIVE_MINUTE = "5m"
    REALTIME = "realtime"


class MarketBar(BaseModel):
    """一条可追溯、可幂等写入的市场行情记录。"""

    model_config = ConfigDict(extra="ignore")

    symbol: str = Field(min_length=1)
    market: str = "cn_a"
    exchange: str = ""
    timestamp: datetime
    frequency: MarketFrequency
    open: float | None = None
    high: float | None = None
    low: float | None = None
    close: float | None = None
    volume: float | None = None
    amount: float | None = None
    provider: str = Field(min_length=1)
    adjustment: str = "none"
    fetched_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    raw_payload: dict[str, Any] = Field(default_factory=dict)

    # 方法作用：统一行情时间为带时区时间，避免跨市场比较时发生歧义。
    # Args: value - 原始时间值。
    # Returns: 带时区的 datetime。
    @field_validator("timestamp", "fetched_at")
    @classmethod
    def _ensure_timezone(cls, value: datetime) -> datetime:
        logger.debug("行情时间校验入口", value=str(value))
        if value.tzinfo is None:
            value = value.replace(tzinfo=timezone.utc)
        result = value.astimezone(timezone.utc)
        logger.info("行情时间校验完成", timestamp=result.isoformat())
        return result

    # 方法作用：校验价格字段的有限性，防止 NaN/Infinity 进入持久化层。
    # Args: value - 可选价格值。
    # Returns: 原始价格值。
    @field_validator("open", "high", "low", "close", "volume", "amount")
    @classmethod
    def _ensure_finite(cls, value: float | None) -> float | None:
        logger.debug("行情数值校验入口", value=value)
        if value is not None and (value != value or value in (float("inf"), float("-inf"))):
            raise ValueError("行情数值必须为有限数")
        logger.info("行情数值校验完成", value=value)
        return value

    # 方法作用：生成 asyncpg executemany 使用的稳定字段顺序记录。
    # Args: self - 行情记录。
    # Returns: 可绑定到 PostgreSQL 参数的元组。
    def to_record(self) -> tuple[Any, ...]:
        logger.debug("行情记录序列化入口", symbol=self.symbol, frequency=self.frequency.value)
        result = (
            self.symbol,
            self.market,
            self.exchange,
            self.frequency.value,
            self.timestamp,
            self.open,
            self.high,
            self.low,
            self.close,
            self.volume,
            self.amount,
            self.provider,
            self.adjustment,
            self.fetched_at,
            self.raw_payload,
        )
        logger.info("行情记录序列化完成", symbol=self.symbol)
        return result
