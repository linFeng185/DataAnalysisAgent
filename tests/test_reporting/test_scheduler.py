"""报表持久化调度测试，覆盖 23.11。"""

from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from src.reporting.models import AnalysisUnit, ReportPlan
from src.reporting.store import ReportStore


class TestReportScheduler:
    """覆盖 23.11 的时区、次数、原子认领、暂停与恢复。"""

    # 方法作用：构造固定起点和频率的调度计划。
    # Args: self - 测试实例；frequency - 调度频率。
    # Returns: 可执行计划。
    def plan(self, frequency: str = "daily") -> ReportPlan:
        """使用过去的首次时间便于确定性认领。"""
        return ReportPlan(execution_mode="scheduled", frequency=frequency, max_runs=2, run_at=datetime(2026, 1, 31, 9, tzinfo=timezone.utc), analysis_units=[AnalysisUnit(unit_id="sales", question="销售")])

    # 方法作用：验证月末推进与结束边界，不把月份视为三十天。
    # Args: self - 测试实例。
    # Returns: 无返回值。
    def test_calendar_schedule_and_run_bounds(self) -> None:
        """月末保持锚点日，短月夹到月末；次数耗尽不再推进。"""
        from src.reporting.scheduler import next_occurrence

        plan = self.plan("monthly")
        after = datetime(2026, 2, 1, tzinfo=timezone.utc)

        assert next_occurrence(plan, after, runs_started=1) == datetime(2026, 2, 28, 9, tzinfo=timezone.utc)
        assert next_occurrence(plan, after, runs_started=2) is None
        plan.schedule_end_at = datetime(2026, 2, 20, tzinfo=timezone.utc)
        assert next_occurrence(plan, after, runs_started=1) is None

    # 方法作用：验证夏令时缺失时刻归一化且每日本地时刻不漂移。
    # Args: self - 测试实例。
    # Returns: 无返回值。
    def test_calendar_dst_and_invalid_timezone(self) -> None:
        """夏令时切换后仍按本地九点执行，而不是固定二十四小时。"""
        from src.reporting.scheduler import next_occurrence

        plan = self.plan()
        plan.timezone = "America/New_York"
        plan.run_at = datetime(2026, 3, 7, 14, tzinfo=timezone.utc)

        assert next_occurrence(plan, datetime(2026, 3, 8, 0, tzinfo=timezone.utc), runs_started=1) == datetime(2026, 3, 8, 13, tzinfo=timezone.utc)
        with pytest.raises(ValueError):
            ReportPlan(**{**plan.model_dump(), "timezone": "Mars/Olympus"})

    # 方法作用：验证自然语言中的星期、小时和月末日不会被频率解析吞掉。
    # Args: self - 测试实例。
    # Returns: 无返回值。
    def test_schedule_clock_and_month_end_are_explicit(self) -> None:
        """九月六日周日预览下周一九点，月末三十一日保留原始锚点。"""
        from src.reporting.planner import ReportPlanner
        from src.reporting.scheduler import next_occurrence

        now = datetime(2026, 9, 6, 8, tzinfo=timezone.utc)
        weekly, _ = ReportPlanner.parse_schedule_start("每周一上午9点生成销售报表", "weekly", "Asia/Shanghai", now=now)
        monthly, month_day = ReportPlanner.parse_schedule_start("每月31日9:30生成销售报表", "monthly", "Asia/Shanghai", now=now)

        assert weekly == datetime(2026, 9, 7, 1, tzinfo=timezone.utc)
        assert monthly == datetime(2026, 9, 30, 1, 30, tzinfo=timezone.utc)
        assert month_day == 31
        plan = self.plan("monthly").model_copy(update={"run_at": monthly, "schedule_day": month_day})
        assert next_occurrence(plan, monthly, runs_started=1) == datetime(2026, 10, 31, 1, 30, tzinfo=timezone.utc)

    # 方法作用：验证同一任务不会被同一轮或第二个 Worker 重复认领。
    # Args: self - 测试实例。
    # Returns: 无返回值。
    async def test_scheduled_creation_is_persistent_and_claimed_once(self) -> None:
        """创建后的首期时间持久化，第二次认领不会重复返回同一任务。"""
        store = ReportStore(memory=True)
        identity = SimpleNamespace(tenant_id=3, user_id=7, role="tenant_admin")
        job = await store.create_job(identity, "每日销售", self.plan())

        first = await store.claim_due_jobs()
        second = await store.claim_due_jobs()

        assert job.next_run_at is not None
        assert [item.id for item in first] == [job.id]
        assert second == []

    # 方法作用：验证暂停任务不被认领，恢复仍受次数与结束时间限制。
    # Args: self - 测试实例。
    # Returns: 无返回值。
    async def test_pause_resume_preserves_schedule(self) -> None:
        """暂停期间不认领，恢复后保持原有计划与次数。"""
        store = ReportStore(memory=True)
        identity = SimpleNamespace(tenant_id=3, user_id=7, role="tenant_admin")
        job = await store.create_job(identity, "每日销售", self.plan())

        assert await store.set_job_paused(job.id, identity, True)
        assert await store.claim_due_jobs() == []
        assert await store.set_job_paused(job.id, identity, False)
        assert len(await store.claim_due_jobs()) == 1

    # 方法作用：验证调度成功后推进下次运行，达到次数后自动结束。
    # Args: self - 测试实例。
    # Returns: 无返回值。
    async def test_scheduler_advances_and_stops(self) -> None:
        """成功运行推进下一期，达到 max_runs 后清空调度时间。"""
        from src.reporting.scheduler import ReportScheduler

        store = ReportStore(memory=True)
        identity = SimpleNamespace(tenant_id=3, user_id=7, role="tenant_admin")
        job = await store.create_job(identity, "每日销售", self.plan())

        # 方法作用：模拟只读业务执行与运行计数，不调用真实模型。
        # Args: job_id - 当前任务；owner - 重建身份；kwargs - 调度上下文。
        # Returns: 成功状态。
        async def execute(job_id: str, owner: object, **kwargs: object) -> dict:
            await store.create_run(job_id, owner)
            return {"status": "succeeded"}

        scheduler = ReportScheduler(store, SimpleNamespace(run_job=execute), identity_loader=AsyncMock(return_value=identity), access_loader=AsyncMock(return_value={}), use_llm=False)
        await scheduler.run_once()
        updated = await store.get_job(job.id, identity)
        assert updated.status == "queued"
        assert updated.next_run_at > datetime.now(timezone.utc)
        store._jobs[job.id]["next_run_at"] = datetime.now(timezone.utc) - timedelta(seconds=1)
        await scheduler.run_once()
        finished = await store.get_job(job.id, identity)
        assert finished.runs_started == 2
        assert finished.next_run_at is None

    # 方法作用：验证后台不能使用已停用身份或权限快照继续查询。
    # Args: self - 测试实例。
    # Returns: 无返回值。
    async def test_disabled_identity_blocks_execution(self) -> None:
        """停用身份不能进入执行器，任务持久化为暂停状态。"""
        from src.reporting.scheduler import ReportScheduler

        store = ReportStore(memory=True)
        identity = SimpleNamespace(tenant_id=3, user_id=7, role="tenant_admin")
        job = await store.create_job(identity, "每日销售", self.plan())
        runner = SimpleNamespace(run_job=AsyncMock())
        scheduler = ReportScheduler(store, runner, identity_loader=AsyncMock(side_effect=PermissionError("身份停用")), use_llm=False)

        await scheduler.run_once()

        runner.run_job.assert_not_awaited()
        assert (await store.get_job(job.id, identity)).is_paused

    # 方法作用：验证进程中断后的恢复只释放旧运行实际持有的一份并发配额。
    # Args: self - 测试实例。
    # Returns: 无返回值。
    async def test_recovery_releases_original_reservation_once(self) -> None:
        """重复恢复不会重复释放其他运行的并发槽。"""
        from src.reporting.quota import QuotaManager
        from src.reporting.scheduler import ReportScheduler

        store = ReportStore(memory=True)
        quota = QuotaManager(store=store)
        owner = SimpleNamespace(tenant_id=3, user_id=7, role="tenant_admin")
        job = await store.create_job(owner, "销售", self.plan())
        run = await store.create_run(job.id, owner)
        await store.reserve_run_quota(run.id, owner, job.plan, quota)
        await store.update_run(run.id, owner, status="running")
        store._jobs[job.id].update(status="running", updated_at=datetime.now(timezone.utc) - timedelta(minutes=3))
        runner = SimpleNamespace(quota=quota, run_job=AsyncMock(return_value={"status": "succeeded"}))
        scheduler = ReportScheduler(store, runner, identity_loader=AsyncMock(return_value=owner), access_loader=AsyncMock(return_value={}), use_llm=False)

        await scheduler.run_once()
        await store.recover_job_runs(job.id, owner, quota)

        assert (await quota.snapshot(owner)).concurrent_remaining == 2
        assert (await store.get_run(run.id, owner)).error_code == "WORKER_INTERRUPTED"
        assert (await quota.snapshot(owner)).runs_remaining == 49

    # 方法作用：验证页面已提交但状态尚未回写时，恢复不会重新执行和发布重复页面。
    # Args: self - 测试实例。
    # Returns: 无返回值。
    async def test_recovery_recognizes_published_version(self) -> None:
        """已提交的页面是恢复证据，不能重新计费执行。"""
        from src.reporting.scheduler import ReportScheduler

        store = ReportStore(memory=True)
        owner = SimpleNamespace(tenant_id=3, user_id=7, role="tenant_admin")
        job = await store.create_job(owner, "销售", self.plan())
        run = await store.create_run(job.id, owner)
        await store.update_run(run.id, owner, status="running")
        await store.persist_version(job.id, run.id, owner, {"report_id": "already-published"}, {}, None)
        store._jobs[job.id].update(status="running", updated_at=datetime.now(timezone.utc) - timedelta(minutes=3))
        runner = SimpleNamespace(run_job=AsyncMock(return_value={"status": "succeeded"}))
        scheduler = ReportScheduler(store, runner, identity_loader=AsyncMock(return_value=owner), access_loader=AsyncMock(return_value={}), use_llm=False)

        await scheduler.run_once()

        runner.run_job.assert_not_awaited()
        assert (await store.get_run(run.id, owner)).status == "succeeded"
        assert (await store.get_job(job.id, owner)).next_run_at > datetime.now(timezone.utc)
