"""报表工作台菜单能力和周期配额服务。"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any
from zoneinfo import ZoneInfo

from src.logging_config import get_logger

logger = get_logger(__name__)


class QuotaExceededError(RuntimeError):
    """报表资源配额不足。"""


@dataclass(frozen=True, slots=True)
class QuotaPolicy:
    """租户或用户的报表周期限制。"""

    max_jobs_created: int = 20
    max_runs_started: int = 50
    max_concurrent_runs: int = 2
    max_analysis_units_per_run: int = 12
    max_queries_per_run: int = 16
    max_rows_per_query: int = 100_000
    max_llm_tokens: int = 200_000
    max_page_bytes: int = 2_097_152
    max_share_links: int = 20


@dataclass(slots=True)
class QuotaUsage:
    """单个用户当前周期的已用资源。"""

    jobs_created: int = 0
    runs_started: int = 0
    concurrent_runs: int = 0
    analysis_units: int = 0
    queries: int = 0
    rows: int = 0
    llm_tokens: int = 0
    share_links: int = 0


@dataclass(frozen=True, slots=True)
class QuotaSnapshot:
    """预占后返回给 API 的剩余配额摘要。"""

    period_start: datetime
    period_end: datetime
    jobs_remaining: int
    runs_remaining: int
    concurrent_remaining: int
    tokens_remaining: int
    shares_remaining: int


_ROLE_CAPABILITIES: dict[str, frozenset[str]] = {
    "super_admin": frozenset({"report.menu", "report.create", "report.schedule", "report.share", "report.extend", "report.permanent", "report.live_drilldown", "report.view_all", "report.download"}),
    "tenant_admin": frozenset({"report.menu", "report.create", "report.schedule", "report.share", "report.extend", "report.permanent", "report.live_drilldown", "report.view_all", "report.download"}),
    "analyst": frozenset({"report.menu", "report.create"}),
    "viewer": frozenset(),
}


class ReportPermissionService:
    """按角色默认值和管理员用户覆盖项判断报表能力。"""

    # 方法作用：初始化进程内权限覆盖缓存，生产可由 Store 持久化同步。
    # Args: store - 可选报表 Store。
    # Returns: 无返回值。
    def __init__(self, store: Any | None = None) -> None:
        self._store = store
        self._user_capabilities: dict[tuple[int, int], frozenset[str]] = {}

    # 方法作用：判断当前身份是否具备指定报表能力。
    # Args: identity - 当前租户身份；capability - report.* 能力键。
    # Returns: 具备能力返回 True，否则返回 False。
    async def has(self, identity: Any, capability: str) -> bool:
        user_id = int(getattr(identity, "user_id", 0) or 0)
        tenant_id = int(getattr(identity, "tenant_id", 0) or 0)
        role = str(getattr(identity, "role", "") or "")
        override = self._user_capabilities.get((tenant_id, user_id))
        if override is None:
            # 兼容旧测试和未迁移的进程内覆盖项，生产配置优先使用租户键。
            override = self._user_capabilities.get((0, user_id))
        if override is None and self._store is not None and hasattr(self._store, "get_user_capabilities"):
            override = await self._store.get_user_capabilities(tenant_id, user_id)
        allowed = capability in (override if override is not None else _ROLE_CAPABILITIES.get(role, frozenset()))
        logger.debug("报表能力判断完成", user_id=user_id, role=role, capability=capability, allowed=allowed)
        return allowed

    # 方法作用：返回当前身份可见的完整报表能力集合，供菜单和路由守卫使用。
    # Args: identity - 当前租户身份。
    # Returns: 排序后的 report.* 能力列表。
    async def capabilities(self, identity: Any) -> list[str]:
        user_id = int(getattr(identity, "user_id", 0) or 0)
        tenant_id = int(getattr(identity, "tenant_id", 0) or 0)
        role = str(getattr(identity, "role", "") or "")
        override = self._user_capabilities.get((tenant_id, user_id))
        if override is None:
            override = self._user_capabilities.get((0, user_id))
        if override is None and self._store is not None and hasattr(self._store, "get_user_capabilities"):
            override = await self._store.get_user_capabilities(tenant_id, user_id)
        result = sorted(str(item) for item in (override if override is not None else _ROLE_CAPABILITIES.get(role, frozenset())))
        logger.debug("报表能力集合读取完成", user_id=user_id, role=role, capability_count=len(result))
        return result

    # 方法作用：保存管理员配置的用户能力覆盖，供测试和内存降级使用。
    # Args: user_id - 目标用户；capabilities - report.* 能力键列表；tenant_id - 可选租户 ID。
    # Returns: 无返回值。
    def set_user_capabilities(self, user_id: int, capabilities: list[str], tenant_id: int = 0) -> None:
        normalized = frozenset(item.strip() for item in capabilities if item.strip().startswith("report."))
        self._user_capabilities[(int(tenant_id), int(user_id))] = normalized
        logger.info("报表用户能力覆盖已更新", tenant_id=int(tenant_id), user_id=int(user_id), capability_count=len(normalized))


class QuotaManager:
    """按租户、用户和时区进行原子配额预占。"""

    # 方法作用：初始化默认策略、可选持久化 Store 和进程内原子锁。
    # Args: default_policy - 租户默认配额；store - 可选 PG/SQLite Store。
    # Returns: 无返回值。
    def __init__(self, default_policy: QuotaPolicy | None = None, store: Any | None = None) -> None:
        self.default_policy = default_policy or QuotaPolicy()
        self._store = store
        self._policies: dict[tuple[int, int], QuotaPolicy] = {}
        self._usage: dict[tuple[int, int, str], QuotaUsage] = {}
        self._locks: dict[tuple[int, int, str], asyncio.Lock] = {}

    # 方法作用：按租户时区计算当前自然月配额周期。
    # Args: timezone_name - IANA 时区名称；now - 可选当前时间。
    # Returns: 周期起止时间，统一带 UTC 时区。
    @staticmethod
    def period_bounds(timezone_name: str = "Asia/Shanghai", now: datetime | None = None) -> tuple[datetime, datetime]:
        try:
            zone = ZoneInfo(timezone_name)
        except Exception:
            zone = ZoneInfo("Asia/Shanghai")
        local_now = (now or datetime.now(timezone.utc)).astimezone(zone)
        start_local = local_now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
        if start_local.month == 12:
            next_local = start_local.replace(year=start_local.year + 1, month=1)
        else:
            next_local = start_local.replace(month=start_local.month + 1)
        return start_local.astimezone(timezone.utc), next_local.astimezone(timezone.utc)

    # 方法作用：为用户设置周期配额覆盖。
    # Args: tenant_id - 租户 ID；user_id - 用户 ID；policy - 新配额策略。
    # Returns: 无返回值。
    def set_policy(self, tenant_id: int, user_id: int, policy: QuotaPolicy) -> None:
        self._policies[(int(tenant_id), int(user_id))] = policy
        logger.info("报表用户配额策略已更新", tenant_id=int(tenant_id), user_id=int(user_id))

    # 方法作用：原子预占任务、运行、查询和 Token 资源。
    # Args: identity - 当前身份；jobs/runs/concurrent/units/queries/rows/tokens/shares - 本次预占量；timezone_name - 周期时区。
    # Returns: 预占后的 QuotaSnapshot；任一上限不足抛出 QuotaExceededError。
    async def reserve(
        self,
        identity: Any,
        *,
        jobs: int = 0,
        runs: int = 0,
        concurrent: int = 0,
        units: int = 0,
        queries: int = 0,
        rows: int = 0,
        tokens: int = 0,
        shares: int = 0,
        timezone_name: str = "Asia/Shanghai",
    ) -> QuotaSnapshot:
        tenant_id = int(getattr(identity, "tenant_id", 0) or 0)
        user_id = int(getattr(identity, "user_id", 0) or 0)
        period_start, period_end = self.period_bounds(timezone_name)
        key = (tenant_id, user_id, period_start.isoformat())
        if self._store is not None and not bool(getattr(self._store, "_memory", True)) and hasattr(self._store, "reserve_quota"):
            persisted = await self._store.get_quota_policy(tenant_id, user_id) if hasattr(self._store, "get_quota_policy") else None
            policy = self._policy_from_mapping(persisted) if persisted else self._policies.get((tenant_id, user_id), self.default_policy)
            row = await self._store.reserve_quota(
                identity,
                {"jobs": jobs, "runs": runs, "concurrent": concurrent, "units": units, "queries": queries, "rows": rows, "tokens": tokens, "shares": shares},
                policy,
                period_start,
                period_end,
            )
            if row is None:
                raise QuotaExceededError("QUOTA_EXCEEDED: persisted quota")
            return self._snapshot(policy, QuotaUsage(jobs_created=int(row.get("jobs_created", 0)), runs_started=int(row.get("runs_started", 0)), concurrent_runs=int(row.get("concurrent_runs", 0)), analysis_units=int(row.get("analysis_units", 0)), queries=int(row.get("queries", 0)), rows=int(row.get("rows", 0)), llm_tokens=int(row.get("llm_tokens", 0)), share_links=int(row.get("share_links", 0))), period_start, period_end)
        lock = self._locks.setdefault(key, asyncio.Lock())
        async with lock:
            policy = self._policies.get((tenant_id, user_id), self.default_policy)
            usage = self._usage.setdefault(key, QuotaUsage())
            checks = (
                (usage.jobs_created + jobs, policy.max_jobs_created, "jobs_created"),
                (usage.runs_started + runs, policy.max_runs_started, "runs_started"),
                (usage.concurrent_runs + concurrent, policy.max_concurrent_runs, "concurrent_runs"),
                (usage.analysis_units + units, policy.max_analysis_units_per_run, "analysis_units"),
                (usage.queries + queries, policy.max_queries_per_run, "queries"),
                (usage.rows + rows, policy.max_rows_per_query, "rows"),
                (usage.llm_tokens + tokens, policy.max_llm_tokens, "llm_tokens"),
                (usage.share_links + shares, policy.max_share_links, "share_links"),
            )
            for next_value, limit, field_name in checks:
                if next_value > limit:
                    logger.warning("报表配额预占拒绝", tenant_id=tenant_id, user_id=user_id, field=field_name)
                    raise QuotaExceededError(f"QUOTA_EXCEEDED: {field_name}")
            usage.jobs_created += jobs
            usage.runs_started += runs
            usage.concurrent_runs += concurrent
            usage.analysis_units += units
            usage.queries += queries
            usage.rows += rows
            usage.llm_tokens += tokens
            usage.share_links += shares
            return self._snapshot(policy, usage, period_start, period_end)

    # 方法作用：释放运行结束后的并发槽，不回滚已经消耗的次数。
    # Args: identity - 当前身份；amount - 待释放槽位数；timezone_name - 周期时区。
    # Returns: 无返回值。
    async def release_concurrent(self, identity: Any, amount: int = 1, timezone_name: str = "Asia/Shanghai") -> None:
        period_start, _ = self.period_bounds(timezone_name)
        key = (int(getattr(identity, "tenant_id", 0) or 0), int(getattr(identity, "user_id", 0) or 0), period_start.isoformat())
        if self._store is not None and not bool(getattr(self._store, "_memory", True)) and hasattr(self._store, "release_concurrent_quota"):
            await self._store.release_concurrent_quota(identity, amount=amount, timezone_name=timezone_name)
            return
        usage = self._usage.get(key)
        if usage is not None:
            usage.concurrent_runs = max(0, usage.concurrent_runs - max(0, int(amount)))

    # 方法作用：读取当前周期的剩余配额，不产生预占副作用。
    # Args: identity - 当前身份；timezone_name - 周期时区。
    # Returns: 当前 QuotaSnapshot。
    async def snapshot(self, identity: Any, timezone_name: str = "Asia/Shanghai") -> QuotaSnapshot:
        period_start, period_end = self.period_bounds(timezone_name)
        key = (int(getattr(identity, "tenant_id", 0) or 0), int(getattr(identity, "user_id", 0) or 0), period_start.isoformat())
        policy = self._policies.get((key[0], key[1]), self.default_policy)
        return self._snapshot(policy, self._usage.get(key, QuotaUsage()), period_start, period_end)

    # 方法作用：把策略和用量转换为不含敏感明细的剩余摘要。
    # Args: policy - 当前策略；usage - 当前用量；period_start/period_end - 周期边界。
    # Returns: QuotaSnapshot。
    @staticmethod
    def _snapshot(policy: QuotaPolicy, usage: QuotaUsage, period_start: datetime, period_end: datetime) -> QuotaSnapshot:
        return QuotaSnapshot(
            period_start=period_start,
            period_end=period_end,
            jobs_remaining=max(0, policy.max_jobs_created - usage.jobs_created),
            runs_remaining=max(0, policy.max_runs_started - usage.runs_started),
            concurrent_remaining=max(0, policy.max_concurrent_runs - usage.concurrent_runs),
            tokens_remaining=max(0, policy.max_llm_tokens - usage.llm_tokens),
            shares_remaining=max(0, policy.max_share_links - usage.share_links),
        )

    # 方法作用：把数据库配额字段映射为受限 QuotaPolicy。
    # Args: value - 数据库返回的字段字典。
    # Returns: QuotaPolicy；非法字段回退默认值。
    def _policy_from_mapping(self, value: dict[str, Any] | None) -> QuotaPolicy:
        if not value:
            return self.default_policy
        keys = set(QuotaPolicy.__dataclass_fields__)
        return QuotaPolicy(**{key: int(value[key]) for key in keys if key in value})
