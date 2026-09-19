"""遗留运行租约回收测试（复现 2026-09-16 事故：并发槽被死进程永久占住）。"""

from __future__ import annotations

from typing import Any


class _FakeConnection:
    """记录 SQL 的假连接：只验证回收逻辑发出的语句与扣减口径。"""

    # 方法作用：初始化假连接并准备语句记录。
    # Args: self - 实例；reclaimed - fetch 应返回的回收行。
    # Returns: 无返回值。
    def __init__(self, reclaimed: list[dict[str, Any]] | None = None) -> None:
        self.reclaimed = list(reclaimed or [])
        self.fetch_calls: list[tuple[str, tuple]] = []
        self.execute_calls: list[tuple[str, tuple]] = []

    # 方法作用：模拟事务上下文。
    # Args: self - 实例。
    # Returns: 空上下文管理器，供 async with 使用。
    def transaction(self) -> Any:
        class _Ctx:
            async def __aenter__(_self) -> None:
                return None

            async def __aexit__(_self, *args: Any) -> None:
                return None

        return _Ctx()

    # 方法作用：记录 UPDATE ... RETURNING 语句并返回预置的回收行。
    # Args: self - 实例；sql - SQL 文本；*params - 绑定参数。
    # Returns: 回收行列表。
    async def fetch(self, sql: str, *params: Any) -> list[dict[str, Any]]:
        self.fetch_calls.append((sql, params))
        return self.reclaimed

    # 方法作用：记录扣减语句。
    # Args: self - 实例；sql - SQL 文本；*params - 绑定参数。
    # Returns: 无返回值。
    async def execute(self, sql: str, *params: Any) -> None:
        self.execute_calls.append((sql, params))


class _FakePool:
    """返回同一个假连接的假连接池。"""

    # 方法作用：保存假连接。
    # Args: self - 实例；connection - 假连接。
    # Returns: 无返回值。
    def __init__(self, connection: _FakeConnection) -> None:
        self.connection = connection

    # 方法作用：模拟连接池借用。
    # Args: self - 实例。
    # Returns: 异步上下文管理器，产出假连接。
    def acquire(self) -> Any:
        connection = self.connection

        class _Ctx:
            async def __aenter__(_self) -> _FakeConnection:
                return connection

            async def __aexit__(_self, *args: Any) -> None:
                return None

        return _Ctx()


class TestStaleLeaseReclaim:
    """覆盖超时租约的识别、状态收尾与并发计数扣减。"""

    # 方法作用：验证内存分支不做任何回收（内存模式没有跨进程遗留租约）。
    async def test_memory_store_is_noop(self) -> None:
        # Arrange
        from src.reporting.store import ReportStore

        store = ReportStore(memory=True)

        # Act
        reclaimed = await store.reclaim_stale_run_leases()

        # Assert
        assert reclaimed == 0

    # 方法作用：验证没有遗留运行时不多写一条扣减语句。
    async def test_no_stale_run_writes_nothing(self) -> None:
        # Arrange
        from src.reporting.store import ReportStore

        connection = _FakeConnection(reclaimed=[])
        store = ReportStore(pg_pool=_FakePool(connection))

        # Act
        reclaimed = await store.reclaim_stale_run_leases()

        # Assert：连接上会先跑 set_config，扣减语句必须一条都没有。
        assert reclaimed == 0
        assert connection.fetch_calls
        assert [sql for sql, _ in connection.execute_calls if "concurrent_runs" in sql] == []

    # 方法作用：验证回收一个遗留运行会同时收尾状态并扣减一个并发槽。
    async def test_reclaims_run_and_decrements_concurrent(self) -> None:
        """实机事故：两个运行停在上次进程退出前后，concurrent_runs 顶到上限 2。"""
        # Arrange
        from src.reporting.store import ReportStore

        connection = _FakeConnection(reclaimed=[
            {"tenant_id": 3, "user_id": 3, "quota_period_start": "2026-08-31T16:00:00+00:00"},
        ])
        store = ReportStore(pg_pool=_FakePool(connection))

        # Act
        reclaimed = await store.reclaim_stale_run_leases(stale_after_seconds=1800)

        # Assert
        assert reclaimed == 1
        update_sql = connection.fetch_calls[0][0]
        # 只回收 queued/running 且仍持有租约的行，且必须写终态与释放标记。
        assert "status IN ('queued','running')" in update_sql
        assert "quota_held IS TRUE" in update_sql
        assert "quota_held=FALSE" in update_sql
        assert "finished_at=NOW()" in update_sql
        assert connection.fetch_calls[0][1] == (1800,)
        decrements = [(sql, params) for sql, params in connection.execute_calls if "concurrent_runs" in sql]
        assert len(decrements) == 1
        decrement_sql, decrement_params = decrements[0]
        assert "concurrent_runs=GREATEST(0, concurrent_runs-1)" in decrement_sql
        assert decrement_params == (3, 3, "2026-08-31T16:00:00+00:00")

    # 方法作用：验证租约周期缺失时只收尾状态、不盲扣并发计数。
    async def test_missing_period_skips_decrement(self) -> None:
        """早于租约列上线的运行没有周期起点，扣到别的周期上会误伤当期额度。"""
        # Arrange
        from src.reporting.store import ReportStore

        connection = _FakeConnection(reclaimed=[
            {"tenant_id": 3, "user_id": 3, "quota_period_start": None},
        ])
        store = ReportStore(pg_pool=_FakePool(connection))

        # Act
        reclaimed = await store.reclaim_stale_run_leases()

        # Assert
        assert reclaimed == 1
        assert [sql for sql, _ in connection.execute_calls if "concurrent_runs" in sql] == []

    # 方法作用：验证阈值下限被夹住，避免调用方传 0 把正在跑的运行回收掉。
    async def test_threshold_is_floored(self) -> None:
        # Arrange
        from src.reporting.store import ReportStore

        connection = _FakeConnection(reclaimed=[])
        store = ReportStore(pg_pool=_FakePool(connection))

        # Act
        await store.reclaim_stale_run_leases(stale_after_seconds=0)

        # Assert
        assert connection.fetch_calls[0][1] == (60,)


class TestReclaimWiring:
    """覆盖回收动作被接入运行入口。"""

    # 方法作用：验证报表运行在预占配额前会先回收遗留租约。
    def test_run_job_reclaims_before_reserving(self) -> None:
        # Arrange
        import inspect

        from src.reporting import runner as runner_module

        source = inspect.getsource(runner_module.ReportRunner.run_job)

        # Assert：调用顺序必须是「先回收、再预占」，否则并发槽仍会被卡住。
        assert "reclaim_stale_run_leases" in source
        assert source.index("reclaim_stale_run_leases") < source.index("reserve_run_quota")
