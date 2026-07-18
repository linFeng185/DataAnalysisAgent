"""Tushare A 股行情 Provider。"""

from __future__ import annotations

import inspect
from datetime import datetime, timezone
from typing import Any
from zoneinfo import ZoneInfo

import httpx

from src.logging_config import get_logger
from src.market.models import MarketBar, MarketFrequency
from src.market.providers.base import MarketDataProvider

logger = get_logger(__name__)


class MarketProviderError(RuntimeError):
    """行情请求、解析或持久化失败。"""


class TushareMarketDataProvider(MarketDataProvider):
    """通过 Tushare Pro API 获取 A 股日线、分钟线和实时行情。"""

    _ENDPOINT = "https://api.tushare.pro"
    _API_NAMES = {
        MarketFrequency.DAILY: "daily",
        MarketFrequency.MINUTE_1: "stk_mins",
        MarketFrequency.MINUTE_5: "stk_mins",
        MarketFrequency.REALTIME: "realtime_quote",
    }

    # 方法作用：初始化 Tushare 客户端和强制持久化依赖。
    # Args: token - Tushare 访问令牌；store - 行情存储；http_client - 可选异步 HTTP 客户端。
    # Returns: 无返回值。
    def __init__(self, token: str, *, store: Any, http_client: Any | None = None) -> None:
        logger.debug("初始化 Tushare provider 入口", token_configured=bool(token), store_type=type(store).__name__)
        if not token or not token.strip():
            raise ValueError("Tushare token 不能为空")
        if store is None or not hasattr(store, "upsert_bars"):
            raise ValueError("store 必须提供 upsert_bars 方法")
        self._token = token.strip()
        self._store = store
        self._http_client = http_client if http_client is not None else httpx.AsyncClient(timeout=30.0)
        self._owns_http_client = http_client is None
        logger.info("Tushare provider 初始化完成", endpoint=self._ENDPOINT)

    # 方法作用：返回 Tushare 的稳定 Provider 名称。
    # Args: self - Provider 实例。
    # Returns: tushare。
    @property
    def name(self) -> str:
        logger.debug("读取 Tushare provider 名称入口")
        logger.info("读取 Tushare provider 名称完成", provider="tushare")
        return "tushare"

    # 方法作用：请求指定粒度行情，解析后先批量持久化再返回。
    # Args: symbol - 证券代码；start - 起始日期/时间；end - 结束日期/时间；frequency - 行情粒度；adjustment - 复权标记。
    # Returns: 按时间升序排列的 MarketBar 列表。
    async def fetch_bars(
        self,
        symbol: str,
        start: str = "",
        end: str = "",
        frequency: MarketFrequency = MarketFrequency.DAILY,
        adjustment: str = "none",
    ) -> list[MarketBar]:
        logger.debug("Tushare 行情请求入口", symbol=symbol, start=start, end=end, frequency=str(frequency), adjustment=adjustment)
        if not symbol or not symbol.strip():
            raise ValueError("symbol 不能为空")
        try:
            normalized_frequency = MarketFrequency(frequency)
        except ValueError as exc:
            raise ValueError(f"不支持的行情频率: {frequency}") from exc
        api_name = self._API_NAMES[normalized_frequency]
        params = {"ts_code": symbol.strip()}
        if start:
            params["start_date"] = start
        if end:
            params["end_date"] = end
        if normalized_frequency in {MarketFrequency.MINUTE_1, MarketFrequency.MINUTE_5}:
            params["freq"] = "1min" if normalized_frequency == MarketFrequency.MINUTE_1 else "5min"
        payload = {
            "api_name": api_name,
            "token": self._token,
            "params": params,
            "fields": "",
        }
        try:
            response = await self._http_client.post(self._ENDPOINT, json=payload)
            status_check = getattr(response, "raise_for_status", None)
            if status_check is not None:
                checked = status_check()
                if inspect.isawaitable(checked):
                    await checked
            data = response.json()
            if inspect.isawaitable(data):
                data = await data
            code = data.get("code", 0)
            if code not in (0, None):
                raise MarketProviderError(f"Tushare 返回错误 code={code}: {data.get('msg', '')}")
            bars = self._parse_bars(data.get("data") or {}, normalized_frequency, adjustment)
            # 行情接口成功后必须先落库，避免调用方拿到不可复现的数据。
            try:
                await self._store.upsert_bars(bars)
            except Exception as exc:
                logger.error("Tushare 行情持久化失败", symbol=symbol, count=len(bars), error=str(exc), exc_info=True)
                raise MarketProviderError(f"行情持久化失败: {exc}") from exc
            logger.info("Tushare 行情请求完成", symbol=symbol, frequency=normalized_frequency.value, count=len(bars))
            return bars
        except MarketProviderError:
            raise
        except Exception as exc:
            logger.error("Tushare 行情请求或持久化失败", symbol=symbol, frequency=normalized_frequency.value, error=str(exc), exc_info=True)
            raise MarketProviderError(f"Tushare 行情请求失败: {exc}") from exc

    # 方法作用：把 Tushare fields/items 载荷转换为统一 MarketBar。
    # Args: data - Tushare data 对象；frequency - 已校验的行情粒度；adjustment - 复权标记。
    # Returns: 标准化行情记录列表。
    def _parse_bars(self, data: dict[str, Any], frequency: MarketFrequency, adjustment: str = "none") -> list[MarketBar]:
        logger.debug("解析 Tushare 行情入口", frequency=frequency.value, adjustment=adjustment)
        fields = list(data.get("fields") or [])
        items = list(data.get("items") or [])
        bars: list[MarketBar] = []
        for item in items:
            row = dict(zip(fields, item, strict=False))
            symbol = str(row.get("ts_code") or row.get("symbol") or "")
            if not symbol:
                logger.warning("跳过缺少证券代码的 Tushare 行情行")
                continue
            timestamp = self._parse_timestamp(row.get("trade_time") or row.get("trade_date") or row.get("datetime"))
            bars.append(MarketBar(
                symbol=symbol,
                market="cn_a",
                exchange=self._exchange_from_symbol(symbol),
                timestamp=timestamp,
                frequency=frequency,
                open=self._number(row.get("open")),
                high=self._number(row.get("high")),
                low=self._number(row.get("low")),
                close=self._number(row.get("close") if row.get("close") is not None else row.get("price")),
                volume=self._number(row.get("vol") if row.get("vol") is not None else row.get("volume")),
                amount=self._number(row.get("amount")),
                provider=self.name,
                adjustment=adjustment or "none",
                raw_payload=row,
            ))
        bars.sort(key=lambda bar: bar.timestamp)
        logger.info("解析 Tushare 行情完成", frequency=frequency.value, count=len(bars))
        return bars

    # 方法作用：解析 Tushare 日期或日期时间并补充 A 股时区。
    # Args: value - 日期字符串、datetime 或空值。
    # Returns: UTC datetime。
    @staticmethod
    def _parse_timestamp(value: Any) -> datetime:
        logger.debug("解析行情时间入口", value=str(value))
        if value is None or value == "":
            raise MarketProviderError("Tushare 行情缺少时间字段")
        if isinstance(value, datetime):
            parsed = value
        else:
            text = str(value).strip()
            if len(text) == 8 and text.isdigit():
                parsed = datetime.strptime(text, "%Y%m%d")
            else:
                parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=ZoneInfo("Asia/Shanghai"))
        result = parsed.astimezone(timezone.utc)
        logger.info("解析行情时间完成", timestamp=result.isoformat())
        return result

    # 方法作用：将可选数值字段转换为浮点数。
    # Args: value - Tushare 原始字段值。
    # Returns: 浮点数或 None。
    @staticmethod
    def _number(value: Any) -> float | None:
        logger.debug("解析行情数值入口", value=value)
        if value in (None, "", "-", "None"):
            return None
        try:
            result = float(value)
        except (TypeError, ValueError) as exc:
            raise MarketProviderError(f"行情数值无法解析: {value}") from exc
        logger.info("解析行情数值完成", value=result)
        return result

    # 方法作用：根据 Tushare 证券代码后缀识别交易所。
    # Args: symbol - Tushare 证券代码。
    # Returns: 交易所代码。
    @staticmethod
    def _exchange_from_symbol(symbol: str) -> str:
        logger.debug("识别交易所入口", symbol=symbol)
        suffix = symbol.rsplit(".", 1)[-1].upper() if "." in symbol else ""
        result = suffix if suffix in {"SZ", "SH", "BJ"} else ""
        logger.info("识别交易所完成", symbol=symbol, exchange=result)
        return result

    # 方法作用：关闭 Provider 自己创建的 HTTP 客户端。
    # Args: self - Provider 实例。
    # Returns: 无返回值。
    async def close(self) -> None:
        logger.debug("关闭 Tushare provider 入口", owns_client=self._owns_http_client)
        if self._owns_http_client:
            await self._http_client.aclose()
        logger.info("关闭 Tushare provider 完成")
