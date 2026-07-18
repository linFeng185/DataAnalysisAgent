"""Tushare A 股 Provider 测试。"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest


class FakeResponse:
    """模拟 Tushare HTTP 响应。"""

    def __init__(self, payload: dict):
        self.payload = payload

    # 方法作用：模拟 HTTP 状态检查。
    # Args: self - 响应对象。
    # Returns: 无返回值。
    def raise_for_status(self) -> None:
        return None

    # 方法作用：返回 Tushare JSON 载荷。
    # Args: self - 响应对象。
    # Returns: Tushare 响应字典。
    def json(self) -> dict:
        return self.payload


class FakeHttpClient:
    """记录请求参数并返回预设行情。"""

    def __init__(self, payload: dict):
        self.payload = payload
        self.calls: list[dict] = []

    # 方法作用：模拟异步 POST 请求。
    # Args: self - HTTP 客户端；url - 请求地址；json - 请求载荷。
    # Returns: FakeResponse。
    async def post(self, url: str, json: dict):
        self.calls.append({"url": url, "json": json})
        return FakeResponse(self.payload)

    # 方法作用：模拟关闭 HTTP 客户端。
    # Args: self - HTTP 客户端。
    # Returns: 无返回值。
    async def aclose(self) -> None:
        return None


class TestTushareProvider:
    """覆盖日线、分钟线、实时快照和落库失败。"""

    async def test_fetch_daily_persists_before_return(self):
        """日线请求成功后必须先 upsert PostgreSQL 存储再返回。"""
        # Arrange
        from src.market.models import MarketFrequency
        from src.market.providers.tushare import TushareMarketDataProvider

        http = FakeHttpClient({
            "code": 0,
            "data": {"fields": ["ts_code", "trade_date", "open", "high", "low", "close", "vol", "amount"],
                     "items": [["000001.SZ", "20260718", 10, 11, 9, 10.5, 1000, 20000]]},
        })
        store = SimpleNamespace(upsert_bars=AsyncMock(return_value=1))
        provider = TushareMarketDataProvider("token", store=store, http_client=http)

        # Act
        bars = await provider.fetch_bars("000001.SZ", "20260718", "20260718", MarketFrequency.DAILY)

        # Assert
        assert len(bars) == 1
        assert bars[0].symbol == "000001.SZ"
        assert bars[0].close == 10.5
        store.upsert_bars.assert_awaited_once_with(bars)
        assert http.calls[0]["json"]["api_name"] == "daily"

    @pytest.mark.parametrize(
        ("frequency", "api_name"),
        [("1m", "stk_mins"), ("5m", "stk_mins"), ("realtime", "realtime_quote")],
    )
    async def test_fetch_intraday_and_realtime_map_to_tushare_api(self, frequency, api_name):
        """分钟线和实时快照应映射到对应 Tushare endpoint。"""
        # Arrange
        from src.market.models import MarketFrequency
        from src.market.providers.tushare import TushareMarketDataProvider

        http = FakeHttpClient({
            "code": 0,
            "data": {"fields": ["ts_code", "trade_time", "open", "high", "low", "close", "vol", "amount"],
                     "items": [["600000.SH", "2026-07-18 10:00:00", 10, 11, 9, 10.5, 1000, 20000]]},
        })
        store = SimpleNamespace(upsert_bars=AsyncMock(return_value=1))
        provider = TushareMarketDataProvider("token", store=store, http_client=http)

        # Act
        await provider.fetch_bars("600000.SH", "20260718", "20260718", MarketFrequency(frequency))

        # Assert
        payload = http.calls[0]["json"]
        assert payload["api_name"] == api_name
        store.upsert_bars.assert_awaited_once()

    async def test_fetch_raises_when_persistence_fails(self):
        """行情接口成功但落库失败时不能返回未持久化数据。"""
        # Arrange
        from src.market.providers.tushare import TushareMarketDataProvider, MarketProviderError

        http = FakeHttpClient({
            "code": 0,
            "data": {"fields": ["ts_code", "trade_date", "close"], "items": [["000001.SZ", "20260718", 10]]},
        })
        store = SimpleNamespace(upsert_bars=AsyncMock(side_effect=RuntimeError("db down")))
        provider = TushareMarketDataProvider("token", store=store, http_client=http)

        # Act / Assert
        with pytest.raises(MarketProviderError, match="持久化"):
            await provider.fetch_bars("000001.SZ", "20260718", "20260718")

    async def test_minute_request_includes_frequency_and_provider_name(self):
        """分钟请求应传递 Tushare 频率参数并暴露稳定 Provider 名称。"""
        # Arrange
        from src.market.models import MarketFrequency
        from src.market.providers.tushare import TushareMarketDataProvider

        http = FakeHttpClient({
            "code": 0,
            "data": {"fields": ["ts_code", "trade_time", "price"], "items": [["000001.SZ", "2026-07-18 10:00:00", 10]]},
        })
        store = SimpleNamespace(upsert_bars=AsyncMock(return_value=1))
        provider = TushareMarketDataProvider("token", store=store, http_client=http)

        # Act
        bars = await provider.fetch_bars("000001.SZ", frequency=MarketFrequency.MINUTE_1, adjustment="qfq")

        # Assert
        assert provider.name == "tushare"
        assert http.calls[0]["json"]["params"]["freq"] == "1min"
        assert bars[0].adjustment == "qfq"
