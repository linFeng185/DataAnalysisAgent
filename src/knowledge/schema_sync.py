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
SNAPSHOT = TypeAdapter(SchemaSnapshot)


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
    # Returns: 不含快照或连接信息的状态。
    async def state(self, tenant: int, name: str) -> dict:
        """任意 Worker 都可以查询或确认失去拥有者的任务。"""
        async with pg_connection(tenant_id=tenant, user_id=0, role="super_admin") as connection:
            row = await connection.fetchrow("SELECT run_id,status,phase,started_at,finished_at,heartbeat_at,table_count,error FROM schema_sync_jobs WHERE tenant_id=$1 AND datasource=$2", tenant, name)
            if row and row["status"] in ACTIVE:
                abandoned = await connection.fetchval("SELECT pg_try_advisory_xact_lock(hashtextextended($1,0))", lock_key(tenant, name))
                if abandoned:
                    await connection.execute("UPDATE schema_sync_jobs SET status='failed',error='同步进程已退出，可重新同步',finished_at=clock_timestamp() WHERE tenant_id=$1 AND datasource=$2 AND run_id=$3", tenant, name, row["run_id"])
                    row = {**dict(row), "status": "failed", "error": "同步进程已退出，可重新同步"}
                    logger.warning("回收无拥有者表结构任务", tenant_id=tenant, datasource=name)
        return {"datasource": name, **(dict(row) if row else {"status": "idle", "phase": "", "table_count": 0, "error": ""})}

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
        """已经发布或结束时保持幂等，发布中返回冲突。"""
        async with pg_connection(tenant_id=tenant, user_id=0, role="super_admin") as connection:
            row = await connection.fetchrow("SELECT status,phase FROM schema_sync_jobs WHERE tenant_id=$1 AND datasource=$2 FOR UPDATE", tenant, name)
            if row and row["status"] in ACTIVE and row["phase"] == "publishing":
                raise HTTPException(409, "表结构正在发布，请等待完成")
            await connection.execute("UPDATE schema_sync_jobs SET status='cancelling' WHERE tenant_id=$1 AND datasource=$2 AND status='running' AND phase='collecting'", tenant, name)
        logger.info("表结构终止请求已记录", tenant_id=tenant, datasource=name)
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

    # 等数据库互斥成功后才返回已启动，兼容入口可等待最终结果。
    # Args: self - 实例；tenant/name - 作用域；wait - 是否等待任务终态。
    # Returns: 共享任务状态。
    async def start(self, tenant: int, name: str, *, wait: bool = False) -> dict:
        """本地任务集合只用于生命周期，互斥权威在数据库。"""
        if self.closing:
            raise HTTPException(503, "同步服务正在关闭")
        if len(self.tasks) >= 3:
            raise HTTPException(429, "当前 Worker 的同步任务已达上限，请稍后重试")
        ready = asyncio.get_running_loop().create_future()
        task = asyncio.create_task(self._run(tenant, name, str(uuid4()), ready), name=f"schema-sync:{tenant}:{name}")
        self.tasks.add(task)
        task.add_done_callback(self.tasks.discard)
        await asyncio.shield(ready)
        if wait:
            await asyncio.shield(task)
        return await self.store.state(tenant, name)

    # 在拥有者锁内采集，轮询取消；发布阶段屏蔽取消直至完整退出。
    # Args: self - 实例；tenant/name/run_id - 任务键；ready - 启动回执。
    # Returns: 无返回值。
    async def _run(self, tenant: int, name: str, run_id: str, ready: asyncio.Future) -> None:
        child: asyncio.Task | None = None
        publishing = False
        try:
            async with self.store.owner(tenant, name) as lease:
                try:
                    await self.store.begin(tenant, name, run_id)
                    ready.set_result(True)
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
            if not ready.done():
                ready.set_exception(exc)
            else:
                logger.error("表结构同步生命周期失败", tenant_id=tenant, datasource=name, exc_info=True)
        finally:
            if not ready.done():
                ready.set_exception(HTTPException(503, "同步服务已关闭"))

    # 先提交规范快照，向量缓存仅为可重建索引，不影响已发布结构的可用性。
    # Args: self - 实例；tenant/name/run_id - 任务键；snapshot/fingerprint - 完整结构及连接摘要。
    # Returns: 无返回值。
    async def _publish(self, tenant: int, name: str, run_id: str, snapshot: SchemaSnapshot, fingerprint: str) -> None:
        await self.store.publish(tenant, name, run_id, snapshot, fingerprint)
        try:
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
