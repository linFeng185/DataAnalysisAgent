"""实时钻取 API 集成边界测试，覆盖 23.12。"""

from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import httpx
from fastapi import FastAPI

import src.api.routes.report_workbench as route
from src.reporting.models import AnalysisUnit, ReportPlan
from src.reporting.quota import QuotaManager, ReportPermissionService
from src.reporting.store import ReportStore


class TestReportLiveApi:
    """覆盖 23.12 的模板隔离、当前授权和并发释放。"""

    # 方法作用：验证页面仅返回模板标识，实时查询执行当前 ACL 且不暴露 SQL。
    # Args: self - 测试实例；monkeypatch - 边界替换工具。
    # Returns: 无返回值。
    async def test_live_http_path_and_forbidden_template_override(self, monkeypatch) -> None:
        """页面到已签发 SQL 的映射只能从私有 Store 取得。"""
        import src.reporting.live_drilldown as live

        owner = SimpleNamespace(tenant_id=3, user_id=7, role="tenant_admin")
        store = ReportStore(memory=True)
        quota = QuotaManager(store=store)
        job = await store.create_job(owner, "销售", ReportPlan(analysis_units=[AnalysisUnit(unit_id="sales", question="销售")]))
        run = await store.create_run(job.id, owner)
        query_id = await store.save_live_query(run.id, owner, "sales", "postgres", "SELECT amount FROM confidential_table")
        definition = {"drilldown_id": "live", "mode": "live", "query_ref": query_id, "columns": ["amount"], "allowed_filters": []}
        await store.persist_version(job.id, run.id, owner, {"report_id": "report", "permissions": {"drilldown": True}, "drilldowns": [definition]}, {}, None)
        monkeypatch.setattr(route, "get_current_identity", lambda: owner)
        monkeypatch.setattr(route, "get_report_store", lambda: store)
        monkeypatch.setattr(route, "get_report_quota_manager", lambda: quota)
        monkeypatch.setattr(route, "get_report_permission_service", lambda: ReportPermissionService(store))
        monkeypatch.setattr(route, "_resolve_plan_access", AsyncMock(return_value={"sales": {"allowed_columns": ["amount"], "row_filter_sql": "department_id=3"}}))
        executor = AsyncMock(return_value={"rows": [{"amount": 3}], "columns": ["amount"], "mode": "live"})
        monkeypatch.setattr(live, "execute_live_drilldown", executor)
        app = FastAPI()
        app.include_router(route.router)

        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
            document = await client.get("/report-workbench/reports/report")
            result = await client.post("/report-workbench/reports/report/drilldowns/live", json={})
            rejected = await client.post("/report-workbench/reports/report/drilldowns/live", json={"sql": "SELECT * FROM private"})
            store._capabilities[(3, 7)] = ["report.menu"]
            revoked = await client.post("/report-workbench/reports/report/drilldowns/live", json={})

        assert document.status_code == result.status_code == 200
        assert "confidential_table" not in document.text + result.text
        assert rejected.status_code == 422
        assert revoked.status_code == 403
        assert executor.await_count == 1
        assert executor.await_args.args[1]["row_filter_sql"] == "department_id=3"
        assert (await quota.snapshot(owner)).concurrent_remaining == 2

    # 方法作用：验证定时确认只持久化入队，暂停恢复接口不依赖请求 Task。
    # Args: self - 测试实例；monkeypatch - 边界替换工具。
    # Returns: 无返回值。
    async def test_scheduled_http_creation_pause_resume(self, monkeypatch) -> None:
        """持久化服务可用且租户授权后返回 202，不调用立即运行入口。"""
        import src.app_context as context_module
        import src.reporting.runner as runner_module

        owner = SimpleNamespace(tenant_id=3, user_id=7, role="tenant_admin")
        store = ReportStore(memory=True)
        quota = QuotaManager(store=store)
        plan = ReportPlan(execution_mode="scheduled", frequency="daily", max_runs=2, analysis_units=[AnalysisUnit(unit_id="sales", question="销售")])
        route._plan_cache[(3, 7, plan.canonical_hash())] = plan
        runner = SimpleNamespace(enqueue=Mock())
        monkeypatch.setattr(context_module, "get_app_context", lambda: SimpleNamespace(get_resource=lambda name: SimpleNamespace(running=True)))
        monkeypatch.setattr(route, "get_current_identity", lambda: owner)
        monkeypatch.setattr(route, "get_report_store", lambda: store)
        monkeypatch.setattr(route, "get_report_quota_manager", lambda: quota)
        monkeypatch.setattr(route, "get_report_permission_service", lambda: ReportPermissionService(store))
        monkeypatch.setattr(route, "_resolve_plan_access", AsyncMock(return_value={}))
        monkeypatch.setattr(runner_module, "get_report_runner", lambda: runner)
        app = FastAPI()
        app.include_router(route.router)

        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
            created = await client.post("/report-workbench/jobs", json={"prompt": "每天生成销售报表", "plan_hash": plan.canonical_hash(), "confirm": True, "idempotency_key": "schedule-test"})
            job_id = created.json()["job_id"]
            paused = await client.post(f"/report-workbench/jobs/{job_id}/pause")
            resumed = await client.post(f"/report-workbench/jobs/{job_id}/resume")

        assert created.status_code == 202
        runner.enqueue.assert_not_called()
        assert paused.json()["is_paused"] is True
        assert resumed.json()["is_paused"] is False
        assert (await store.get_job(job_id, owner)).next_run_at is not None
