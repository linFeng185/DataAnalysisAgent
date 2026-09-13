"""跨 Worker 表结构任务与规范快照；取消不删除已发布结构。"""

import asyncio
import json
from contextlib import asynccontextmanager
from dataclasses import asdict
from typing import Any
from uuid import uuid4

from fastapi import HTTPException
from pydantic import TypeAdapter

from src.app_context import get_app_context
from src.datasource.schema_snapshot import SchemaSnapshot
from src.logging_config import get_logger
from src.memory.pg_pool import get_pg_pool, pg_connection

logger = get_logger(__name__)
ACTIVE = {"running", "cancelling"}
QUEUED = "queued"
# 同一数据源同时只能有一个活动任务，排队行同样占用该数据源的启动名额。
OCCUPIED = ACTIVE | {QUEUED}
SNAPSHOT = TypeAdapter(SchemaSnapshot)
_INDEX_REFRESH_TIMEOUT_SECONDS = 30.0
_QUEUE_POLL_INTERVAL_SECONDS = 2.0


# 构造稳定租户数据源锁键，进程间无需共享内存。
# Args: tenant/name - 数据源作用域。
# Returns: PostgreSQL advisory lock 的文本输入。
def lock_key(tenant: int, name: str) -> str:
    return f"schema-sync:{tenant}:{name}"


# 清理必须等驱动真正退出，重复取消不能打断锁内收尾。
# Args: awaitable - 清理或发布异步操作。
# Returns: 操作最终结果。
async def settle(awaitable: Any) -> Any:
    task = asyncio.ensure_future(awaitable)
    while not task.done():
        try:
            await asyncio.shield(task)
        except asyncio.CancelledError:
            continue
    return task.result()


# 安全写回启动回执，调度循环认领的任务没有等待方时为空操作。
# Args: ready - 可选启动回执；exc - 需要写回的错误。
# Returns: 无返回值。
def _settle_ready(ready: asyncio.Future | None, exc: Exception | None = None) -> None:
    if ready is None or ready.done():
        return
    if exc is None:
        ready.set_result(True)
    else:
        ready.set_exception(exc)


# 每个 Worker 最多同时执行几个表结构同步任务，超出后排队而不是拒绝。
# Args: 无。
# Returns: 不小于 1 的并发上限。
def concurrency_limit() -> int:
    """读取当前生效配置，支持 /admin/runtime-settings 热改后立即收缩或放宽。"""
    from src.config import get_settings

    limit = int(getattr(get_settings(), "max_schema_sync_concurrent", 3) or 1)
    return max(1, limit)


class SchemaSyncStore:
    """持久化任务状态，使用会话锁证明拥有者仍存活。"""

    # 持有独立数据库会话锁直到采集、清理和发布全部退出。
    # Args: self - 实例；tenant/name - 数据源作用域。
    # Returns: 已持锁的数据库连接。
    @asynccontextmanager
    async def owner(self, tenant: int, name: str):
        """崩溃会释放会话锁；不按心跳超时直接接管仍在执行的旧任务。"""
        pool = await get_pg_pool()
        async with pool.acquire() as connection:
            key = lock_key(tenant, name)
            acquired = await connection.fetchval("SELECT pg_try_advisory_lock(hashtextextended($1,0))", key)
            if not acquired:
                raise HTTPException(409, "该数据源正在同步，请等待或终止现有任务")
            try:
                yield connection
            finally:
                await connection.execute("SELECT pg_advisory_unlock(hashtextextended($1,0))", key)

    # 在已持互斥锁后写入运行状态，保留上次成功快照。
    # Args: self - 实例；tenant/name/run_id - 任务键。
    # Returns: 无返回值。
    async def begin(self, tenant: int, name: str, run_id: str) -> None:
        """只有 owner 持有者可以开始任务。"""
        async with pg_connection(tenant_id=tenant, user_id=0, role="super_admin") as connection:
            await connection.execute(
                "INSERT INTO schema_sync_jobs(tenant_id,datasource,run_id,status) VALUES($1,$2,$3::uuid,'running') "
                "ON CONFLICT(tenant_id,datasource) DO UPDATE SET run_id=EXCLUDED.run_id,status='running',phase='collecting',"
                "started_at=clock_timestamp(),heartbeat_at=clock_timestamp(),finished_at=NULL,error=''",
                tenant, name, run_id,
            )
        logger.info("表结构同步已启动", tenant_id=tenant, datasource=name, run_id=run_id)

    # 查询共享状态，并仅在会话互斥锁已释放时回收崩溃任务。
    # Args: self - 实例；tenant/name - 数据源作用域。
    # Returns: 不含快照或连接信息的状态，排队时附带同租户队列位置。
    async def state(self, tenant: int, name: str) -> dict:
        """任意 Worker 都可以查询或确认失去拥有者的任务。"""
        queue_position: int | None = None
        async with pg_connection(tenant_id=tenant, user_id=0, role="super_admin") as connection:
            row = await connection.fetchrow("SELECT run_id,status,phase,started_at,finished_at,heartbeat_at,table_count,error FROM schema_sync_jobs WHERE tenant_id=$1 AND datasource=$2", tenant, name)
            if row and row["status"] == QUEUED:
                # 排队行不持有互斥锁，且排队不是运行中：这里既不做无主回收，
                # 也不能把排队误判为失败（崩溃回收只处理 running/cancelling）。
                # 位置按同租户口径计算：用户只看得见本租户的数据源，全局口径会显示与感知不符的数字。
                queue_position = int(await connection.fetchval(
                    "SELECT count(*) FROM schema_sync_jobs WHERE tenant_id=$1 AND status='queued' AND (started_at,datasource) < ($2,$3)",
                    tenant, row["started_at"], name,
                ) or 0)
            elif row and row["status"] in ACTIVE:
                abandoned = await connection.fetchval("SELECT pg_try_advisory_xact_lock(hashtextextended($1,0))", lock_key(tenant, name))
                if abandoned:
                    await connection.execute("UPDATE schema_sync_jobs SET status='failed',error='同步进程已退出，可重新同步',finished_at=clock_timestamp() WHERE tenant_id=$1 AND datasource=$2 AND run_id=$3", tenant, name, row["run_id"])
                    row = {**dict(row), "status": "failed", "error": "同步进程已退出，可重新同步"}
                    logger.warning("回收无拥有者表结构任务", tenant_id=tenant, datasource=name)
        return {"datasource": name, "queue_position": queue_position, **(dict(row) if row else {"status": "idle", "phase": "", "table_count": 0, "error": ""})}

    # 持久化排队请求，并发满时不再拒绝启动。
    # Args: self - 实例；tenant/name/run_id - 任务键。
    # Returns: 无返回值。
    async def enqueue(self, tenant: int, name: str, run_id: str) -> None:
        """排队行不持有互斥锁，保留上次成功快照，可被任意 Worker 重新认领。"""
        async with pg_connection(tenant_id=tenant, user_id=0, role="super_admin") as connection:
            await connection.execute(
                "INSERT INTO schema_sync_jobs(tenant_id,datasource,run_id,status,phase) VALUES($1,$2,$3::uuid,'queued','queued') "
                "ON CONFLICT(tenant_id,datasource) DO UPDATE SET run_id=EXCLUDED.run_id,status='queued',phase='queued',"
                "started_at=clock_timestamp(),heartbeat_at=clock_timestamp(),finished_at=NULL,error=''",
                tenant, name, run_id,
            )
        logger.info("表结构同步已写入排队行", tenant_id=tenant, datasource=name, run_id=run_id)

    # 按先进先出读取待认领的排队任务。
    # Args: self - 实例；limit - 本次最多返回条数。
    # Returns: 按 started_at 升序的 (tenant_id,datasource,run_id) 列表。
    async def queued(self, limit: int) -> list[dict]:
        """跨租户读取由调度方持有，实际互斥仍由逐个抢占会话锁保证。"""
        async with pg_connection(tenant_id=0, user_id=0, role="super_admin") as connection:
            rows = await connection.fetch(
                "SELECT tenant_id,datasource,run_id FROM schema_sync_jobs WHERE status='queued' "
                "ORDER BY started_at ASC,datasource ASC LIMIT $1",
                max(1, int(limit)),
            )
        return [dict(row) for row in rows]

    # 原子认领排队任务：排队转运行中与用户取消只有一个胜出。
    # Args: self - 实例；tenant/name/run_id - 任务键。
    # Returns: 认领成功返回 True；已被取消或被其他 Worker 认领返回 False。
    async def claim(self, tenant: int, name: str, run_id: str) -> bool:
        """条件更新必须带上 run_id，绝不复活用户已取消的排队任务。"""
        async with pg_connection(tenant_id=tenant, user_id=0, role="super_admin") as connection:
            claimed = bool(await connection.fetchval(
                "UPDATE schema_sync_jobs SET status='running',phase='collecting',heartbeat_at=clock_timestamp(),finished_at=NULL,error='' "
                "WHERE tenant_id=$1 AND datasource=$2 AND run_id=$3::uuid AND status='queued' RETURNING true",
                tenant, name, run_id,
            ))
        if claimed:
            logger.info("表结构同步排队任务已认领", tenant_id=tenant, datasource=name, run_id=run_id)
        else:
            logger.info("表结构同步排队认领未命中", tenant_id=tenant, datasource=name, run_id=run_id)
        return claimed

    # 更新心跳并在同一语句中读取任务取消信号。
    # Args: self - 实例；tenant/name/run_id - 任务键。
    # Returns: 当前任务状态。
    async def heartbeat(self, tenant: int, name: str, run_id: str) -> dict:
        """任务代次不匹配必须停止发布。"""
        async with pg_connection(tenant_id=tenant, user_id=0, role="super_admin") as connection:
            row = await connection.fetchrow("UPDATE schema_sync_jobs SET heartbeat_at=clock_timestamp() WHERE tenant_id=$1 AND datasource=$2 AND run_id=$3::uuid RETURNING status,phase", tenant, name, run_id)
        if row is None:
            raise RuntimeError("表结构同步任务所有权丢失")
        return dict(row)

    # 以条件更新串行化取消请求与发布开始，二者只有一个胜出。
    # Args: self - 实例；tenant/name/run_id - 任务键。
    # Returns: 是否已进入发布阶段。
    async def publishing(self, tenant: int, name: str, run_id: str) -> bool:
        """进入发布后不再接受手动取消。"""
        async with pg_connection(tenant_id=tenant, user_id=0, role="super_admin") as connection:
            return bool(await connection.fetchval("UPDATE schema_sync_jobs SET phase='publishing' WHERE tenant_id=$1 AND datasource=$2 AND run_id=$3::uuid AND status='running' RETURNING true", tenant, name, run_id))

    # 原子提交完整规范快照，数据库事务是跨进程读者的发布边界。
    # Args: self - 实例；tenant/name/run_id - 任务键；snapshot - 新快照；fingerprint - 数据源配置指纹。
    # Returns: 无返回值。
    async def publish(self, tenant: int, name: str, run_id: str, snapshot: SchemaSnapshot, fingerprint: str) -> None:
        """向量缓存不是事务参与者，规范快照不依赖其原子性。"""
        async with pg_connection(tenant_id=tenant, user_id=0, role="super_admin") as connection:
            saved = await connection.fetchval(
                "UPDATE schema_sync_jobs SET snapshot_json=$4::jsonb,snapshot_version=run_id,fingerprint=$5,table_count=$6,"
                "heartbeat_at=clock_timestamp() WHERE tenant_id=$1 AND datasource=$2 AND run_id=$3::uuid AND phase='publishing' AND status='running' RETURNING true",
                tenant, name, run_id, json.dumps(asdict(snapshot), ensure_ascii=False), fingerprint, len(snapshot.tables),
            )
            if not saved:
                raise RuntimeError("表结构快照发布被过期任务围栏阻断")
        logger.info("表结构规范快照已发布", tenant_id=tenant, datasource=name, run_id=run_id, table_count=len(snapshot.tables))

    # 终止或失败只写任务终态，不清除旧快照。
    # Args: self - 实例；tenant/name/run_id - 任务键；status/error - 终态及脱敏错误。
    # Returns: 无返回值。
    async def finish(self, tenant: int, name: str, run_id: str, status: str, error: str = "") -> None:
        """过期 Worker 不能覆盖新代次的任务。"""
        async with pg_connection(tenant_id=tenant, user_id=0, role="super_admin") as connection:
            await connection.execute("UPDATE schema_sync_jobs SET status=$4,error=$5,finished_at=clock_timestamp() WHERE tenant_id=$1 AND datasource=$2 AND run_id=$3::uuid AND status IN ('running','cancelling')", tenant, name, run_id, status, error)
        logger.info("表结构同步已结束", tenant_id=tenant, datasource=name, status=status)

    # 标记终止请求，不释放拥有者持有的互斥锁。
    # Args: self - 实例；tenant/name - 数据源作用域。
    # Returns: 取消信号写入后的状态。
    async def cancel(self, tenant: int, name: str) -> dict:
        """排队任务没有运行进程，直接置为已终止；已经发布或结束时保持幂等，发布中返回冲突。"""
        async with pg_connection(tenant_id=tenant, user_id=0, role="super_admin") as connection:
            row = await connection.fetchrow("SELECT status,phase FROM schema_sync_jobs WHERE tenant_id=$1 AND datasource=$2 FOR UPDATE", tenant, name)
            if row and row["status"] in ACTIVE and row["phase"] == "publishing":
                raise HTTPException(409, "表结构正在发布，请等待完成")
            cancelled_queued = await connection.fetchval(
                "UPDATE schema_sync_jobs SET status='cancelled',phase='cancelled',error='',finished_at=clock_timestamp() "
                "WHERE tenant_id=$1 AND datasource=$2 AND status='queued' RETURNING true",
                tenant, name,
            )
            await connection.execute("UPDATE schema_sync_jobs SET status='cancelling' WHERE tenant_id=$1 AND datasource=$2 AND status='running' AND phase='collecting'", tenant, name)
        logger.info("表结构终止请求已记录", tenant_id=tenant, datasource=name, queued=bool(cancelled_queued))
        return await self.state(tenant, name)

    # 读取已发布快照，数据源连接配置变化后不复用旧结构。
    # Args: self - 实例；tenant/name - 作用域；fingerprint - 当前配置指纹。
    # Returns: 快照版本和已验证的结构，尚无快照返回 None。
    async def snapshot(self, tenant: int, name: str, fingerprint: str) -> tuple[str, SchemaSnapshot] | None:
        """采集和取消期间仍可读取上次成功快照。"""
        async with pg_connection(tenant_id=tenant, user_id=0, role="super_admin") as connection:
            row = await connection.fetchrow("SELECT snapshot_version,snapshot_json FROM schema_sync_jobs WHERE tenant_id=$1 AND datasource=$2 AND fingerprint=$3 AND snapshot_json IS NOT NULL", tenant, name, fingerprint)
        if row is None:
            return None
        data = json.loads(row["snapshot_json"]) if isinstance(row["snapshot_json"], str) else row["snapshot_json"]
        return str(row["snapshot_version"]), SNAPSHOT.validate_python(data)

    # 同步手工字段备注到规范快照，避免下一请求恢复为旧备注。
    # Args: self - 实例；tenant/name/table/column/comment - 字段定位和备注。
    # Returns: 无返回值。
    async def annotate(self, tenant: int, name: str, table: str, column: str, comment: str) -> None:
        """没有已发布快照时保持原缓存兼容路径。"""
        async with pg_connection(tenant_id=tenant, user_id=0, role="super_admin") as connection:
            row = await connection.fetchrow("SELECT snapshot_json FROM schema_sync_jobs WHERE tenant_id=$1 AND datasource=$2 FOR UPDATE", tenant, name)
            if not row or row["snapshot_json"] is None:
                return
            data = json.loads(row["snapshot_json"]) if isinstance(row["snapshot_json"], str) else dict(row["snapshot_json"])
            for item in data["tables"]:
                if item["name"] == table:
                    for field in item["columns"]:
                        if field["name"] == column:
                            field["comment"] = comment
            await connection.execute("UPDATE schema_sync_jobs SET snapshot_json=$3::jsonb,snapshot_version=$4::uuid WHERE tenant_id=$1 AND datasource=$2", tenant, name, json.dumps(data), str(uuid4()))


# 读取权威连接配置指纹，不将凭证写入任务状态。
# Args: tenant/name - 数据源作用域。
# Returns: 不可逆配置摘要。
async def source_fingerprint(tenant: int, name: str) -> str:
    from src.datasource.registry import get_registry
    from src.knowledge.datasource_cache import build_connection_fingerprint
    source = await get_registry().resolve(name, tenant_id=tenant)
    return build_connection_fingerprint(source)


class SchemaSyncService:
    """任务属于应用 Context，关闭时等待取消清理或发布完成。"""

    # 注入存储和内省边界，便于隔离测试。
    # Args: self - 实例；store/manager/fingerprint - 可选依赖。
    # Returns: 无返回值。
    def __init__(self, store: Any = None, manager: Any = None, fingerprint: Any = None) -> None:
        from src.knowledge.schema_manager import get_schema_manager
        self.store = store or SchemaSyncStore()
        self.manager = manager or get_schema_manager()
        self.fingerprint = fingerprint or source_fingerprint
        self.tasks: set[asyncio.Task] = set()
        self.closing = False
        self._scheduler: asyncio.Task | None = None
        self._scheduler_stop = asyncio.Event()

    # 等数据库互斥成功后才返回已启动，兼容入口可等待最终结果。
    # Args: self - 实例；tenant/name - 作用域；wait - 是否等待任务终态。
    # Returns: 共享任务状态，并发已满时返回排队状态而不是报错。
    async def start(self, tenant: int, name: str, *, wait: bool = False) -> dict:
        """本地任务集合只用于生命周期，互斥权威在数据库。"""
        if self.closing:
            raise HTTPException(503, "同步服务正在关闭")
        current = await self.store.state(tenant, name)
        if current.get("status") in OCCUPIED:
            # 排队同样占用该数据源的名额，重复启动保持 409，避免重置队列位置。
            raise HTTPException(409, "该数据源正在同步，请等待或终止现有任务")
        run_id = str(uuid4())
        if len(self.tasks) >= concurrency_limit():
            # 并发已满不再拒绝：持久化为排队行，由调度循环按先进先出认领。
            await self.store.enqueue(tenant, name, run_id)
            logger.info("表结构同步进入排队", tenant_id=tenant, datasource=name, running=len(self.tasks))
            return await self.store.state(tenant, name)
        ready = asyncio.get_running_loop().create_future()
        task = asyncio.create_task(self._run(tenant, name, run_id, ready), name=f"schema-sync:{tenant}:{name}")
        self.tasks.add(task)
        task.add_done_callback(self.tasks.discard)
        await asyncio.shield(ready)
        if wait:
            await asyncio.shield(task)
        return await self.store.state(tenant, name)

    # 幂等启动排队任务调度循环，由应用启动阶段调用。
    # Args: self - 实例。
    # Returns: 无返回值。
    async def start_scheduler(self) -> None:
        """每个 Worker 各自认领，数据库条件更新保证同一排队任务只被执行一次。"""
        if self._scheduler is None or self._scheduler.done():
            self._scheduler_stop.clear()
            self._scheduler = asyncio.create_task(self._schedule_loop(), name="schema-sync-scheduler")
            logger.info("表结构同步调度服务已启动")

    # 关闭排队调度循环，不打断已持锁任务的清理或发布收尾。
    # Args: self - 实例。
    # Returns: 无返回值。
    async def stop_scheduler(self) -> None:
        """只停止认领，已认领任务仍由 close 逐个取消并等待退出。"""
        self._scheduler_stop.set()
        if self._scheduler is not None:
            self._scheduler.cancel()
            await asyncio.gather(self._scheduler, return_exceptions=True)
            self._scheduler = None
        logger.info("表结构同步调度服务已关闭")

    # 周期认领排队任务，单轮异常只记录日志并继续下一轮。
    # Args: self - 实例。
    # Returns: 无返回值。
    async def _schedule_loop(self) -> None:
        while not self._scheduler_stop.is_set():
            try:
                await self.claim_queued()
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.error("表结构同步排队认领失败，保留排队行等待下一轮", exc_info=True)
            try:
                await asyncio.wait_for(self._scheduler_stop.wait(), timeout=_QUEUE_POLL_INTERVAL_SECONDS)
            except TimeoutError:
                continue

    # 按先进先出认领排队任务，额度与直接启动共享同一并发上限。
    # Args: self - 实例。
    # Returns: 本轮创建认领协程的数量。
    async def claim_queued(self) -> int:
        """抢不到数据库互斥或已被取消的排队行由 _run 跳过，下一轮继续。"""
        if self.closing:
            return 0
        free = concurrency_limit() - len(self.tasks)
        if free <= 0:
            logger.debug("表结构同步并发已满，本轮不认领排队任务", running=len(self.tasks))
            return 0
        rows = await self.store.queued(free)
        claimed = 0
        for row in rows:
            if self.closing or len(self.tasks) >= concurrency_limit():
                break
            task = asyncio.create_task(
                self._run(int(row["tenant_id"]), str(row["datasource"]), str(row["run_id"]), None, claim=True),
                name=f"schema-sync-claim:{row['tenant_id']}:{row['datasource']}",
            )
            self.tasks.add(task)
            task.add_done_callback(self.tasks.discard)
            claimed += 1
        if rows:
            logger.info("表结构同步排队认领完成", claimed=claimed, candidate_count=len(rows))
        return claimed

    # 在拥有者锁内采集，轮询取消；发布阶段屏蔽取消直至完整退出。
    # Args: self - 实例；tenant/name/run_id - 任务键；ready - 可选启动回执；claim - 是否为调度循环认领。
    # Returns: 无返回值。
    async def _run(self, tenant: int, name: str, run_id: str, ready: asyncio.Future | None = None, *, claim: bool = False) -> None:
        child: asyncio.Task | None = None
        publishing = False
        try:
            async with self.store.owner(tenant, name) as lease:
                try:
                    if claim:
                        # 认领必须原子：用户取消与调度认领只有一个胜出，不复活已取消的排队任务。
                        if not await self.store.claim(tenant, name, run_id):
                            logger.info("排队任务认领未命中，排队行保持不变", tenant_id=tenant, datasource=name, run_id=run_id)
                            _settle_ready(ready)
                            return
                    else:
                        await self.store.begin(tenant, name, run_id)
                    _settle_ready(ready)
                    fingerprint = await self.fingerprint(tenant, name)
                    child = asyncio.create_task(self.manager.inspect_live_schema(name, tenant_id=tenant))
                    while not child.done():
                        await asyncio.wait({child}, timeout=0.5)
                        await lease.fetchval("SELECT 1")
                        row = await self.store.heartbeat(tenant, name, run_id)
                        if row["status"] == "cancelling":
                            raise asyncio.CancelledError
                    snapshot = child.result()
                    if not isinstance(snapshot, SchemaSnapshot):
                        raise ValueError("内省必须返回完整的 SchemaSnapshot")
                    if await self.fingerprint(tenant, name) != fingerprint:
                        raise ValueError("同步期间数据源配置已变化，请重新同步")
                    await lease.fetchval("SELECT 1")
                    if not await self.store.publishing(tenant, name, run_id):
                        raise asyncio.CancelledError
                    publishing = True
                    child = asyncio.create_task(self._publish(tenant, name, run_id, snapshot, fingerprint))
                    await asyncio.shield(child)
                except asyncio.CancelledError:
                    logger.info("表结构取消清理边界", run_id=run_id, publishing=publishing, child_done=child.done() if child else True)
                    if child is not None:
                        if not publishing:
                            child.cancel()
                        results = await settle(asyncio.gather(child, return_exceptions=True))
                        if publishing and not isinstance(results[0], BaseException):
                            return
                    await settle(self.store.finish(tenant, name, run_id, "failed" if publishing else "cancelled", "发布失败，请重新同步" if publishing else ""))
                    logger.info("表结构取消状态写回完成", run_id=run_id)
                except Exception:
                    if child is not None and not child.done():
                        child.cancel()
                        await asyncio.gather(child, return_exceptions=True)
                    logger.error("表结构同步失败", tenant_id=tenant, datasource=name, run_id=run_id, exc_info=True)
                    await self.store.finish(tenant, name, run_id, "failed", "同步失败，请联系管理员查看日志")
        except Exception as exc:
            if claim and isinstance(exc, HTTPException) and exc.status_code == 409:
                # 其他 Worker 已持有该数据源互斥：排队行保持原样，等待下一轮认领。
                logger.info("排队任务认领跳过：数据源已被其他 Worker 占用", tenant_id=tenant, datasource=name)
            elif ready is not None and not ready.done():
                ready.set_exception(exc)
            else:
                logger.error("表结构同步生命周期失败", tenant_id=tenant, datasource=name, exc_info=True)
        finally:
            _settle_ready(ready, HTTPException(503, "同步服务已关闭"))

    # 先提交规范快照，向量缓存仅为可重建索引，不影响已发布结构的可用性。
    # Args: self - 实例；tenant/name/run_id - 任务键；snapshot/fingerprint - 完整结构及连接摘要。
    # Returns: 无返回值。
    async def _publish(self, tenant: int, name: str, run_id: str, snapshot: SchemaSnapshot, fingerprint: str) -> None:
        await self.store.publish(tenant, name, run_id, snapshot, fingerprint)
        try:
            # 派生索引有独立期限；其失效不能让已提交的表结构永久停在发布中。
            async with asyncio.timeout(_INDEX_REFRESH_TIMEOUT_SECONDS):
                await self.manager.refresh_from_snapshot(name, snapshot, tenant_id=tenant)
        except Exception:
            logger.error("规范表结构已发布，向量索引更新失败", tenant_id=tenant, datasource=name, exc_info=True)
        await self.store.finish(tenant, name, run_id, "succeeded")

    # 把跨 Worker 规范快照写回当前数据源对象。
    # Args: self - 实例；source - 已解析数据源。
    # Returns: 当前规范快照，不存在时返回 None。
    async def apply_snapshot(self, source: Any) -> SchemaSnapshot | None:
        """每次读规范版本，避免其他 Worker 一直持有旧的内存结构。"""
        from src.knowledge.datasource_cache import build_connection_fingerprint
        saved = await self.store.snapshot(source.tenant_id, source.name, build_connection_fingerprint(source))
        if saved is not None:
            source.schema = saved[1]
            return saved[1]
        return None

    # 取消采集任务，发布中的任务等待其事务和索引写入退出。
    # Args: self - 实例。
    # Returns: 无返回值。
    async def close(self) -> None:
        """不要关闭数据源共享连接池来取消单个同步。"""
        self.closing = True
        await self.stop_scheduler()
        tasks = list(self.tasks)
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)


# 获取应用注册的同步服务，没有初始化时返回明确不可用错误。
# Args: 无。
# Returns: 当前应用同步服务。
def get_schema_sync() -> SchemaSyncService:
    service = get_app_context().get_resource("schema_sync")
    if service is None:
        raise HTTPException(503, "表结构同步服务尚未启动")
    return service
