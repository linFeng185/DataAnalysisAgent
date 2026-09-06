"""G24.2 查询共享配额的确定性事务契约测试。"""

import asyncio
from contextlib import asynccontextmanager
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from fastapi import HTTPException
from pydantic import ValidationError


class QuotaDatabase:
    """仅供测试的共享数据库替身，验证多个服务实例共用事务计数。"""

    # 构造带事务锁的计数替身。
    # Args: self - 数据库实例。
    # Returns: 无返回值。
    def __init__(self) -> None:
        self.lock = asyncio.Lock()
        self.used = {}
        self.window = 100
        self.policy = {"tenant_per_hour": 4, "user_per_hour": 2}

    # 以同一把事务锁模拟多个 Worker 的数据库行锁。
    # Args: self - 数据库；kwargs - 当前请求身份。
    # Returns: 数据库连接替身。
    @asynccontextmanager
    async def connect(self, **kwargs):
        async with self.lock:
            yield self

    # 返回数据库小时窗口或租户用户已用量。
    # Args: self - 数据库；sql - SQL；args - 参数。
    # Returns: 标量查询结果。
    async def fetchval(self, sql, *args):
        if "clock_timestamp" in sql:
            return self.window
        if "SELECT used" in sql:
            return self.used.get(tuple(args), 0)
        return None

    # 返回租户软额度。
    # Args: self - 数据库；sql/args - 查询参数。
    # Returns: 软额度记录。
    async def fetchrow(self, sql, *args):
        return dict(self.policy)

    # 记录事务内计数更新，不模拟真实 PostgreSQL RLS。
    # Args: self - 数据库；sql/args - SQL 与参数。
    # Returns: 操作状态。
    async def execute(self, sql, *args):
        if "INSERT INTO query_rate_usage" in sql:
            self.used[tuple(args)] = self.used.get(tuple(args), 0) + 1
        return "OK"


class TestQueryQuota:
    """覆盖 G24.2 并发、租户隔离、边界和失败关闭。"""

    # 验证两个服务实例不能突破共享用户额度。
    # Args: self - 测试实例；monkeypatch - 配置替身。
    # Returns: 无返回值。
    async def test_workers_share_atomic_usage(self, monkeypatch) -> None:
        """十次并发请求中只有两次获准，同租户另一用户继续使用余量。"""
        from src.security import query_quota

        database = QuotaDatabase()
        monkeypatch.setattr(query_quota, "pg_connection", database.connect)
        monkeypatch.setattr(query_quota, "get_settings", lambda: SimpleNamespace(max_queries_per_hour=10))
        results = await asyncio.gather(*(query_quota.consume_query_quota(1, 2) for _ in range(10)))
        assert results.count(True) == 2
        assert await query_quota.consume_query_quota(1, 3)
        assert await query_quota.consume_query_quota(1, 3)
        assert not await query_quota.consume_query_quota(1, 4)
        assert await query_quota.consume_query_quota(2, 2)
        database.window += 1
        assert await query_quota.consume_query_quota(1, 2)

    # 验证零额度与平台硬上限收紧立即生效。
    # Args: self - 测试实例；monkeypatch - 配置替身。
    # Returns: 无返回值。
    async def test_zero_and_hard_limit(self, monkeypatch) -> None:
        """旧软额度比平台大时运行时截断，零额度拒绝且不计数。"""
        from src.security import query_quota

        database = QuotaDatabase()
        monkeypatch.setattr(query_quota, "pg_connection", database.connect)
        monkeypatch.setattr(query_quota, "get_settings", lambda: SimpleNamespace(max_queries_per_hour=1))
        assert await query_quota.consume_query_quota(1, 2)
        assert not await query_quota.consume_query_quota(1, 3)
        database.policy["user_per_hour"] = 0
        assert not await query_quota.consume_query_quota(2, 2)
        assert (2, 2, database.window) not in database.used

    # 验证配置异常和数据库异常均不放行。
    # Args: self - 测试实例；monkeypatch - 数据库故障替身。
    # Returns: 无返回值。
    async def test_invalid_input_and_storage_failure(self, monkeypatch) -> None:
        """非法身份拒绝，数据库失效向入口返回 503。"""
        from src.security import query_quota

        with pytest.raises(ValidationError):
            query_quota.QueryQuotaPolicy(tenant_per_hour=-1, user_per_hour=1)
        with pytest.raises(ValidationError):
            query_quota.QueryQuotaPolicy(tenant_per_hour=1, user_per_hour=2)
        assert not await query_quota.consume_query_quota(0, 0)
        database = QuotaDatabase()
        database.fetchval = AsyncMock(side_effect=RuntimeError("数据库不可用"))
        monkeypatch.setattr(query_quota, "pg_connection", database.connect)
        with pytest.raises(HTTPException) as error:
            await query_quota.consume_query_quota(1, 2)
        assert error.value.status_code == 503
