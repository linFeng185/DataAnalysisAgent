"""报表工作台持久化 Store：PostgreSQL 生产分支与 SQLite/内存测试分支。"""

from __future__ import annotations

import asyncio
import json
from dataclasses import asdict, is_dataclass
from datetime import datetime, timezone
from typing import Any
from uuid import uuid4

from src.config import get_settings
from src.logging_config import get_logger
from src.reporting.models import (
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
        self._jobs: dict[str, dict[str, Any]] = {}
        self._idempotency: dict[tuple[int, int, str], str] = {}
        self._runs: dict[str, dict[str, Any]] = {}
        self._steps: dict[str, list[dict[str, Any]]] = {}
        self._versions: dict[str, dict[str, Any]] = {}
        self._shares: dict[str, dict[str, Any]] = {}
        self._share_by_token: dict[str, str] = {}
        self._audits: list[dict[str, Any]] = []
        self._capabilities: dict[tuple[int, int], list[str]] = {}
        self._quota_policies: dict[tuple[int, int], dict[str, Any]] = {}
        logger.debug("ReportStore 初始化完成", dialect=self.dialect, memory=self._memory)

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
                "status": "queued" if plan.execution_mode == "immediate" else "draft",
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
                "INSERT INTO report_jobs (id,tenant_id,user_id,prompt,plan_json,plan_hash,execution_mode,frequency,timezone,max_runs,status,idempotency_key) "
                "VALUES ($1::uuid,$2,$3,$4,$5::jsonb,$6,$7,$8,$9,$10,$11,$12) RETURNING id,tenant_id,user_id,prompt,plan_json,status,runs_started,created_at,updated_at",
                job_id, tenant_id, user_id, prompt, json.dumps(plan.model_dump(mode="json"), ensure_ascii=False),
                plan.canonical_hash(), plan.execution_mode, plan.frequency, plan.timezone, plan.max_runs,
                "queued" if plan.execution_mode == "immediate" else "draft", idempotency_key.strip() or None,
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
                "SELECT id,tenant_id,user_id,prompt,plan_json,status,runs_started,created_at,updated_at FROM report_jobs "
                "WHERE id=$1::uuid AND tenant_id=$2 AND ($3 IN ('super_admin','tenant_admin') OR user_id=$4)",
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
                "SELECT id,tenant_id,user_id,prompt,plan_json,status,runs_started,created_at,updated_at FROM report_jobs "
                "WHERE tenant_id=$1 AND ($2 IN ('super_admin','tenant_admin') OR user_id=$3) ORDER BY updated_at DESC LIMIT $4",
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
            row = await connection.fetchrow("UPDATE report_jobs SET status=$1,updated_at=NOW() WHERE id=$2::uuid AND tenant_id=$3 AND ($4 IN ('super_admin','tenant_admin') OR user_id=$5) RETURNING id,tenant_id,user_id,prompt,plan_json,status,runs_started,created_at,updated_at", status, str(job_id), tenant_id, role, user_id)
        return self._to_job(row) if row else None

    # 方法作用：创建幂等的报表运行记录并增加任务运行计数。
    # Args: job_id - 任务 ID；identity - 当前身份；attempt - 运行尝试序号。
    # Returns: 新建的 ReportRunRecord。
    async def create_run(self, job_id: str, identity: Any, attempt: int = 1) -> ReportRunRecord:
        job = await self.get_job(job_id, identity)
        if job is None:
            raise LookupError("报表任务不存在")
        run_id = str(uuid4())
        now = datetime.now(timezone.utc)
        row = {"id": run_id, "job_id": str(job_id), "tenant_id": job.tenant_id, "user_id": job.user_id, "status": "queued", "attempt": int(attempt), "last_node": "", "query_count": 0, "row_count": 0, "token_usage": 0, "partial": False, "error_code": "", "internal_error_message": "", "user_error_message": "", "trace_id": run_id, "created_at": now, "started_at": None, "finished_at": None}
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
                run_id, job_id, job.tenant_id, job.user_id, int(attempt), f"{job_id}:{attempt}", run_id,
            )
            await connection.execute("UPDATE report_jobs SET runs_started=runs_started+1,updated_at=NOW() WHERE id=$1::uuid AND tenant_id=$2", job_id, job.tenant_id)
        return self._to_run(db_row)

    # 方法作用：更新运行状态、错误双字段和资源统计。
    # Args: run_id - 运行 ID；identity - 当前身份；updates - 允许更新的字段。
    # Returns: 更新后的 ReportRunRecord，越权或不存在返回 None。
    async def update_run(self, run_id: str, identity: Any, **updates: Any) -> ReportRunRecord | None:
        allowed = {"status", "last_node", "query_count", "row_count", "token_usage", "partial", "error_code", "internal_error_message", "user_error_message", "started_at", "finished_at"}
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
                "SELECT * FROM report_runs WHERE id=$1::uuid AND tenant_id=$2 AND ($3 IN ('super_admin','tenant_admin') OR user_id=$4)",
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
    # Args: run_id - 运行 ID；node - 节点名；status - 节点状态；attempt - 节点尝试次数；input_hash/output_ref - 非敏感引用；error_code/internal_error/user_error - 错误字段。
    # Returns: 无返回值。
    async def append_step(self, run_id: str, node: str, status: str, *, attempt: int = 1, input_hash: str = "", output_ref: str = "", error_code: str = "", internal_error: str = "", user_error: str = "") -> None:
        row = {"id": str(uuid4()), "run_id": str(run_id), "seq": len(self._steps.get(str(run_id), [])) + 1, "node": node, "status": status, "attempt": int(attempt), "input_hash": input_hash, "output_ref": output_ref, "error_code": error_code, "internal_error_message": internal_error[:2000], "user_error_message": user_error[:500], "started_at": datetime.now(timezone.utc), "finished_at": datetime.now(timezone.utc)}
        if self._memory:
            self._steps.setdefault(str(run_id), []).append(row)
            return
        pool = await self._pool()
        from src.memory.pg_pool import pg_pool_connection

        run = self._runs.get(str(run_id), {})
        async with pg_pool_connection(pool, int(run.get("tenant_id", 0)), int(run.get("user_id", 0)), "super_admin") as connection:
            await connection.execute(
                "INSERT INTO report_run_steps (id,run_id,seq,node,status,attempt,input_hash,output_ref,error_code,internal_error_message,user_error_message,started_at,finished_at) VALUES ($1::uuid,$2::uuid,$3,$4,$5,$6,$7,$8,$9,$10,$11,$12,$13)",
                row["id"], run_id, row["seq"], node, status, int(attempt), input_hash, output_ref, error_code, internal_error[:2000], user_error[:500], row["started_at"], row["finished_at"],
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
    async def persist_version(self, job_id: str, run_id: str, identity: Any, document: dict[str, Any], manifest: dict[str, Any], expires_at: datetime | None) -> ReportVersionRecord:
        report_id = str(document.get("report_id") or f"report-{uuid4()}")
        version_id = str(uuid4())
        checksum = self._checksum(document, manifest)
        if self._memory:
            previous = [item for item in self._versions.values() if item["report_id"] == report_id]
            version = max((int(item["version"]) for item in previous), default=0) + 1
            for item in previous:
                item["is_latest"] = False
            row = {"id": version_id, "report_id": report_id, "job_id": str(job_id), "run_id": str(run_id), "version": version, "document": document, "data_manifest": manifest, "checksum": checksum, "expires_at": expires_at, "is_latest": True, "tenant_id": int(getattr(identity, "tenant_id", 0) or 0), "user_id": int(getattr(identity, "user_id", 0) or 0)}
            self._versions[version_id] = row
            return self._to_version(row)
        pool = await self._pool()
        from src.memory.pg_pool import pg_pool_connection

        tenant_id = int(getattr(identity, "tenant_id", 0) or 0)
        user_id = int(getattr(identity, "user_id", 0) or 0)
        role = str(getattr(identity, "role", "") or "")
        async with pg_pool_connection(pool, tenant_id, user_id, role) as connection:
            async with connection.transaction():
                version = int(await connection.fetchval("SELECT COALESCE(MAX(version),0)+1 FROM report_versions WHERE report_id=$1::uuid AND tenant_id=$2", report_id, tenant_id))
                await connection.execute("UPDATE report_versions SET is_latest=FALSE WHERE report_id=$1::uuid AND tenant_id=$2", report_id, tenant_id)
                row = await connection.fetchrow("INSERT INTO report_versions (id,report_id,job_id,run_id,tenant_id,version,document_json,data_manifest_json,checksum,expires_at,is_latest) VALUES ($1::uuid,$2::uuid,$3::uuid,$4::uuid,$5,$6,$7::jsonb,$8::jsonb,$9,$10,TRUE) RETURNING *", version_id, report_id, job_id, run_id, tenant_id, version, json.dumps(document, ensure_ascii=False), json.dumps(manifest, ensure_ascii=False), checksum, expires_at)
        return self._to_version(row)

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
            row = await connection.fetchrow("SELECT v.* FROM report_versions v JOIN report_jobs j ON j.id=v.job_id WHERE v.report_id=$1::uuid AND v.tenant_id=$2 AND v.deleted_at IS NULL AND ($3 IN ('super_admin','tenant_admin') OR j.user_id=$4) ORDER BY v.is_latest DESC,v.version DESC LIMIT 1", str(report_id), tenant_id, role, user_id)
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
            rows = await connection.fetch("SELECT id::text,report_id::text,scope,user_scope,user_ids,allow_drilldown,allow_download,pin_latest,expires_at,revoked_at,view_count,last_viewed_at,created_at FROM report_shares WHERE report_id=$1::uuid AND tenant_id=$2 AND ($3 IN ('super_admin','tenant_admin') OR created_by=$4) ORDER BY created_at DESC", report_id, int(getattr(identity, "tenant_id", 0) or 0), str(getattr(identity, "role", "") or ""), int(getattr(identity, "user_id", 0) or 0))
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
        async with pg_pool_connection(pool, tenant_id, int(record.get("created_by", 0) or 0), "super_admin") as connection:
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
            status = await connection.execute("UPDATE report_shares SET revoked_at=NOW() WHERE id=$1::uuid AND tenant_id=$2 AND ($3 IN ('super_admin','tenant_admin') OR created_by=$4) AND revoked_at IS NULL", share_id, int(getattr(identity, "tenant_id", 0) or 0), str(getattr(identity, "role", "") or ""), int(getattr(identity, "user_id", 0) or 0))
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

            async with pg_pool_connection(pool, int(safe.get("tenant_id", 0) or 0), int(safe.get("user_id", 0) or 0), "super_admin") as connection:
                await connection.execute("INSERT INTO report_access_audit (tenant_id,user_id,share_id,report_id,action,result,ip_hash,metadata_json) VALUES ($1,$2,$3::uuid,$4::uuid,$5,$6,$7,$8::jsonb)", safe.get("tenant_id"), safe.get("user_id"), safe.get("share_id"), safe.get("report_id"), safe.get("action", "view"), safe.get("result", "success"), safe.get("ip_hash", ""), json.dumps(safe.get("metadata", {}), ensure_ascii=False))
        except Exception as exc:
            logger.error("报表访问审计写入失败", error=str(exc), exc_info=True)

    # 方法作用：读取用户能力覆盖，供权限服务判断菜单和操作权限。
    # Args: tenant_id - 租户 ID；user_id - 用户 ID。
    # Returns: 能力列表或 None 表示使用角色默认值。
    async def get_user_capabilities(self, tenant_id: int, user_id: int) -> list[str] | None:
        if self._memory:
            return self._capabilities.get((int(tenant_id), int(user_id)))
        pool = await self._pool()
        async with pool.acquire() as connection:
            value = await connection.fetchval("SELECT capabilities FROM report_user_permissions WHERE tenant_id=$1 AND user_id=$2", tenant_id, user_id)
        return list(value or []) if value is not None else None

    # 方法作用：保存租户管理员配置的用户报表能力。
    # Args: tenant_id - 当前租户；user_id - 目标用户；capabilities - report.* 能力列表。
    # Returns: 无返回值。
    async def set_user_capabilities(self, tenant_id: int, user_id: int, capabilities: list[str]) -> None:
        normalized = list(dict.fromkeys(item.strip() for item in capabilities if item.strip().startswith("report.")))
        if self._memory:
            self._capabilities[(int(tenant_id), int(user_id))] = normalized
            return
        pool = await self._pool()
        async with pool.acquire() as connection:
            await connection.execute("INSERT INTO report_user_permissions (tenant_id,user_id,capabilities) VALUES ($1,$2,$3::jsonb) ON CONFLICT (tenant_id,user_id) DO UPDATE SET capabilities=EXCLUDED.capabilities,updated_at=NOW()", tenant_id, user_id, json.dumps(normalized, ensure_ascii=False))

    # 方法作用：读取用户持久化配额覆盖，缺失时交给调用方使用默认策略。
    # Args: tenant_id - 租户 ID；user_id - 用户 ID。
    # Returns: 配额字段字典或 None。
    async def get_quota_policy(self, tenant_id: int, user_id: int) -> dict[str, Any] | None:
        if self._memory:
            value = self._quota_policies.get((int(tenant_id), int(user_id)))
            return dict(value) if value else None
        pool = await self._pool()
        async with pool.acquire() as connection:
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
        async with pool.acquire() as connection:
            await connection.execute("INSERT INTO report_quota_policies (tenant_id,user_id,timezone,max_jobs_created,max_runs_started,max_concurrent_runs,max_analysis_units_per_run,max_queries_per_run,max_rows_per_query,max_llm_tokens,max_page_bytes,max_share_links) VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11,$12) ON CONFLICT (tenant_id,user_id) DO UPDATE SET timezone=EXCLUDED.timezone,max_jobs_created=EXCLUDED.max_jobs_created,max_runs_started=EXCLUDED.max_runs_started,max_concurrent_runs=EXCLUDED.max_concurrent_runs,max_analysis_units_per_run=EXCLUDED.max_analysis_units_per_run,max_queries_per_run=EXCLUDED.max_queries_per_run,max_rows_per_query=EXCLUDED.max_rows_per_query,max_llm_tokens=EXCLUDED.max_llm_tokens,max_page_bytes=EXCLUDED.max_page_bytes,max_share_links=EXCLUDED.max_share_links,updated_at=NOW()", tenant_id, user_id, timezone_name, fields.get("max_jobs_created", 20), fields.get("max_runs_started", 50), fields.get("max_concurrent_runs", 2), fields.get("max_analysis_units_per_run", 12), fields.get("max_queries_per_run", 16), fields.get("max_rows_per_query", 100000), fields.get("max_llm_tokens", 200000), fields.get("max_page_bytes", 2097152), fields.get("max_share_links", 20))

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
        async with pool.acquire() as connection:
            async with connection.transaction():
                await connection.execute("INSERT INTO report_quota_usage (tenant_id,user_id,period_start,period_end) VALUES ($1,$2,$3,$4) ON CONFLICT (tenant_id,user_id,period_start) DO NOTHING", tenant_id, user_id, period_start, period_end)
                row = await connection.fetchrow("UPDATE report_quota_usage SET jobs_created=jobs_created+$3,runs_started=runs_started+$4,concurrent_runs=concurrent_runs+$5,analysis_units=analysis_units+$6,queries=queries+$7,rows=rows+$8,llm_tokens=llm_tokens+$9,share_links=share_links+$10 WHERE tenant_id=$1 AND user_id=$2 AND period_start=$11 AND jobs_created+$3 <= $12 AND runs_started+$4 <= $13 AND concurrent_runs+$5 <= $14 AND analysis_units+$6 <= $15 AND queries+$7 <= $16 AND rows+$8 <= $17 AND llm_tokens+$9 <= $18 AND share_links+$10 <= $19 RETURNING *", tenant_id, user_id, int(increments.get("jobs", 0)), int(increments.get("runs", 0)), int(increments.get("concurrent", 0)), int(increments.get("units", 0)), int(increments.get("queries", 0)), int(increments.get("rows", 0)), int(increments.get("tokens", 0)), int(increments.get("shares", 0)), period_start, fields.get("max_jobs_created", 20), fields.get("max_runs_started", 50), fields.get("max_concurrent_runs", 2), fields.get("max_analysis_units_per_run", 12), fields.get("max_queries_per_run", 16), fields.get("max_rows_per_query", 100000), fields.get("max_llm_tokens", 200000), fields.get("max_share_links", 20))
        return dict(row) if row else None

    # 方法作用：按数据库方言认领到期任务，PG 使用 SKIP LOCKED，SQLite 使用应用层锁。
    # Args: limit - 最大认领数量。
    # Returns: 本实例认领的任务摘要列表。
    async def claim_due_jobs(self, limit: int = 20) -> list[ReportJobRecord]:
        bounded = max(1, min(int(limit), 100))
        if self._memory:
            async with self._claim_lock:
                now = datetime.now(timezone.utc)
                rows = [row for row in self._jobs.values() if row.get("status") == "queued" and row.get("next_run_at") and row["next_run_at"] <= now]
                for row in rows[:bounded]:
                    row["status"] = "running"
                return [self._to_job(row) for row in rows[:bounded]]
        pool = await self._pool()
        from src.memory.pg_pool import pg_pool_connection

        async with pg_pool_connection(pool, 0, 0, "super_admin") as connection:
            if self.dialect in {"sqlite", "sqlite3"}:
                rows = await connection.fetch("SELECT * FROM report_jobs WHERE status='queued' AND next_run_at<=CURRENT_TIMESTAMP ORDER BY next_run_at LIMIT $1", bounded)
            else:
                rows = await connection.fetch("WITH due AS (SELECT id FROM report_jobs WHERE status='queued' AND next_run_at<=NOW() ORDER BY next_run_at FOR UPDATE SKIP LOCKED LIMIT $1) UPDATE report_jobs SET status='running',updated_at=NOW() FROM due WHERE report_jobs.id=due.id RETURNING report_jobs.*", bounded)
        return [self._to_job(row) for row in rows]

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
        return role in {"super_admin", "tenant_admin"} or int(owner or -1) == user_id

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
        return ReportJobRecord(id=str(row["id"]), tenant_id=int(row["tenant_id"]), user_id=int(row["user_id"]), prompt=str(row["prompt"]), plan=ReportPlan.model_validate(plan_data), status=str(row.get("status", "draft")), runs_started=int(row.get("runs_started", 0) or 0), created_at=row.get("created_at"), updated_at=row.get("updated_at"))

    # 方法作用：把数据库行转换为安全的运行模型。
    # Args: row - asyncpg Record 或内存字典。
    # Returns: ReportRunRecord。
    @staticmethod
    def _to_run(row: Any) -> ReportRunRecord:
        return ReportRunRecord(id=str(row["id"]), job_id=str(row["job_id"]), status=str(row.get("status", "queued")), attempt=int(row.get("attempt", 1) or 1), last_node=str(row.get("last_node", "") or ""), query_count=int(row.get("query_count", 0) or 0), row_count=int(row.get("row_count", 0) or 0), token_usage=int(row.get("token_usage", 0) or 0), partial=bool(row.get("partial", False)), error_code=str(row.get("error_code", "") or ""), user_error_message=str(row.get("user_error_message", "") or ""), trace_id=str(row.get("trace_id", "") or ""), started_at=row.get("started_at"), finished_at=row.get("finished_at"))

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
