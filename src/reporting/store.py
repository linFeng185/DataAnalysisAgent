"""报表工作台持久化 Store：PostgreSQL 生产分支与 SQLite/内存测试分支。"""

from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import asdict, is_dataclass
from datetime import datetime, timedelta, timezone
from typing import Any
from uuid import UUID, uuid4

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
from src.reporting.quota_reset import normalize_reset_fields
from src.reporting.share_analytics import to_share_list_item

logger = get_logger(__name__)


# 方法作用：判断字符串是否是 PostgreSQL 可接受的 UUID，避免非法标识触发数据库转型错误。
# Args: value - 待校验的标识。
# Returns: 合法 UUID 返回 True。
def _is_uuid(value: Any) -> bool:
    try:
        UUID(str(value))
        return True
    except (TypeError, ValueError, AttributeError):
        return False


# 链接管理列表与总数查询共用同一份可见范围与筛选谓词，避免两处规则脱钩。
# 参数顺序：$1 tenant_id、$2 role、$3 user_id、$4 report_id、$5 created_by、$6 status。
_SHARE_LIST_FILTER_SQL = (
    "s.tenant_id=$1 AND ($2<>'super_admin' AND ($2='tenant_admin' OR s.created_by=$3)) "
    "AND ($4::uuid IS NULL OR s.report_id=$4::uuid) "
    "AND ($5::bigint IS NULL OR s.created_by=$5::bigint) "
    "AND ($6 = '' "
    "OR ($6='revoked' AND s.revoked_at IS NOT NULL) "
    "OR ($6='expired' AND s.revoked_at IS NULL AND s.expires_at IS NOT NULL AND s.expires_at <= NOW()) "
    "OR ($6='active' AND s.revoked_at IS NULL AND (s.expires_at IS NULL OR s.expires_at > NOW())))"
)


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
        # 单元快照按 (run_id, unit_id) 唯一：重跑覆盖同一条，与 PG 唯一约束对齐。
        self._unit_snapshots: dict[tuple[str, str], dict[str, Any]] = {}
        self._shares: dict[str, dict[str, Any]] = {}
        self._share_by_token: dict[str, str] = {}
        self._share_visits: dict[str, dict[str, Any]] = {}
        self._tenant_settings: dict[int, dict[str, Any]] = {}
        # 内存分支没有 users 表，访问者用户名由测试或调用方登记后回填。
        self._usernames: dict[int, str] = {}
        self._audits: list[dict[str, Any]] = []
        self._capabilities: dict[tuple[int, int], list[str]] = {}
        self._quota_policies: dict[tuple[int, int], dict[str, Any]] = {}
        logger.debug("ReportStore 初始化完成", dialect=self.dialect, memory=self._memory)

    # 方法作用：在同一事务中去重、扣任务配额并创建任务，返回是否需要首次入队。
    # Args: identity - 当前身份；prompt - 描述；plan - 已确认计划；idempotency_key - 幂等键；quota_manager - 配额服务。
    # Returns: 任务、是否新建及配额快照。
    async def create_job_with_quota(self, identity: Any, prompt: str, plan: ReportPlan, idempotency_key: str, quota_manager: Any, preview_hash: str = "", template_id: str | None = None) -> tuple[ReportJobRecord, bool, Any]:
        """内存使用应用锁，PostgreSQL 使用事务锁保证多实例幂等。"""
        if self._memory:
            async with self._claim_lock:
                return await self._create_reserved_job(identity, prompt, plan, idempotency_key, quota_manager, preview_hash, template_id)
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
            return await scoped_store._create_reserved_job(identity, prompt, plan, idempotency_key, scoped_quota, preview_hash, template_id)

    # 方法作用：在调用方已经取得的创建事务或应用锁内完成去重和预占。
    # Args: identity - 当前身份；prompt - 描述；plan - 计划；idempotency_key - 幂等键；quota_manager - 同事务配额服务。
    # Returns: 任务、是否新建及配额快照。
    async def _create_reserved_job(self, identity: Any, prompt: str, plan: ReportPlan, idempotency_key: str, quota_manager: Any, preview_hash: str = "", template_id: str | None = None) -> tuple[ReportJobRecord, bool, Any]:
        existing = await self.get_job_by_idempotency(identity, idempotency_key)
        if existing is not None:
            if existing.plan.canonical_hash() != plan.canonical_hash():
                raise ValueError("该幂等键已用于其他报表计划")
            return existing, False, await quota_manager.snapshot(identity, timezone_name=plan.timezone)
        snapshot = await quota_manager.reserve(identity, jobs=1, timezone_name=plan.timezone)
        try:
            job = await self.create_job(identity, prompt, plan, idempotency_key, preview_hash=preview_hash, template_id=template_id)
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
    async def create_job(self, identity: Any, prompt: str, plan: ReportPlan, idempotency_key: str = "", preview_hash: str = "", template_id: str | None = None) -> ReportJobRecord:
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
                "preview_hash": preview_hash or plan.canonical_hash(),
                "template_id": template_id,
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
                "INSERT INTO report_jobs (id,tenant_id,user_id,prompt,plan_json,plan_hash,preview_hash,template_id,execution_mode,frequency,timezone,max_runs,status,idempotency_key,next_run_at,schedule_end_at,page_ttl_days,permanent) "
                "VALUES ($1::uuid,$2,$3,$4,$5::jsonb,$6,$7,$8,$9,$10,$11,$12,$13,$14,$15,$16,$17,$18) RETURNING *",
                job_id, tenant_id, user_id, prompt, json.dumps(plan.model_dump(mode="json"), ensure_ascii=False),
                plan.canonical_hash(), preview_hash or plan.canonical_hash(), template_id, plan.execution_mode, plan.frequency, plan.timezone, plan.max_runs,
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

    # 方法作用：判断报表记录是否对当前身份可见（本人、租户管理员或具备查看全部能力）。
    # Args: row - 记录行；identity - 当前身份；view_all - 是否具备 report.view_all。
    # Returns: 可见返回 True。
    @staticmethod
    def _report_visible(row: dict[str, Any], identity: Any, view_all: bool = False) -> bool:
        tenant_id = int(getattr(identity, "tenant_id", 0) or 0)
        user_id = int(getattr(identity, "user_id", 0) or 0)
        role = str(getattr(identity, "role", "") or "")
        if int(row.get("tenant_id", -1) or -1) != tenant_id:
            return False
        # 平台身份不参与租户业务报表的可见范围。
        return role != "super_admin" and (
            bool(view_all) or role == "tenant_admin" or int(row.get("user_id", -1) or -1) == user_id
        )

    # 方法作用：把任务记录行规范化为报表信息列表与详情共用的安全字典。
    # Args: row - 任务记录行；latest_report_id - 可选的最新页面标识。
    # Returns: 含列表 7 列字段与计划对象的字典。
    @classmethod
    def _report_row_dict(cls, row: Any, latest_report_id: str | None = None) -> dict[str, Any]:
        plan_data = cls._json_dict(row.get("plan_json", {}) if hasattr(row, "get") else row["plan_json"])
        plan = None
        if plan_data.get("analysis_units"):
            try:
                plan = ReportPlan.model_validate(plan_data)
            except ValueError:
                # 计划损坏不应让详情接口整体失败：列表字段仍可展示，计划按缺失处理。
                logger.warning("报表记录中的计划无法解析", report_id=str(row.get("id", "")))
                plan = None
        datasources = cls._datasource_list(row.get("datasources_json", []))
        if not datasources and plan is not None:
            # 旧 create_job 入口创建的记录没有数据源列，用计划里的已选源兜底出列。
            datasources = list(plan.requested_datasources)
        return {
            "id": str(row["id"]),
            "tenant_id": int(row.get("tenant_id", 0) or 0),
            "user_id": int(row.get("user_id", 0) or 0),
            # 创建者登录名：列表要让人认得出是谁的报表；旧记录没有该列时留空。
            "username": str(row.get("username", "") or ""),
            "title": str(row.get("title", "") or ""),
            "prompt": str(row.get("prompt", "") or ""),
            "datasources": datasources,
            "divergence": str(row.get("divergence", "standard") or "standard"),
            "status": str(row.get("status", "draft") or "draft"),
            "runs_started": int(row.get("runs_started", 0) or 0),
            "next_run_at": row.get("next_run_at"),
            "created_at": row.get("created_at"),
            "updated_at": row.get("updated_at"),
            "execution_mode": str(row.get("execution_mode", "immediate") or "immediate"),
            "frequency": row.get("frequency"),
            "max_runs": row.get("max_runs"),
            "schedule_end_at": row.get("schedule_end_at"),
            "page_ttl_days": int(row.get("page_ttl_days", 7) or 7),
            "permanent": bool(row.get("permanent", False)),
            "timezone": str(row.get("timezone", "Asia/Shanghai") or "Asia/Shanghai"),
            "template_id": (str(row.get("template_id")) if row.get("template_id") else None),
            "plan_hash": str(row.get("plan_hash", "") or ""),
            "plan_error": str(row.get("plan_error", "") or ""),
            "is_paused": bool(row.get("is_paused", False)),
            "latest_report_id": latest_report_id,
            "plan": plan,
        }

    # 方法作用：创建后台规划中的报表记录，不预占任何额度（需求 1.5）。
    # Args: identity - 当前身份；prompt - 用户描述；datasources - 已选数据源；timezone_name - 租户时区；divergence - 发散档位；template_id - 视觉模板；idempotency_key - 幂等键。
    # Returns: 任务记录与是否新建；幂等命中时返回已有记录且 created 为 False。
    async def create_planning_job(self, identity: Any, prompt: str, *, datasources: list[str], timezone_name: str = "Asia/Shanghai", divergence: str = "standard", template_id: str | None = None, idempotency_key: str = "") -> tuple[ReportJobRecord, bool]:
        """与 create_job_with_quota 的写法一致，但不调用任何配额预占。"""
        tenant_id = int(getattr(identity, "tenant_id", 0) or 0)
        user_id = int(getattr(identity, "user_id", 0) or 0)
        key = str(idempotency_key or "").strip()
        normalized_datasources = self._datasource_list(datasources)
        logger.debug("报表后台规划记录创建入口", tenant_id=tenant_id, user_id=user_id, datasource_count=len(normalized_datasources), divergence=divergence)
        if self._memory:
            async with self._claim_lock:
                if key and (tenant_id, user_id, key) in self._idempotency:
                    existing = self._to_job(self._jobs[self._idempotency[(tenant_id, user_id, key)]])
                    logger.info("报表后台规划记录幂等命中", report_id=existing.id, created=False)
                    return existing, False
                job_id = str(uuid4())
                now = datetime.now(timezone.utc)
                row = {
                    "id": job_id, "tenant_id": tenant_id, "user_id": user_id, "prompt": prompt,
                    # 创建者登录名随行保存：PG 分支靠 JOIN users 取，内存分支只能自己带。
                    "username": str(getattr(identity, "username", "") or ""),
                    # plan_json 必须非空才能满足列约束；空对象表示计划尚未生成。
                    "plan_json": {}, "plan_hash": "", "preview_hash": "",
                    "template_id": template_id, "execution_mode": "immediate", "frequency": None,
                    "timezone": timezone_name, "max_runs": None, "schedule_end_at": None,
                    "page_ttl_days": 7, "permanent": False,
                    "status": "planning", "title": "", "divergence": divergence,
                    "datasources_json": normalized_datasources, "plan_error": "",
                    "idempotency_key": key or None,
                    "next_run_at": None, "is_paused": False, "runs_started": 0,
                    "created_at": now, "updated_at": now,
                }
                self._jobs[job_id] = row
                if key:
                    self._idempotency[(tenant_id, user_id, key)] = job_id
                logger.info("报表后台规划记录创建完成", report_id=job_id, tenant_id=tenant_id, user_id=user_id, created=True)
                return self._to_job(row), True
        pool = await self._pool()
        from src.memory.pg_pool import pg_pool_connection

        job_id = str(uuid4())
        role = str(getattr(identity, "role", "") or "")
        async with pg_pool_connection(pool, tenant_id, user_id, role) as connection:
            if key:
                # 相同身份与幂等键的并发重试必须在去重查询前串行化。
                await connection.execute(
                    "SELECT pg_advisory_xact_lock(hashtextextended($1, 0))",
                    f"report-plan:{tenant_id}:{user_id}:{key}",
                )
                existing = await connection.fetchrow(
                    "SELECT * FROM report_jobs WHERE tenant_id=$1 AND user_id=$2 AND idempotency_key=$3",
                    tenant_id, user_id, key,
                )
                if existing is not None:
                    logger.info("报表后台规划记录幂等命中", report_id=str(existing["id"]), created=False)
                    return self._to_job(existing), False
            row = await connection.fetchrow(
                "INSERT INTO report_jobs (id,tenant_id,user_id,prompt,plan_json,plan_hash,execution_mode,timezone,status,title,divergence,datasources_json,plan_error,idempotency_key,template_id,page_ttl_days,permanent) "
                "VALUES ($1::uuid,$2,$3,$4,$5::jsonb,$6,$7,$8,'planning',$9,$10,$11::jsonb,$12,$13,$14,$15,$16) RETURNING *",
                job_id, tenant_id, user_id, prompt, "{}", "", "immediate", timezone_name, "", divergence,
                json.dumps(normalized_datasources, ensure_ascii=False), "", key or None, template_id, 7, False,
            )
        logger.info("报表后台规划记录创建完成", report_id=job_id, tenant_id=tenant_id, user_id=user_id, created=True)
        return self._to_job(row), True

    # 方法作用：写入后台规划结果并把记录推进到「未生成」（draft）。
    # Args: report_id - 报表标识；identity - 记录拥有者身份；plan - 生成好的计划；title - 报表标题。
    # Returns: 写入成功返回 True，记录不存在或越权返回 False。
    async def save_planned_job(self, report_id: str, identity: Any, *, plan: ReportPlan, title: str) -> bool:
        """计划与调度列同时落库，使列表出列与点「生成」读到同一份状态。"""
        plan_hash = plan.canonical_hash()
        # 立即执行没有下次时间；定时执行由计划里的首次时间兜底。
        next_run_at = (plan.run_at or datetime.now(timezone.utc)) if plan.execution_mode == "scheduled" else None
        resolved_title = str(title or "").strip()[:256]
        tenant_id = int(getattr(identity, "tenant_id", 0) or 0)
        user_id = int(getattr(identity, "user_id", 0) or 0)
        logger.debug("报表计划写入入口", report_id=report_id, unit_count=len(plan.analysis_units), divergence=str(plan.divergence))
        if self._memory:
            async with self._claim_lock:
                row = self._jobs.get(str(report_id))
                if row is None or not self._report_visible(row, identity):
                    logger.warning("报表计划写入拒绝", report_id=report_id, reason="记录不存在或越权")
                    return False
                row.update(
                    plan_json=plan.model_dump(mode="json"), plan_hash=plan_hash, title=resolved_title,
                    status="draft", plan_error="", execution_mode=plan.execution_mode, frequency=plan.frequency,
                    max_runs=plan.max_runs, next_run_at=next_run_at, schedule_end_at=plan.schedule_end_at,
                    page_ttl_days=plan.page_ttl_days, permanent=plan.permanent, timezone=plan.timezone,
                    updated_at=datetime.now(timezone.utc),
                )
                logger.info("报表计划写入完成", report_id=report_id, status="draft", unit_count=len(plan.analysis_units))
                return True
        pool = await self._pool()
        from src.memory.pg_pool import pg_pool_connection

        role = str(getattr(identity, "role", "") or "")
        async with pg_pool_connection(pool, tenant_id, user_id, role) as connection:
            row = await connection.fetchrow(
                "UPDATE report_jobs SET plan_json=$1::jsonb,plan_hash=$2,title=$3,status='draft',plan_error='',execution_mode=$4,"
                "frequency=$5,max_runs=$6,next_run_at=$7,schedule_end_at=$8,page_ttl_days=$9,permanent=$10,timezone=$11,updated_at=NOW() "
                "WHERE id=$12::uuid AND tenant_id=$13 AND ($14<>'super_admin' AND ($14='tenant_admin' OR user_id=$15)) RETURNING id",
                json.dumps(plan.model_dump(mode="json"), ensure_ascii=False), plan_hash, resolved_title,
                plan.execution_mode, plan.frequency, plan.max_runs, next_run_at, plan.schedule_end_at,
                plan.page_ttl_days, plan.permanent, plan.timezone, str(report_id), tenant_id, role, user_id,
            )
        if row is None:
            logger.warning("报表计划写入拒绝", report_id=report_id, reason="记录不存在或越权")
            return False
        logger.info("报表计划写入完成", report_id=report_id, status="draft", unit_count=len(plan.analysis_units))
        return True

    # 方法作用：把规划失败的可读原因写入记录并置为 plan_failed。
    # Args: report_id - 报表标识；identity - 记录拥有者身份；reason - 用户可读失败原因。
    # Returns: 写入成功返回 True，记录不存在或越权返回 False。
    async def mark_plan_failed(self, report_id: str, identity: Any, *, reason: str) -> bool:
        message = str(reason or "").strip()[:2000] or "计划生成失败，请稍后重试"
        tenant_id = int(getattr(identity, "tenant_id", 0) or 0)
        user_id = int(getattr(identity, "user_id", 0) or 0)
        logger.debug("报表计划失败写入入口", report_id=report_id, reason_chars=len(message))
        if self._memory:
            async with self._claim_lock:
                row = self._jobs.get(str(report_id))
                if row is None or not self._report_visible(row, identity):
                    return False
                row.update(status="plan_failed", plan_error=message, updated_at=datetime.now(timezone.utc))
                logger.info("报表计划失败写入完成", report_id=report_id, status="plan_failed")
                return True
        pool = await self._pool()
        from src.memory.pg_pool import pg_pool_connection

        role = str(getattr(identity, "role", "") or "")
        async with pg_pool_connection(pool, tenant_id, user_id, role) as connection:
            row = await connection.fetchrow(
                "UPDATE report_jobs SET status='plan_failed',plan_error=$1,updated_at=NOW() WHERE id=$2::uuid AND tenant_id=$3 "
                "AND ($4<>'super_admin' AND ($4='tenant_admin' OR user_id=$5)) RETURNING id",
                message, str(report_id), tenant_id, role, user_id,
            )
        if row is None:
            return False
        logger.info("报表计划失败写入完成", report_id=report_id, status="plan_failed")
        return True

    # 方法作用：清空计划失败原因并把记录退回「生成中」，用于用户点重试。
    # Args: report_id - 报表标识；identity - 记录拥有者身份。
    # Returns: 复位成功返回 True，记录不存在或越权返回 False。
    async def reset_plan_status(self, report_id: str, identity: Any) -> bool:
        tenant_id = int(getattr(identity, "tenant_id", 0) or 0)
        user_id = int(getattr(identity, "user_id", 0) or 0)
        logger.debug("报表计划重试复位入口", report_id=report_id, user_id=user_id)
        if self._memory:
            async with self._claim_lock:
                row = self._jobs.get(str(report_id))
                if row is None or not self._report_visible(row, identity):
                    return False
                row.update(status="planning", plan_error="", updated_at=datetime.now(timezone.utc))
                logger.info("报表计划重试复位完成", report_id=report_id)
                return True
        pool = await self._pool()
        from src.memory.pg_pool import pg_pool_connection

        role = str(getattr(identity, "role", "") or "")
        async with pg_pool_connection(pool, tenant_id, user_id, role) as connection:
            row = await connection.fetchrow(
                "UPDATE report_jobs SET status='planning',plan_error='',updated_at=NOW() WHERE id=$1::uuid AND tenant_id=$2 "
                "AND ($3<>'super_admin' AND ($3='tenant_admin' OR user_id=$4)) RETURNING id",
                str(report_id), tenant_id, role, user_id,
            )
        if row is None:
            return False
        logger.info("报表计划重试复位完成", report_id=report_id)
        return True

    # 方法作用：按可见范围分页读取报表信息列表，默认创建时间倒序。
    # Args: identity - 当前身份；user_id - 可选目标用户（仅租户管理员按用户筛选）；status - 可选状态；page/page_size - 分页；view_all - 是否具备查看全部能力。
    # Returns: 列表行与总数。
    async def list_reports(self, identity: Any, *, user_id: int | None = None, status: str | None = None, page: int = 1, page_size: int = 20, view_all: bool = False) -> tuple[list[dict[str, Any]], int]:
        """一条 SQL 带出列表、总数和最新页面标识，避免按行查询造成 N+1。"""
        bounded_page = max(1, int(page or 1))
        bounded_size = max(1, min(int(page_size or 20), 100))
        tenant_id = int(getattr(identity, "tenant_id", 0) or 0)
        logger.debug("报表信息列表查询入口", tenant_id=tenant_id, user_id=int(getattr(identity, "user_id", 0) or 0), page=bounded_page, page_size=bounded_size, status=str(status or ""))
        if self._memory:
            rows = [row for row in self._jobs.values() if self._report_visible(row, identity, view_all)]
            rows = [row for row in rows if str(row.get("status", "")) != "deleted"]
            if user_id is not None:
                rows = [row for row in rows if int(row.get("user_id", 0) or 0) == int(user_id)]
            if status:
                rows = [row for row in rows if str(row.get("status", "")) == str(status)]
            rows.sort(key=lambda row: row.get("created_at") or datetime.min.replace(tzinfo=timezone.utc), reverse=True)
            total = len(rows)
            start = (bounded_page - 1) * bounded_size
            items = [self._report_row_dict(row, self._latest_report_id(str(row["id"]))) for row in rows[start:start + bounded_size]]
            logger.debug("报表信息列表查询完成", tenant_id=tenant_id, total=total, returned=len(items))
            return items, total
        pool = await self._pool()
        from src.memory.pg_pool import pg_pool_connection

        role = str(getattr(identity, "role", "") or "")
        offset = (bounded_page - 1) * bounded_size
        async with pg_pool_connection(pool, tenant_id, int(getattr(identity, "user_id", 0) or 0), role) as connection:
            rows = await connection.fetch(
                "SELECT j.id::text AS id, j.tenant_id, j.user_id, j.prompt, j.title, j.divergence, j.datasources_json, j.status, "
                "j.runs_started, j.next_run_at, j.created_at, j.updated_at, j.execution_mode, j.frequency, j.max_runs, "
                "j.schedule_end_at, j.page_ttl_days, j.permanent, j.timezone, j.template_id, j.plan_hash, j.plan_error, j.is_paused, "
                "u.username AS username, "
                "lv.id::text AS latest_report_id, COUNT(*) OVER () AS total_count "
                "FROM report_jobs j "
                "LEFT JOIN users u ON u.id=j.user_id "
                "LEFT JOIN LATERAL (SELECT v.id FROM report_versions v WHERE v.job_id=j.id AND v.tenant_id=j.tenant_id AND v.deleted_at IS NULL ORDER BY v.is_latest DESC,v.version DESC LIMIT 1) lv ON TRUE "
                "WHERE j.tenant_id=$1 AND j.status<>'deleted' "
                "AND ($2<>'super_admin' AND ($3::boolean OR $2='tenant_admin' OR j.user_id=$4)) "
                "AND ($5::bigint IS NULL OR j.user_id=$5::bigint) AND ($6='' OR j.status=$6) "
                "ORDER BY j.created_at DESC LIMIT $7 OFFSET $8",
                tenant_id, role, bool(view_all), int(getattr(identity, "user_id", 0) or 0),
                (int(user_id) if user_id is not None else None), str(status or ""), bounded_size, offset,
            )
        total = int(rows[0]["total_count"]) if rows else 0
        items = [self._report_row_dict(row, str(row["latest_report_id"]) if row.get("latest_report_id") else None) for row in rows]
        logger.debug("报表信息列表查询完成", tenant_id=tenant_id, total=total, returned=len(items))
        return items, total

    # 方法作用：读取单条报表记录的原始字段与计划对象，供详情、编辑、运行和删除共用。
    # Args: report_id - 报表标识；identity - 当前身份；view_all - 是否具备查看全部能力。
    # Returns: 报表字典，不存在或越权返回 None。
    async def get_report_row(self, report_id: str, identity: Any, *, view_all: bool = False) -> dict[str, Any] | None:
        if self._memory:
            row = self._jobs.get(str(report_id))
            # 已软删除的记录与数据库分支保持一致：一律按不存在处理。
            if row is None or str(row.get("status", "")) == "deleted" or not self._report_visible(row, identity, view_all):
                return None
            return self._report_row_dict(row, self._latest_report_id(str(report_id)))
        pool = await self._pool()
        from src.memory.pg_pool import pg_pool_connection

        tenant_id = int(getattr(identity, "tenant_id", 0) or 0)
        role = str(getattr(identity, "role", "") or "")
        async with pg_pool_connection(pool, tenant_id, int(getattr(identity, "user_id", 0) or 0), role) as connection:
            row = await connection.fetchrow(
                "SELECT j.id::text AS id, j.tenant_id, j.user_id, j.prompt, j.title, j.divergence, j.datasources_json, j.status, "
                "j.runs_started, j.next_run_at, j.created_at, j.updated_at, j.execution_mode, j.frequency, j.max_runs, "
                "j.schedule_end_at, j.page_ttl_days, j.permanent, j.timezone, j.template_id, j.plan_hash, j.plan_error, j.is_paused, "
                "j.plan_json, lv.id::text AS latest_report_id "
                "FROM report_jobs j "
                "LEFT JOIN LATERAL (SELECT v.id FROM report_versions v WHERE v.job_id=j.id AND v.tenant_id=j.tenant_id AND v.deleted_at IS NULL ORDER BY v.is_latest DESC,v.version DESC LIMIT 1) lv ON TRUE "
                "WHERE j.id=$1::uuid AND j.tenant_id=$2 AND j.status<>'deleted' "
                "AND ($3<>'super_admin' AND ($4::boolean OR $3='tenant_admin' OR j.user_id=$5))",
                str(report_id), tenant_id, role, bool(view_all), int(getattr(identity, "user_id", 0) or 0),
            )
        return self._report_row_dict(row, str(row["latest_report_id"]) if row and row.get("latest_report_id") else None) if row else None

    # 方法作用：把编辑后的计划与调度写回 draft 记录，并重算计划哈希。
    # Args: report_id - 报表标识；identity - 记录拥有者身份；plan - 重新校验过的计划；columns - 待更新的调度列。
    # Returns: 写回成功返回 True，记录不存在或越权返回 False。
    async def update_report_plan(self, report_id: str, identity: Any, *, plan: ReportPlan, columns: dict[str, Any]) -> bool:
        plan_hash = plan.canonical_hash()
        tenant_id = int(getattr(identity, "tenant_id", 0) or 0)
        user_id = int(getattr(identity, "user_id", 0) or 0)
        logger.debug("报表计划编辑写入入口", report_id=report_id, execution_mode=plan.execution_mode, unit_count=len(plan.analysis_units))
        if self._memory:
            async with self._claim_lock:
                row = self._jobs.get(str(report_id))
                if row is None or not self._report_visible(row, identity):
                    return False
                # 与数据库分支保持同一组列：列表出列读的是列而不是 plan_json。
                row.update(
                    plan_json=plan.model_dump(mode="json"), plan_hash=plan_hash, execution_mode=plan.execution_mode,
                    frequency=plan.frequency, max_runs=plan.max_runs, schedule_end_at=plan.schedule_end_at,
                    page_ttl_days=plan.page_ttl_days, permanent=plan.permanent, timezone=plan.timezone,
                    **columns, updated_at=datetime.now(timezone.utc),
                )
                logger.info("报表计划编辑写入完成", report_id=report_id, plan_hash=plan_hash)
                return True
        pool = await self._pool()
        from src.memory.pg_pool import pg_pool_connection

        role = str(getattr(identity, "role", "") or "")
        async with pg_pool_connection(pool, tenant_id, user_id, role) as connection:
            row = await connection.fetchrow(
                "UPDATE report_jobs SET plan_json=$1::jsonb,plan_hash=$2,execution_mode=$3,frequency=$4,max_runs=$5,next_run_at=$6,"
                "schedule_end_at=$7,page_ttl_days=$8,permanent=$9,timezone=$10,template_id=$11,updated_at=NOW() "
                "WHERE id=$12::uuid AND tenant_id=$13 AND ($14<>'super_admin' AND ($14='tenant_admin' OR user_id=$15)) RETURNING id",
                json.dumps(plan.model_dump(mode="json"), ensure_ascii=False), plan_hash,
                plan.execution_mode, plan.frequency, plan.max_runs, columns.get("next_run_at"),
                plan.schedule_end_at, plan.page_ttl_days, plan.permanent, plan.timezone, columns.get("template_id"),
                str(report_id), tenant_id, role, user_id,
            )
        if row is None:
            return False
        logger.info("报表计划编辑写入完成", report_id=report_id, plan_hash=plan_hash)
        return True

    # 方法作用：软删除报表记录，保留运行与页面数据供审计追溯。
    # Args: report_id - 报表标识；identity - 当前身份；view_all - 是否具备查看全部能力。
    # Returns: 删除成功返回 True，记录不存在或越权返回 False。
    async def delete_report(self, report_id: str, identity: Any, *, view_all: bool = False) -> bool:
        tenant_id = int(getattr(identity, "tenant_id", 0) or 0)
        logger.debug("报表记录删除入口", report_id=report_id, user_id=int(getattr(identity, "user_id", 0) or 0))
        if self._memory:
            async with self._claim_lock:
                row = self._jobs.get(str(report_id))
                if row is None or not self._report_visible(row, identity, view_all):
                    return False
                row.update(status="deleted", updated_at=datetime.now(timezone.utc))
                logger.info("报表记录删除完成", report_id=report_id)
                return True
        pool = await self._pool()
        from src.memory.pg_pool import pg_pool_connection

        role = str(getattr(identity, "role", "") or "")
        async with pg_pool_connection(pool, tenant_id, int(getattr(identity, "user_id", 0) or 0), role) as connection:
            row = await connection.fetchrow(
                "UPDATE report_jobs SET status='deleted',updated_at=NOW() WHERE id=$1::uuid AND tenant_id=$2 "
                "AND ($3<>'super_admin' AND ($4::boolean OR $3='tenant_admin' OR user_id=$5)) RETURNING id",
                str(report_id), tenant_id, role, bool(view_all), int(getattr(identity, "user_id", 0) or 0),
            )
        if row is None:
            return False
        logger.info("报表记录删除完成", report_id=report_id)
        return True

    # 方法作用：列出报表的全部页面版本摘要，按版本倒序。
    # Args: report_id - 报表标识；identity - 当前身份；view_all - 是否具备查看全部能力。
    # Returns: 页面摘要字典列表（id/version/expires_at/is_latest/created_at）。
    async def list_report_pages(self, report_id: str, identity: Any, *, view_all: bool = False) -> list[dict[str, Any]]:
        logger.debug("报表页面列表查询入口", report_id=report_id, user_id=int(getattr(identity, "user_id", 0) or 0))
        if self._memory:
            report = await self.get_report_row(report_id, identity, view_all=view_all)
            if report is None:
                return []
            rows = [row for row in self._versions.values() if str(row.get("job_id")) == str(report_id) and row.get("deleted_at") is None]
            rows.sort(key=lambda row: int(row.get("version", 0) or 0), reverse=True)
            return [self._page_summary(row) for row in rows]
        pool = await self._pool()
        from src.memory.pg_pool import pg_pool_connection

        tenant_id = int(getattr(identity, "tenant_id", 0) or 0)
        role = str(getattr(identity, "role", "") or "")
        async with pg_pool_connection(pool, tenant_id, int(getattr(identity, "user_id", 0) or 0), role) as connection:
            rows = await connection.fetch(
                "SELECT v.id::text AS id, v.version, v.expires_at, v.is_latest, v.created_at FROM report_versions v "
                "JOIN report_jobs j ON j.id=v.job_id "
                "WHERE v.job_id=$1::uuid AND v.tenant_id=$2 AND v.deleted_at IS NULL "
                "AND ($3<>'super_admin' AND ($4::boolean OR $3='tenant_admin' OR j.user_id=$5)) ORDER BY v.version DESC LIMIT 200",
                str(report_id), tenant_id, role, bool(view_all), int(getattr(identity, "user_id", 0) or 0),
            )
        return [self._page_summary(row) for row in rows]

    # 方法作用：把页面版本行规范化为只读摘要。
    # Args: row - 版本行。
    # Returns: 页面摘要字典。
    @staticmethod
    def _page_summary(row: Any) -> dict[str, Any]:
        return {
            "id": str(row.get("id", "")),
            "version": int(row.get("version", 0) or 0),
            "expires_at": row.get("expires_at"),
            "is_latest": bool(row.get("is_latest", False)),
            "created_at": row.get("created_at"),
        }

    # 方法作用：读取某报表最新页面的标识，内存分支列表出列使用。
    # Args: job_id - 报表任务标识。
    # Returns: 最新页面标识；没有页面时返回 None。
    def _latest_report_id(self, job_id: str) -> str | None:
        rows = [row for row in self._versions.values() if str(row.get("job_id")) == str(job_id) and row.get("deleted_at") is None]
        row = max(rows, key=lambda item: (item.get("created_at") or datetime.min.replace(tzinfo=timezone.utc), int(item.get("version", 0) or 0)), default=None)
        return str(row["id"]) if row else None

    # 方法作用：更新任务状态并记录最近更新时间。
    # Args: job_id - 任务 ID；identity - 当前身份；status - draft/planning/plan_failed/queued/running/succeeded/partial/failed/cancelled。
    # Returns: 更新后的任务或 None。
    async def update_job_status(self, job_id: str, identity: Any, status: str) -> ReportJobRecord | None:
        # plan_failed 与迁移 032 的 CHECK 约束保持一致：计划失败是显式终态，可重试。
        allowed = {"draft", "planning", "plan_failed", "queued", "running", "succeeded", "partial", "failed", "cancelled", "expired", "deleted"}
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
        allowed = {"status", "last_node", "query_count", "row_count", "token_usage", "prompt_token_usage", "completion_token_usage", "llm_call_count", "llm_failure_count", "sql_query_count", "sql_success_count", "sql_failure_count", "partial", "error_code", "internal_error_message", "user_error_message", "started_at", "finished_at"}
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

    # 方法作用：跨任务分页列出报表运行明细，供执行明细页展示 LLM/SQL 调用次数与 token 用量。
    # Args: identity - 当前身份；job_id - 可选任务过滤；status - 可选运行状态过滤；limit/offset - 分页参数；view_all - 是否具备 report.view_all。
    # Returns: (运行明细行列表, 命中总数)，每行含运行计数汇总与所属任务标题。
    async def list_run_details(self, identity: Any, *, job_id: str | None = None, status: str | None = None, limit: int = 50, offset: int = 0, view_all: bool = False) -> tuple[list[dict[str, Any]], int]:
        """一条 SQL 带出运行行、所属任务标题和总数，避免明细页按行回查任务。"""
        bounded = max(1, min(int(limit or 50), 200))
        bounded_offset = max(0, int(offset or 0))
        tenant_id = int(getattr(identity, "tenant_id", 0) or 0)
        logger.debug("报表执行明细查询入口", tenant_id=tenant_id, user_id=int(getattr(identity, "user_id", 0) or 0), job_id=str(job_id or ""), status=str(status or ""), limit=bounded)
        if self._memory:
            rows: list[dict[str, Any]] = []
            for row in self._runs.values():
                if not self._report_visible(row, identity, view_all):
                    continue
                if job_id and str(row.get("job_id", "")) != str(job_id):
                    continue
                if status and str(row.get("status", "")) != str(status):
                    continue
                # 内存模式没有 JOIN，这里补上所属任务字段，保证与数据库路径返回同构。
                job_row = self._jobs.get(str(row.get("job_id", "")), {})
                rows.append({**row, "job_title": job_row.get("title", ""), "job_prompt": job_row.get("prompt", ""), "job_status": job_row.get("status", "")})
            rows.sort(key=lambda row: row.get("created_at") or datetime.min.replace(tzinfo=timezone.utc), reverse=True)
            total = len(rows)
            items = [self._run_detail_dict(row) for row in rows[bounded_offset:bounded_offset + bounded]]
            logger.info("报表执行明细查询完成", tenant_id=tenant_id, total=total, returned=len(items), memory=True)
            return items, total
        pool = await self._pool()
        from src.memory.pg_pool import pg_pool_connection

        role = str(getattr(identity, "role", "") or "")
        async with pg_pool_connection(pool, tenant_id, int(getattr(identity, "user_id", 0) or 0), role) as connection:
            db_rows = await connection.fetch(
                "SELECT r.*, j.title AS job_title, j.prompt AS job_prompt, j.status AS job_status, COUNT(*) OVER () AS total_count "
                "FROM report_runs r JOIN report_jobs j ON j.id=r.job_id "
                "WHERE r.tenant_id=$1 "
                "AND ($2<>'super_admin' AND ($3::boolean OR $2='tenant_admin' OR r.user_id=$4)) "
                "AND ($5::uuid IS NULL OR r.job_id=$5::uuid) AND ($6='' OR r.status=$6) "
                "ORDER BY r.created_at DESC LIMIT $7 OFFSET $8",
                tenant_id, role, bool(view_all), int(getattr(identity, "user_id", 0) or 0),
                (str(job_id) if job_id else None), str(status or ""), bounded, bounded_offset,
            )
        total = int(db_rows[0]["total_count"]) if db_rows else 0
        items = [self._run_detail_dict(row) for row in db_rows]
        logger.info("报表执行明细查询完成", tenant_id=tenant_id, total=total, returned=len(items), memory=False)
        return items, total

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

    # 方法作用：保存或覆盖一条单元执行快照，重跑按 (run_id, unit_id) 覆盖且快照标识保持稳定。
    # Args: snapshot - UnitSnapshot 或同构字典；identity - 实际执行身份，归属字段以其为准。
    # Returns: 无返回值；缺少运行或单元标识时抛出 ValueError。
    async def save_unit_snapshot(self, snapshot: Any, identity: Any) -> None:
        """归属取执行身份而不是快照载荷，防止调用方伪造查看范围；重跑覆盖旧快照。"""
        run_id = str(getattr(snapshot, "run_id", "") or "")
        unit_id = str(getattr(snapshot, "unit_id", "") or "")
        logger.debug("单元快照保存入口", run_id=run_id, unit_id=unit_id, tenant_id=int(getattr(identity, "tenant_id", 0) or 0))
        if not run_id or not unit_id:
            raise ValueError("单元快照缺少 run_id 或 unit_id")
        row = snapshot.model_dump() if hasattr(snapshot, "model_dump") else dict(snapshot)
        row["tenant_id"] = int(getattr(identity, "tenant_id", 0) or 0)
        row["user_id"] = int(getattr(identity, "user_id", 0) or 0)
        row["created_at"] = datetime.now(timezone.utc)
        if self._memory:
            existing = self._unit_snapshots.get((run_id, unit_id))
            # 覆盖时沿用首次写入的快照标识，与 PG 的 ON CONFLICT DO UPDATE 语义一致。
            row["id"] = str((existing.get("id") if existing else "") or row.get("id") or uuid4())
            self._unit_snapshots[(run_id, unit_id)] = row
        else:
            from src.memory.pg_pool import pg_pool_connection

            row["id"] = str(row.get("id") or uuid4())
            pool = await self._pool()
            async with pg_pool_connection(pool, row["tenant_id"], row["user_id"], str(getattr(identity, "role", "") or "")) as connection:
                await connection.execute(
                    "INSERT INTO report_unit_snapshots (id,run_id,report_id,tenant_id,user_id,unit_id,status,datasource,dialect,sql_text,prompt_text,prompt_id,model_id,params_json,row_count,full_count,truncated,truncated_reason,elapsed_ms,error_code,error_message) "
                    "VALUES ($1::uuid,$2::uuid,$3::uuid,$4,$5,$6,$7,$8,$9,$10,$11,$12,$13,$14::jsonb,$15,$16,$17,$18,$19,$20,$21) "
                    "ON CONFLICT (run_id, unit_id) DO UPDATE SET report_id=EXCLUDED.report_id,status=EXCLUDED.status,datasource=EXCLUDED.datasource,dialect=EXCLUDED.dialect,sql_text=EXCLUDED.sql_text,prompt_text=EXCLUDED.prompt_text,prompt_id=EXCLUDED.prompt_id,model_id=EXCLUDED.model_id,params_json=EXCLUDED.params_json,row_count=EXCLUDED.row_count,full_count=EXCLUDED.full_count,truncated=EXCLUDED.truncated,truncated_reason=EXCLUDED.truncated_reason,elapsed_ms=EXCLUDED.elapsed_ms,error_code=EXCLUDED.error_code,error_message=EXCLUDED.error_message,created_at=NOW()",
                    row["id"], run_id, str(row.get("report_id") or ""), row["tenant_id"], row["user_id"], unit_id,
                    str(row.get("status") or "success"), str(row.get("datasource") or ""), str(row.get("dialect") or ""),
                    str(row.get("sql_text") or ""), str(row.get("prompt_text") or ""), str(row.get("prompt_id") or ""),
                    str(row.get("model_id") or ""), json.dumps(dict(row.get("params") or {}), ensure_ascii=False),
                    int(row.get("row_count") or 0), int(row.get("full_count") or 0), bool(row.get("truncated")),
                    str(row.get("truncated_reason") or ""), int(row.get("elapsed_ms") or 0),
                    str(row.get("error_code") or ""), str(row.get("error_message") or ""),
                )
        logger.info("单元快照保存完成", run_id=run_id, unit_id=unit_id, status=str(row.get("status") or ""))

    # 方法作用：列出一次运行的全部单元快照，按单元标识排序返回。
    # Args: run_id - 运行标识；identity - 当前身份。
    # Returns: 快照字典列表；内存分支按可见性过滤，PG 分支由 RLS 过滤。
    async def list_unit_snapshots(self, run_id: str, identity: Any) -> list[dict[str, Any]]:
        logger.debug("单元快照列表入口", run_id=str(run_id), tenant_id=int(getattr(identity, "tenant_id", 0) or 0))
        if self._memory:
            rows = [dict(row) for (rid, _unit), row in self._unit_snapshots.items() if rid == str(run_id) and self._visible(row, identity)]
            rows.sort(key=lambda item: str(item.get("unit_id", "")))
        else:
            from src.memory.pg_pool import pg_pool_connection

            pool = await self._pool()
            async with pg_pool_connection(pool, int(getattr(identity, "tenant_id", 0) or 0), int(getattr(identity, "user_id", 0) or 0), str(getattr(identity, "role", "") or "")) as connection:
                fetched = await connection.fetch("SELECT * FROM report_unit_snapshots WHERE run_id=$1::uuid ORDER BY unit_id", str(run_id))
            rows = [dict(item) for item in fetched]
        result = [self._snapshot_dict(row) for row in rows]
        logger.info("单元快照列表完成", run_id=str(run_id), count=len(result))
        return result

    # 方法作用：读取当前身份可访问的页面版本，页面版本 id 优先、任务 id 回退取最新页。
    # Args: report_id - 页面版本 id 或任务 id；identity - 当前身份。
    # Returns: 命中的页面版本或 None。
    async def get_report_version(self, report_id: str, identity: Any) -> ReportVersionRecord | None:
        """每次运行产生的页面必须独立可访问（设计 48 第 7.4 节），因此同一参数先按页面版本 id 精确命中，未命中再当任务 id 取最新页。"""
        if self._memory:
            # 页面版本 id 精确命中：多页面场景下非最新页面也要能按自己的 id 取到正文。
            row = self._versions.get(str(report_id))
            if row is not None and row.get("deleted_at") is None and self._visible(row, identity):
                logger.debug("页面版本按版本 id 命中", report_id=str(report_id), version=int(row.get("version", 0) or 0))
                return self._to_version(row)
            # 回退到任务 id：兼容旧查看页与只持有任务 id 的调用方。
            rows = [row for row in self._versions.values() if row["report_id"] == str(report_id) and self._visible(row, identity) and row.get("deleted_at") is None]
            row = next((item for item in rows if item.get("is_latest")), None) or (max(rows, key=lambda item: item["version"]) if rows else None)
            return self._to_version(row) if row else None
        if not _is_uuid(report_id):
            # 非 UUID 既不是页面版本 id 也不是任务 id：直接按不存在处理，避免数据库转型错误。
            logger.info("报表页面读取未命中", report_id=str(report_id), reason="invalid_identifier")
            return None
        pool = await self._pool()
        from src.memory.pg_pool import pg_pool_connection

        tenant_id = int(getattr(identity, "tenant_id", 0) or 0)
        user_id = int(getattr(identity, "user_id", 0) or 0)
        role = str(getattr(identity, "role", "") or "")
        async with pg_pool_connection(pool, tenant_id, user_id, role) as connection:
            # ORDER BY 第一项让「页面版本 id 命中」排在「任务 id 回退」之前，与内存分支判定一致。
            row = await connection.fetchrow("SELECT v.* FROM report_versions v JOIN report_jobs j ON j.id=v.job_id WHERE (v.id=$1::uuid OR v.report_id=$1::uuid) AND v.tenant_id=$2 AND v.deleted_at IS NULL AND ($3<>'super_admin' AND ($3='tenant_admin' OR j.user_id=$4)) ORDER BY (v.id=$1::uuid) DESC,v.is_latest DESC,v.version DESC LIMIT 1", str(report_id), tenant_id, role, user_id)
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

            if not _is_uuid(report_id):
                logger.info("页面生命周期更新未命中", report_id=str(report_id), reason="invalid_identifier")
                return None
            pool = await self._pool()
            tenant_id, user_id, role = int(identity.tenant_id), int(identity.user_id), str(identity.role)
            async with pg_pool_connection(pool, tenant_id, user_id, role) as connection:
                async with connection.transaction():
                    # 与 get_report_version 同一套定位规则：页面版本 id 优先，任务 id 回退到最新页。
                    row = await connection.fetchrow("SELECT v.* FROM report_versions v JOIN report_jobs j ON j.id=v.job_id WHERE (v.id=$1::uuid OR v.report_id=$1::uuid) AND v.tenant_id=$2 AND v.deleted_at IS NULL AND ($3='tenant_admin' OR j.user_id=$4) ORDER BY (v.id=$1::uuid) DESC,v.is_latest DESC,v.version DESC LIMIT 1 FOR UPDATE OF v", report_id, tenant_id, role, user_id)
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

    # 方法作用：统计指定用户在当前租户内已保存的永久页面数量，供永久报表上限校验。
    # Args: identity - 发起校验的当前身份（决定数据库会话的租户与角色）；user_id - 被统计的用户。
    # Returns: 永久页面数量（deleted_at IS NULL 且 expires_at IS NULL）。
    async def count_permanent_versions_for_user(self, identity: Any, user_id: int) -> int:
        """口径与租户设置里的 max_permanent_reports_per_user 一致：只统计当前租户、未清理、无限期的页面。"""
        tenant_id = int(getattr(identity, "tenant_id", 0) or 0)
        target_user_id = int(user_id or 0)
        logger.debug("永久页面计数入口", tenant_id=tenant_id, target_user_id=target_user_id)
        if self._memory:
            count = 0
            for row in self._versions.values():
                if int(row.get("tenant_id", 0) or 0) != tenant_id or row.get("deleted_at") is not None or row.get("expires_at") is not None:
                    continue
                # 归属按所属任务的拥有者判定；内存里没有任务行（单元测试直接写页面）时回退到页面写入身份。
                job = self._jobs.get(str(row.get("job_id", "")))
                owner = int((job or {}).get("user_id", row.get("user_id", 0)) or 0)
                if owner == target_user_id:
                    count += 1
        else:
            pool = await self._pool()
            from src.memory.pg_pool import pg_pool_connection

            # 用调用方身份开会话，让租户级 RLS 继续生效；统计本身只回传计数，不返回任何页面内容。
            async with pg_pool_connection(pool, tenant_id, int(getattr(identity, "user_id", 0) or 0), str(getattr(identity, "role", "") or "")) as connection:
                count = int(await connection.fetchval("SELECT COUNT(*) FROM report_versions v JOIN report_jobs j ON j.id=v.job_id WHERE v.tenant_id=$1 AND j.user_id=$2 AND v.deleted_at IS NULL AND v.expires_at IS NULL", tenant_id, target_user_id) or 0)
        logger.info("永久页面计数完成", tenant_id=tenant_id, target_user_id=target_user_id, count=count)
        return count

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
        # created_at 与数据库 DEFAULT NOW() 对齐，内存分支的链接管理列表才能给出真实创建时间。
        row = {**record, "id": share_id, "token_hash": token_hash, "code_hash": code_hash, "revoked_at": None, "view_count": 0, "created_at": record.get("created_at") or datetime.now(timezone.utc)}
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
    # Args: token_hash - HMAC token 摘要；count_view - 是否累加 view_count（埋点心跳不计数）。
    # Returns: 分享记录或 None。
    async def get_share_by_token(self, token_hash: str, *, count_view: bool = True) -> dict[str, Any] | None:
        if self._memory:
            share_id = self._share_by_token.get(token_hash)
            row = self._shares.get(share_id) if share_id else None
            if row and count_view:
                row["view_count"] = int(row.get("view_count", 0)) + 1
            return dict(row) if row else None
        pool = await self._pool()
        async with pool.acquire() as connection:
            async with connection.transaction():
                await connection.execute("SELECT set_config('app.report_share_access','true',true)")
                if count_view:
                    row = await connection.fetchrow("UPDATE report_shares SET view_count=view_count+1,last_viewed_at=NOW() WHERE token_hash=$1 AND revoked_at IS NULL RETURNING *", token_hash)
                else:
                    # 访问明细上报会高频调用本方法，绝不能把心跳累加进 view_count。
                    row = await connection.fetchrow("SELECT * FROM report_shares WHERE token_hash=$1 AND revoked_at IS NULL", token_hash)
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

    # 方法作用：记录一次分享访问明细，匿名访问只留 0 占位不写真实身份。
    # Args: share - 分享记录（字典或 ReportShareRecord）；tenant_id - 分享所属租户；visitor_kind - user/anonymous；user_id - 访客用户 ID；ip_hash - 客户端 IP 摘要；unlocked - 是否已用授权码解锁。
    # Returns: 新建的访问记录字典。
    async def create_share_visit(self, share: Any, tenant_id: int, visitor_kind: str, user_id: int, ip_hash: str, unlocked: bool) -> dict[str, Any]:
        visit_id = str(uuid4())
        share_id = str(share.get("id") if isinstance(share, dict) else getattr(share, "id", ""))
        report_id = str(share.get("report_id") if isinstance(share, dict) else getattr(share, "report_id", ""))
        # 关闭身份记录开关时调用方已降级为 anonymous，这里再兜一次底：没有用户 ID 一律匿名。
        kind = "user" if str(visitor_kind) == "user" and int(user_id or 0) > 0 else "anonymous"
        now = datetime.now(timezone.utc)
        logger.debug("分享访问记录创建入口", share_id=share_id, tenant_id=int(tenant_id or 0), visitor_kind=kind)
        row = {
            "id": visit_id,
            "share_id": share_id,
            "report_id": report_id,
            "tenant_id": int(tenant_id or 0),
            "visitor_kind": kind,
            "user_id": int(user_id or 0) if kind == "user" else 0,
            "ip_hash": str(ip_hash or "")[:128],
            "unlocked": bool(unlocked),
            "started_at": now,
            "last_seen_at": now,
            "duration_seconds": 0,
        }
        if self._memory:
            self._share_visits[visit_id] = row
            logger.info("分享访问记录创建完成", visit_id=visit_id, visitor_kind=kind)
            return dict(row)
        # 公开侧没有登录身份，只凭分享访问标记放行 INSERT；token 归属校验由调用方先行完成。
        pool = await self._pool()
        async with pool.acquire() as connection:
            async with connection.transaction():
                await connection.execute("SELECT set_config('app.report_share_access','true',true)")
                db_row = await connection.fetchrow(
                    "INSERT INTO report_share_visits (id,tenant_id,share_id,report_id,visitor_kind,user_id,ip_hash,unlocked,started_at,last_seen_at,duration_seconds) "
                    "VALUES ($1::uuid,$2,$3::uuid,$4::uuid,$5,$6,$7,$8,NOW(),NOW(),0) RETURNING *",
                    visit_id, row["tenant_id"], share_id, report_id, kind, row["user_id"], row["ip_hash"], row["unlocked"],
                )
        logger.info("分享访问记录创建完成", visit_id=visit_id, visitor_kind=kind)
        return dict(db_row)

    # 方法作用：按 token 归属幂等覆盖访问停留时长，并刷新最近可见时间。
    # Args: token_hash - 分享 token 摘要；visit_id - 访问记录标识；duration_seconds - 累积停留秒数。
    # Returns: 更新后的访问记录；不属于该 token 或不存在时返回 None。
    async def update_share_visit(self, token_hash: str, visit_id: str, duration_seconds: int) -> dict[str, Any] | None:
        bounded = max(0, int(duration_seconds))
        logger.debug("分享访问时长更新入口", visit_id=visit_id, duration_seconds=bounded)
        if self._memory:
            row = self._share_visits.get(str(visit_id))
            share_id = self._share_by_token.get(str(token_hash))
            share_row = self._shares.get(str(share_id)) if share_id else None
            # 与数据库分支保持同一判据：token 归属不符或分享已撤销都不允许再写时长。
            if row is None or share_row is None or share_row.get("revoked_at") is not None or str(row.get("share_id")) != str(share_id):
                logger.info("分享访问时长更新跳过", visit_id=visit_id, reason="归属校验未通过")
                return None
            row["duration_seconds"] = bounded
            row["last_seen_at"] = datetime.now(timezone.utc)
            logger.info("分享访问时长更新完成", visit_id=visit_id, duration_seconds=bounded)
            return dict(row)
        if not _is_uuid(visit_id):
            # 非法标识在数据库里不可能存在，直接按未命中处理，避免 UUID 转型异常。
            logger.info("分享访问时长更新跳过", visit_id=visit_id, reason="标识非 UUID")
            return None
        pool = await self._pool()
        async with pool.acquire() as connection:
            async with connection.transaction():
                await connection.execute("SELECT set_config('app.report_share_access','true',true)")
                # 归属校验与写入放在同一条语句里，token 与 visit 不一致时不会命中任何行。
                db_row = await connection.fetchrow(
                    "UPDATE report_share_visits v SET duration_seconds=$3,last_seen_at=NOW() FROM report_shares s "
                    "WHERE v.id=$1::uuid AND v.share_id=s.id AND s.token_hash=$2 AND s.revoked_at IS NULL RETURNING v.*",
                    str(visit_id), str(token_hash), bounded,
                )
        if db_row is None:
            logger.info("分享访问时长更新跳过", visit_id=visit_id, reason="访问记录不存在或不属于该分享")
            return None
        logger.info("分享访问时长更新完成", visit_id=visit_id, duration_seconds=bounded)
        return dict(db_row)

    # 方法作用：按可见范围平铺租户内分享，并带出按访问明细聚合的计数与时长。
    # Args: identity - 当前身份；report_id - 可选报表筛选；created_by - 可选创建人筛选（仅租户管理员）；status - active/revoked/expired；page - 页码；page_size - 每页条数。
    # Returns: (当前页列表行, 命中总数)。
    async def list_tenant_shares(self, identity: Any, *, report_id: str | None = None, created_by: int | None = None, status: str | None = None, page: int = 1, page_size: int = 20) -> tuple[list[dict[str, Any]], int]:
        bounded_page = max(1, int(page or 1))
        bounded_size = max(1, min(int(page_size or 20), 100))
        logger.debug("链接管理列表查询入口", tenant_id=int(getattr(identity, "tenant_id", 0) or 0), page=bounded_page, page_size=bounded_size)
        if self._memory:
            rows = [row for row in self._shares.values() if self._visible(row, identity)]
            if report_id:
                rows = [row for row in rows if str(row.get("report_id", "")) == str(report_id)]
            if created_by is not None:
                rows = [row for row in rows if int(row.get("created_by", 0) or 0) == int(created_by)]
            if status:
                rows = [row for row in rows if self._share_status(row) == str(status)]
            rows.sort(key=lambda row: row.get("created_at") or datetime.min.replace(tzinfo=timezone.utc), reverse=True)
            total = len(rows)
            start = (bounded_page - 1) * bounded_size
            items = [self._memory_share_item(row) for row in rows[start:start + bounded_size]]
            logger.info("链接管理列表查询完成", tenant_id=int(getattr(identity, "tenant_id", 0) or 0), total=total, returned=len(items))
            return items, total
        pool = await self._pool()
        from src.memory.pg_pool import pg_pool_connection

        tenant_id = int(getattr(identity, "tenant_id", 0) or 0)
        user_id = int(getattr(identity, "user_id", 0) or 0)
        role = str(getattr(identity, "role", "") or "")
        # report_id 在数据库中是 UUID 列；非 UUID 的筛选值不可能命中任何分享。
        report_filter = str(report_id) if report_id else None
        if report_filter is not None and not _is_uuid(report_filter):
            logger.info("链接管理列表查询完成", tenant_id=tenant_id, total=0, reason="报表标识非 UUID")
            return [], 0
        parameters = (tenant_id, role, user_id, report_filter, created_by, str(status or ""))
        async with pg_pool_connection(pool, tenant_id, user_id, role) as connection:
            # 一条聚合 SQL 带出列表与访问统计，避免按分享逐条查询造成 N+1。
            rows = await connection.fetch(
                "SELECT s.id::text AS id, s.report_id::text AS report_id, s.scope, s.user_scope, s.created_by, s.created_at, s.expires_at, s.revoked_at, "
                "COALESCE(v.version,0) AS version, COALESCE(j.prompt,'') AS report_prompt, "
                "CASE WHEN COALESCE(s.code_hash,'') = '' THEN FALSE ELSE TRUE END AS require_code, "
                "COUNT(vs.id) AS visitor_count, "
                "COUNT(vs.id) FILTER (WHERE vs.visitor_kind='user') AS system_visitor_count, "
                "COUNT(vs.id) FILTER (WHERE vs.visitor_kind='anonymous') AS anonymous_visitor_count, "
                "COALESCE(SUM(vs.duration_seconds),0) AS total_duration_seconds, "
                "COUNT(*) OVER () AS total_count "
                "FROM report_shares s "
                "LEFT JOIN report_versions v ON v.id=s.report_version_id "
                "LEFT JOIN report_jobs j ON j.id=v.job_id "
                "LEFT JOIN report_share_visits vs ON vs.share_id=s.id "
                f"WHERE {_SHARE_LIST_FILTER_SQL} "
                "GROUP BY s.id, s.report_id, s.scope, s.user_scope, s.created_by, s.created_at, s.expires_at, s.revoked_at, v.version, j.prompt "
                "ORDER BY s.created_at DESC LIMIT $7 OFFSET $8",
                *parameters, bounded_size, (bounded_page - 1) * bounded_size,
            )
            if rows:
                total = int(rows[0]["total_count"])
            elif bounded_page > 1:
                # 窗口计数随行返回，翻过尾页拿不到行时才补一次总数查询。
                total = int(await connection.fetchval(f"SELECT COUNT(*) FROM report_shares s WHERE {_SHARE_LIST_FILTER_SQL}", *parameters) or 0)
            else:
                total = 0
        items = [self._pg_share_item(row) for row in rows]
        logger.info("链接管理列表查询完成", tenant_id=tenant_id, total=total, returned=len(items))
        return items, total

    # 方法作用：读取分享最近访问的系统用户及其停留时长，权限与该分享的可见范围一致。
    # Args: share_id - 分享标识；identity - 当前身份。
    # Returns: 访问者列表；分享不存在或越权返回 None。
    async def list_share_visitors(self, share_id: str, identity: Any) -> list[dict[str, Any]] | None:
        # 上限 50：链接管理只展示最近访问，不提供全量导出。
        limit = 50
        logger.debug("分享访问者列表入口", share_id=share_id, user_id=int(getattr(identity, "user_id", 0) or 0))
        if self._memory:
            row = self._shares.get(str(share_id))
            if row is None or not self._visible(row, identity):
                logger.info("分享访问者列表跳过", share_id=share_id, reason="分享不存在或越权")
                return None
            visits = [
                item
                for item in self._share_visits.values()
                if str(item.get("share_id", "")) == str(share_id) and str(item.get("visitor_kind", "")) == "user"
            ]
            visits.sort(key=lambda item: item.get("started_at") or datetime.min.replace(tzinfo=timezone.utc), reverse=True)
            result = [self._memory_visitor_item(item) for item in visits[:limit]]
            logger.info("分享访问者列表完成", share_id=share_id, visit_count=len(result))
            return result
        if not _is_uuid(share_id):
            return None
        pool = await self._pool()
        from src.memory.pg_pool import pg_pool_connection

        tenant_id = int(getattr(identity, "tenant_id", 0) or 0)
        user_id = int(getattr(identity, "user_id", 0) or 0)
        role = str(getattr(identity, "role", "") or "")
        async with pg_pool_connection(pool, tenant_id, user_id, role) as connection:
            # 先判分享可见性，再取访问明细；两步分别复用分享可见范围规则。
            visible = await connection.fetchval(
                "SELECT 1 FROM report_shares WHERE id=$1::uuid AND tenant_id=$2 AND ($3<>'super_admin' AND ($3='tenant_admin' OR created_by=$4))",
                str(share_id), tenant_id, role, user_id,
            )
            if not visible:
                logger.info("分享访问者列表跳过", share_id=share_id, reason="分享不存在或越权")
                return None
            rows = await connection.fetch(
                "SELECT v.user_id, COALESCE(u.username,'') AS username, v.started_at, v.last_seen_at, v.duration_seconds, v.ip_hash "
                "FROM report_share_visits v JOIN report_shares s ON s.id=v.share_id "
                "LEFT JOIN users u ON u.id=v.user_id "
                "WHERE v.share_id=$1::uuid AND v.tenant_id=$2 AND v.visitor_kind='user' "
                "ORDER BY v.started_at DESC LIMIT $3",
                str(share_id), tenant_id, limit,
            )
        result = [
            {
                "user_id": int(row["user_id"]),
                "username": str(row["username"] or ""),
                "started_at": row["started_at"],
                "last_seen_at": row["last_seen_at"],
                "duration_seconds": int(row["duration_seconds"] or 0),
                "ip_hash": str(row["ip_hash"] or ""),
            }
            for row in rows
        ]
        logger.info("分享访问者列表完成", share_id=share_id, visit_count=len(result))
        return result

    # 方法作用：批量撤销可见范围内的分享，个别标识越权或已撤销不影响其余撤销。
    # Args: share_ids - 待撤销的分享标识列表；identity - 当前身份。
    # Returns: (成功撤销数, 跳过数)。
    async def revoke_shares(self, share_ids: list[str], identity: Any) -> tuple[int, int]:
        normalized = list(dict.fromkeys(str(item) for item in share_ids if str(item).strip()))
        logger.debug("批量撤销分享入口", tenant_id=int(getattr(identity, "tenant_id", 0) or 0), request_count=len(normalized))
        if self._memory:
            now = datetime.now(timezone.utc)
            revoked = 0
            for share_id in normalized:
                row = self._shares.get(share_id)
                if row is None or not self._visible(row, identity) or row.get("revoked_at") is not None:
                    continue
                row["revoked_at"] = now
                revoked += 1
            skipped = len(normalized) - revoked
            logger.info("批量撤销分享完成", revoked=revoked, skipped=skipped)
            return revoked, skipped
        pool = await self._pool()
        from src.memory.pg_pool import pg_pool_connection

        tenant_id = int(getattr(identity, "tenant_id", 0) or 0)
        user_id = int(getattr(identity, "user_id", 0) or 0)
        role = str(getattr(identity, "role", "") or "")
        valid_ids = [item for item in normalized if _is_uuid(item)]
        revoked = 0
        if valid_ids:
            async with pg_pool_connection(pool, tenant_id, user_id, role) as connection:
                rows = await connection.fetch(
                    "UPDATE report_shares SET revoked_at=NOW() WHERE id=ANY($1::uuid[]) AND tenant_id=$2 "
                    "AND ($3<>'super_admin' AND ($3='tenant_admin' OR created_by=$4)) AND revoked_at IS NULL RETURNING id",
                    valid_ids, tenant_id, role, user_id,
                )
            revoked = len(rows)
        skipped = len(normalized) - revoked
        logger.info("批量撤销分享完成", revoked=revoked, skipped=skipped)
        return revoked, skipped

    # 方法作用：读取租户级报表设置，未配置时返回默认值。
    # Args: tenant_id - 租户标识。
    # Returns: 设置字典（含开关与永久报表上限）。
    async def get_tenant_settings(self, tenant_id: int) -> dict[str, Any]:
        defaults = {
            "tenant_id": int(tenant_id or 0),
            "record_share_visitor_identity": True,
            "max_permanent_reports_per_user": 0,
            "updated_by": 0,
            "updated_at": None,
        }
        if self._memory:
            stored = self._tenant_settings.get(int(tenant_id or 0))
            return {**defaults, **dict(stored)} if stored else defaults
        pool = await self._pool()
        from src.memory.pg_pool import pg_pool_connection

        record_tenant = int(tenant_id or 0)
        async with pg_pool_connection(pool, record_tenant, 0, "analyst") as connection:
            row = await connection.fetchrow("SELECT tenant_id, record_share_visitor_identity, max_permanent_reports_per_user, updated_by, updated_at FROM report_tenant_settings WHERE tenant_id=$1", record_tenant)
        return {**defaults, **dict(row)} if row else defaults

    # 方法作用：保存租户级报表设置。
    # Args: tenant_id - 租户标识；settings - 开关与永久报表上限；updated_by - 操作管理员用户 ID。
    # Returns: 保存后的设置字典。
    async def save_tenant_settings(self, tenant_id: int, settings: dict[str, Any], updated_by: int) -> dict[str, Any]:
        record_tenant = int(tenant_id or 0)
        values = {
            "tenant_id": record_tenant,
            "record_share_visitor_identity": bool(settings.get("record_share_visitor_identity", True)),
            "max_permanent_reports_per_user": max(0, int(settings.get("max_permanent_reports_per_user", 0) or 0)),
            "updated_by": int(updated_by or 0),
        }
        logger.debug("租户报表设置保存入口", tenant_id=record_tenant, updated_by=values["updated_by"])
        if self._memory:
            stored = {**values, "updated_at": datetime.now(timezone.utc)}
            self._tenant_settings[record_tenant] = stored
            logger.info("租户报表设置保存完成", tenant_id=record_tenant)
            return dict(stored)
        pool = await self._pool()
        from src.memory.pg_pool import pg_pool_connection

        # 设置表的管理策略要求 tenant_admin 角色；调用方在 API 层已校验租户管理员身份。
        async with pg_pool_connection(pool, record_tenant, values["updated_by"], "tenant_admin") as connection:
            row = await connection.fetchrow(
                "INSERT INTO report_tenant_settings (tenant_id,record_share_visitor_identity,max_permanent_reports_per_user,updated_by,updated_at) "
                "VALUES ($1,$2,$3,$4,NOW()) "
                "ON CONFLICT (tenant_id) DO UPDATE SET record_share_visitor_identity=EXCLUDED.record_share_visitor_identity, "
                "max_permanent_reports_per_user=EXCLUDED.max_permanent_reports_per_user, updated_by=EXCLUDED.updated_by, updated_at=NOW() RETURNING *",
                record_tenant, values["record_share_visitor_identity"], values["max_permanent_reports_per_user"], values["updated_by"],
            )
        logger.info("租户报表设置保存完成", tenant_id=record_tenant)
        return dict(row)

    # 方法作用：按分享 ID 聚合访问计数与停留时长。
    # Args: share_id - 分享标识。
    # Returns: 计数、系统/匿名访客数与平均时长。
    def _visit_aggregate(self, share_id: str) -> dict[str, int]:
        visits = [row for row in self._share_visits.values() if str(row.get("share_id", "")) == str(share_id)]
        total_duration = sum(int(row.get("duration_seconds", 0) or 0) for row in visits)
        count = len(visits)
        return {
            "visitor_count": count,
            "system_visitor_count": sum(1 for row in visits if str(row.get("visitor_kind", "")) == "user"),
            "anonymous_visitor_count": sum(1 for row in visits if str(row.get("visitor_kind", "")) != "user"),
            "total_duration_seconds": total_duration,
            "average_duration_seconds": total_duration // count if count else 0,
        }

    # 方法作用：判定分享当前状态，供有效/已撤销/已过期筛选复用。
    # Args: row - 内存分享记录。
    # Returns: active/revoked/expired 之一。
    @staticmethod
    def _share_status(row: dict[str, Any]) -> str:
        if row.get("revoked_at") is not None:
            return "revoked"
        expires_at = row.get("expires_at")
        if isinstance(expires_at, str):
            try:
                expires_at = datetime.fromisoformat(expires_at.replace("Z", "+00:00"))
            except ValueError:
                return "active"
        if isinstance(expires_at, datetime):
            value = expires_at if expires_at.tzinfo is not None else expires_at.replace(tzinfo=timezone.utc)
            if value <= datetime.now(timezone.utc):
                return "expired"
        return "active"

    # 方法作用：把内存分享记录组装成链接管理列表行。
    # Args: row - 内存分享记录。
    # Returns: 含访问聚合的列表行字典。
    def _memory_share_item(self, row: dict[str, Any]) -> dict[str, Any]:
        share_id = str(row.get("id", ""))
        version = self._versions.get(str(row.get("report_version_id", ""))) or {}
        job = self._jobs.get(str(version.get("job_id", ""))) or {}
        # report_jobs 暂无 title 列，先用任务描述充当标题来源。
        prompt = str(row.get("report_prompt") or job.get("prompt", "") or "")
        data = {
            "id": share_id,
            "report_id": row.get("report_id", ""),
            "report_prompt": prompt,
            "version": row.get("version", version.get("version", 0)),
            "scope": row.get("scope", "public"),
            "user_scope": row.get("user_scope", "none"),
            "require_code": bool(str(row.get("code_hash", "") or "")),
            "created_by": row.get("created_by", 0),
            "created_at": row.get("created_at"),
            "expires_at": row.get("expires_at"),
            "revoked_at": row.get("revoked_at"),
            **self._visit_aggregate(share_id),
        }
        return to_share_list_item(data)

    # 方法作用：把数据库聚合行组装成链接管理列表行。
    # Args: row - asyncpg 聚合查询结果行。
    # Returns: 含访问聚合的列表行字典。
    @staticmethod
    def _pg_share_item(row: Any) -> dict[str, Any]:
        data = dict(row)
        total_duration = int(data.get("total_duration_seconds", 0) or 0)
        count = int(data.get("visitor_count", 0) or 0)
        data["total_duration_seconds"] = total_duration
        data["average_duration_seconds"] = total_duration // count if count else 0
        return to_share_list_item(data)

    # 方法作用：把内存访问记录转换为系统用户访问条目。
    # Args: row - 内存访问记录。
    # Returns: 访问者条目字典。
    def _memory_visitor_item(self, row: dict[str, Any]) -> dict[str, Any]:
        visitor_id = int(row.get("user_id", 0) or 0)
        return {
            "user_id": visitor_id,
            "username": str(self._usernames.get(visitor_id, "") or ""),
            "started_at": row.get("started_at"),
            "last_seen_at": row.get("last_seen_at"),
            "duration_seconds": int(row.get("duration_seconds", 0) or 0),
            "ip_hash": str(row.get("ip_hash", "") or ""),
        }

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

    # 方法作用：列出本租户的全部用户 ID，供租户管理员按「全租户」范围重置额度。
    # Args: identity - 操作管理员身份。
    # Returns: 去重升序的用户 ID 列表，只含当前租户用户；租户缺失时返回空列表。
    async def list_tenant_user_ids(self, identity: Any) -> list[int]:
        """平台与跨租户身份都拿不到用户清单：查询始终以当前身份的租户为条件。"""
        tenant_id = int(getattr(identity, "tenant_id", 0) or 0)
        logger.debug("租户用户清单读取入口", tenant_id=tenant_id, user_id=int(getattr(identity, "user_id", 0) or 0))
        if tenant_id <= 0:
            logger.info("租户用户清单读取跳过", tenant_id=tenant_id, reason="缺少租户")
            return []
        if self._memory:
            # 内存分支没有 users 表，只能汇总 Store 内已登记的本租户用户（任务、分享与策略覆盖），
            # 统一按 tenant_id 过滤，不把其他租户的用户算进来。
            known: set[int] = set()
            for row in self._jobs.values():
                if int(row.get("tenant_id", 0) or 0) == tenant_id:
                    known.add(int(row.get("user_id", 0) or 0))
            for row in self._shares.values():
                if int(row.get("tenant_id", 0) or 0) == tenant_id:
                    known.add(int(row.get("created_by", 0) or 0))
            for tenant_key, user_key in list(self._quota_policies) + list(self._capabilities):
                if int(tenant_key) == tenant_id:
                    known.add(int(user_key))
            known.discard(0)
            users = sorted(known)
            logger.info("租户用户清单读取完成", tenant_id=tenant_id, user_count=len(users), memory=True)
            return users
        from src.memory.pg_pool import pg_pool_connection

        async with pg_pool_connection(await self._pool(), tenant_id, int(getattr(identity, "user_id", 0) or 0), str(getattr(identity, "role", "") or "")) as connection:
            rows = await connection.fetch("SELECT id FROM users WHERE tenant_id=$1", tenant_id)
        users = sorted({int(row["id"]) for row in rows})
        logger.info("租户用户清单读取完成", tenant_id=tenant_id, user_count=len(users))
        return users

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

    # 方法作用：把目标用户当前周期的指定已用量列清零，返回实际被清零的用户 ID。
    # Args: identity - 操作管理员身份；user_ids - 目标用户 ID 列表；fields - 要清零的字段名（仅四个按月累计字段）；period_start - 当前周期起点。
    # Returns: 实际被清零的用户 ID 升序列表；没有匹配的用量行时为空列表。
    async def reset_quota_usage(self, identity: Any, user_ids: list[int], fields: list[str], period_start: datetime) -> list[int]:
        """只清用量不动上限：SET 列名只能取自白名单，请求里的字段名绝不直接拼进 SQL。"""
        columns = normalize_reset_fields(fields)
        targets = sorted({int(item) for item in user_ids if int(item) > 0})
        tenant_id = int(getattr(identity, "tenant_id", 0) or 0)
        logger.debug("报表额度用量重置入口", tenant_id=tenant_id, target_count=len(targets), fields=columns, period_start=period_start.isoformat())
        if not targets:
            logger.info("报表额度用量重置跳过", tenant_id=tenant_id, reason="没有有效目标用户")
            return []
        if self._memory:
            # 内存模式的周期用量由 QuotaManager 的内存缓存持有（与 reserve_quota/get_quota_usage 的既有约定一致），
            # Store 侧没有用量行，清零由 QuotaManager.reset_usage 的内存分支完成。
            logger.info("报表额度用量重置跳过", tenant_id=tenant_id, reason="内存分支无用量行")
            return []
        from src.memory.pg_pool import pg_pool_connection

        # 列名来自 normalize_reset_fields 的白名单，值全部走占位符，注入面只剩这四个固定列名。
        assignments = ", ".join(f"{column}=0" for column in columns)
        sql = f"UPDATE report_quota_usage SET {assignments} WHERE tenant_id=$1 AND user_id = ANY($2) AND period_start=$3 RETURNING user_id"
        pool = await self._pool()
        async with pg_pool_connection(pool, tenant_id, int(getattr(identity, "user_id", 0) or 0), str(getattr(identity, "role", "") or "")) as connection:
            rows = await connection.fetch(sql, tenant_id, targets, period_start)
        cleared = sorted({int(row["user_id"]) for row in rows})
        logger.info("报表额度用量重置完成", tenant_id=tenant_id, reset_users=len(cleared), fields=columns)
        return cleared

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

    # 方法作用：回收超时仍未收尾的运行租约，避免死进程永久占用并发槽。
    # Args: stale_after_seconds - 超过该时长仍处于 queued/running 的运行视为遗留。
    # Returns: 被回收的运行数。
    async def reclaim_stale_run_leases(self, stale_after_seconds: int = 1800) -> int:
        """执行进程被杀、容器重启都会留下 quota_held 未释放的运行；不回收则并发槽只减不增。

        实机事故：两个运行分别停在上一次进程退出前后，`concurrent_runs` 顶到上限 2，
        此后任何报表都直接 QUOTA_EXCEEDED，而恢复逻辑只在重跑同一任务时触发。
        """
        threshold = max(60, int(stale_after_seconds))
        if self._memory:
            return 0
        from src.memory.pg_pool import pg_pool_connection

        pool = await self._pool()
        # 系统身份清理跨租户的遗留租约：调用方不是某个业务用户，不能用业务身份的作用域连接。
        async with pg_pool_connection(pool, 0, 0, "super_admin") as connection:
            reclaimed = await connection.fetch(
                "UPDATE report_runs SET status='failed', error_code='WORKER_INTERRUPTED', "
                "user_error_message='执行进程已中断，并发资源已释放', finished_at=NOW(), quota_held=FALSE "
                "WHERE status IN ('queued','running') AND quota_held IS TRUE "
                "AND COALESCE(started_at, created_at) < NOW() - make_interval(secs => $1::int) "
                "RETURNING tenant_id, user_id, quota_period_start",
                threshold,
            )
            if not reclaimed:
                logger.debug("报表遗留租约回收完成", reclaimed=0, threshold_seconds=threshold)
                return 0
            # 按 (租户, 用户, 周期) 归并后一次扣减：逐条扣会放大成 N 次往返。
            for row in reclaimed:
                if row["quota_period_start"] is None:
                    continue
                await connection.execute(
                    "UPDATE report_quota_usage SET concurrent_runs=GREATEST(0, concurrent_runs-1) "
                    "WHERE tenant_id=$1 AND user_id=$2 AND period_start=$3",
                    int(row["tenant_id"]),
                    int(row["user_id"]),
                    row["quota_period_start"],
                )
        logger.warning("报表遗留租约已回收", reclaimed=len(reclaimed), threshold_seconds=threshold)
        return len(reclaimed)

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

    # 方法作用：把内存或数据库行归一成对外快照字典，统一 params 与时间格式。
    # Args: row - 内存字典或 asyncpg Record。
    # Returns: 字段名与迁移列一致的字典，params_json 改写为 params。
    @staticmethod
    def _snapshot_dict(row: Any) -> dict[str, Any]:
        data = dict(row)
        data["params"] = ReportStore._json_dict(data.get("params_json", data.get("params", {})))
        data.pop("params_json", None)
        created = data.get("created_at")
        if isinstance(created, datetime):
            data["created_at"] = created.isoformat()
        return data

    # 方法作用：把数据库行转换为安全的任务模型。
    # Args: row - asyncpg Record 或内存字典。
    # Returns: ReportJobRecord。
    @classmethod
    def _to_job(cls, row: Any) -> ReportJobRecord:
        plan_data = cls._json_dict(row.get("plan_json", {}) if hasattr(row, "get") else row["plan_json"])
        if not plan_data.get("analysis_units"):
            # 后台规划中的记录还没有真实计划（plan_json 仍是空对象）：用占位计划满足
            # ReportPlan 的单元数下限，真实计划由 plan_worker 写回后被替换。
            plan_data = cls._placeholder_plan_data(row)
        return ReportJobRecord(id=str(row["id"]), tenant_id=int(row["tenant_id"]), user_id=int(row["user_id"]), prompt=str(row["prompt"]), plan=ReportPlan.model_validate(plan_data), plan_hash=str(row.get("plan_hash", "") or ""), preview_hash=str(row.get("preview_hash", "") or ""), template_id=(str(row.get("template_id")) if row.get("template_id") else None), status=str(row.get("status", "draft")), runs_started=int(row.get("runs_started", 0) or 0), next_run_at=row.get("next_run_at"), is_paused=bool(row.get("is_paused", False)), created_at=row.get("created_at"), updated_at=row.get("updated_at"), title=str(row.get("title", "") or ""), divergence=str(row.get("divergence", "standard") or "standard"), datasources=cls._datasource_list(row.get("datasources_json", [])), plan_error=str(row.get("plan_error", "") or ""))

    # 方法作用：为「计划尚未生成」的记录构造可校验的占位计划。
    # Args: row - 任务记录行（含 prompt / datasources_json / divergence / timezone）。
    # Returns: 满足 ReportPlan 契约的占位计划字典。
    @classmethod
    def _placeholder_plan_data(cls, row: Any) -> dict[str, Any]:
        prompt = str(row.get("prompt", "") or "").strip() or "报表计划生成中"
        return {
            # unit_id 带 pending 前缀：任何误把占位计划当真实计划的行为都能一眼看出。
            "analysis_units": [{"unit_id": "pending_plan", "question": prompt[:4000]}],
            "requested_datasources": cls._datasource_list(row.get("datasources_json", [])),
            "divergence": str(row.get("divergence", "standard") or "standard"),
            "timezone": str(row.get("timezone", "Asia/Shanghai") or "Asia/Shanghai"),
        }

    # 方法作用：把数据库或内存里的数据源列表列规范化为字符串列表。
    # Args: value - JSONB 列、JSON 字符串或列表。
    # Returns: 去重后的非空数据源名列表。
    @staticmethod
    def _datasource_list(value: Any) -> list[str]:
        items = value
        if isinstance(items, str):
            try:
                items = json.loads(items)
            except (TypeError, ValueError):
                items = []
        if not isinstance(items, list):
            return []
        return list(dict.fromkeys(str(item).strip() for item in items if str(item).strip()))

    # 方法作用：把数据库行转换为安全的运行模型。
    # Args: row - asyncpg Record 或内存字典。
    # Returns: ReportRunRecord。
    @staticmethod
    def _to_run(row: Any) -> ReportRunRecord:
        return ReportRunRecord(id=str(row["id"]), job_id=str(row["job_id"]), status=str(row.get("status", "queued")), attempt=int(row.get("attempt", 1) or 1), last_node=str(row.get("last_node", "") or ""), query_count=int(row.get("query_count", 0) or 0), row_count=int(row.get("row_count", 0) or 0), token_usage=int(row.get("token_usage", 0) or 0), prompt_token_usage=int(row.get("prompt_token_usage", 0) or 0), completion_token_usage=int(row.get("completion_token_usage", 0) or 0), llm_call_count=int(row.get("llm_call_count", 0) or 0), llm_failure_count=int(row.get("llm_failure_count", 0) or 0), sql_query_count=int(row.get("sql_query_count", 0) or 0), sql_success_count=int(row.get("sql_success_count", 0) or 0), sql_failure_count=int(row.get("sql_failure_count", 0) or 0), partial=bool(row.get("partial", False)), error_code=str(row.get("error_code", "") or ""), user_error_message=str(row.get("user_error_message", "") or ""), trace_id=str(row.get("trace_id", "") or ""), started_at=row.get("started_at"), finished_at=row.get("finished_at"))

    # 方法作用：把运行行转换为执行明细页直接可用的字典，附上计数汇总与所属任务摘要。
    # Args: row - asyncpg Record 或内存字典，需含 job_title/job_prompt/job_status 可选列。
    # Returns: 含 run 字段、stats 计数汇总与任务摘要的字典。
    @classmethod
    def _run_detail_dict(cls, row: Any) -> dict[str, Any]:
        run = cls._to_run(row)
        payload = run.model_dump(mode="json")
        payload["stats"] = run.stats_summary()
        payload["job_title"] = str(row.get("job_title", "") or "")
        payload["job_prompt"] = str(row.get("job_prompt", "") or "")
        payload["job_status"] = str(row.get("job_status", "") or "")
        return payload

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
