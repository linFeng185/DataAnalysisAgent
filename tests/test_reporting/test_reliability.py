"""报表执行和周期配额的边界回归。"""

import asyncio
import sqlite3
from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

from src.logging_config import get_logger
from src.reporting.models import AnalysisUnit, ReportJobCreateRequest, ReportPlan, UnitResult
from src.reporting.quota import QuotaExceededError, QuotaManager
from src.reporting.runner import ReportRunner
from src.reporting.store import ReportStore

logger = get_logger(__name__)


class TestReportReliability:
    """覆盖功能 23.2、23.8、23.10 的幂等、隔离和资源释放。"""

    # 方法作用：验证同一任务重复运行追加同一报表的新版本，而不是另建页面并误选旧版本。
    # Args: self - 测试实例。
    # Returns: 无返回值。
    async def test_repeated_runs_keep_stable_report_identity(self) -> None:
        """最新页面和固定版本分享应具有稳定的任务归属。"""
        store = ReportStore(memory=True)
        identity = SimpleNamespace(tenant_id=3, user_id=7, role="analyst")
        job = await store.create_job(identity, "销售", ReportPlan(analysis_units=[AnalysisUnit(unit_id="sales", question="销售")]))
        executor = AsyncMock(return_value=UnitResult(unit_id="sales", rows=[{"amount": 1}]))
        runner = ReportRunner(store=store, execute_unit=executor)

        first = await runner.run_job(job.id, identity)
        second = await runner.run_job(job.id, identity)
        logger.info("报表版本连续性探针", first_report_id=first["report_id"], second_report_id=second["report_id"], first_version=first["version"], second_version=second["version"])

        assert second["report_id"] == first["report_id"]
        assert second["version"] == 2

    # 方法作用：验证单次上限不会被当成月度累计上限。
    # Args: self - 测试实例。
    # Returns: 无返回值。
    async def test_quota_per_run_limits_do_not_accumulate(self) -> None:
        """连续十三次各一个单元，仍应有三十七次月度额度。"""
        # 准备隔离的身份和月度配额。
        identity = SimpleNamespace(tenant_id=991, user_id=991, role="analyst")
        quota = QuotaManager()
        # 执行跨运行的正常请求。
        for run_number in range(13):
            logger.info("配额回归入口", run_number=run_number, units=1)
            await quota.reserve(identity, runs=1, units=1, queries=1)
        # 验证累计次数和单次越界分别约束。
        assert (await quota.snapshot(identity)).runs_remaining == 37
        with pytest.raises(QuotaExceededError):
            await quota.reserve(identity, units=13)

    # 方法作用：验证负数不能用于返还或绕过配额。
    # Args: self - 测试实例。
    # Returns: 无返回值。
    async def test_quota_rejects_negative_increments(self) -> None:
        """负增量必须被拒绝且不改变余额。"""
        quota = QuotaManager()
        identity = SimpleNamespace(tenant_id=991, user_id=991)
        with pytest.raises(ValueError):
            await quota.reserve(identity, jobs=-1)
        assert (await quota.snapshot(identity)).jobs_remaining == 20

    # 方法作用：验证配额读取使用持久化用量和管理员策略。
    # Args: self - 测试实例。
    # Returns: 无返回值。
    async def test_quota_snapshot_reads_persisted_usage(self) -> None:
        """数据库用量七次、上限二十五次时，剩余额度必须为十八次。"""
        store = SimpleNamespace(
            _memory=False,
            get_quota_policy=AsyncMock(return_value={"max_jobs_created": 25}),
            get_quota_usage=AsyncMock(return_value={"jobs_created": 7}),
        )
        identity = SimpleNamespace(tenant_id=991, user_id=991, role="analyst")
        snapshot = await QuotaManager(store=store).snapshot(identity)
        logger.info("持久化余额回归", jobs_remaining=snapshot.jobs_remaining)
        assert snapshot.jobs_remaining == 18
        store.get_quota_usage.assert_awaited_once()

    # 方法作用：验证重复确认不会重复计费或入队。
    # Args: self - 测试实例；monkeypatch - 补丁 fixture。
    # Returns: 无返回值。
    async def test_create_job_retry_is_idempotent(self, monkeypatch) -> None:
        """相同幂等键并发请求必须只扣一次额度并只入队一次。"""
        import src.api.routes.report_workbench as route
        import src.reporting.runner as runner_module

        identity = SimpleNamespace(tenant_id=991, user_id=991, role="analyst")
        store = ReportStore(memory=True)
        quota = QuotaManager(store=store)
        runner = Mock()
        plan = ReportPlan(analysis_units=[AnalysisUnit(question="销售")])
        monkeypatch.setitem(route._plan_cache, (991, 991, plan.canonical_hash()), plan)
        monkeypatch.setattr(route, "get_current_identity", lambda: identity)
        monkeypatch.setattr(route, "_require_capability", AsyncMock())
        monkeypatch.setattr(route, "_resolve_plan_access", AsyncMock(return_value={}))
        monkeypatch.setattr(route, "get_report_store", lambda: store)
        monkeypatch.setattr(route, "get_report_quota_manager", lambda: quota)
        monkeypatch.setattr(runner_module, "get_report_runner", lambda: runner)
        request = ReportJobCreateRequest(prompt="销售", plan_hash=plan.canonical_hash(), idempotency_key="retry-key")
        first, second = await asyncio.gather(route.create_job(request), route.create_job(request))
        logger.info("幂等回归", enqueue_calls=runner.enqueue.call_count)
        assert first["job_id"] == second["job_id"]
        assert runner.enqueue.call_count == 1
        assert (await quota.snapshot(identity)).jobs_remaining == 19

    # 方法作用：验证持久化权限变化不会被某个 Worker 的旧缓存遮蔽。
    # Args: self - 测试实例。
    # Returns: 无返回值。
    async def test_permission_revocation_overrides_worker_cache(self) -> None:
        """管理员在其他实例撤权后，当前实例不能沿用之前的授权覆盖。"""
        from src.reporting.quota import ReportPermissionService

        store = SimpleNamespace(_memory=False, get_user_capabilities=AsyncMock(return_value=[]))
        permissions = ReportPermissionService(store=store)
        permissions.set_user_capabilities(991, ["report.menu", "report.create"], tenant_id=991)
        identity = SimpleNamespace(tenant_id=991, user_id=991, role="analyst")
        assert not await permissions.has(identity, "report.create")
        assert await permissions.capabilities(identity) == []

    # 方法作用：验证权限库异常时失败关闭，不恢复默认业务授权。
    # Args: self - 测试实例。
    # Returns: 无返回值。
    async def test_permission_storage_failure_fails_closed(self) -> None:
        """权限数据无法读取时应禁止报表操作，而不是默认为 analyst 授权。"""
        connection = SimpleNamespace(fetchval=AsyncMock(side_effect=RuntimeError("权限库不可用")))
        store = ReportStore(pg_pool=connection, memory=False)
        assert await store.get_user_capabilities(991, 991) == []

    # 方法作用：验证取消传播后写回终态并释放并发槽。
    # Args: self - 测试实例。
    # Returns: 无返回值。
    async def test_runner_cancellation_releases_slot(self) -> None:
        """执行中取消不能遗留 running 状态或占用配额。"""
        identity = SimpleNamespace(tenant_id=991, user_id=991, role="analyst")
        store = ReportStore(memory=True)
        quota = QuotaManager(store=store)
        entered = asyncio.Event()

        # 方法作用：模拟可取消的外部请求。
        # Args: unit - 分析单元；state - 图状态。
        # Returns: 测试中只等待取消，不产出结果。
        async def execute(unit, state) -> UnitResult:
            entered.set()
            await asyncio.Event().wait()
            return UnitResult(unit_id=unit.unit_id)

        plan = ReportPlan(analysis_units=[AnalysisUnit(question="销售")])
        job = await store.create_job(identity, "销售", plan)
        task = ReportRunner(store, quota, execute).enqueue(job.id, identity)
        await asyncio.wait_for(entered.wait(), timeout=5)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert (await store.get_job(job.id, identity)).status == "cancelled"
        assert (await store.list_runs(job.id, identity))[0].status == "cancelled"
        assert (await quota.snapshot(identity)).concurrent_remaining == 2

    # 方法作用：验证报表线程使用显式身份与运行标识。
    # Args: self - 测试实例；monkeypatch - 补丁 fixture。
    # Returns: 无返回值。
    async def test_execution_thread_isolates_runs(self, monkeypatch) -> None:
        """相同单元在不同运行中获得不同且租户正确的线程标识。"""
        import src.graph.workflow as workflow

        graph = SimpleNamespace(ainvoke=AsyncMock(return_value={"final_response": {"success": True, "data": []}}))
        monkeypatch.setattr(workflow, "app", graph)
        plan = ReportPlan(requested_datasources=["sales"], analysis_units=[AnalysisUnit(question="销售")])
        runner = ReportRunner(store=ReportStore(memory=True))
        for run_id in ("run-a", "run-b"):
            await runner._default_execute_unit(plan.analysis_units[0], {
                "plan": plan, "run_id": run_id, "job_id": "job-a",
                "identity": {"tenant_id": 991, "user_id": 991, "role": "analyst"},
                "authorized_datasources": {"sales": {}},
            })
        threads = [call.args[1]["configurable"]["thread_id"] for call in graph.ainvoke.await_args_list]
        assert len(set(threads)) == 2
        assert all(thread.startswith("tenant:991:user:991:") for thread in threads)

    # 方法作用：验证预占失败时不能释放其他运行的并发额度。
    # Args: self - 测试实例。
    # Returns: 无返回值。
    async def test_reservation_failure_does_not_release_another_run(self) -> None:
        """预占服务异常不释放未取得的槽位。"""
        identity = SimpleNamespace(tenant_id=991, user_id=991, role="analyst")
        store = ReportStore(memory=True)
        plan = ReportPlan(analysis_units=[AnalysisUnit(question="销售")])
        job = await store.create_job(identity, "销售", plan)
        quota = SimpleNamespace(reserve=AsyncMock(side_effect=RuntimeError("不可用")), release_concurrent=AsyncMock())
        result = await ReportRunner(store, quota).run_job(job.id, identity)
        assert result["status"] == "failed"
        quota.release_concurrent.assert_not_awaited()

    # 方法作用：验证 PostgreSQL 分支生成的运行键满足真实唯一约束。
    # Args: self - 测试实例；monkeypatch - 补丁 fixture。
    # Returns: 无返回值。
    async def test_persistent_manual_runs_have_unique_keys(self, monkeypatch) -> None:
        """同一 Job 连续默认重跑两次不复用 job:attempt 键。"""
        identity = SimpleNamespace(tenant_id=991, user_id=991, role="analyst")
        plan = ReportPlan(analysis_units=[AnalysisUnit(question="销售")])
        job = await ReportStore(memory=True).create_job(identity, "销售", plan)
        database = sqlite3.connect(":memory:")
        database.execute("CREATE TABLE run_keys (run_key TEXT NOT NULL UNIQUE)")

        # 方法作用：让生产分支 INSERT 参数经过真实数据库唯一键约束。
        # Args: sql - 参数化 SQL；values - 绑定值。
        # Returns: Store 需要的最小运行记录。
        async def insert_run(sql, *values):
            logger.info("运行唯一键探针", sql_operation=sql.split()[0], run_key=values[5])
            database.execute("INSERT INTO run_keys VALUES (?)", (values[5],))
            return {"id": values[0], "job_id": values[1], "status": "queued", "attempt": values[4]}

        connection = SimpleNamespace(fetchrow=AsyncMock(side_effect=insert_run), execute=AsyncMock())
        store = ReportStore(pg_pool=connection, memory=False)
        monkeypatch.setattr(store, "get_job", AsyncMock(return_value=job))
        try:
            first = await store.create_run(job.id, identity)
            second = await store.create_run(job.id, identity)
            assert first.id != second.id
            assert database.execute("SELECT COUNT(*) FROM run_keys").fetchone()[0] == 2
        finally:
            database.close()

    # 方法作用：验证跨月结束的运行释放原来预占周期的槽位。
    # Args: self - 测试实例。
    # Returns: 无返回值。
    async def test_release_preserves_original_period(self) -> None:
        """释放请求应携带原周期，不能误减新月份其他运行的额度。"""
        period_start = datetime(2026, 7, 31, 16, tzinfo=timezone.utc)
        store = SimpleNamespace(_memory=False, release_concurrent_quota=AsyncMock())
        identity = SimpleNamespace(tenant_id=991, user_id=991, role="analyst")
        await QuotaManager(store=store).release_concurrent(identity, period_start=period_start)
        assert store.release_concurrent_quota.await_args.kwargs["period_start"] == period_start

    # 方法作用：验证任务创建持久化失败时回滚预占且未留下任务。
    # Args: self - 测试实例；monkeypatch - 补丁 fixture。
    # Returns: 无返回值。
    async def test_create_failure_refunds_uncreated_job(self, monkeypatch) -> None:
        """预占之后创建异常不能消耗用户的任务次数。"""
        identity = SimpleNamespace(tenant_id=991, user_id=991, role="analyst")
        store = ReportStore(memory=True)
        quota = QuotaManager(store=store)
        plan = ReportPlan(analysis_units=[AnalysisUnit(question="销售")])
        monkeypatch.setattr(store, "create_job", AsyncMock(side_effect=RuntimeError("存储失败")))
        with pytest.raises(RuntimeError, match="存储失败"):
            await store.create_job_with_quota(identity, "销售", plan, "request", quota)
        assert (await quota.snapshot(identity)).jobs_remaining == 20
        assert not await store.list_jobs(identity)

    # 方法作用：验证同一幂等键不能被复用于不同执行计划。
    # Args: self - 测试实例。
    # Returns: 无返回值。
    async def test_idempotency_rejects_changed_plan(self) -> None:
        """修改计划后的重复幂等键必须拒绝且不能重复扣额度。"""
        identity = SimpleNamespace(tenant_id=991, user_id=991, role="analyst")
        store = ReportStore(memory=True)
        quota = QuotaManager(store=store)
        first = ReportPlan(analysis_units=[AnalysisUnit(question="销售")])
        second = ReportPlan(analysis_units=[AnalysisUnit(question="库存")])
        await store.create_job_with_quota(identity, "销售", first, "request", quota)
        with pytest.raises(ValueError, match="其他报表计划"):
            await store.create_job_with_quota(identity, "库存", second, "request", quota)
        assert (await quota.snapshot(identity)).jobs_remaining == 19
