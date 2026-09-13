"""需求 8：表结构同步并发满时排队、FIFO 认领、队列位置与排队中终止。"""

import asyncio
from contextlib import asynccontextmanager
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from fastapi import HTTPException

import src.knowledge.schema_sync as sync_module
from src.datasource.schema_snapshot import SchemaSnapshot, TableSchema


# 绑定无外部依赖的数据库连接，保留异常传播。
# Args: connection - 可断言 SQL 的替身连接。
# Returns: 可替换 pg_connection 的上下文工厂。
def connection_factory(connection):
    # 暴露同一测试连接，供被测方法记录执行过的语句。
    # Args: kwargs - 请求身份。
    # Returns: 测试连接。
    @asynccontextmanager
    async def connect(**kwargs):
        yield connection
    return connect


class FakeConnection:
    """按调用顺序返回预置结果的最小数据库替身。"""

    # 保存预置的查询结果与已执行语句。
    # Args: self - 实例；rows - fetchrow 结果序列；values - fetchval 结果序列。
    # Returns: 无返回值。
    def __init__(self, rows: list | None = None, values: list | None = None) -> None:
        self.rows = list(rows or [])
        self.values = list(values or [])
        self.statements: list[tuple[str, str, tuple]] = []

    # 按顺序弹出下一条单行查询结果。
    # Args: self - 实例；sql/args - 语句与参数。
    # Returns: 预置的行或 None。
    async def fetchrow(self, sql: str, *args):
        self.statements.append(("fetchrow", sql, args))
        return self.rows.pop(0) if self.rows else None

    # 按顺序弹出下一条单值查询结果。
    # Args: self - 实例；sql/args - 语句与参数。
    # Returns: 预置的值或 None。
    async def fetchval(self, sql: str, *args):
        self.statements.append(("fetchval", sql, args))
        return self.values.pop(0) if self.values else None

    # 记录写入语句。
    # Args: self - 实例；sql/args - 语句与参数。
    # Returns: 无返回值。
    async def execute(self, sql: str, *args):
        self.statements.append(("execute", sql, args))

    # 取出已执行语句文本，便于断言分支是否命中。
    # Args: self - 实例；kind - 可选语句类型过滤。
    # Returns: 语句文本列表。
    def sql(self, kind: str | None = None) -> list[str]:
        return [sql for item_kind, sql, _args in self.statements if kind is None or item_kind == kind]


class FakeQueueStore:
    """内存排队存储：模拟排队行、FIFO 认领、取消与崩溃回收语义。"""

    # 初始化队列、互斥与认领轨迹。
    # Args: self - 实例。
    # Returns: 无返回值。
    def __init__(self) -> None:
        self.rows: dict[tuple[int, str], dict] = {}
        self.locked: set[tuple[int, str]] = set()
        self.claimed: list[str] = []
        self.sequence = 0

    # 模拟会话级互斥：已持有时抛 409，与生产实现一致。
    # Args: self - 实例；tenant/name - 数据源作用域。
    # Returns: 已持锁的模拟租约。
    @asynccontextmanager
    async def owner(self, tenant: int, name: str):
        key = (tenant, name)
        if key in self.locked:
            raise HTTPException(409, "该数据源正在同步，请等待或终止现有任务")
        self.locked.add(key)
        try:
            yield SimpleNamespace(fetchval=AsyncMock(return_value=1))
        finally:
            self.locked.discard(key)

    # 读取共享状态，排队行附带同租户队列位置。
    # Args: self - 实例；tenant/name - 数据源作用域。
    # Returns: 状态副本，无行时返回 idle。
    async def state(self, tenant: int, name: str) -> dict:
        row = self.rows.get((tenant, name))
        if row is None:
            return {"datasource": name, "status": "idle", "queue_position": None}
        position = None
        if row["status"] == "queued":
            position = sum(
                1 for (other_tenant, _other_name), other in self.rows.items()
                if other_tenant == tenant and other["status"] == "queued" and other["started_at"] < row["started_at"]
            )
        return {"datasource": name, **row, "queue_position": position}

    # 写入运行中状态。
    # Args: self - 实例；tenant/name/run_id - 任务键。
    # Returns: 无返回值。
    async def begin(self, tenant: int, name: str, run_id: str) -> None:
        self.rows[(tenant, name)] = {"status": "running", "phase": "collecting", "run_id": run_id, "started_at": 0}

    # 写入排队行并按入队顺序分配时间序号。
    # Args: self - 实例；tenant/name/run_id - 任务键。
    # Returns: 无返回值。
    async def enqueue(self, tenant: int, name: str, run_id: str) -> None:
        self.sequence += 1
        self.rows[(tenant, name)] = {"status": "queued", "phase": "queued", "run_id": run_id, "started_at": self.sequence}

    # 按先进先出返回待认领的排队行。
    # Args: self - 实例；limit - 最多返回条数。
    # Returns: 任务定位列表。
    async def queued(self, limit: int) -> list[dict]:
        candidates = [
            (key, row) for key, row in self.rows.items() if row["status"] == "queued"
        ]
        candidates.sort(key=lambda item: item[1]["started_at"])
        return [
            {"tenant_id": key[0], "datasource": key[1], "run_id": row["run_id"]}
            for key, row in candidates[:limit]
        ]

    # 原子认领：代次或状态不匹配时拒绝，模拟不复活已取消任务。
    # Args: self - 实例；tenant/name/run_id - 任务键。
    # Returns: 是否认领成功。
    async def claim(self, tenant: int, name: str, run_id: str) -> bool:
        row = self.rows.get((tenant, name))
        if row is None or row["status"] != "queued" or row["run_id"] != run_id:
            return False
        row.update(status="running", phase="collecting")
        self.claimed.append(name)
        return True

    # 返回当前状态作为取消信号。
    # Args: self - 实例；tenant/name/run_id - 任务键。
    # Returns: 当前状态。
    async def heartbeat(self, tenant: int, name: str, run_id: str) -> dict:
        return dict(self.rows[(tenant, name)])

    # 模拟进入发布阶段。
    # Args: self - 实例；tenant/name/run_id - 任务键。
    # Returns: 是否允许发布。
    async def publishing(self, tenant: int, name: str, run_id: str) -> bool:
        row = self.rows[(tenant, name)]
        if row["status"] != "running":
            return False
        row["phase"] = "publishing"
        return True

    # 记录已发布快照。
    # Args: self - 实例；tenant/name/run_id - 任务键；snapshot/fingerprint - 快照与摘要。
    # Returns: 无返回值。
    async def publish(self, tenant: int, name: str, run_id: str, snapshot: SchemaSnapshot, fingerprint: str) -> None:
        self.rows[(tenant, name)]["snapshot"] = snapshot

    # 写回终态。
    # Args: self - 实例；tenant/name/run_id - 任务键；status/error - 终态与错误。
    # Returns: 无返回值。
    async def finish(self, tenant: int, name: str, run_id: str, status: str, error: str = "") -> None:
        row = self.rows.get((tenant, name))
        if row is not None and row["status"] in sync_module.ACTIVE:
            row.update(status=status, error=error)

    # 排队行直接置为已取消，运行中任务写取消信号。
    # Args: self - 实例；tenant/name - 数据源作用域。
    # Returns: 取消后的状态。
    async def cancel(self, tenant: int, name: str) -> dict:
        row = self.rows.get((tenant, name))
        if row is not None and row["status"] == "queued":
            row.update(status="cancelled", phase="cancelled")
        return await self.state(tenant, name)


# 构造带可替换内省边界的同步服务。
# Args: store - 存储替身；inspect - 内省协程。
# Returns: 未启动调度循环的服务实例。
def build_service(store, inspect) -> SimpleNamespace:
    manager = SimpleNamespace(inspect_live_schema=inspect, refresh_from_snapshot=AsyncMock())
    service = sync_module.SchemaSyncService(store=store, manager=manager, fingerprint=AsyncMock(return_value="signature"))
    return SimpleNamespace(service=service, manager=manager)


class TestSchemaSyncQueueing:
    """覆盖需求 8.1～8.3：并发满排队、队列位置与 FIFO 顺序。"""

    # 验证并发满时启动进队列并返回排队状态，不再抛 429。
    # Args: self - 实例；monkeypatch - 依赖替换。
    # Returns: 无返回值。
    async def test_concurrency_full_enqueues_instead_of_429(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """同一 Worker 超过并发上限的同步必须排队等待，而不是直接中断。"""
        # Arrange：上限设为 1，并让第一个任务停在内省里占满额度。
        release = asyncio.Event()

        # 保持内省挂起，模拟第一个任务仍在运行。
        # Args: name/tenant_id - 数据源与租户。
        # Returns: 释放后返回完整快照。
        async def inspect(name: str, *, tenant_id: int) -> SchemaSnapshot:
            await release.wait()
            return SchemaSnapshot([TableSchema("orders")])

        monkeypatch.setattr(sync_module, "concurrency_limit", lambda: 1)
        store = FakeQueueStore()
        service = build_service(store, inspect).service

        # Act：第一个任务占用额度，第二个数据源只能排队。
        await service.start(2, "sales")
        result = await service.start(2, "orders")

        # Assert：返回排队状态与队列位置，不抛 429，也不启动第二个协程。
        assert result["status"] == "queued"
        assert result["queue_position"] == 0
        assert store.rows[(2, "orders")]["status"] == "queued"
        assert len([task for task in service.tasks if not task.done()]) == 1
        release.set()
        await service.close()

    # 验证队列位置按同租户已排队数量计算，非排队时为 null。
    # Args: self - 实例。
    # Returns: 无返回值。
    async def test_queue_position_is_per_tenant(self) -> None:
        """用户只看得见自己租户的数据源，位置必须按同租户口径计算。"""
        # Arrange：本租户两条排队，其他租户一条更早的排队任务。
        store = FakeQueueStore()

        # 直接写入排队行，模拟启动排队时的持久化结果。
        # Args: tenant/name - 作用域；sequence - 入队序号。
        # Returns: 无返回值。
        def seed(tenant: int, name: str, sequence: int) -> None:
            store.rows[(tenant, name)] = {"status": "queued", "phase": "queued", "run_id": name, "started_at": sequence}

        seed(9, "other-tenant", 1)
        seed(2, "sales", 2)
        seed(2, "orders", 3)

        # Act
        first = await store.state(2, "sales")
        second = await store.state(2, "orders")

        # Assert：其他租户的排队行不计入本租户位置。
        assert first["queue_position"] == 0
        assert second["queue_position"] == 1
        assert first["queue_position"] == second["queue_position"] - 1

    # 验证调度循环按 started_at 升序认领排队任务。
    # Args: self - 实例；monkeypatch - 依赖替换。
    # Returns: 无返回值。
    async def test_fifo_claim_order(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """先排队的任务必须先被认领，认领后进入运行并最终成功。"""
        # Arrange：三个排队任务，仅前两个容量可用。
        monkeypatch.setattr(sync_module, "concurrency_limit", lambda: 2)
        store = FakeQueueStore()

        # 写入排队行，序号越小越早排队。
        # Args: name - 数据源名；sequence - 入队序号。
        # Returns: 无返回值。
        def seed(name: str, sequence: int) -> None:
            store.rows[(2, name)] = {"status": "queued", "phase": "queued", "run_id": name, "started_at": sequence}

        seed("third", 3)
        seed("first", 1)
        seed("second", 2)
        service = build_service(store, AsyncMock(return_value=SchemaSnapshot([TableSchema("orders")]))).service

        # Act
        claimed = await service.claim_queued()
        await asyncio.gather(*service.tasks)

        # Assert：认领顺序为先进先出，且认领后进入运行并发布成功。
        assert claimed == 2
        assert store.claimed == ["first", "second"]
        assert store.rows[(2, "first")]["status"] == "succeeded"
        assert store.rows[(2, "third")]["status"] == "queued"
        await service.close()

    # 验证认领时抢不到互斥的数据源被跳过，排队行保持原样。
    # Args: self - 实例；monkeypatch - 依赖替换。
    # Returns: 无返回值。
    async def test_claim_skips_locked_datasource(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """其他 Worker 正在同步时不能重开任务，也不得把排队行写成失败。"""
        # Arrange：数据源互斥已被占用。
        monkeypatch.setattr(sync_module, "concurrency_limit", lambda: 2)
        store = FakeQueueStore()
        store.rows[(2, "sales")] = {"status": "queued", "phase": "queued", "run_id": "run-1", "started_at": 1}
        store.locked.add((2, "sales"))
        service = build_service(store, AsyncMock(return_value=SchemaSnapshot([TableSchema("orders")]))).service

        # Act
        await service.claim_queued()
        await asyncio.gather(*service.tasks)

        # Assert
        assert store.rows[(2, "sales")]["status"] == "queued"
        await service.close()

    # 验证已被用户取消的排队任务不会在下一轮被认领复活。
    # Args: self - 实例；monkeypatch - 依赖替换。
    # Returns: 无返回值。
    async def test_claim_does_not_revive_cancelled_row(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """认领必须原子，取消与认领只有一个胜出。"""
        # Arrange：排队行代次与认领请求不一致（模拟取消后残留的代次）。
        monkeypatch.setattr(sync_module, "concurrency_limit", lambda: 2)
        store = FakeQueueStore()
        store.rows[(2, "sales")] = {"status": "cancelled", "phase": "cancelled", "run_id": "run-1", "started_at": 1}
        service = build_service(store, AsyncMock(return_value=SchemaSnapshot([TableSchema("orders")]))).service

        # Act：直接按旧代次认领。
        running = await service._run(2, "sales", "run-1", None, claim=True)

        # Assert
        assert running is None
        assert store.rows[(2, "sales")]["status"] == "cancelled"
        assert store.claimed == []
        await service.close()

    # 验证调度循环单轮认领异常不会打死循环。
    # Args: self - 实例；monkeypatch - 依赖替换。
    # Returns: 无返回值。
    async def test_schedule_loop_survives_claim_failure(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """认领抛异常只记录日志并继续下一轮，否则排队任务永远不会被执行。"""
        # Arrange：缩短轮询间隔并把第一轮认领改为抛异常。
        monkeypatch.setattr(sync_module, "_QUEUE_POLL_INTERVAL_SECONDS", 0.01)
        service = build_service(FakeQueueStore(), AsyncMock()).service
        calls: list[int] = []

        # 记录轮询次数：第一轮抛异常，第二轮后主动停止循环。
        # Args: 无。
        # Returns: 无返回值。
        async def flaky_claim() -> int:
            calls.append(1)
            if len(calls) >= 2:
                service._scheduler_stop.set()
            raise RuntimeError("模拟认领失败")

        service.claim_queued = flaky_claim

        # Act
        await asyncio.wait_for(service._schedule_loop(), 5)

        # Assert：第二轮仍然发生，说明异常没有终止循环。
        assert len(calls) >= 2
        await service.close()

    # 验证调度服务可幂等启停，供应用生命周期管理。
    # Args: self - 实例；monkeypatch - 依赖替换。
    # Returns: 无返回值。
    async def test_scheduler_start_and_stop_are_idempotent(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """重复启动不得产生第二个轮询任务，关闭必须等待其退出。"""
        # Arrange
        monkeypatch.setattr(sync_module, "_QUEUE_POLL_INTERVAL_SECONDS", 0.01)
        service = build_service(FakeQueueStore(), AsyncMock()).service

        # Act
        await service.start_scheduler()
        first = service._scheduler
        await service.start_scheduler()

        # Assert
        assert service._scheduler is first
        await service.stop_scheduler()
        assert service._scheduler is None
        await service.close()


class TestSchemaSyncStoreQueue:
    """覆盖排队行的 SQL 边界：位置统计、崩溃回收与终止。"""

    # 验证排队行返回同租户队列位置，且不触发无主任务回收。
    # Args: self - 实例；monkeypatch - 依赖替换。
    # Returns: 无返回值。
    async def test_queued_row_reports_position_without_reclaim(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """排队行不持有互斥锁，既不能回收成失败，也不能漏掉位置统计。"""
        # Arrange：状态行为排队，位置统计返回 2。
        row = {"run_id": "run-1", "status": "queued", "phase": "queued", "started_at": 1,
               "finished_at": None, "heartbeat_at": None, "table_count": 0, "error": ""}
        connection = FakeConnection(rows=[row], values=[2])
        monkeypatch.setattr(sync_module, "pg_connection", connection_factory(connection))
        store = sync_module.SchemaSyncStore()

        # Act
        state = await store.state(2, "sales")

        # Assert：位置写入响应，且没有执行任何失败回收语句。
        assert state["status"] == "queued"
        assert state["queue_position"] == 2
        assert not [sql for sql in connection.sql("execute") if "failed" in sql]
        assert not [sql for sql in connection.sql("fetchval") if "pg_try_advisory_xact_lock" in sql]
        # 位置必须按同租户统计：SQL 绑定当前租户，绝不使用全局口径。
        position_statements = [
            (sql, args) for item_kind, sql, args in connection.statements
            if item_kind == "fetchval" and "count(*)" in sql
        ]
        assert position_statements, "排队位置未执行统计语句"
        assert position_statements[0][1] == (2, 1, "sales")

    # 验证非排队状态不返回队列位置。
    # Args: self - 实例；monkeypatch - 依赖替换。
    # Returns: 无返回值。
    async def test_non_queued_state_has_null_position(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """只有排队中的行才有「前面还有几个」的语义。"""
        # Arrange：已完成的行 + 无主探测返回 False（说明仍有人持有会话锁）。
        row = {"run_id": "run-1", "status": "running", "phase": "collecting", "started_at": 1,
               "finished_at": None, "heartbeat_at": None, "table_count": 0, "error": ""}
        connection = FakeConnection(rows=[row], values=[False])
        monkeypatch.setattr(sync_module, "pg_connection", connection_factory(connection))
        store = sync_module.SchemaSyncStore()

        # Act
        state = await store.state(2, "sales")

        # Assert
        assert state["queue_position"] is None
        assert state["status"] == "running"

    # 验证崩溃回收仍然处理无主的运行中任务。
    # Args: self - 实例；monkeypatch - 依赖替换。
    # Returns: 无返回值。
    async def test_running_row_without_owner_is_reclaimed(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """排除排队行不能顺手关掉运行中任务的崩溃回收。"""
        # Arrange：运行中任务且会话锁已释放（探测返回 True）。
        row = {"run_id": "run-1", "status": "cancelling", "phase": "collecting", "started_at": 1,
               "finished_at": None, "heartbeat_at": None, "table_count": 0, "error": ""}
        connection = FakeConnection(rows=[row], values=[True])
        monkeypatch.setattr(sync_module, "pg_connection", connection_factory(connection))
        store = sync_module.SchemaSyncStore()

        # Act
        state = await store.state(2, "sales")

        # Assert
        assert state["status"] == "failed"
        assert [sql for sql in connection.sql("execute") if "status='failed'" in sql]

    # 验证排队中的任务可以被直接终止，不再返回 409。
    # Args: self - 实例；monkeypatch - 依赖替换。
    # Returns: 无返回值。
    async def test_cancel_queued_row_marks_cancelled(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """排队任务没有运行进程，终止就是直接置为已取消。"""
        # Arrange：排队行 + 取消写入命中；state 复读同一行。
        row = {"run_id": "run-1", "status": "queued", "phase": "queued", "started_at": 1,
               "finished_at": None, "heartbeat_at": None, "table_count": 0, "error": ""}
        connection = FakeConnection(rows=[row, row], values=[True, 0])
        monkeypatch.setattr(sync_module, "pg_connection", connection_factory(connection))
        store = sync_module.SchemaSyncStore()

        # Act
        result = await store.cancel(2, "sales")

        # Assert：排队行直接写入 cancelled；运行中分支保持条件更新，不会误改排队行，也没有抛 409。
        assert [sql for _kind, sql, _args in connection.statements if "status='cancelled'" in sql]
        cancel_signal = [sql for sql in connection.sql("execute") if "status='cancelling'" in sql]
        assert cancel_signal and all("status='running'" in sql for sql in cancel_signal)
        assert result["datasource"] == "sales"

    # 验证发布阶段仍然拒绝终止。
    # Args: self - 实例；monkeypatch - 依赖替换。
    # Returns: 无返回值。
    async def test_cancel_publishing_still_conflicts(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """发布中的任务不能被终止，避免留下半发布快照。"""
        # Arrange
        connection = FakeConnection(rows=[{"status": "running", "phase": "publishing"}])
        monkeypatch.setattr(sync_module, "pg_connection", connection_factory(connection))
        store = sync_module.SchemaSyncStore()

        # Act
        with pytest.raises(HTTPException) as error:
            await store.cancel(2, "sales")

        # Assert
        assert error.value.status_code == 409
        assert not connection.sql("execute")


class TestSchemaSyncStartGuards:
    """覆盖启动守卫：同一数据源不能重复启动，排队中也算占用。"""

    # 验证排队中的数据源重复启动仍返回 409。
    # Args: self - 实例。
    # Returns: 无返回值。
    async def test_duplicate_start_on_queued_row_conflicts(self) -> None:
        """同一数据源只允许一个活动任务，重复启动不得重置队列位置。"""
        # Arrange
        store = FakeQueueStore()
        store.rows[(2, "sales")] = {"status": "queued", "phase": "queued", "run_id": "run-1", "started_at": 1}
        service = build_service(store, AsyncMock()).service

        # Act
        with pytest.raises(HTTPException) as error:
            await service.start(2, "sales")

        # Assert
        assert error.value.status_code == 409
        assert store.rows[(2, "sales")]["started_at"] == 1
        await service.close()
