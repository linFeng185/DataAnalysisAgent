"""持久化报表调度：跨 Worker 独占、心跳、身份重建和中断恢复。"""

from __future__ import annotations

import asyncio
import calendar
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from typing import Any
from zoneinfo import ZoneInfo

from src.logging_config import get_logger
from src.reporting.models import ReportPlan
from src.reporting.quota import QuotaManager, ReportPermissionService
from src.reporting.store import ReportStore

logger = get_logger(__name__)


# 方法作用：从数据源访问解析结果中提取授权语义，剔除数据源元数据。
# Args: resolved - _resolve_plan_access 返回的数据源映射。
# Returns: 仅含数据源集合、列白名单、行过滤和访问级别的可比较视图。
def _authorization_view(resolved: dict[str, dict[str, Any]]) -> dict[str, tuple[tuple[str, ...], str, str]]:
    """执行期间数据源元数据会变化，权限比对只能看授权相关字段。"""
    return {
        name: (
            tuple(sorted(str(column) for column in (entry.get("allowed_columns") or []))),
            str(entry.get("row_filter_sql", "") or ""),
            str(entry.get("access_level", "") or ""),
        )
        for name, entry in resolved.items()
    }


# 方法作用：按本地日历推进到严格晚于当前时间的下一期，跳过停机积压而不突发补跑。
# Args: plan - 调度计划；after - 当前时间；runs_started - 已启动次数。
# Returns: UTC 下次时间；次数、截止日期耗尽或立即任务返回 None。
def next_occurrence(plan: ReportPlan, after: datetime, *, runs_started: int) -> datetime | None:
    """月度保留原始锚点日；小时使用实际经过时长，日周月使用本地日历。"""
    if plan.execution_mode != "scheduled" or not plan.frequency or plan.max_runs is not None and runs_started >= plan.max_runs:
        return None
    zone = ZoneInfo(plan.timezone)
    current = after.astimezone(timezone.utc)
    anchor = (plan.run_at or after).astimezone(zone)
    local = current.astimezone(zone)
    if plan.frequency == "hourly":
        periods = max(0, int((current - anchor.astimezone(timezone.utc)).total_seconds() // 3600) + 1)
        candidate = anchor.astimezone(timezone.utc) + timedelta(hours=periods)
    else:
        if plan.frequency == "monthly":
            periods = max(0, (local.year - anchor.year) * 12 + local.month - anchor.month)
        else:
            interval = 7 if plan.frequency == "weekly" else 1
            periods = max(0, (local.date() - anchor.date()).days // interval)
        for _attempt in range(3):
            if plan.frequency == "monthly":
                month_index = anchor.year * 12 + anchor.month - 1 + periods
                year, month_zero = divmod(month_index, 12)
                month = month_zero + 1
                candidate = anchor.replace(year=year, month=month, day=min(plan.schedule_day or anchor.day, calendar.monthrange(year, month)[1]))
            else:
                candidate = anchor + timedelta(days=periods * interval)
            candidate = candidate.astimezone(timezone.utc)
            if candidate > current:
                break
            periods += 1
        else:
            raise ValueError("无法推进调度日历")
    if plan.schedule_end_at is not None and candidate > plan.schedule_end_at.astimezone(timezone.utc):
        return None
    return candidate


# 方法作用：为后台 SQL、记忆与模型链路绑定实际任务拥有者，离开时恢复系统上下文。
# Args: identity - 数据库重建的当前业务身份。
# Returns: 身份上下文管理器。
@contextmanager
def report_identity_scope(identity: Any) -> Iterator[None]:
    from src.api import auth

    variables = (auth._current_tenant_id, auth._current_user_id, auth._current_role, auth._current_tenant_code, auth._current_username)
    values = (identity.tenant_id, identity.user_id, identity.role, getattr(identity, "tenant_code", ""), getattr(identity, "username", ""))
    tokens = [variable.set(value) for variable, value in zip(variables, values, strict=True)]
    try:
        yield
    finally:
        for variable, token in reversed(list(zip(variables, tokens, strict=True))):
            variable.reset(token)


class ReportScheduler:
    """周期认领数据库任务，不依赖请求进程保存运行权或凭证。"""

    # 方法作用：注入可替换边界，默认使用持久化当前身份和当前租户模型配置。
    # Args: store/runner - 报表存储和运行器；identity_loader/access_loader - 测试边界；use_llm - 是否重建租户模型。
    # Returns: 无返回值。
    def __init__(self, store: ReportStore, runner: Any, *, identity_loader: Any = None, access_loader: Any = None, use_llm: bool = True) -> None:
        self.store = store
        self.runner = runner
        self.quota = getattr(runner, "quota", None) or QuotaManager(store=store)
        self.identity_loader = identity_loader or self._load_identity
        self.access_loader = access_loader
        self.use_llm = use_llm
        self._task: asyncio.Task | None = None
        self._stop = asyncio.Event()

    # 方法作用：读取轮询是否存活，供启动检查和 API 排队校验使用。
    # Args: self - 调度服务。
    # Returns: 是否运行中。
    @property
    def running(self) -> bool:
        """不把已失败的后台 Task 视为可用服务。"""
        return self._task is not None and not self._task.done()

    # 方法作用：幂等启动独立轮询任务。
    # Args: self - 调度服务。
    # Returns: 无返回值。
    async def start(self) -> None:
        """每次应用启动都会扫描持久化的到期与中断任务。"""
        if not self.running:
            self._stop.clear()
            self._task = asyncio.create_task(self._loop(), name="report-scheduler")
            logger.info("报表调度服务已启动")

    # 方法作用：取消并等待后台任务退出，运行器负责释放配额。
    # Args: self - 调度服务。
    # Returns: 无返回值。
    async def close(self) -> None:
        """取消传播到数据库和模型任务，不遗留本进程后台执行。"""
        self._stop.set()
        if self._task is not None:
            self._task.cancel()
            await asyncio.gather(self._task, return_exceptions=True)
            self._task = None
        logger.info("报表调度服务已关闭")

    # 方法作用：有界认领并并行处理任务，单个失败不阻断其他任务。
    # Args: self - 调度服务。
    # Returns: 本轮认领数量。
    async def run_once(self) -> int:
        """每个 Worker 最多认领两个任务，为锁连接和业务连接保留池容量。"""
        jobs = await self.store.claim_due_jobs(limit=2)
        await asyncio.gather(*(self.dispatch(job.id, SimpleNamespace(tenant_id=job.tenant_id, user_id=job.user_id, role="analyst")) for job in jobs), return_exceptions=True)
        return len(jobs)

    # 方法作用：在独占锁内重建身份、恢复遗留运行并执行一个任务。
    # Args: job_id - 任务；hint - 仅含租户与拥有者的定位信息；force - 显式手动运行。
    # Returns: 运行或跳过状态。
    async def dispatch(self, job_id: str, hint: Any, *, force: bool = False) -> dict[str, Any]:
        """发布使用同一持锁连接，失联旧 Worker 无法在接管后写入页面。"""
        async with self.store.execution_lock(job_id) as writer:
            if writer is None:
                return {"status": "already_running"}
            job = await self.store.get_job(job_id, hint)
            if job is None:
                return {"status": "not_found"}
            recovery = await self.store.recover_job_runs(job_id, hint, self.quota)
            if recovery["published"] and not force:
                anchored = job.plan.model_copy(update={"run_at": job.plan.run_at or job.created_at})
                upcoming = next_occurrence(anchored, datetime.now(timezone.utc), runs_started=job.runs_started)
                await self.store.update_dispatch(job_id, hint, status="queued" if upcoming else "succeeded", next_run_at=upcoming)
                return {"status": "recovered_published"}
            if job.is_paused:
                await self.store.update_dispatch(job_id, hint, status="queued", next_run_at=job.next_run_at)
                return {"status": "paused"}
            if not force and job.status not in {"queued", "running"}:
                return {"status": "already_finished"}
            now = datetime.now(timezone.utc)
            if job.plan.execution_mode == "scheduled" and (job.plan.max_runs is not None and job.runs_started >= job.plan.max_runs or job.plan.schedule_end_at is not None and job.plan.schedule_end_at < now):
                await self.store.update_dispatch(job_id, hint, status="succeeded")
                return {"status": "schedule_finished"}
            heartbeat: asyncio.Task | None = None
            try:
                identity = await self.identity_loader(job.tenant_id, job.user_id)
                if identity.tenant_id != job.tenant_id or identity.user_id != job.user_id or identity.role == "super_admin":
                    raise PermissionError("后台任务不能替换业务身份")
                permissions = ReportPermissionService(self.store)
                required = "report.schedule" if job.plan.execution_mode == "scheduled" else "report.create"
                if not await permissions.has(identity, required) or job.plan.permanent and not await permissions.has(identity, "report.permanent"):
                    raise PermissionError("任务业务能力已撤销")
                from src.api.routes.report_workbench import _resolve_plan_access
                from src.llm.tenant_config import (
                    resolve_tenant_llm_selection,
                    use_tenant_llm_selection,
                )

                with report_identity_scope(identity):
                    access = await (self.access_loader or _resolve_plan_access)(identity, job.plan)

                    # 方法作用：在发布边界再次验证身份、能力和 ACL，阻止执行期间撤权后的产物。
                    # Args: 无，读取当前任务的固定身份与授权摘要。
                    # Returns: 无返回值，权限变化时抛出异常。
                    async def verify_publication() -> None:
                        current = await self.identity_loader(job.tenant_id, job.user_id)
                        if current.role != identity.role or not await permissions.has(current, required):
                            raise PermissionError("发布前业务身份或能力已变更")
                        latest_access = await (self.access_loader or _resolve_plan_access)(current, job.plan)
                        # 只比对授权语义：数据源摘要里带 updated_at、connected 等易变元数据，
                        # 整包比对会把执行期间正常的元数据抖动误判成“权限已变更”。
                        if _authorization_view(latest_access) != _authorization_view(access):
                            raise PermissionError("发布前数据源权限已变更，请按新权限重跑")
                        if latest_access != access:
                            changed_sources = sorted(
                                name for name in set(access) | set(latest_access)
                                if access.get(name) != latest_access.get(name)
                            )
                            changed_fields = sorted({
                                key
                                for name in set(access) | set(latest_access)
                                for key in set(access.get(name) or {}) | set(latest_access.get(name) or {})
                                if (access.get(name) or {}).get(key) != (latest_access.get(name) or {}).get(key)
                            })
                            logger.warning(
                                "发布前数据源元数据差异探针",
                                changed_datasources=changed_sources,
                                changed_fields=changed_fields,
                            )

                    selection = await resolve_tenant_llm_selection(tenant_id=identity.tenant_id) if self.use_llm else None
                    anchored = job.plan.model_copy(update={"run_at": job.plan.run_at or job.created_at})
                    reserved_next = next_occurrence(anchored, datetime.now(timezone.utc), runs_started=job.runs_started + 1)
                    await self.store.update_dispatch(job_id, identity, status="running", next_run_at=reserved_next)
                    parent = asyncio.current_task()
                    heartbeat = asyncio.create_task(self._heartbeat(job_id, identity, parent))
                    with use_tenant_llm_selection(selection):
                        async with asyncio.timeout(600):
                            result = await self.runner.run_job(job_id, identity, attempt=job.runs_started + 1, authorized_datasources=access, version_store=writer, before_publish=verify_publication)
                updated = await self.store.get_job(job_id, identity)
                if updated is None:
                    raise PermissionError("任务授权已撤销")
                anchored = job.plan.model_copy(update={"run_at": job.plan.run_at or job.created_at})
                upcoming = next_occurrence(anchored, datetime.now(timezone.utc), runs_started=updated.runs_started)
                await self.store.update_dispatch(job_id, identity, status="queued" if upcoming else str(result.get("status", "failed")), next_run_at=upcoming)
                logger.info("报表调度执行完成", job_id=job_id, next_run_at=upcoming, status=result.get("status"))
                return result
            except asyncio.CancelledError:
                await self.store.update_dispatch(job_id, hint, status="queued", next_run_at=datetime.now(timezone.utc))
                raise
            except Exception:
                logger.error("报表调度执行失败并暂停", job_id=job_id, exc_info=True)
                await self.store.set_job_paused(job_id, hint, True)
                await self.store.update_dispatch(job_id, hint, status="failed", next_run_at=job.next_run_at)
                return {"status": "failed"}
            finally:
                if heartbeat is not None:
                    heartbeat.cancel()
                    await asyncio.gather(heartbeat, return_exceptions=True)

    # 方法作用：定期写心跳并复核身份，失联、停用或暂停时取消旧执行。
    # Args: job_id - 任务；identity - 当前身份；parent - 实际运行 Task。
    # Returns: 无返回值。
    async def _heartbeat(self, job_id: str, identity: Any, parent: asyncio.Task | None) -> None:
        try:
            while True:
                await asyncio.sleep(20)
                latest = await self.identity_loader(identity.tenant_id, identity.user_id)
                job = await self.store.get_job(job_id, latest)
                if latest.role != identity.role or job is None or job.is_paused:
                    raise PermissionError("任务拥有者或运行状态发生变更")
                await self.store.update_dispatch(job_id, identity, heartbeat=True)
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.error("报表心跳失败，取消当前运行", job_id=job_id, exc_info=True)
            if parent is not None:
                parent.cancel()

    # 方法作用：从持久化身份表复核租户、用户启用状态及当前角色。
    # Args: tenant_id - 任务租户；user_id - 任务拥有者。
    # Returns: 当前启用身份；不可用时抛出 PermissionError。
    async def _load_identity(self, tenant_id: int, user_id: int) -> Any:
        pool = await self.store._pool()
        async with pool.acquire() as connection:
            row = await connection.fetchrow("SELECT u.id AS user_id,u.tenant_id,u.role,u.username,t.code AS tenant_code FROM users u JOIN tenants t ON t.id=u.tenant_id WHERE u.id=$1 AND u.tenant_id=$2 AND u.is_active AND t.is_active", user_id, tenant_id)
        if row is None:
            raise PermissionError("任务用户或租户已停用")
        return SimpleNamespace(**dict(row))

    # 方法作用：轮询失败后延迟重试，不热循环、不回退到不受控内存执行。
    # Args: self - 调度服务。
    # Returns: 无返回值。
    async def _loop(self) -> None:
        while not self._stop.is_set():
            try:
                await self.run_once()
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.error("报表调度扫描失败", exc_info=True)
            try:
                await asyncio.wait_for(self._stop.wait(), timeout=10)
            except TimeoutError:
                continue
