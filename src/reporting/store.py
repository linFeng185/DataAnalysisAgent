"""报表工作台持久化 Store：PostgreSQL 生产分支与 SQLite/内存测试分支。"""

from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import asdict, is_dataclass
from datetime import datetime, timedelta, timezone
from typing import Any
from uuid import uuid4

from src.config import get_settings
from src.logging_config import get_logger
from src.reporting.models import (
    PageLifecycleRequest,
    ReportJobRecord,
    ReportPlan,
    ReportRunRecord,
    ReportShareRecord,
    ReportVersionRecord,
)

logger = get_logger(__name__)


class ReportStore:
    """隔离报表任务、运行、版本、分享、配额和审计数据。"""

    # 方法作用：初始化可注入数据库连接池和 SQLite 应用层锁。
    # Args: pg_pool - 可选 asyncpg Pool；dialect - postgres/sqlite；memory - 是否强制内存模式。
    # Returns: 无返回值。
    def __init__(self, pg_pool: Any | None = None, dialect: str = "postgres", memory: bool | None = None) -> None:
        self._pg_pool = pg_pool
        self.dialect = str(dialect or "postgres").lower()
        self._memory = bool(memory) if memory is not None else (pg_pool is None)
        self._claim_lock = asyncio.Lock()
        self._execution_locks: dict[str, asyncio.Lock] = {}
        self._jobs: dict[str, dict[str, Any]] = {}
        self._idempotency: dict[tuple[int, int, str], str] = {}
        self._runs: dict[str, dict[str, Any]] = {}
        self._steps: dict[str, list[dict[str, Any]]] = {}
        self._versions: dict[str, dict[str, Any]] = {}
        self._live_queries: dict[str, dict[str, Any]] = {}
        self._shares: dict[str, dict[str, Any]] = {}
        self._share_by_token: dict[str, str] = {}
        self._audits: list[dict[str, Any]] = []
        self._capabilities: dict[tuple[int, int], list[str]] = {}
        self._quota_policies: dict[tuple[int, int], dict[str, Any]] = {}
        logger.debug("ReportStore 初始化完成", dialect=self.dialect, memory=self._memory)

    # 方法作用：在同一事务中去重、扣任务配额并创建任务，返回是否需要首次入队。
    # Args: identity - 当前身份；prompt - 描述；plan - 已确认计划；idempotency_key - 幂等键；quota_manager - 配额服务。
    # Returns: 任务、是否新建及配额快照。
    async def create_job_with_quota(self, identity: Any, prompt: str, plan: ReportPlan, idempotency_key: str, quota_manager: Any) -> tuple[ReportJobRecord, bool, Any]:
        """内存使用应用锁，PostgreSQL 使用事务锁保证多实例幂等。"""
        if self._memory:
            async with self._claim_lock:
                return await self._create_reserved_job(identity, prompt, plan, idempotency_key, quota_manager)
        from src.memory.pg_pool import pg_pool_connection
        from src.reporting.quota import QuotaManager

        pool = await self._pool()
        async with pg_pool_connection(pool, int(identity.tenant_id), int(identity.user_id), str(identity.role)) as connection:
            # 相同身份与幂等键的重试必须在去重查询前串行化。
            if idempotency_key.strip():
                await connection.execute(
                    "SELECT pg_advisory_xact_lock(hashtextextended($1, 0))",
                    f"report-create:{identity.tenant_id}:{identity.user_id}:{idempotency_key.strip()}",
                )
            scoped_store = ReportStore(pg_pool=connection, memory=False)
            scoped_quota = QuotaManager(store=scoped_store, default_policy=quota_manager.default_policy)
            return await scoped_store._create_reserved_job(identity, prompt, plan, idempotency_key, scoped_quota)

    # 方法作用：在调用方已经取得的创建事务或应用锁内完成去重和预占。
    # Args: identity - 当前身份；prompt - 描述；plan - 计划；idempotency_key - 幂等键；quota_manager - 同事务配额服务。
    # Returns: 任务、是否新建及配额快照。
    async def _create_reserved_job(self, identity: Any, prompt: str, plan: ReportPlan, idempotency_key: str, quota_manager: Any) -> tuple[ReportJobRecord, bool, Any]:
        existing = await self.get_job_by_idempotency(identity, idempotency_key)
        if existing is not None:
            if existing.plan.canonical_hash() != plan.canonical_hash():
                raise ValueError("该幂等键已用于其他报表计划")
            return existing, False, await quota_manager.snapshot(identity, timezone_name=plan.timezone)
        snapshot = await quota_manager.reserve(identity, jobs=1, timezone_name=plan.timezone)
        try:
            job = await self.create_job(identity, prompt, plan, idempotency_key)
        except BaseException:
            if self._memory:
                await quota_manager.refund_uncreated_job(identity, plan.timezone, period_start=snapshot.period_start)
            raise
        return job, True, snapshot

    # 方法作用：按当前租户和用户查询已创建的幂等任务，不跨身份复用。
    # Args: identity - 当前身份；idempotency_key - 客户端幂等键。
    # Returns: 已存在任务，空键或未命中返回 None。
    async def get_job_by_idempotency(self, identity: Any, idempotency_key: str) -> ReportJobRecord | None:
        """读取当前身份幂等键关联的任务。"""
        key = idempotency_key.strip()
        if not key:
            return None
        if self._memory:
            job_id = self._idempotency.get((int(identity.tenant_id), int(identity.user_id), key))
            return await self.get_job(job_id, identity) if job_id else None
        from src.memory.pg_pool import pg_pool_connection

        async with pg_pool_connection(await self._pool(), int(identity.tenant_id), int(identity.user_id), str(identity.role)) as connection:
            row = await connection.fetchrow(
                "SELECT * FROM report_jobs WHERE tenant_id=$1 AND user_id=$2 AND idempotency_key=$3",
                int(identity.tenant_id), int(identity.user_id), key,
            )
        return self._to_job(row) if row else None

    # 方法作用：创建当前身份的报表任务并执行幂等键去重。
    # Args: identity - 当前租户身份；prompt - 用户描述；plan - 已确认计划；idempotency_key - 客户端幂等键。
    # Returns: 新建或已存在的 ReportJobRecord。
    async def create_job(self, identity: Any, prompt: str, plan: ReportPlan, idempotency_key: str = "") -> ReportJobRecord:
        tenant_id = int(getattr(identity, "tenant_id", 0) or 0)
        user_id = int(getattr(identity, "user_id", 0) or 0)
        if self._memory:
            key = (tenant_id, user_id, idempotency_key.strip()) if idempotency_key.strip() else None
            if key and key in self._idempotency:
                return self._to_job(self._jobs[self._idempotency[key]])
            job_id = str(uuid4())
            now = datetime.now(timezone.utc)
            row = {
                "id": job_id, "tenant_id": tenant_id, "user_id": user_id, "prompt": prompt,
                "plan_json": plan.model_dump(mode="json"), "plan_hash": plan.canonical_hash(),
                "status": "queued", "next_run_at": plan.run_at or now, "is_paused": False,
                "runs_started": 0, "created_at": now, "updated_at": now,
            }
            self._jobs[job_id] = row
            if key:
                self._idempotency[key] = job_id
            logger.info("报表任务创建完成", job_id=job_id, tenant_id=tenant_id, user_id=user_id)
            return self._to_job(row)
        pool = await self._pool()
        from src.memory.pg_pool import pg_pool_connection

        job_id = str(uuid4())
        async with pg_pool_connection(pool, tenant_id, user_id, str(getattr(identity, "role", ""))) as connection:
            if idempotency_key.strip():
                existing = await connection.fetchrow(
                    "SELECT id, tenant_id, user_id, prompt, plan_json, status, runs_started, created_at, updated_at "
                    "FROM report_jobs WHERE tenant_id=$1 AND user_id=$2 AND idempotency_key=$3",
                    tenant_id, user_id, idempotency_key.strip(),
                )
                if existing:
                    return self._to_job(existing)
            row = await connection.fetchrow(
                "INSERT INTO report_jobs (id,tenant_id,user_id,prompt,plan_json,plan_hash,execution_mode,frequency,timezone,max_runs,status,idempotency_key,next_run_at,schedule_end_at,page_ttl_days,permanent) "
                "VALUES ($1::uuid,$2,$3,$4,$5::jsonb,$6,$7,$8,$9,$10,$11,$12,$13,$14,$15,$16) RETURNING *",
                job_id, tenant_id, user_id, prompt, json.dumps(plan.model_dump(mode="json"), ensure_ascii=False),
                plan.canonical_hash(), plan.execution_mode, plan.frequency, plan.timezone, plan.max_runs,
                "queued", idempotency_key.strip() or None, plan.run_at or datetime.now(timezone.utc), plan.schedule_end_at, plan.page_ttl_days, plan.permanent,
            )
        logger.info("报表任务创建完成", job_id=job_id, tenant_id=tenant_id, user_id=user_id)
        return self._to_job(row)

    # 方法作用：按租户和当前角色读取单个可见报表任务。
    # Args: job_id - 报表任务 ID；identity - 当前身份。
    # Returns: 可见任务，不存在或越权返回 None。
    async def get_job(self, job_id: str, identity: Any) -> ReportJobRecord | None:
        if self._memory:
            row = self._jobs.get(str(job_id))
            return self._to_job(row) if row and self._visible(row, identity) else None
        pool = await self._pool()
        from src.memory.pg_pool import pg_pool_connection

        tenant_id = int(getattr(identity, "tenant_id", 0) or 0)
        user_id = int(getattr(identity, "user_id", 0) or 0)
        role = str(getattr(identity, "role", "") or "")
        async with pg_pool_connection(pool, tenant_id, user_id, role) as connection:
            row = await connection.fetchrow(
                "SELECT id,tenant_id,user_id,prompt,plan_json,status,runs_started,created_at,updated_at,next_run_at,is_paused FROM report_jobs "
                "WHERE id=$1::uuid AND tenant_id=$2 AND ($3<>'super_admin' AND ($3='tenant_admin' OR user_id=$4))",
                str(job_id), tenant_id, role, user_id,
            )
        return self._to_job(row) if row else None

    # 方法作用：列出当前身份可见的报表任务。
    # Args: identity - 当前身份；limit - 返回数量上限。
    # Returns: 按更新时间倒序的任务列表。
    async def list_jobs(self, identity: Any, limit: int = 50) -> list[ReportJobRecord]:
        bounded = max(1, min(int(limit), 100))
        if self._memory:
            rows = [row for row in self._jobs.values() if self._visible(row, identity)]
            rows.sort(key=lambda row: row.get("updated_at") or datetime.min.replace(tzinfo=timezone.utc), reverse=True)
            return [self._to_job(row) for row in rows[:bounded]]
        pool = await self._pool()
        from src.memory.pg_pool import pg_pool_connection

        tenant_id = int(getattr(identity, "tenant_id", 0) or 0)
        user_id = int(getattr(identity, "user_id", 0) or 0)
        role = str(getattr(identity, "role", "") or "")
        async with pg_pool_connection(pool, tenant_id, user_id, role) as connection:
            rows = await connection.fetch(
                "SELECT id,tenant_id,user_id,prompt,plan_json,status,runs_started,created_at,updated_at,next_run_at,is_paused FROM report_jobs "
                "WHERE tenant_id=$1 AND ($2<>'super_admin' AND ($2='tenant_admin' OR user_id=$3)) ORDER BY updated_at DESC LIMIT $4",
                tenant_id, role, user_id, bounded,
            )
        return [self._to_job(row) for row in rows]

    # 方法作用：更新任务状态并记录最近更新时间。
    # Args: job_id - 任务 ID；identity - 当前身份；status - draft/queued/running/succeeded/partial/failed/cancelled。
    # Returns: 更新后的任务或 None。
    async def update_job_status(self, job_id: str, identity: Any, status: str) -> ReportJobRecord | None:
        allowed = {"draft", "planning", "queued", "running", "succeeded", "partial", "failed", "cancelled", "expired", "deleted"}
        if status not in allowed:
            raise ValueError("报表任务状态无效")
        now = datetime.now(timezone.utc)
        if self._memory:
            row = self._jobs.get(str(job_id))
            if row is None or not self._visible(row, identity):
                return None
            row["status"] = status
            row["updated_at"] = now
            return self._to_job(row)
        pool = await self._pool()
        from src.memory.pg_pool import pg_pool_connection

        tenant_id = int(getattr(identity, "tenant_id", 0) or 0)
        user_id = int(getattr(identity, "user_id", 0) or 0)
        role = str(getattr(identity, "role", "") or "")
        async with pg_pool_connection(pool, tenant_id, user_id, role) as connection:
            row = await connection.fetchrow("UPDATE report_jobs SET status=$1,updated_at=NOW() WHERE id=$2::uuid AND tenant_id=$3 AND ($4<>'super_admin' AND ($4='tenant_admin' OR user_id=$5)) RETURNING id,tenant_id,user_id,prompt,plan_json,status,runs_started,created_at,updated_at", status, str(job_id), tenant_id, role, user_id)
        return self._to_job(row) if row else None

    # 方法作用：创建幂等的报表运行记录并增加任务运行计数。
    # Args: job_id - 任务 ID；identity - 当前身份；attempt - 运行尝试序号。
    # Returns: 新建的 ReportRunRecord。
    async def create_run(self, job_id: str, identity: Any, attempt: int = 1) -> ReportRunRecord:
        """为每次实际运行创建独立标识，尝试编号不充当任务全局幂等键。"""
        job = await self.get_job(job_id, identity)
        if job is None:
            raise LookupError("报表任务不存在")
        run_id = str(uuid4())
        now = datetime.now(timezone.utc)
        row = {"id": run_id, "job_id": str(job_id), "tenant_id": job.tenant_id, "user_id": job.user_id, "status": "queued", "attempt": int(attempt), "last_node": "", "query_count": 0, "row_count": 0, "token_usage": 0, "llm_call_count": 0, "llm_failure_count": 0, "sql_query_count": 0, "sql_success_count": 0, "sql_failure_count": 0, "partial": False, "error_code": "", "internal_error_message": "", "user_error_message": "", "trace_id": run_id, "created_at": now, "started_at": None, "finished_at": None}
        if self._memory:
            self._runs[run_id] = row
            self._steps[run_id] = []
            self._jobs[str(job_id)]["runs_started"] = int(self._jobs[str(job_id)].get("runs_started", 0)) + 1
            self._jobs[str(job_id)]["updated_at"] = now
            return self._to_run(row)
        pool = await self._pool()
        from src.memory.pg_pool import pg_pool_connection

        role = str(getattr(identity, "role", "") or "")
        async with pg_pool_connection(pool, job.tenant_id, job.user_id, role) as connection:
            db_row = await connection.fetchrow(
                "INSERT INTO report_runs (id,job_id,tenant_id,user_id,status,attempt,run_key,trace_id) VALUES ($1::uuid,$2::uuid,$3,$4,'queued',$5,$6,$7) RETURNING *",
                run_id, job_id, job.tenant_id, job.user_id, int(attempt), f"{job_id}:{run_id}", run_id,
            )
            await connection.execute("UPDATE report_jobs SET runs_started=runs_started+1,updated_at=NOW() WHERE id=$1::uuid AND tenant_id=$2", job_id, job.tenant_id)
        return self._to_run(db_row)

    # 方法作用：更新运行状态、错误双字段和资源统计。
    # Args: run_id - 运行 ID；identity - 当前身份；updates - 允许更新的字段。
    # Returns: 更新后的 ReportRunRecord，越权或不存在返回 None。
    async def update_run(self, run_id: str, identity: Any, **updates: Any) -> ReportRunRecord | None:
        allowed = {"status", "last_node", "query_count", "row_count", "token_usage", "llm_call_count", "llm_failure_count", "sql_query_count", "sql_success_count", "sql_failure_count", "partial", "error_code", "internal_error_message", "user_error_message", "started_at", "finished_at"}
        data = {key: value for key, value in updates.items() if key in allowed}
        if self._memory:
            row = self._runs.get(str(run_id))
            if row is None or not self._visible(row, identity):
                return None
            row.update(data)
            return self._to_run(row)
        pool = await self._pool()
        from src.memory.pg_pool import pg_pool_connection

        if not data:
            return await self.get_run(run_id, identity)
        assignments = []
        values: list[Any] = [str(run_id), int(getattr(identity, "tenant_id", 0) or 0)]
        for index, (key, value) in enumerate(data.items(), start=3):
            assignments.append(f"{key}=${index}")
            values.append(value)
        async with pg_pool_connection(pool, values[1], int(getattr(identity, "user_id", 0) or 0), str(getattr(identity, "role", "") or "")) as connection:
            row = await connection.fetchrow(
                f"UPDATE report_runs SET {','.join(assignments)} WHERE id=$1::uuid AND tenant_id=$2 RETURNING *",
                *values,
            )
        return self._to_run(row) if row else None

    # 方法作用：读取当前身份可见的运行记录。
    # Args: run_id - 运行 ID；identity - 当前身份。
    # Returns: 运行摘要或 None。
    async def get_run(self, run_id: str, identity: Any) -> ReportRunRecord | None:
        if self._memory:
            row = self._runs.get(str(run_id))
            return self._to_run(row) if row and self._visible(row, identity) else None
        pool = await self._pool()
        from src.memory.pg_pool import pg_pool_connection

        tenant_id = int(getattr(identity, "tenant_id", 0) or 0)
        user_id = int(getattr(identity, "user_id", 0) or 0)
        role = str(getattr(identity, "role", "") or "")
        async with pg_pool_connection(pool, tenant_id, user_id, role) as connection:
            row = await connection.fetchrow(
                "SELECT * FROM report_runs WHERE id=$1::uuid AND tenant_id=$2 AND ($3<>'super_admin' AND ($3='tenant_admin' OR user_id=$4))",
                str(run_id), tenant_id, role, user_id,
            )
        return self._to_run(row) if row else None

    # 方法作用：列出任务的有界运行记录。
    # Args: job_id - 任务 ID；identity - 当前身份；limit - 返回数量。
    # Returns: 最新运行记录列表。
    async def list_runs(self, job_id: str, identity: Any, limit: int = 20) -> list[ReportRunRecord]:
        job = await self.get_job(job_id, identity)
        if job is None:
            return []
        bounded = max(1, min(int(limit), 100))
        if self._memory:
            rows = [row for row in self._runs.values() if str(row.get("job_id")) == str(job_id)]
            rows.sort(key=lambda row: row.get("created_at") or datetime.min.replace(tzinfo=timezone.utc), reverse=True)
            return [self._to_run(row) for row in rows[:bounded]]
        pool = await self._pool()
        from src.memory.pg_pool import pg_pool_connection

        async with pg_pool_connection(pool, job.tenant_id, job.user_id, str(getattr(identity, "role", "") or "")) as connection:
            rows = await connection.fetch("SELECT * FROM report_runs WHERE job_id=$1::uuid AND tenant_id=$2 ORDER BY created_at DESC LIMIT $3", str(job_id), job.tenant_id, bounded)
        return [self._to_run(row) for row in rows]

    # 方法作用：记录节点级运行日志并区分内部错误与用户可读错误。
    # Args: run_id - 运行 ID；node - 节点名；status - 节点状态；attempt - 节点尝试次数；input_hash/output_ref - 非敏感引用；error_code/internal_error/user_error - 错误字段；identity - 可选当前身份。
    # Returns: 无返回值。
    async def append_step(self, run_id: str, node: str, status: str, *, attempt: int = 1, input_hash: str = "", output_ref: str = "", error_code: str = "", internal_error: str = "", user_error: str = "", identity: Any | None = None) -> None:
        row = {"id": str(uuid4()), "run_id": str(run_id), "seq": len(self._steps.get(str(run_id), [])) + 1, "node": node, "status": status, "attempt": int(attempt), "input_hash": input_hash, "output_ref": output_ref, "error_code": error_code, "internal_error_message": internal_error[:2000], "user_error_message": user_error[:500], "started_at": datetime.now(timezone.utc), "finished_at": datetime.now(timezone.utc)}
        if self._memory:
            self._steps.setdefault(str(run_id), []).append(row)
            return
        pool = await self._pool()
        from src.memory.pg_pool import pg_pool_connection

        run = self._runs.get(str(run_id), {})
        tenant_id = int(getattr(identity, "tenant_id", run.get("tenant_id", 0)) or 0)
        user_id = int(getattr(identity, "user_id", run.get("user_id", 0)) or 0)
        role = str(getattr(identity, "role", "super_admin") or "super_admin")
        async with pg_pool_connection(pool, tenant_id, user_id, role) as connection:
            # 持久化模式的 seq 必须由数据库计算：内存计数在 PG 模式下恒为 1，
            # 第二次写同一节点就会撞 (run_id, seq, attempt) 唯一约束。
            # 多单元并行执行时重试日志会并发插入，先取事务级 advisory lock 串行化同一 run，
            # 否则两个并发写入会算出同一个 MAX(seq)+1。
            await connection.execute("SELECT pg_advisory_xact_lock(hashtextextended($1, 0))", f"report-run-steps:{run_id}")
            await connection.execute(
                "INSERT INTO report_run_steps (id,run_id,seq,node,status,attempt,input_hash,output_ref,error_code,internal_error_message,user_error_message,started_at,finished_at) "
                "VALUES ($1::uuid,$2::uuid,(SELECT COALESCE(MAX(seq),0)+1 FROM report_run_steps WHERE run_id=$2::uuid),$3,$4,$5,$6,$7,$8,$9,$10,$11,$12)",
                row["id"], run_id, node, status, int(attempt), input_hash, output_ref, error_code, internal_error[:2000], user_error[:500], row["started_at"], row["finished_at"],
            )

    # 方法作用：列出某次运行的有界节点日志。
    # Args: run_id - 运行 ID；identity - 当前身份。
    # Returns: 节点日志字典列表。
    async def list_steps(self, run_id: str, identity: Any) -> list[dict[str, Any]]:
        run = await self.get_run(run_id, identity)
        if run is None:
            return []
        if self._memory:
            return [dict(item) for item in self._steps.get(str(run_id), [])]
        pool = await self._pool()
        from src.memory.pg_pool import pg_pool_connection

        async with pg_pool_connection(pool, int(getattr(identity, "tenant_id", 0) or 0), int(getattr(identity, "user_id", 0) or 0), str(getattr(identity, "role", "") or "")) as connection:
            rows = await connection.fetch("SELECT id::text,run_id::text,seq,node,status,attempt,input_hash,output_ref,error_code,user_error_message,started_at,finished_at,duration_ms FROM report_run_steps WHERE run_id=$1::uuid ORDER BY seq LIMIT 100", str(run_id))
        return [dict(row) for row in rows]

    # 方法作用：以不可变版本写入页面、数据清单和校验摘要。
    # Args: job_id - 任务 ID；run_id - 运行 ID；identity - 当前身份；document - 页面字典；manifest - 脱敏数据清单；expires_at - 到期时间。
    # Returns: ReportVersionRecord。
    async def persist_version(self, job_id: str, run_id: str, identity: Any, document: dict[str, Any], manifest: dict[str, Any], expires_at: datetime | None, *, require_active_job: bool = False) -> ReportVersionRecord:
        report_id = str(document.get("report_id") or f"report-{uuid4()}")
        version_id = str(uuid4())
        checksum = self._checksum(document, manifest)
        if self._memory:
            if require_active_job and self._jobs.get(job_id, {}).get("is_paused", True):
                raise asyncio.CancelledError("任务已暂停，阻止发布")
            previous = [item for item in self._versions.values() if item["report_id"] == report_id]
            version = max((int(item["version"]) for item in previous), default=0) + 1
            document = {**document, "version": version}
            checksum = self._checksum(document, manifest)
            for item in previous:
                item["is_latest"] = False
            row = {"id": version_id, "report_id": report_id, "job_id": str(job_id), "run_id": str(run_id), "version": version, "document": document, "data_manifest": manifest, "checksum": checksum, "expires_at": expires_at, "is_latest": True, "tenant_id": int(getattr(identity, "tenant_id", 0) or 0), "user_id": int(getattr(identity, "user_id", 0) or 0)}
            self._versions[version_id] = row
            row["created_at"] = datetime.now(timezone.utc)
            return self._to_version(row)
        pool = await self._pool()
        from src.memory.pg_pool import pg_pool_connection

        tenant_id = int(getattr(identity, "tenant_id", 0) or 0)
        user_id = int(getattr(identity, "user_id", 0) or 0)
        role = str(getattr(identity, "role", "") or "")
        async with pg_pool_connection(pool, tenant_id, user_id, role) as connection:
            async with connection.transaction():
                if require_active_job:
                    await connection.execute("SELECT set_config('app.current_tenant_id',$1,true),set_config('app.current_user_id',$2,true),set_config('app.current_role',$3,true)", str(tenant_id), str(user_id), role)
                    active = await connection.fetchrow("SELECT is_paused FROM report_jobs WHERE id=$1::uuid AND tenant_id=$2 FOR UPDATE", job_id, tenant_id)
                    if active is None or active["is_paused"]:
                        raise asyncio.CancelledError("任务已暂停，阻止发布")
                version = int(await connection.fetchval("SELECT COALESCE(MAX(version),0)+1 FROM report_versions WHERE report_id=$1::uuid AND tenant_id=$2", report_id, tenant_id))
                document = {**document, "version": version}
                checksum = self._checksum(document, manifest)
                await connection.execute("UPDATE report_versions SET is_latest=FALSE WHERE report_id=$1::uuid AND tenant_id=$2", report_id, tenant_id)
                row = await connection.fetchrow("INSERT INTO report_versions (id,report_id,job_id,run_id,tenant_id,version,document_json,data_manifest_json,checksum,expires_at,is_latest) VALUES ($1::uuid,$2::uuid,$3::uuid,$4::uuid,$5,$6,$7::jsonb,$8::jsonb,$9,$10,TRUE) RETURNING *", version_id, report_id, job_id, run_id, tenant_id, version, json.dumps(document, ensure_ascii=False), json.dumps(manifest, ensure_ascii=False), checksum, expires_at)
        return self._to_version(row)

    # 方法作用：保存成功执行后的私有只读模板，仅返回随机标识进入页面。
    # Args: run_id - 当前运行；identity - 实际执行身份；datasource/dialect/sql - 服务端查询。
    # Returns: 私有模板标识。
    async def save_live_query(self, run_id: str, identity: Any, datasource: str, dialect: str, sql: str) -> str:
        """模板不进入公开 DSL、数据清单或分享响应。"""
        logger.debug("实时钻取模板保存入口", run_id=run_id, tenant_id=identity.tenant_id)
        if not sql.strip() or len(sql) > 100_000:
            raise ValueError("实时查询模板长度无效")
        query_id = str(uuid4())
        row = {"id": query_id, "run_id": run_id, "tenant_id": int(identity.tenant_id), "user_id": int(identity.user_id), "datasource": datasource, "dialect": dialect, "sql": sql}
        if self._memory:
            self._live_queries[query_id] = row
        else:
            from src.memory.pg_pool import pg_pool_connection

            pool = await self._pool()
            async with pg_pool_connection(pool, identity.tenant_id, identity.user_id, identity.role) as connection:
                await connection.execute("INSERT INTO report_live_queries (id,run_id,tenant_id,user_id,datasource,dialect,sql_text) VALUES ($1::uuid,$2::uuid,$3,$4,$5,$6,$7)", query_id, run_id, identity.tenant_id, identity.user_id, datasource, dialect, sql)
        logger.info("实时钻取模板保存完成", run_id=run_id, query_id=query_id)
        return query_id

    # 方法作用：只读取与已授权页面运行绑定的私有模板，防止跨页面标识替换。
    # Args: query_id - 随机模板标识；version - 当前授权页面；identity - 当前身份。
    # Returns: 私有模板或 None。
    async def get_live_query(self, query_id: str, version: ReportVersionRecord, identity: Any) -> dict[str, Any] | None:
        """调用者须先完成页面可见性、到期和实时能力校验。"""
        if self._memory:
            row = self._live_queries.get(query_id)
            return dict(row) if row and row["tenant_id"] == identity.tenant_id and row["run_id"] == version.run_id else None
        from src.memory.pg_pool import pg_pool_connection

        pool = await self._pool()
        async with pg_pool_connection(pool, identity.tenant_id, identity.user_id, identity.role) as connection:
            row = await connection.fetchrow("SELECT id,run_id,datasource,dialect,sql_text AS sql FROM report_live_queries WHERE id=$1::uuid AND run_id=$2::uuid AND tenant_id=$3", query_id, version.run_id, identity.tenant_id)
        return dict(row) if row else None

    # 方法作用：读取当前身份可访问的最新报表版本。
    # Args: report_id - 报表 ID；identity - 当前身份。
    # Returns: 最新版本或 None。
    async def get_report_version(self, report_id: str, identity: Any) -> ReportVersionRecord | None:
        if self._memory:
            rows = [row for row in self._versions.values() if row["report_id"] == str(report_id) and self._visible(row, identity) and row.get("deleted_at") is None]
            row = next((item for item in rows if item.get("is_latest")), None) or (max(rows, key=lambda item: item["version"]) if rows else None)
            return self._to_version(row) if row else None
        pool = await self._pool()
        from src.memory.pg_pool import pg_pool_connection

        tenant_id = int(getattr(identity, "tenant_id", 0) or 0)
        user_id = int(getattr(identity, "user_id", 0) or 0)
        role = str(getattr(identity, "role", "") or "")
        async with pg_pool_connection(pool, tenant_id, user_id, role) as connection:
            row = await connection.fetchrow("SELECT v.* FROM report_versions v JOIN report_jobs j ON j.id=v.job_id WHERE v.report_id=$1::uuid AND v.tenant_id=$2 AND v.deleted_at IS NULL AND ($3<>'super_admin' AND ($3='tenant_admin' OR j.user_id=$4)) ORDER BY v.is_latest DESC,v.version DESC LIMIT 1", str(report_id), tenant_id, role, user_id)
        return self._to_version(row) if row else None

    # 方法作用：锁定页面后更新有效期，并收紧而不延长既有分享授权。
    # Args: report_id - 页面标识；identity - 当前身份；days - 延期天数；permanent - 永久保留。
    # Returns: 更新后的页面；不存在、已清理或越权返回 None。
    async def update_page_lifecycle(self, report_id: str, identity: Any, *, days: int | None = None, permanent: bool = False) -> ReportVersionRecord | None:
        """从当前期限与当前时间较晚者延期，永久转有限从现在计算。"""
        request = PageLifecycleRequest(days=days, permanent=permanent)
        logger.debug("页面生命周期更新入口", report_id=report_id, days=days, permanent=permanent)
        if self._memory:
            async with self._claim_lock:
                version = await self.get_report_version(report_id, identity)
                if version is None:
                    return None
                row = self._versions[version.id]
                expiry, document, checksum = self._lifecycle_values(version, request)
                row.update(expires_at=expiry, document=document, checksum=checksum)
                for share in self._shares.values():
                    if share.get("report_version_id") == version.id and expiry is not None:
                        previous = share.get("expires_at")
                        if previous is None or previous > expiry:
                            share["expires_at"] = expiry
                result = self._to_version(row)
        else:
            from src.memory.pg_pool import pg_pool_connection

            pool = await self._pool()
            tenant_id, user_id, role = int(identity.tenant_id), int(identity.user_id), str(identity.role)
            async with pg_pool_connection(pool, tenant_id, user_id, role) as connection:
                async with connection.transaction():
                    row = await connection.fetchrow("SELECT v.* FROM report_versions v JOIN report_jobs j ON j.id=v.job_id WHERE v.report_id=$1::uuid AND v.tenant_id=$2 AND v.deleted_at IS NULL AND ($3='tenant_admin' OR j.user_id=$4) ORDER BY v.is_latest DESC,v.version DESC LIMIT 1 FOR UPDATE OF v", report_id, tenant_id, role, user_id)
                    if row is None:
                        return None
                    version = self._to_version(row)
                    expiry, document, checksum = self._lifecycle_values(version, request)
                    updated = await connection.fetchrow("UPDATE report_versions SET expires_at=$1,document_json=$2::jsonb,checksum=$3 WHERE id=$4::uuid AND tenant_id=$5 RETURNING *", expiry, json.dumps(document, ensure_ascii=False), checksum, version.id, tenant_id)
                    if expiry is not None:
                        await connection.execute("UPDATE report_shares SET expires_at=$1 WHERE report_version_id=$2::uuid AND tenant_id=$3 AND (expires_at IS NULL OR expires_at>$1)", expiry, version.id, tenant_id)
                    result = self._to_version(updated)
        logger.info("页面生命周期更新完成", report_id=report_id, permanent=permanent, expires_at=result.expires_at)
        return result

    # 方法作用：统一内存与数据库页面延期计算，保持内容校验摘要一致。
    # Args: version - 原页面；request - 已校验操作。
    # Returns: 新有效期、页面文档和校验摘要。
    @classmethod
    def _lifecycle_values(cls, version: ReportVersionRecord, request: PageLifecycleRequest) -> tuple[datetime | None, dict[str, Any], str]:
        now = datetime.now(timezone.utc)
        previous = version.expires_at
        if previous is not None and previous.tzinfo is None:
            previous = previous.replace(tzinfo=timezone.utc)
        expiry = None if request.permanent else max(now, previous or now) + timedelta(days=request.days or 0)
        document = {**version.document, "expires_at": expiry.isoformat() if expiry else None}
        return expiry, document, cls._checksum(document, version.data_manifest)

    # 方法作用：按任务读取当前身份可访问的最新不可变报表版本。
    # Args: job_id - 报表任务 ID；identity - 当前租户身份。
    # Returns: 最新 ReportVersionRecord，不存在或越权时返回 None。
    async def get_latest_version_for_job(self, job_id: str, identity: Any) -> ReportVersionRecord | None:
        if self._memory:
            rows = [
                row
                for row in self._versions.values()
                if str(row.get("job_id")) == str(job_id)
                and row.get("deleted_at") is None
                and self._visible(row, identity)
            ]
            row = max(rows, key=lambda item: (item.get("created_at") or datetime.min.replace(tzinfo=timezone.utc), int(item.get("version", 0) or 0)), default=None)
            return self._to_version(row) if row else None
        pool = await self._pool()
        from src.memory.pg_pool import pg_pool_connection

        tenant_id = int(getattr(identity, "tenant_id", 0) or 0)
        user_id = int(getattr(identity, "user_id", 0) or 0)
        role = str(getattr(identity, "role", "") or "")
        async with pg_pool_connection(pool, tenant_id, user_id, role) as connection:
            row = await connection.fetchrow(
                "SELECT v.* FROM report_versions v JOIN report_jobs j ON j.id=v.job_id "
                "WHERE v.job_id=$1::uuid AND v.tenant_id=$2 AND v.deleted_at IS NULL "
                "AND ($3<>'super_admin' AND ($3='tenant_admin' OR j.user_id=$4)) "
                "ORDER BY v.created_at DESC,v.version DESC LIMIT 1",
                str(job_id), tenant_id, role, user_id,
            )
        return self._to_version(row) if row else None

    # 方法作用：按版本 ID 读取公开分享引用的不可变页面。
    # Args: version_id - 页面版本 ID。
    # Returns: 版本记录或 None。
    async def get_version_by_id(self, version_id: str) -> ReportVersionRecord | None:
        if self._memory:
            row = self._versions.get(str(version_id))
            return self._to_version(row) if row and row.get("deleted_at") is None else None
        pool = await self._pool()
        async with pool.acquire() as connection:
            async with connection.transaction():
                await connection.execute("SELECT set_config('app.report_share_access','true',true)")
                row = await connection.fetchrow("SELECT * FROM report_versions WHERE id=$1::uuid AND deleted_at IS NULL", str(version_id))
        return self._to_version(row) if row else None

    # 方法作用：列出报表分享摘要，不返回 token 或授权码。
    # Args: report_id - 报表 ID；identity - 当前身份。
    # Returns: 分享摘要列表。
    async def list_shares(self, report_id: str, identity: Any) -> list[dict[str, Any]]:
        if self._memory:
            rows = [row for row in self._shares.values() if str(row.get("report_id")) == str(report_id) and self._visible(row, identity)]
            return [self._share_public_dict(row) for row in rows]
        pool = await self._pool()
        from src.memory.pg_pool import pg_pool_connection

        async with pg_pool_connection(pool, int(getattr(identity, "tenant_id", 0) or 0), int(getattr(identity, "user_id", 0) or 0), str(getattr(identity, "role", "") or "")) as connection:
            rows = await connection.fetch("SELECT id::text,report_id::text,scope,user_scope,user_ids,allow_drilldown,allow_download,pin_latest,expires_at,revoked_at,view_count,last_viewed_at,created_at FROM report_shares WHERE report_id=$1::uuid AND tenant_id=$2 AND ($3<>'super_admin' AND ($3='tenant_admin' OR created_by=$4)) ORDER BY created_at DESC", report_id, int(getattr(identity, "tenant_id", 0) or 0), str(getattr(identity, "role", "") or ""), int(getattr(identity, "user_id", 0) or 0))
        return [dict(row) for row in rows]

    # 方法作用：保存分享 token 摘要和用户范围授权，不保存明文 token。
    # Args: record - 分享记录字典；token_hash - token 摘要；code_hash - 授权码哈希。
    # Returns: ReportShareRecord。
    async def create_share(self, record: dict[str, Any], token_hash: str, code_hash: str = "") -> ReportShareRecord:
        share_id = str(record.get("id") or uuid4())
        row = {**record, "id": share_id, "token_hash": token_hash, "code_hash": code_hash, "revoked_at": None, "view_count": 0}
        if self._memory:
            self._shares[share_id] = row
            self._share_by_token[token_hash] = share_id
            return self._to_share(row)
        pool = await self._pool()
        from src.memory.pg_pool import pg_pool_connection

        tenant_id = int(record.get("tenant_id", 0) or 0)
        async with pg_pool_connection(pool, 0, 0, "super_admin") as connection:
            db_row = await connection.fetchrow("INSERT INTO report_shares (id,report_version_id,report_id,tenant_id,created_by,token_hash,code_hash,scope,user_scope,user_ids,allow_drilldown,allow_download,pin_latest,expires_at) VALUES ($1::uuid,$2::uuid,$3::uuid,$4,$5,$6,$7,$8,$9,$10::jsonb,$11,$12,$13,$14) RETURNING *", share_id, record["report_version_id"], record["report_id"], tenant_id, record["created_by"], token_hash, code_hash, record.get("scope", "public"), record.get("user_scope", "none"), json.dumps(record.get("user_ids", [])), bool(record.get("allow_drilldown", False)), bool(record.get("allow_download", False)), bool(record.get("pin_latest", False)), record.get("expires_at"))
        return self._to_share(db_row)

    # 方法作用：按 token 摘要读取未撤销的分享并增加访问计数。
    # Args: token_hash - HMAC token 摘要。
    # Returns: 分享记录或 None。
    async def get_share_by_token(self, token_hash: str) -> dict[str, Any] | None:
        if self._memory:
            share_id = self._share_by_token.get(token_hash)
            row = self._shares.get(share_id) if share_id else None
            if row:
                row["view_count"] = int(row.get("view_count", 0)) + 1
            return dict(row) if row else None
        pool = await self._pool()
        async with pool.acquire() as connection:
            async with connection.transaction():
                await connection.execute("SELECT set_config('app.report_share_access','true',true)")
                row = await connection.fetchrow("UPDATE report_shares SET view_count=view_count+1,last_viewed_at=NOW() WHERE token_hash=$1 AND revoked_at IS NULL RETURNING *", token_hash)
        return dict(row) if row else None

    # 方法作用：撤销当前身份可管理的分享链接。
    # Args: share_id - 分享 ID；identity - 当前身份。
    # Returns: 成功撤销返回 True。
    async def revoke_share(self, share_id: str, identity: Any) -> bool:
        if self._memory:
            row = self._shares.get(str(share_id))
            if not row or not self._visible(row, identity):
                return False
            row["revoked_at"] = datetime.now(timezone.utc)
            return True
        pool = await self._pool()
        from src.memory.pg_pool import pg_pool_connection

        async with pg_pool_connection(pool, int(getattr(identity, "tenant_id", 0) or 0), int(getattr(identity, "user_id", 0) or 0), str(getattr(identity, "role", "") or "")) as connection:
            status = await connection.execute("UPDATE report_shares SET revoked_at=NOW() WHERE id=$1::uuid AND tenant_id=$2 AND ($3<>'super_admin' AND ($3='tenant_admin' OR created_by=$4)) AND revoked_at IS NULL", share_id, int(getattr(identity, "tenant_id", 0) or 0), str(getattr(identity, "role", "") or ""), int(getattr(identity, "user_id", 0) or 0))
        return str(status).endswith(" 1")

    # 方法作用：把内部分享记录转换为不含 token 和授权码的摘要。
    # Args: row - 内存分享记录。
    # Returns: 可返回 API 的分享字典。
    @staticmethod
    def _share_public_dict(row: dict[str, Any]) -> dict[str, Any]:
        return {key: value for key, value in row.items() if key not in {"token_hash", "code_hash"}}

    # 方法作用：记录分享访问和 Drilldown 访问的持久化审计事件。
    # Args: event - 不包含完整 token、SQL 或敏感数据的审计字段。
    # Returns: 无返回值；审计失败只记录日志，不阻断页面响应。
    async def record_audit(self, event: dict[str, Any]) -> None:
        safe = {key: value for key, value in event.items() if key not in {"token", "authorization_code", "sql", "prompt"}}
        self._audits.append(safe)
        if self._memory:
            return
        try:
            pool = await self._pool()
            from src.memory.pg_pool import pg_pool_connection

            async with pg_pool_connection(pool, 0, 0, "super_admin") as connection:
                await connection.execute("INSERT INTO report_access_audit (tenant_id,user_id,share_id,report_id,action,result,ip_hash,metadata_json) VALUES ($1,$2,$3::uuid,$4::uuid,$5,$6,$7,$8::jsonb)", safe.get("tenant_id"), safe.get("user_id"), safe.get("share_id"), safe.get("report_id"), safe.get("action", "view"), safe.get("result", "success"), safe.get("ip_hash", ""), json.dumps(safe.get("metadata", {}), ensure_ascii=False))
        except Exception as exc:
            logger.error("报表访问审计写入失败", error=str(exc), exc_info=True)

    # 方法作用：读取用户能力覆盖，供权限服务判断菜单和操作权限。
    # Args: tenant_id - 租户 ID；user_id - 用户 ID。
    # Returns: 能力列表或 None 表示使用角色默认值。
    async def get_user_capabilities(self, tenant_id: int, user_id: int) -> list[str] | None:
        if self._memory:
            return self._capabilities.get((int(tenant_id), int(user_id)))
        try:
            pool = await self._pool()
            from src.memory.pg_pool import pg_pool_connection

            async with pg_pool_connection(pool, tenant_id, user_id, "analyst") as connection:
                value = await connection.fetchval("SELECT capabilities FROM report_user_permissions WHERE tenant_id=$1 AND user_id=$2", tenant_id, user_id)
            if isinstance(value, str):
                value = json.loads(value)
            return list(value or []) if value is not None else None
        except Exception as exc:
            # 无法确认授权时禁止报表业务操作，不能把已撤销能力恢复为角色默认值。
            logger.error(
                "报表用户能力持久化读取失败，拒绝报表操作",
                tenant_id=int(tenant_id),
                user_id=int(user_id),
                error_type=type(exc).__name__,
                exc_info=True,
            )
            return []

    # 方法作用：保存租户管理员配置的用户报表能力。
    # Args: tenant_id - 当前租户；user_id - 目标用户；capabilities - report.* 能力列表。
    # Returns: 无返回值。
    async def set_user_capabilities(self, tenant_id: int, user_id: int, capabilities: list[str]) -> None:
        normalized = list(dict.fromkeys(item.strip() for item in capabilities if item.strip().startswith("report.")))
        if self._memory:
            self._capabilities[(int(tenant_id), int(user_id))] = normalized
            return
        pool = await self._pool()
        from src.memory.pg_pool import pg_pool_connection

        async with pg_pool_connection(pool, tenant_id, user_id, "analyst") as connection:
            await connection.execute("INSERT INTO report_user_permissions (tenant_id,user_id,capabilities) VALUES ($1,$2,$3::jsonb) ON CONFLICT (tenant_id,user_id) DO UPDATE SET capabilities=EXCLUDED.capabilities,updated_at=NOW()", tenant_id, user_id, json.dumps(normalized, ensure_ascii=False))

    # 方法作用：验证当前管理员管理的目标用户归属并锁定账号行。
    # Args: identity - 操作管理员；user_id - 目标用户。
    # Returns: 当前租户内的目标用户，不存在或跨租户时抛出 LookupError。
    async def get_policy_subject(self, identity: Any, user_id: int) -> dict[str, Any]:
        """策略管理不能仅凭客户端用户 ID 写入外租户用户配置。"""
        if str(identity.role) != "tenant_admin" or int(identity.tenant_id) <= 0:
            raise PermissionError("需要当前租户管理员权限")
        from src.memory.pg_pool import pg_pool_connection

        async with pg_pool_connection(await self._pool(), int(identity.tenant_id), int(identity.user_id), str(identity.role)) as connection:
            row = await connection.fetchrow(
                "SELECT id, tenant_id, role, username FROM users WHERE id=$1 AND tenant_id=$2 FOR SHARE",
                int(user_id), int(identity.tenant_id),
            )
        if row is None:
            raise LookupError("目标用户不存在或不属于当前租户")
        return dict(row)

    # 方法作用：验证目标租户归属并在同一事务保存能力与额度。
    # Args: identity - 租户管理员；user_id - 目标用户；policy - 额度策略；capabilities - 能力列表。
    # Returns: 无返回值。
    async def save_user_policy(self, identity: Any, user_id: int, policy: Any, capabilities: list[str]) -> None:
        """数据库写入失败时全部回滚，调用方不能提前更新缓存。"""
        from src.memory.pg_pool import pg_pool_connection

        logger.debug("保存租户报表策略入口", tenant_id=identity.tenant_id, user_id=user_id)
        async with pg_pool_connection(await self._pool(), int(identity.tenant_id), int(identity.user_id), str(identity.role)) as connection:
            scoped = ReportStore(pg_pool=connection, memory=False)
            await scoped.get_policy_subject(identity, user_id)
            await scoped.set_quota_policy(int(identity.tenant_id), int(user_id), policy)
            await scoped.set_user_capabilities(int(identity.tenant_id), int(user_id), capabilities)
        logger.info("租户报表策略保存完成", tenant_id=identity.tenant_id, user_id=user_id)

    # 方法作用：读取用户持久化配额覆盖，缺失时交给调用方使用默认策略。
    # Args: tenant_id - 租户 ID；user_id - 用户 ID。
    # Returns: 配额字段字典或 None。
    async def get_quota_policy(self, tenant_id: int, user_id: int) -> dict[str, Any] | None:
        if self._memory:
            value = self._quota_policies.get((int(tenant_id), int(user_id)))
            return dict(value) if value else None
        pool = await self._pool()
        from src.memory.pg_pool import pg_pool_connection

        async with pg_pool_connection(pool, tenant_id, user_id, "analyst") as connection:
            row = await connection.fetchrow("SELECT max_jobs_created,max_runs_started,max_concurrent_runs,max_analysis_units_per_run,max_queries_per_run,max_rows_per_query,max_llm_tokens,max_page_bytes,max_share_links,timezone FROM report_quota_policies WHERE tenant_id=$1 AND user_id=$2", tenant_id, user_id)
        return dict(row) if row else None

    # 方法作用：持久化租户管理员设置的用户报表配额覆盖。
    # Args: tenant_id - 租户 ID；user_id - 目标用户；policy - QuotaPolicy 或字段字典；timezone_name - 周期时区。
    # Returns: 无返回值。
    async def set_quota_policy(self, tenant_id: int, user_id: int, policy: Any, timezone_name: str = "Asia/Shanghai") -> None:
        fields = asdict(policy) if is_dataclass(policy) else (dict(policy) if isinstance(policy, dict) else {})
        if self._memory:
            self._quota_policies[(int(tenant_id), int(user_id))] = {**fields, "timezone": timezone_name}
            return
        pool = await self._pool()
        from src.memory.pg_pool import pg_pool_connection

        async with pg_pool_connection(pool, tenant_id, user_id, "analyst") as connection:
            await connection.execute("INSERT INTO report_quota_policies (tenant_id,user_id,timezone,max_jobs_created,max_runs_started,max_concurrent_runs,max_analysis_units_per_run,max_queries_per_run,max_rows_per_query,max_llm_tokens,max_page_bytes,max_share_links) VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11,$12) ON CONFLICT (tenant_id,user_id) DO UPDATE SET timezone=EXCLUDED.timezone,max_jobs_created=EXCLUDED.max_jobs_created,max_runs_started=EXCLUDED.max_runs_started,max_concurrent_runs=EXCLUDED.max_concurrent_runs,max_analysis_units_per_run=EXCLUDED.max_analysis_units_per_run,max_queries_per_run=EXCLUDED.max_queries_per_run,max_rows_per_query=EXCLUDED.max_rows_per_query,max_llm_tokens=EXCLUDED.max_llm_tokens,max_page_bytes=EXCLUDED.max_page_bytes,max_share_links=EXCLUDED.max_share_links", tenant_id, user_id, timezone_name, fields.get("max_jobs_created", 20), fields.get("max_runs_started", 50), fields.get("max_concurrent_runs", 2), fields.get("max_analysis_units_per_run", 12), fields.get("max_queries_per_run", 16), fields.get("max_rows_per_query", 100000), fields.get("max_llm_tokens", 200000), fields.get("max_page_bytes", 2097152), fields.get("max_share_links", 20))

    # 方法作用：用 PostgreSQL 条件 UPDATE 原子预占周期配额，避免多 Worker 超额。
    # Args: identity - 当前身份；increments - jobs/runs/concurrent/queries/rows/tokens/shares 增量；policy - 当前限制；period_start/period_end - 周期边界。
    # Returns: 更新后的用量字典；条件不满足返回 None。
    async def reserve_quota(self, identity: Any, increments: dict[str, int], policy: Any, period_start: datetime, period_end: datetime) -> dict[str, Any] | None:
        if self._memory:
            return None
        pool = await self._pool()
        tenant_id = int(getattr(identity, "tenant_id", 0) or 0)
        user_id = int(getattr(identity, "user_id", 0) or 0)
        fields = asdict(policy) if is_dataclass(policy) else (dict(policy) if isinstance(policy, dict) else {})
        from src.memory.pg_pool import pg_pool_connection

        async with pg_pool_connection(pool, tenant_id, user_id, str(identity.role)) as connection:
            await connection.execute("INSERT INTO report_quota_usage (tenant_id,user_id,period_start,period_end) VALUES ($1,$2,$3,$4) ON CONFLICT (tenant_id,user_id,period_start) DO NOTHING", tenant_id, user_id, period_start, period_end)
            row = await connection.fetchrow(
                "UPDATE report_quota_usage SET jobs_created=jobs_created+$3,runs_started=runs_started+$4,"
                "concurrent_runs=concurrent_runs+$5,analysis_units=analysis_units+$6,queries=queries+$7,"
                "rows=rows+$8,llm_tokens=llm_tokens+$9,share_links=share_links+$10 "
                "WHERE tenant_id=$1 AND user_id=$2 AND period_start=$11 "
                # 上限参数必须显式转型：$6 <= $15 这类占位符互比没有列上下文，
                # PostgreSQL 会把它们推断成 text，与 jobs_created+$3 的整数用法冲突。
                "AND jobs_created+$3 <= $12::bigint AND runs_started+$4 <= $13::bigint AND concurrent_runs+$5 <= $14::bigint "
                "AND $6::bigint <= $15::bigint AND $7::bigint <= $16::bigint AND $8::bigint <= $17::bigint "
                "AND llm_tokens+$9 <= $18::bigint AND share_links+$10 <= $19::bigint RETURNING *",
                tenant_id, user_id, int(increments.get("jobs", 0)), int(increments.get("runs", 0)),
                int(increments.get("concurrent", 0)), int(increments.get("units", 0)),
                int(increments.get("queries", 0)), int(increments.get("rows", 0)),
                int(increments.get("tokens", 0)), int(increments.get("shares", 0)), period_start,
                fields.get("max_jobs_created", 20), fields.get("max_runs_started", 50),
                fields.get("max_concurrent_runs", 2), fields.get("max_analysis_units_per_run", 12),
                fields.get("max_queries_per_run", 16), fields.get("max_rows_per_query", 100000),
                fields.get("max_llm_tokens", 200000), fields.get("max_share_links", 20),
            )
        return dict(row) if row else None

    # 方法作用：按显式身份读取指定周期的真实持久化用量。
    # Args: identity - 当前身份；period_start - 周期起点。
    # Returns: 用量字典，没有记录时返回空字典。
    async def get_quota_usage(self, identity: Any, period_start: datetime) -> dict[str, Any]:
        """供配额展示和事务内幂等重试查询使用。"""
        if self._memory:
            return {}
        from src.memory.pg_pool import pg_pool_connection

        async with pg_pool_connection(await self._pool(), int(identity.tenant_id), int(identity.user_id), str(identity.role)) as connection:
            row = await connection.fetchrow(
                "SELECT * FROM report_quota_usage WHERE tenant_id=$1 AND user_id=$2 AND period_start=$3",
                int(identity.tenant_id), int(identity.user_id), period_start,
            )
        return dict(row) if row else {}

    # 方法作用：释放 PostgreSQL 周期用量中的并发运行槽，避免失败或完成后永久占用。
    # Args: identity - 当前租户身份；amount - 释放槽位数；timezone_name - 周期时区；period_start - 原预占周期起点。
    # Returns: 无返回值。
    async def release_concurrent_quota(self, identity: Any, amount: int = 1, timezone_name: str = "Asia/Shanghai", period_start: datetime | None = None) -> None:
        """按原周期参数化减少并发用量，不回滚已经发生的运行次数。"""
        bounded = max(0, int(amount))
        if bounded == 0:
            return
        if self._memory:
            return
        from src.memory.pg_pool import pg_pool_connection

        period_start = period_start or self._period_bounds(timezone_name)[0]
        tenant_id = int(getattr(identity, "tenant_id", 0) or 0)
        user_id = int(getattr(identity, "user_id", 0) or 0)
        pool = await self._pool()
        async with pg_pool_connection(pool, tenant_id, user_id, str(getattr(identity, "role", "") or "")) as connection:
            await connection.execute(
                "UPDATE report_quota_usage SET concurrent_runs=GREATEST(0, concurrent_runs-$3) WHERE tenant_id=$1 AND user_id=$2 AND period_start=$4",
                tenant_id,
                user_id,
                bounded,
                period_start,
            )

    # 方法作用：按租户时区计算当前自然月周期起止时间，供持久化配额读写复用。
    # Args: timezone_name - IANA 时区名称。
    # Returns: UTC 周期起点和终点。
    @staticmethod
    def _period_bounds(timezone_name: str = "Asia/Shanghai") -> tuple[datetime, datetime]:
        from zoneinfo import ZoneInfo

        try:
            zone = ZoneInfo(timezone_name)
        except Exception:
            zone = ZoneInfo("Asia/Shanghai")
        local_now = datetime.now(timezone.utc).astimezone(zone)
        start_local = local_now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
        next_local = start_local.replace(year=start_local.year + 1, month=1) if start_local.month == 12 else start_local.replace(month=start_local.month + 1)
        return start_local.astimezone(timezone.utc), next_local.astimezone(timezone.utc)

    # 方法作用：按数据库方言认领到期任务，PG 使用 SKIP LOCKED，SQLite 使用应用层锁。
    # Args: limit - 最大认领数量。
    # Returns: 本实例认领的任务摘要列表。
    async def claim_due_jobs(self, limit: int = 20) -> list[ReportJobRecord]:
        bounded = max(1, min(int(limit), 100))
        if self._memory:
            async with self._claim_lock:
                now = datetime.now(timezone.utc)
                rows = [row for row in self._jobs.values() if (row.get("status") == "queued" and not row.get("is_paused", False) and row.get("next_run_at") and row["next_run_at"] <= now) or (row.get("status") == "running" and row.get("updated_at", now) < now - timedelta(seconds=120))]
                for row in rows[:bounded]:
                    row["status"] = "running"
                    row["updated_at"] = now
                return [self._to_job(row) for row in rows[:bounded]]
        pool = await self._pool()
        from src.memory.pg_pool import pg_pool_connection

        async with pg_pool_connection(pool, 0, 0, "super_admin") as connection:
            if self.dialect in {"sqlite", "sqlite3"}:
                # SQLite 没有 SKIP LOCKED；应用锁配合 queued 条件更新，确保同一进程内只认领一次。
                async with self._claim_lock:
                    candidates = await connection.fetch(
                        "SELECT * FROM report_jobs WHERE status='queued' AND next_run_at<=CURRENT_TIMESTAMP ORDER BY next_run_at LIMIT $1",
                        bounded,
                    )
                    rows = []
                    for candidate in candidates:
                        claimed = await connection.fetchrow(
                            "UPDATE report_jobs SET status='running',updated_at=CURRENT_TIMESTAMP WHERE id=$1 AND status='queued' RETURNING *",
                            candidate["id"],
                        )
                        if claimed is not None:
                            rows.append(claimed)
            else:
                rows = await connection.fetch("WITH due AS (SELECT id FROM report_jobs WHERE (status='queued' AND NOT is_paused AND next_run_at<=NOW()) OR (status='running' AND updated_at<NOW()-INTERVAL '120 seconds') ORDER BY next_run_at FOR UPDATE SKIP LOCKED LIMIT $1) UPDATE report_jobs SET status='running',updated_at=NOW() FROM due WHERE report_jobs.id=due.id RETURNING report_jobs.*", bounded)
        return [self._to_job(row) for row in rows]

    # 方法作用：持久化暂停标志，不改变既有次数或绕过租户归属。
    # Args: job_id - 任务；identity - 当前租户身份；paused - 是否暂停。
    # Returns: 是否更新成功。
    async def set_job_paused(self, job_id: str, identity: Any, paused: bool) -> bool:
        """暂停中的执行在发布前再次检查，已发布历史页面不受影响。"""
        logger.debug("报表暂停状态入口", job_id=job_id, paused=paused)
        if self._memory:
            row = self._jobs.get(job_id)
            if row is None or not self._visible(row, identity):
                return False
            row["is_paused"] = paused
        else:
            from src.memory.pg_pool import pg_pool_connection

            async with pg_pool_connection(await self._pool(), identity.tenant_id, identity.user_id, identity.role) as connection:
                row = await connection.fetchrow("UPDATE report_jobs SET is_paused=$1,updated_at=NOW() WHERE id=$2::uuid AND tenant_id=$3 AND (user_id=$4 OR $5='tenant_admin') RETURNING id", paused, job_id, identity.tenant_id, identity.user_id, identity.role)
                if row is None:
                    return False
        logger.info("报表暂停状态更新完成", job_id=job_id, paused=paused)
        return True

    # 方法作用：写入下次运行时间与最终调度状态，心跳只刷新更新时间。
    # Args: job_id - 任务；identity - 身份；status - 状态；next_run_at - 下次时间；heartbeat - 仅更新心跳。
    # Returns: 无返回值。
    async def update_dispatch(self, job_id: str, identity: Any, *, status: str = "running", next_run_at: datetime | None = None, heartbeat: bool = False) -> None:
        """运行器和恢复扫描使用同一持久化更新时间。"""
        if self._memory:
            row = self._jobs.get(job_id)
            if row is not None and self._visible(row, identity):
                row["updated_at"] = datetime.now(timezone.utc)
                if not heartbeat:
                    row.update(status=status, next_run_at=next_run_at)
            return
        from src.memory.pg_pool import pg_pool_connection

        async with pg_pool_connection(await self._pool(), identity.tenant_id, identity.user_id, identity.role) as connection:
            if heartbeat:
                await connection.execute("UPDATE report_jobs SET updated_at=NOW() WHERE id=$1::uuid AND tenant_id=$2", job_id, identity.tenant_id)
            else:
                await connection.execute("UPDATE report_jobs SET status=$1,next_run_at=$2,updated_at=NOW() WHERE id=$3::uuid AND tenant_id=$4", status, next_run_at, job_id, identity.tenant_id)

    # 方法作用：跨 Worker 持有任务专属会话锁，锁连接同时用于发布以阻断失联旧 Worker。
    # Args: job_id - 任务标识。
    # Returns: 专属发布 Store；其他实例持锁时返回 None。
    @asynccontextmanager
    async def execution_lock(self, job_id: str) -> AsyncIterator["ReportStore | None"]:
        """数据库连接失联会自动释放锁，旧连接也无法提交页面。"""
        if self._memory:
            lock = self._execution_locks.setdefault(job_id, asyncio.Lock())
            if lock.locked():
                yield None
                return
            async with lock:
                yield self
            return
        pool = await self._pool()
        async with pool.acquire() as connection:
            lock_name = f"report-dispatch:{job_id}"
            acquired = await connection.fetchval("SELECT pg_try_advisory_lock(hashtextextended($1, 0))", lock_name)
            if not acquired:
                yield None
                return
            try:
                yield ReportStore(pg_pool=connection, memory=False)
            finally:
                if not connection.is_closed():
                    await connection.execute("SELECT pg_advisory_unlock(hashtextextended($1, 0))", lock_name)

    # 方法作用：将配额预占和原周期租约写入同一事务，防止进程退出遗留未知并发槽。
    # Args: run_id - 运行；identity - 身份；plan - 资源估算；quota - 配额服务。
    # Returns: 已持久化的配额快照。
    async def reserve_run_quota(self, run_id: str, identity: Any, plan: ReportPlan, quota: Any) -> Any:
        """数据库事务失败时，预占和租约同时回滚。"""
        parameters = {"runs": 1, "concurrent": 1, "units": len(plan.analysis_units), "queries": plan.estimated.query_count, "tokens": plan.estimated.llm_tokens, "timezone_name": plan.timezone}
        if self._memory:
            reservation = await quota.reserve(identity, **parameters)
            self._runs[run_id].update(quota_held=True, quota_period_start=reservation.period_start)
            return reservation
        from src.memory.pg_pool import pg_pool_connection
        from src.reporting.quota import QuotaManager

        async with pg_pool_connection(await self._pool(), identity.tenant_id, identity.user_id, identity.role) as connection:
            scoped = QuotaManager(store=ReportStore(pg_pool=connection, memory=False), default_policy=quota.default_policy)
            reservation = await scoped.reserve(identity, **parameters)
            await connection.execute("UPDATE report_runs SET quota_held=TRUE,quota_period_start=$1 WHERE id=$2::uuid AND tenant_id=$3", reservation.period_start, run_id, identity.tenant_id)
        return reservation

    # 方法作用：只释放一次运行持有的原周期并发槽，正常收尾和恢复扫描共用。
    # Args: run_id - 运行；identity - 原业务身份；quota - 配额服务。
    # Returns: 无返回值。
    async def release_run_quota(self, run_id: str, identity: Any, quota: Any) -> None:
        """次数和 Token 预算不退款，仅释放可重用并发槽。"""
        if self._memory:
            async with self._claim_lock:
                row = self._runs.get(run_id)
                if row and row.get("quota_held"):
                    await quota.release_concurrent(identity, period_start=row["quota_period_start"])
                    row["quota_held"] = False
            return
        from src.memory.pg_pool import pg_pool_connection

        async with pg_pool_connection(await self._pool(), identity.tenant_id, identity.user_id, identity.role) as connection:
            row = await connection.fetchrow("SELECT quota_held,quota_period_start FROM report_runs WHERE id=$1::uuid AND tenant_id=$2 FOR UPDATE", run_id, identity.tenant_id)
            if row and row["quota_held"]:
                # report_quota_usage 没有 updated_at 列，写它会直接报 UndefinedColumnError。
                await connection.execute("UPDATE report_quota_usage SET concurrent_runs=GREATEST(0,concurrent_runs-1) WHERE tenant_id=$1 AND user_id=$2 AND period_start=$3", identity.tenant_id, identity.user_id, row["quota_period_start"])
                await connection.execute("UPDATE report_runs SET quota_held=FALSE WHERE id=$1::uuid AND tenant_id=$2", run_id, identity.tenant_id)

    # 方法作用：取得独占执行锁后终结旧进程遗留运行并释放可确认的配额租约。
    # Args: job_id - 任务；identity - 原任务拥有者身份；quota - 配额服务。
    # Returns: 中断恢复数与已发布页面恢复数。
    async def recover_job_runs(self, job_id: str, identity: Any, quota: Any) -> dict[str, int]:
        """不覆盖已经成功的页面，后续运行使用新的 run_id 与审计记录。"""
        runs = await self.list_runs(job_id, identity, 100)
        published = await self.get_latest_version_for_job(job_id, identity)
        logger.info("报表恢复持久化探针", job_id=job_id, run_count=len(runs), published_run_id=published.run_id if published else None)
        recovered = 0
        published_count = 0
        for run in runs:
            await self.release_run_quota(run.id, identity, quota)
            if run.status in {"queued", "running"}:
                if self._memory:
                    has_version = any(row["run_id"] == run.id and row.get("deleted_at") is None for row in self._versions.values())
                else:
                    from src.memory.pg_pool import pg_pool_connection

                    async with pg_pool_connection(await self._pool(), identity.tenant_id, identity.user_id, identity.role) as connection:
                        has_version = bool(await connection.fetchval("SELECT 1 FROM report_versions WHERE run_id=$1::uuid AND tenant_id=$2 AND deleted_at IS NULL LIMIT 1", run.id, identity.tenant_id))
                if has_version:
                    await self.update_run(run.id, identity, status="succeeded", finished_at=datetime.now(timezone.utc))
                    published_count += 1
                else:
                    await self.update_run(run.id, identity, status="failed", error_code="WORKER_INTERRUPTED", user_error_message="执行进程中断，已释放并发资源并进入恢复", finished_at=datetime.now(timezone.utc))
                recovered += 1
        if recovered:
            logger.info("报表遗留运行恢复完成", job_id=job_id, recovered=recovered)
        return {"interrupted": recovered, "published": published_count}

    # 方法作用：惰性获取 PostgreSQL 连接池。
    # Args: self - 当前 Store。
    # Returns: asyncpg Pool。
    async def _pool(self) -> Any:
        if self._pg_pool is None:
            from src.memory.pg_pool import get_pg_pool

            self._pg_pool = await get_pg_pool()
        return self._pg_pool

    # 方法作用：判断数据库记录是否对当前身份可见。
    # Args: row - 含 tenant_id/user_id/created_by 的记录；identity - 当前身份。
    # Returns: 同租户且拥有者或管理员返回 True。
    @staticmethod
    def _visible(row: dict[str, Any], identity: Any) -> bool:
        tenant_id = int(getattr(identity, "tenant_id", 0) or 0)
        user_id = int(getattr(identity, "user_id", 0) or 0)
        role = str(getattr(identity, "role", "") or "")
        if int(row.get("tenant_id", -1) or -1) != tenant_id:
            return False
        owner = row.get("user_id", row.get("created_by", -1))
        return role != "super_admin" and (role == "tenant_admin" or int(owner or -1) == user_id)

    # 方法作用：计算页面和数据清单的稳定校验摘要。
    # Args: document - 页面 JSON；manifest - 脱敏数据清单。
    # Returns: sha256 摘要。
    @staticmethod
    def _checksum(document: dict[str, Any], manifest: dict[str, Any]) -> str:
        import hashlib

        payload = json.dumps({"document": document, "manifest": manifest}, ensure_ascii=False, sort_keys=True, default=str, separators=(",", ":"))
        return hashlib.sha256(payload.encode("utf-8")).hexdigest()

    # 方法作用：把任意数据库 JSON 字段解析成字典。
    # Args: value - asyncpg JSONB 或字符串。
    # Returns: 字典。
    @staticmethod
    def _json_dict(value: Any) -> dict[str, Any]:
        if isinstance(value, dict):
            return dict(value)
        if isinstance(value, str):
            try:
                loaded = json.loads(value)
                return dict(loaded) if isinstance(loaded, dict) else {}
            except (TypeError, ValueError):
                return {}
        return {}

    # 方法作用：把数据库行转换为安全的任务模型。
    # Args: row - asyncpg Record 或内存字典。
    # Returns: ReportJobRecord。
    @classmethod
    def _to_job(cls, row: Any) -> ReportJobRecord:
        plan_data = cls._json_dict(row.get("plan_json", {}) if hasattr(row, "get") else row["plan_json"])
        return ReportJobRecord(id=str(row["id"]), tenant_id=int(row["tenant_id"]), user_id=int(row["user_id"]), prompt=str(row["prompt"]), plan=ReportPlan.model_validate(plan_data), status=str(row.get("status", "draft")), runs_started=int(row.get("runs_started", 0) or 0), next_run_at=row.get("next_run_at"), is_paused=bool(row.get("is_paused", False)), created_at=row.get("created_at"), updated_at=row.get("updated_at"))

    # 方法作用：把数据库行转换为安全的运行模型。
    # Args: row - asyncpg Record 或内存字典。
    # Returns: ReportRunRecord。
    @staticmethod
    def _to_run(row: Any) -> ReportRunRecord:
        return ReportRunRecord(id=str(row["id"]), job_id=str(row["job_id"]), status=str(row.get("status", "queued")), attempt=int(row.get("attempt", 1) or 1), last_node=str(row.get("last_node", "") or ""), query_count=int(row.get("query_count", 0) or 0), row_count=int(row.get("row_count", 0) or 0), token_usage=int(row.get("token_usage", 0) or 0), llm_call_count=int(row.get("llm_call_count", 0) or 0), llm_failure_count=int(row.get("llm_failure_count", 0) or 0), sql_query_count=int(row.get("sql_query_count", 0) or 0), sql_success_count=int(row.get("sql_success_count", 0) or 0), sql_failure_count=int(row.get("sql_failure_count", 0) or 0), partial=bool(row.get("partial", False)), error_code=str(row.get("error_code", "") or ""), user_error_message=str(row.get("user_error_message", "") or ""), trace_id=str(row.get("trace_id", "") or ""), started_at=row.get("started_at"), finished_at=row.get("finished_at"))

    # 方法作用：把数据库行转换为安全的版本模型。
    # Args: row - asyncpg Record 或内存字典。
    # Returns: ReportVersionRecord。
    @classmethod
    def _to_version(cls, row: Any) -> ReportVersionRecord:
        document = cls._json_dict(row.get("document_json", row.get("document", {})))
        manifest = cls._json_dict(row.get("data_manifest_json", row.get("data_manifest", {})))
        return ReportVersionRecord(id=str(row["id"]), report_id=str(row["report_id"]), job_id=str(row["job_id"]), run_id=str(row["run_id"]), version=int(row["version"]), document=document, data_manifest=manifest, checksum=str(row.get("checksum", "")), expires_at=row.get("expires_at"), is_latest=bool(row.get("is_latest", True)))

    # 方法作用：把数据库行转换为不含 token/授权码的分享模型。
    # Args: row - asyncpg Record 或内存字典。
    # Returns: ReportShareRecord。
    @classmethod
    def _to_share(cls, row: Any) -> ReportShareRecord:
        users = row.get("user_ids", [])
        if isinstance(users, str):
            try:
                users = json.loads(users)
            except (TypeError, ValueError):
                users = []
        return ReportShareRecord(id=str(row["id"]), report_id=str(row.get("report_id", row.get("report_version_id", ""))), version=int(row.get("version", 1) or 1), scope=str(row.get("scope", "public")), user_ids=[int(item) for item in users or []], allow_drilldown=bool(row.get("allow_drilldown", False)), allow_download=bool(row.get("allow_download", False)), pin_latest=bool(row.get("pin_latest", False)), expires_at=row.get("expires_at"), revoked_at=row.get("revoked_at"))


# 方法作用：获取当前应用上下文中的报表 Store 单例。
# Args: 无。
# Returns: ReportStore。
def get_report_store() -> ReportStore:
    from src.app_context import get_app_context

    configured = bool(str(getattr(get_settings(), "database_url", "") or ""))
    return get_app_context().get_or_create(
        "report_store",
        lambda: ReportStore(memory=not configured),
    )
