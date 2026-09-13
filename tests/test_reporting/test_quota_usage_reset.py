"""报表额度已用量重置测试，覆盖需求 4（D 批：额度重置）。"""

from __future__ import annotations

from datetime import datetime, timezone
from types import SimpleNamespace
from typing import Any

import httpx
import pytest
from fastapi import FastAPI
from pydantic import ValidationError

import src.api.routes.report_workbench as route
from src.logging_config import get_logger
from src.reporting.models import AnalysisUnit, ReportPlan
from src.reporting.quota import QuotaExceededError, QuotaManager, QuotaPolicy
from src.reporting.quota_reset import (
    RESETTABLE_QUOTA_FIELDS,
    QuotaUsageResetRequest,
    literal_reset_fields,
)
from src.reporting.store import ReportStore

logger = get_logger(__name__)
PREFIX = "/report-workbench"
RESET_PATH = f"{PREFIX}/admin/quota-usage/reset"


# 方法作用：构造租户管理员身份。
# Args: tenant_id - 租户 ID；user_id - 管理员用户 ID。
# Returns: 具备 role 的轻量身份对象。
def _admin(tenant_id: int = 3, user_id: int = 9) -> Any:
    return SimpleNamespace(tenant_id=tenant_id, user_id=user_id, role="tenant_admin")


# 方法作用：构造普通租户成员身份。
# Args: tenant_id - 租户 ID；user_id - 成员用户 ID。
# Returns: 具备 role 的轻量身份对象。
def _member(tenant_id: int = 3, user_id: int = 7) -> Any:
    return SimpleNamespace(tenant_id=tenant_id, user_id=user_id, role="analyst")


# 方法作用：把路由模块的全局依赖替换为内存 Store、配额服务与当前身份。
# Args: monkeypatch - pytest 补丁工具；store - 内存 Store；quota - 配额服务；identity - 当前请求身份。
# Returns: 无返回值。
def _bind(monkeypatch, store: ReportStore, quota: QuotaManager, identity: Any) -> None:
    logger.debug("测试路由依赖绑定入口", tenant_id=identity.tenant_id, user_id=identity.user_id, role=identity.role)
    monkeypatch.setattr(route, "get_current_identity", lambda: identity)
    monkeypatch.setattr(route, "get_report_store", lambda: store)
    monkeypatch.setattr(route, "get_report_quota_manager", lambda: quota)


# 方法作用：构造只挂载报表工作台路由的测试客户端。
# Args: 无。
# Returns: httpx 异步客户端。
def _client() -> httpx.AsyncClient:
    app = FastAPI()
    app.include_router(route.router)
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test")


# 方法作用：为指定身份在内存 Store 中登记一个报表任务，使该用户进入本租户用户清单。
# Args: store - 内存 Store；identity - 任务归属身份。
# Returns: 无返回值。
async def _seed_job(store: ReportStore, identity: Any) -> None:
    plan = ReportPlan(analysis_units=[AnalysisUnit(question="销量")])
    job = await store.create_job(identity, "季度销售分析", plan)
    logger.debug("测试任务登记完成", job_id=job.id, tenant_id=identity.tenant_id, user_id=identity.user_id)


# 方法作用：构造固定返回行的持久化连接与连接池替身，用于验证 SQL 与参数。
# Args: rows - fetch 返回的行列表。
# Returns: (连接替身, 连接池替身)。
def _persistent_pool(rows: list[dict[str, Any]] | None = None) -> tuple[Any, Any]:
    class _FakeTransaction:
        # 方法作用：进入测试事务上下文。
        # Args: self - 事务替身。
        # Returns: 事务自身。
        async def __aenter__(self):
            return self

        # 方法作用：退出测试事务上下文。
        # Args: self - 事务替身；exc_type/exc/value - 异常信息。
        # Returns: False 让异常继续传播。
        async def __aexit__(self, exc_type, exc, value):
            return False

    class _FakeConnection:
        """记录 SQL 与参数的连接替身，不访问真实数据库。"""

        # 方法作用：初始化记录容器与固定返回行。
        # Args: self - 连接替身。
        # Returns: 无返回值。
        def __init__(self) -> None:
            self.rows = list(rows or [])
            self.queries: list[tuple[str, tuple[Any, ...]]] = []

        # 方法作用：记录执行语句（RLS 身份设置与 UPDATE 共用）。
        # Args: self - 连接替身；query - SQL；args - 参数。
        # Returns: 固定字符串结果。
        async def execute(self, query: str, *args: Any) -> str:
            self.queries.append((str(query), tuple(args)))
            return "OK"

        # 方法作用：记录查询语句并返回预设行。
        # Args: self - 连接替身；query - SQL；args - 参数。
        # Returns: 预设行列表。
        async def fetch(self, query: str, *args: Any) -> list[dict[str, Any]]:
            self.queries.append((str(query), tuple(args)))
            return [dict(row) for row in self.rows]

        # 方法作用：提供事务上下文。
        # Args: self - 连接替身。
        # Returns: 测试事务替身。
        def transaction(self) -> Any:
            return _FakeTransaction()

        # 方法作用：返回记录到的全部语句，供断言使用。
        # Args: self - 连接替身。
        # Returns: (SQL, 参数) 列表。
        def statements(self) -> list[tuple[str, tuple[Any, ...]]]:
            return self.queries

    class _FakeAcquire:
        # 方法作用：初始化连接借出上下文。
        # Args: connection - 连接替身。
        # Returns: 无返回值。
        def __init__(self, connection: Any) -> None:
            self.connection = connection

        # 方法作用：进入连接池上下文。
        # Args: self - 借出上下文。
        # Returns: 连接替身。
        async def __aenter__(self):
            return self.connection

        # 方法作用：退出连接池上下文。
        # Args: self - 借出上下文；exc_type/exc/value - 异常信息。
        # Returns: False 让异常继续传播。
        async def __aexit__(self, exc_type, exc, value):
            return False

    class _FakePool:
        # 方法作用：初始化带单连接的连接池替身。
        # Args: self - 连接池替身。
        # Returns: 无返回值。
        def __init__(self) -> None:
            self.connection = _FakeConnection()

        # 方法作用：借出测试连接。
        # Args: self - 连接池替身。
        # Returns: 借出上下文。
        def acquire(self) -> Any:
            return _FakeAcquire(self.connection)

    pool = _FakePool()
    return pool.connection, pool


class TestQuotaUsageResetService:
    """覆盖额度重置的服务语义：清零已用量、不动上限、只清选中字段。"""

    # 方法作用：验证清零后剩余额度恢复且额度上限保持不变。
    # Args: self - 测试实例。
    # Returns: 无返回值，断言失败时由 pytest 报告。
    async def test_reset_usage_raises_remaining_and_keeps_policy(self) -> None:
        """重置只清用量：snapshot 剩余额度变大，policy 上限原样保留。"""
        # Arrange
        store = ReportStore(memory=True)
        policy = QuotaPolicy(max_runs_started=5, max_llm_tokens=1000)
        quota = QuotaManager(store=store, default_policy=QuotaPolicy(max_runs_started=2, max_llm_tokens=1000))
        quota.set_policy(3, 7, policy)
        await store.set_quota_policy(3, 7, policy)
        member = _member()
        before = await quota.reserve(member, runs=3, tokens=400)
        policy_before = dict(store._quota_policies[(3, 7)])

        # Act
        period_start, period_end, cleared = await quota.reset_usage(_admin(), [7], ["runs_started"])

        # Assert
        assert before.runs_remaining == 2
        assert cleared == [7]
        after = await quota.snapshot(member)
        assert after.runs_remaining == 5
        assert after.tokens_remaining == 600
        assert store._quota_policies[(3, 7)] == policy_before
        assert quota._policies[(3, 7)].max_runs_started == 5
        assert quota.period_bounds("Asia/Shanghai") == (period_start, period_end)

    # 方法作用：验证多字段多选时只清选中字段。
    # Args: self - 测试实例。
    # Returns: 无返回值，断言失败时由 pytest 报告。
    async def test_reset_usage_only_zeroes_selected_fields(self) -> None:
        """未选中的已用量必须原样保留，这是本需求最容易写错的地方。"""
        # Arrange
        quota = QuotaManager(default_policy=QuotaPolicy(max_jobs_created=10, max_runs_started=10, max_llm_tokens=10_000, max_share_links=10))
        member = _member()
        await quota.reserve(member, jobs=2, runs=3, tokens=400, shares=4)
        key = next(iter(quota._usage))

        # Act
        _, _, cleared = await quota.reset_usage(_admin(), [7], ["runs_started", "share_links"])

        # Assert
        usage = quota._usage[key]
        assert cleared == [7]
        assert (usage.runs_started, usage.share_links) == (0, 0)
        assert (usage.jobs_created, usage.llm_tokens) == (2, 400)
        after = await quota.snapshot(member)
        assert after.jobs_remaining == 8
        assert after.tokens_remaining == 9_600

    # 方法作用：验证重置后下一次预占立即放行。
    # Args: self - 测试实例。
    # Returns: 无返回值，断言失败时由 pytest 报告。
    async def test_reset_usage_enables_next_reservation(self) -> None:
        """被拒的预占在重置对应字段后必须能通过。"""
        # Arrange
        quota = QuotaManager(default_policy=QuotaPolicy(max_jobs_created=1))
        member = _member()
        await quota.reserve(member, jobs=1)
        with pytest.raises(QuotaExceededError):
            await quota.reserve(member, jobs=1)

        # Act
        await quota.reset_usage(_admin(), [7], ["jobs_created"])
        retried = await quota.reserve(member, jobs=1)

        # Assert
        assert retried.jobs_remaining == 0

    # 方法作用：验证本期没有用量行的用户不计入实际清零人数。
    # Args: self - 测试实例。
    # Returns: 无返回值，断言失败时由 pytest 报告。
    async def test_reset_usage_skips_users_without_usage(self) -> None:
        """没有用量缓存的用户既不计数也不报错。"""
        # Arrange
        quota = QuotaManager()
        await quota.reserve(_member(), runs=1)

        # Act
        _, _, cleared = await quota.reset_usage(_admin(), [7, 8, 99], ["runs_started"])

        # Assert
        assert cleared == [7]

    # 方法作用：验证 all_users 只覆盖本租户用量，跨租户用量不受影响。
    # Args: self - 测试实例。
    # Returns: 无返回值，断言失败时由 pytest 报告。
    async def test_reset_usage_all_users_is_tenant_scoped(self) -> None:
        """全租户范围以租户键过滤，不能清掉其他租户的用量。"""
        # Arrange
        quota = QuotaManager()
        other_tenant = _member(tenant_id=9, user_id=8)
        await quota.reserve(_member(user_id=7), runs=1)
        await quota.reserve(_member(user_id=8), runs=1)
        await quota.reserve(other_tenant, runs=1)

        # Act
        _, _, cleared = await quota.reset_usage(_admin(), [], ["runs_started"], all_users=True)

        # Assert
        assert cleared == [7, 8]
        assert (await quota.snapshot(other_tenant)).runs_remaining == quota.default_policy.max_runs_started - 1

    # 方法作用：验证重复用户 ID 只清零一次。
    # Args: self - 测试实例。
    # Returns: 无返回值，断言失败时由 pytest 报告。
    async def test_reset_usage_counts_duplicate_user_once(self) -> None:
        """重复目标不应产生重复计数或重复审计。"""
        # Arrange
        quota = QuotaManager()
        await quota.reserve(_member(user_id=7), runs=1)

        # Act
        _, _, cleared = await quota.reset_usage(_admin(), [7, 7, 7], ["runs_started"])

        # Assert
        assert cleared == [7]

    # 方法作用：验证未知字段被白名单拒绝。
    # Args: self - 测试实例。
    # Returns: 无返回值，断言失败时由 pytest 报告。
    async def test_reset_usage_rejects_unknown_field(self) -> None:
        """concurrent_runs 等实时或单次上限字段不可重置。"""
        # Arrange
        quota = QuotaManager()

        # Act / Assert
        with pytest.raises(ValueError, match="不支持重置"):
            await quota.reset_usage(_admin(), [7], ["concurrent_runs"])

    # 方法作用：验证四个字段可以一次全部清零。
    # Args: self - 测试实例。
    # Returns: 无返回值，断言失败时由 pytest 报告。
    async def test_reset_usage_accepts_all_four_fields(self) -> None:
        """边界：fields 传全部四项时四个累计字段一起归零。"""
        # Arrange
        quota = QuotaManager(default_policy=QuotaPolicy(max_jobs_created=10, max_runs_started=10, max_llm_tokens=10_000, max_share_links=10))
        member = _member()
        await quota.reserve(member, jobs=1, runs=1, tokens=100, shares=1)
        key = next(iter(quota._usage))

        # Act
        _, _, cleared = await quota.reset_usage(_admin(), [7], list(RESETTABLE_QUOTA_FIELDS))

        # Assert
        usage = quota._usage[key]
        assert cleared == [7]
        assert (usage.jobs_created, usage.runs_started, usage.llm_tokens, usage.share_links) == (0, 0, 0, 0)


class TestQuotaUsageResetStore:
    """覆盖持久化 Store 的两条分支：PG 白名单 UPDATE 与内存分支约定。"""

    # 方法作用：验证持久化分支只按白名单列拼 UPDATE 并透传参数。
    # Args: self - 测试实例。
    # Returns: 无返回值，断言失败时由 pytest 报告。
    async def test_store_pg_branch_updates_whitelisted_columns(self) -> None:
        """SET 子句只出现选中列，WHERE 参数与返回行严格对应。"""
        # Arrange
        connection, pool = _persistent_pool(rows=[{"user_id": 7}, {"user_id": 8}])
        store = ReportStore(pg_pool=pool, dialect="postgres", memory=False)
        period_start = QuotaManager.period_bounds("Asia/Shanghai")[0]

        # Act
        cleared = await store.reset_quota_usage(_admin(), [7, 8], ["llm_tokens", "runs_started"], period_start)

        # Assert
        updates = [item for item in connection.statements() if item[0].startswith("UPDATE report_quota_usage")]
        assert len(updates) == 1
        sql, args = updates[0]
        assert sql == (
            "UPDATE report_quota_usage SET llm_tokens=0, runs_started=0 "
            "WHERE tenant_id=$1 AND user_id = ANY($2) AND period_start=$3 RETURNING user_id"
        )
        assert args == (3, [7, 8], period_start)
        assert cleared == [7, 8]

    # 方法作用：验证请求里的字段名不会拼进 SQL。
    # Args: self - 测试实例。
    # Returns: 无返回值，断言失败时由 pytest 报告。
    async def test_store_pg_branch_rejects_injected_field_name(self) -> None:
        """恶意字段名在拼接前就被白名单拦下，数据库一条语句都收不到。"""
        # Arrange
        connection, pool = _persistent_pool(rows=[{"user_id": 7}])
        store = ReportStore(pg_pool=pool, dialect="postgres", memory=False)
        period_start = QuotaManager.period_bounds("Asia/Shanghai")[0]

        # Act / Assert
        with pytest.raises(ValueError, match="不支持重置"):
            await store.reset_quota_usage(_admin(), [7], ["runs_started=0; DROP TABLE users"], period_start)
        assert [item for item in connection.statements() if "UPDATE" in item[0]] == []

    # 方法作用：验证内存分支没有用量行时返回空列表而不是报错。
    # Args: self - 测试实例。
    # Returns: 无返回值，断言失败时由 pytest 报告。
    async def test_store_memory_branch_has_no_usage_rows(self) -> None:
        """内存模式的用量由 QuotaManager 缓存持有，Store 侧清零交给配额服务。"""
        # Arrange
        store = ReportStore(memory=True)
        period_start = QuotaManager.period_bounds("Asia/Shanghai")[0]

        # Act
        cleared = await store.reset_quota_usage(_admin(), [7], ["runs_started"], period_start)

        # Assert
        assert cleared == []

    # 方法作用：验证内存 Store 的本租户用户清单按租户过滤。
    # Args: self - 测试实例。
    # Returns: 无返回值，断言失败时由 pytest 报告。
    async def test_store_memory_user_list_filters_tenant(self) -> None:
        """任务、策略覆盖与服务过的分享用户都算本租户用户，其他租户不算。"""
        # Arrange
        store = ReportStore(memory=True)
        await _seed_job(store, _member(user_id=7))
        await _seed_job(store, _member(tenant_id=9, user_id=8))
        await store.set_quota_policy(3, 12, QuotaPolicy())

        # Act
        users = await store.list_tenant_user_ids(_admin())

        # Assert
        assert users == [7, 12]

    # 方法作用：验证持久化 Store 的用户清单只查当前租户。
    # Args: self - 测试实例。
    # Returns: 无返回值，断言失败时由 pytest 报告。
    async def test_store_persistent_user_list_is_tenant_scoped(self) -> None:
        """用户清单回源 users 表，SQL 必须带 tenant_id 条件。"""
        # Arrange
        connection, pool = _persistent_pool(rows=[{"id": 8}, {"id": 7}, {"id": 7}])
        store = ReportStore(pg_pool=pool, dialect="postgres", memory=False)

        # Act
        users = await store.list_tenant_user_ids(_admin())

        # Assert
        selects = [item for item in connection.statements() if item[0].startswith("SELECT id FROM users")]
        assert len(selects) == 1
        assert selects[0][1] == (3,)
        assert users == [7, 8]


class TestQuotaUsageResetApi:
    """覆盖冻结的 HTTP 契约：请求校验 422、权限 403、响应字段与审计。"""

    # 方法作用：验证正常请求返回冻结契约的响应结构。
    # Args: self - 测试实例；monkeypatch - 补丁工具。
    # Returns: 无返回值，断言失败时由 pytest 报告。
    async def test_api_reset_returns_frozen_contract(self, monkeypatch) -> None:
        """响应给出实际清零人数、字段与按租户时区算出的周期边界。"""
        # Arrange
        store = ReportStore(memory=True)
        quota = QuotaManager(store=store, default_policy=QuotaPolicy(max_runs_started=10, max_llm_tokens=10_000))
        for user_id in (7, 8):
            await _seed_job(store, _member(user_id=user_id))
            await quota.reserve(_member(user_id=user_id), runs=2, tokens=100)
        admin = _admin()
        _bind(monkeypatch, store, quota, admin)

        # Act
        async with _client() as client:
            response = await client.post(RESET_PATH, json={"user_ids": [7, 8], "all_users": False, "fields": ["runs_started", "llm_tokens"]})

        # Assert
        assert response.status_code == 200
        body = response.json()
        assert body["reset_users"] == 2
        assert body["fields"] == ["runs_started", "llm_tokens"]
        expected_start, expected_end = QuotaManager.period_bounds("Asia/Shanghai")
        assert datetime.fromisoformat(body["period_start"]) == expected_start
        assert datetime.fromisoformat(body["period_end"]) == expected_end
        restored = await quota.snapshot(_member(user_id=7))
        assert (restored.runs_remaining, restored.tokens_remaining) == (10, 10_000)

    # 方法作用：验证范围互斥与必选在契约层返回 422。
    # Args: self - 测试实例；monkeypatch - 补丁工具；payload - 非法请求体。
    # Returns: 无返回值，断言失败时由 pytest 报告。
    @pytest.mark.parametrize(
        "payload",
        [
            {"user_ids": [7], "all_users": True, "fields": ["runs_started"]},
            {"fields": ["runs_started"]},
            {"user_ids": [], "fields": ["runs_started"]},
            {"user_ids": [], "all_users": False, "fields": ["runs_started"]},
        ],
    )
    async def test_api_reset_rejects_invalid_scope(self, monkeypatch, payload: dict[str, Any]) -> None:
        """两者同时出现、都不提供或 user_ids 为空都是 422。"""
        # Arrange
        store = ReportStore(memory=True)
        _bind(monkeypatch, store, QuotaManager(store=store), _admin())

        # Act
        async with _client() as client:
            response = await client.post(RESET_PATH, json=payload)

        # Assert
        assert response.status_code == 422

    # 方法作用：验证字段取值与空数组在契约层返回 422。
    # Args: self - 测试实例；monkeypatch - 补丁工具；fields - 非法字段列表。
    # Returns: 无返回值，断言失败时由 pytest 报告。
    @pytest.mark.parametrize("fields", [[], ["queries"], ["concurrent_runs"], ["runs_started", "token_budget"]])
    async def test_api_reset_rejects_invalid_fields(self, monkeypatch, fields: list[str]) -> None:
        """空数组、单次上限字段和未知字段一律 422。"""
        # Arrange
        store = ReportStore(memory=True)
        _bind(monkeypatch, store, QuotaManager(store=store), _admin())

        # Act
        async with _client() as client:
            response = await client.post(RESET_PATH, json={"user_ids": [7], "fields": fields})

        # Assert
        assert response.status_code == 422

    # 方法作用：验证非正整数用户 ID 返回 422。
    # Args: self - 测试实例；monkeypatch - 补丁工具；user_ids - 非法用户列表。
    # Returns: 无返回值，断言失败时由 pytest 报告。
    @pytest.mark.parametrize("user_ids", [[0], [-3], [7, 0], ["abc"]])
    async def test_api_reset_rejects_invalid_user_ids(self, monkeypatch, user_ids: list[Any]) -> None:
        """0、负数与非整数目标都不能进入处理链路。"""
        # Arrange
        store = ReportStore(memory=True)
        _bind(monkeypatch, store, QuotaManager(store=store), _admin())

        # Act
        async with _client() as client:
            response = await client.post(RESET_PATH, json={"user_ids": user_ids, "fields": ["runs_started"]})

        # Assert
        assert response.status_code == 422

    # 方法作用：验证非租户管理员与无租户身份返回 403。
    # Args: self - 测试实例；monkeypatch - 补丁工具；identity - 非法身份。
    # Returns: 无返回值，断言失败时由 pytest 报告。
    @pytest.mark.parametrize(
        "identity",
        [
            SimpleNamespace(tenant_id=3, user_id=7, role="analyst"),
            SimpleNamespace(tenant_id=3, user_id=7, role="viewer"),
            SimpleNamespace(tenant_id=0, user_id=1, role="super_admin"),
            SimpleNamespace(tenant_id=0, user_id=7, role="tenant_admin"),
        ],
    )
    async def test_api_reset_requires_tenant_admin(self, monkeypatch, identity: Any) -> None:
        """业务额度只由当前租户管理员重置。"""
        # Arrange
        store = ReportStore(memory=True)
        _bind(monkeypatch, store, QuotaManager(store=store), identity)

        # Act
        async with _client() as client:
            response = await client.post(RESET_PATH, json={"user_ids": [7], "fields": ["runs_started"]})

        # Assert
        assert response.status_code == 403

    # 方法作用：验证跨租户与无用量目标被跳过，且每个被清零用户各写一条审计。
    # Args: self - 测试实例；monkeypatch - 补丁工具。
    # Returns: 无返回值，断言失败时由 pytest 报告。
    async def test_api_reset_skips_foreign_targets_and_audits_each_cleared_user(self, monkeypatch) -> None:
        """reset_users 只算真实清零人数，审计按人写入且带字段。"""
        # Arrange
        store = ReportStore(memory=True)
        quota = QuotaManager(store=store)
        for user_id in (7, 8):
            await _seed_job(store, _member(user_id=user_id))
            await quota.reserve(_member(user_id=user_id), runs=1)
        await quota.reserve(_member(tenant_id=9, user_id=8), runs=1)
        admin = _admin()
        _bind(monkeypatch, store, quota, admin)

        # Act
        async with _client() as client:
            response = await client.post(RESET_PATH, json={"user_ids": [7, 8, 99], "fields": ["runs_started"]})

        # Assert
        assert response.status_code == 200
        assert response.json()["reset_users"] == 2
        # 跨租户用户 8（tenant 9）的用量必须原样保留
        assert (await quota.snapshot(_member(tenant_id=9, user_id=8))).runs_remaining == quota.default_policy.max_runs_started - 1
        audits = [item for item in store._audits if item.get("action") == "quota_usage_reset"]
        assert len(audits) == 2

    # 方法作用：验证重置不影响下个月周期，也不清其他租户用户。
    # Args: self - 测试实例；monkeypatch - 补丁工具。
    # Returns: 无返回值，断言失败时由 pytest 报告。
    async def test_api_reset_covers_all_users_of_tenant(self, monkeypatch) -> None:
        """all_users 覆盖本租户全部有用量用户，重复目标只算一次。"""
        # Arrange
        store = ReportStore(memory=True)
        quota = QuotaManager(store=store)
        for user_id in (7, 8):
            await _seed_job(store, _member(user_id=user_id))
            await quota.reserve(_member(user_id=user_id), runs=1)
        await quota.reserve(_member(tenant_id=9, user_id=8), runs=1)
        admin = _admin()
        _bind(monkeypatch, store, quota, admin)

        # Act
        async with _client() as client:
            duplicates = await client.post(RESET_PATH, json={"user_ids": [7, 7], "fields": ["runs_started"]})
            all_users = await client.post(RESET_PATH, json={"all_users": True, "fields": ["runs_started"]})

        # Assert
        assert duplicates.status_code == 200
        assert duplicates.json()["reset_users"] == 1
        assert all_users.status_code == 200
        assert all_users.json()["reset_users"] == 2
        # 全租户范围不能波及 tenant 9 的用量
        assert (await quota.snapshot(_member(tenant_id=9, user_id=8))).runs_remaining == quota.default_policy.max_runs_started - 1

    # 方法作用：验证审计按每个被清零用户各写一条并带字段元数据。
    # Args: self - 测试实例；monkeypatch - 补丁工具。
    # Returns: 无返回值，断言失败时由 pytest 报告。
    async def test_api_reset_writes_one_audit_per_cleared_user(self, monkeypatch) -> None:
        """审计记录操作者、目标用户与被清零字段。"""
        # Arrange
        store = ReportStore(memory=True)
        quota = QuotaManager(store=store)
        for user_id in (7, 8):
            await _seed_job(store, _member(user_id=user_id))
            await quota.reserve(_member(user_id=user_id), jobs=1, runs=1, tokens=10, shares=1)
        admin = _admin()
        _bind(monkeypatch, store, quota, admin)

        # Act
        async with _client() as client:
            response = await client.post(RESET_PATH, json={"user_ids": [7, 8, 99], "fields": ["runs_started", "llm_tokens"]})

        # Assert
        assert response.status_code == 200
        audits = [item for item in store._audits if item.get("action") == "quota_usage_reset"]
        assert len(audits) == 2
        assert {item["metadata"]["target_user_id"] for item in audits} == {7, 8}
        assert all(item["metadata"]["fields"] == ["runs_started", "llm_tokens"] for item in audits)
        assert all(item["tenant_id"] == 3 and item["user_id"] == 9 for item in audits)


class TestQuotaResetFieldContract:
    """覆盖字段白名单与前端冻结取值的一致性。"""

    # 方法作用：验证 Literal 类型与白名单元组同源且不含不可重置字段。
    # Args: self - 测试实例。
    # Returns: 无返回值，断言失败时由 pytest 报告。
    def test_literal_fields_match_tuple(self) -> None:
        """两处字段清单脱钩会让请求被 422 或被误清，必须同源。"""
        # Arrange / Act
        literal_fields = literal_reset_fields()

        # Assert
        assert literal_fields == set(RESETTABLE_QUOTA_FIELDS)
        assert literal_fields == {"jobs_created", "runs_started", "llm_tokens", "share_links"}
        assert not literal_fields & {"concurrent_runs", "analysis_units", "queries", "rows"}

    # 方法作用：验证请求模型对重复目标与重复字段只保留一次。
    # Args: self - 测试实例。
    # Returns: 无返回值，断言失败时由 pytest 报告。
    def test_request_normalizes_duplicates(self) -> None:
        """重复目标与重复字段在契约层归一，避免重复审计。"""
        # Arrange / Act
        request = QuotaUsageResetRequest(user_ids=[7, 7, 8], fields=["runs_started", "runs_started"])

        # Assert
        assert request.user_ids == [7, 8]
        assert request.fields == ["runs_started"]

    # 方法作用：验证互斥范围在模型层直接抛出校验错误。
    # Args: self - 测试实例；payload - 非法请求体。
    # Returns: 无返回值，断言失败时由 pytest 报告。
    @pytest.mark.parametrize(
        "payload",
        [
            {"user_ids": [7], "all_users": True, "fields": ["runs_started"]},
            {"all_users": False, "fields": ["runs_started"]},
            {"user_ids": [], "fields": ["runs_started"]},
        ],
    )
    def test_request_rejects_conflicting_scope(self, payload: dict[str, Any]) -> None:
        """范围必须且只能提供一项。"""
        # Arrange / Act / Assert
        with pytest.raises(ValidationError):
            QuotaUsageResetRequest(**payload)

    # 方法作用：验证周期边界是当前自然月且带时区。
    # Args: self - 测试实例。
    # Returns: 无返回值，断言失败时由 pytest 报告。
    def test_period_bounds_are_current_calendar_month(self) -> None:
        """契约里的 period_start/period_end 就是 period_bounds 的结果。"""
        # Arrange / Act
        period_start, period_end = QuotaManager.period_bounds("Asia/Shanghai")
        now = datetime.now(timezone.utc)

        # Assert
        assert period_start <= now < period_end
        assert period_start.utcoffset() is not None
        assert (period_end - period_start).days in (28, 29, 30, 31)
