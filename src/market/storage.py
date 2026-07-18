"""行情 PostgreSQL 持久化和查询。"""

from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path
from typing import Any

from src.logging_config import get_logger
from src.market.models import MarketBar, MarketFrequency

logger = get_logger(__name__)


class MarketDataStore:
    """使用 PostgreSQL 批量 upsert 行情，按证券、频率和时间建立查询索引。"""

    # 方法作用：初始化行情存储，可注入连接池以便测试或复用全局连接池。
    # Args: pool - asyncpg 连接池；table_name - 行情表名。
    # Returns: 无返回值。
    def __init__(self, pool: Any | None = None, table_name: str = "market_bars") -> None:
        logger.debug("初始化行情存储入口", pool_type=type(pool).__name__ if pool else "lazy", table=table_name)
        if not table_name.replace("_", "").isalnum():
            raise ValueError("table_name 只能包含字母、数字和下划线")
        self._pool = pool
        self._table_name = table_name
        self._owns_pool = False
        logger.info("初始化行情存储完成", table=table_name)

    # 方法作用：按需获取 PostgreSQL 连接池。
    # Args: self - 行情存储。
    # Returns: asyncpg 连接池。
    async def _get_pool(self) -> Any:
        logger.debug("获取行情连接池入口", injected=self._pool is not None)
        if self._pool is None:
            from src.memory.pg_pool import get_pg_pool

            self._pool = await get_pg_pool()
            self._owns_pool = False
        logger.info("获取行情连接池完成")
        return self._pool

    # 方法作用：创建行情表、唯一约束和时间查询索引。
    # Args: self - 行情存储。
    # Returns: 无返回值。
    async def ensure_schema(self) -> None:
        logger.debug("初始化行情 schema 入口", table=self._table_name)
        pool = await self._get_pool()
        migration_path = Path(__file__).resolve().parents[2] / "migrations" / "002_market_data.sql"
        sql = migration_path.read_text(encoding="utf-8")
        async with pool.acquire() as connection:
            await connection.execute(sql)
        logger.info("初始化行情 schema 完成", table=self._table_name)

    # 方法作用：批量写入行情并按唯一键更新最新抓取内容。
    # Args: bars - 待写入行情记录。
    # Returns: 实际处理的记录数量。
    async def upsert_bars(self, bars: list[MarketBar]) -> int:
        logger.debug("行情批量 upsert 入口", count=len(bars))
        if not bars:
            logger.info("行情批量 upsert 完成", count=0)
            return 0
        pool = await self._get_pool()
        query = f"""
            INSERT INTO {self._table_name} (
                symbol, market, exchange, frequency, timestamp,
                open, high, low, close, volume, amount,
                provider, adjustment, fetched_at, raw_payload
            ) VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11, $12, $13, $14, $15::jsonb)
            ON CONFLICT (symbol, timestamp, frequency, adjustment, provider)
            DO UPDATE SET
                market = EXCLUDED.market,
                exchange = EXCLUDED.exchange,
                open = EXCLUDED.open,
                high = EXCLUDED.high,
                low = EXCLUDED.low,
                close = EXCLUDED.close,
                volume = EXCLUDED.volume,
                amount = EXCLUDED.amount,
                fetched_at = EXCLUDED.fetched_at,
                raw_payload = EXCLUDED.raw_payload
        """
        records = [self._record_for_asyncpg(bar) for bar in bars]
        async with pool.acquire() as connection:
            await connection.executemany(query, records)
        logger.info("行情批量 upsert 完成", count=len(records))
        return len(records)

    # 方法作用：查询指定证券、频率和时间范围的行情。
    # Args: symbol - 证券代码；start - 起始时间；end - 结束时间；frequency - 粒度；adjustment - 复权；limit - 最大行数。
    # Returns: 按时间升序排列的 MarketBar 列表。
    async def fetch_bars(
        self,
        symbol: str,
        start: datetime | None = None,
        end: datetime | None = None,
        frequency: MarketFrequency = MarketFrequency.DAILY,
        adjustment: str | None = None,
        limit: int = 10000,
    ) -> list[MarketBar]:
        logger.debug("查询行情入口", symbol=symbol, start=str(start), end=str(end), frequency=str(frequency))
        if limit <= 0:
            raise ValueError("limit 必须大于零")
        normalized_frequency = MarketFrequency(frequency).value
        pool = await self._get_pool()
        conditions = ["symbol = $1", "frequency = $2"]
        args: list[Any] = [symbol, normalized_frequency]
        if start is not None:
            args.append(start)
            conditions.append(f"timestamp >= ${len(args)}")
        if end is not None:
            args.append(end)
            conditions.append(f"timestamp <= ${len(args)}")
        if adjustment is not None:
            args.append(adjustment)
            conditions.append(f"adjustment = ${len(args)}")
        args.append(limit)
        query = f"SELECT symbol, market, exchange, timestamp, frequency, open, high, low, close, volume, amount, provider, adjustment, fetched_at, raw_payload FROM {self._table_name} WHERE {' AND '.join(conditions)} ORDER BY timestamp ASC LIMIT ${len(args)}"
        async with pool.acquire() as connection:
            rows = await connection.fetch(query, *args)
        result = [self._bar_from_row(row) for row in rows]
        logger.info("查询行情完成", symbol=symbol, count=len(result))
        return result

    # 方法作用：兼容常见的 list_bars 调用命名。
    # Args: self - 行情存储；其余参数 - 查询条件。
    # Returns: 行情记录列表。
    async def list_bars(self, *args: Any, **kwargs: Any) -> list[MarketBar]:
        logger.debug("兼容 list_bars 入口")
        result = await self.fetch_bars(*args, **kwargs)
        logger.info("兼容 list_bars 完成", count=len(result))
        return result

    # 方法作用：兼容 query_bars 调用命名，保持查询接口向后兼容。
    # Args: self - 行情存储；其余参数 - 查询条件。
    # Returns: 行情记录列表。
    async def query_bars(self, *args: Any, **kwargs: Any) -> list[MarketBar]:
        logger.debug("兼容 query_bars 入口")
        result = await self.fetch_bars(*args, **kwargs)
        logger.info("兼容 query_bars 完成", count=len(result))
        return result

    # 方法作用：关闭由存储对象创建的连接池。
    # Args: self - 行情存储。
    # Returns: 无返回值。
    async def close(self) -> None:
        logger.debug("关闭行情存储入口", owns_pool=self._owns_pool)
        if self._owns_pool and self._pool is not None:
            await self._pool.close()
            self._pool = None
        logger.info("关闭行情存储完成")

    # 方法作用：把 Pydantic 行情模型转换为 asyncpg 可接受的 JSONB 记录。
    # Args: bar - 行情记录。
    # Returns: 批量写入元组。
    @staticmethod
    def _record_for_asyncpg(bar: MarketBar) -> tuple[Any, ...]:
        logger.debug("构造行情数据库记录入口", symbol=bar.symbol)
        record = bar.to_record()
        mutable = list(record)
        mutable[-1] = json.dumps(mutable[-1], ensure_ascii=False, default=str)
        result = tuple(mutable)
        logger.info("构造行情数据库记录完成", symbol=bar.symbol)
        return result

    # 方法作用：将数据库行字段映射为 MarketBar，并兼容 JSONB 驱动返回字符串的情况。
    # Args: row - asyncpg 返回的映射行。
    # Returns: 标准行情模型。
    @staticmethod
    def _bar_from_row(row: Any) -> MarketBar:
        logger.debug("数据库行情行转换入口")
        data = dict(row)
        payload = data.get("raw_payload")
        if isinstance(payload, str):
            try:
                data["raw_payload"] = json.loads(payload)
            except json.JSONDecodeError:
                data["raw_payload"] = {}
        result = MarketBar.model_validate(data)
        logger.info("数据库行情行转换完成", symbol=result.symbol)
        return result
