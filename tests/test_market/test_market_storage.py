"""行情 PostgreSQL 存储测试。"""

from __future__ import annotations

from datetime import datetime, timezone
from types import SimpleNamespace


class FakeConnection:
    """记录 SQL 批量调用。"""

    def __init__(self):
        self.executemany_calls = []
        self.execute_calls = []
        self.fetch_calls = []
        self.fetch_rows = []

    # 方法作用：记录批量 upsert。
    # Args: self - 连接；query - SQL；records - 参数序列。
    # Returns: 无返回值。
    async def executemany(self, query, records):
        self.executemany_calls.append((query, list(records)))

    # 方法作用：记录 schema SQL。
    # Args: self - 连接；query - SQL。
    # Returns: 无返回值。
    async def execute(self, query):
        self.execute_calls.append(query)

    # 方法作用：记录查询 SQL 并返回预设行。
    # Args: self - 连接；query - SQL；args - 查询参数。
    # Returns: 预设数据库行列表。
    async def fetch(self, query, *args):
        self.fetch_calls.append((query, args))
        return self.fetch_rows


class AcquireContext:
    """模拟 pool.acquire 异步上下文。"""

    def __init__(self, connection):
        self.connection = connection

    # 方法作用：进入连接上下文。
    # Args: self - 上下文对象。
    # Returns: FakeConnection。
    async def __aenter__(self):
        return self.connection

    # 方法作用：退出连接上下文。
    # Args: self - 上下文对象；exc_type - 异常类型；exc - 异常；tb - traceback。
    # Returns: False，继续传播异常。
    async def __aexit__(self, exc_type, exc, tb):
        return False


class FakePool:
    """模拟 asyncpg pool。"""

    def __init__(self, connection):
        self.connection = connection

    # 方法作用：返回异步连接上下文。
    # Args: self - 连接池。
    # Returns: AcquireContext。
    def acquire(self):
        return AcquireContext(self.connection)


class TestMarketDataStore:
    """覆盖 schema 初始化和批量 upsert 参数。"""

    async def test_upsert_bars_uses_batch_sql_and_dedup_key(self):
        """行情存储必须通过 executemany 批量写入。"""
        # Arrange
        from src.market.models import MarketBar
        from src.market.storage import MarketDataStore

        connection = FakeConnection()
        store = MarketDataStore(pool=FakePool(connection))
        bars = [MarketBar(
            symbol="000001.SZ", timestamp=datetime(2026, 7, 18, tzinfo=timezone.utc),
            frequency="1d", open=10, high=11, low=9, close=10.5,
            provider="tushare", adjustment="qfq",
        )]

        # Act
        count = await store.upsert_bars(bars)

        # Assert
        assert count == 1
        assert len(connection.executemany_calls) == 1
        query, records = connection.executemany_calls[0]
        assert "ON CONFLICT" in query
        assert records[0][0] == "000001.SZ"
        assert records[0][3] == "1d"

    async def test_ensure_schema_executes_market_migration(self):
        """schema 初始化应执行 PostgreSQL 行情迁移脚本。"""
        # Arrange
        from src.market.storage import MarketDataStore

        connection = FakeConnection()
        store = MarketDataStore(pool=FakePool(connection))

        # Act
        await store.ensure_schema()

        # Assert
        assert len(connection.execute_calls) == 1
        assert "CREATE TABLE IF NOT EXISTS market_bars" in connection.execute_calls[0]

    async def test_fetch_bars_converts_json_payload(self):
        """查询结果应转换为 MarketBar，并兼容 JSONB 字符串。"""
        # Arrange
        from datetime import datetime, timezone
        from src.market.models import MarketFrequency
        from src.market.storage import MarketDataStore

        connection = FakeConnection()
        connection.fetch_rows = [{
            "symbol": "000001.SZ", "market": "cn_a", "exchange": "SZ",
            "timestamp": datetime(2026, 7, 18, tzinfo=timezone.utc), "frequency": "1d",
            "open": 10, "high": 11, "low": 9, "close": 10.5, "volume": 1000,
            "amount": 20000, "provider": "tushare", "adjustment": "none",
            "fetched_at": datetime(2026, 7, 18, tzinfo=timezone.utc), "raw_payload": '{"close": 10.5}',
        }]
        store = MarketDataStore(pool=FakePool(connection))

        # Act
        bars = await store.fetch_bars("000001.SZ", frequency=MarketFrequency.DAILY)

        # Assert
        assert len(bars) == 1
        assert bars[0].raw_payload["close"] == 10.5
        assert connection.fetch_calls[0][1][0] == "000001.SZ"

    async def test_empty_upsert_is_noop(self):
        """空行情批次不应打开数据库连接。"""
        # Arrange
        from src.market.storage import MarketDataStore

        connection = FakeConnection()
        store = MarketDataStore(pool=FakePool(connection))

        # Act
        count = await store.upsert_bars([])

        # Assert
        assert count == 0
        assert connection.executemany_calls == []
