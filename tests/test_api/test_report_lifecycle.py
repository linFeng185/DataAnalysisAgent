"""报表生命周期与交互权限边界，覆盖 23.6、23.13。"""

from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import httpx
from fastapi import FastAPI

import src.api.routes.report_workbench as route
from src.reporting.models import AnalysisUnit, ReportPlan
from src.reporting.quota import ReportPermissionService
from src.reporting.store import ReportStore


class TestReportLifecycleApi:
    """覆盖 23.13 过期管理、能力分离与 23.6 到期阻断。"""

    # 方法作用：验证已过期页面不能借助私有钻取返回数据。
    # Args: self - 测试实例；monkeypatch - 依赖替换工具。
    # Returns: 无返回值。
    async def test_expired_private_drilldown_is_blocked(self, monkeypatch) -> None:
        """正文到期后，明细接口也必须返回 410。"""
        identity = SimpleNamespace(tenant_id=3, user_id=7, role="tenant_admin")
        store = ReportStore(memory=True)
        expired = datetime.now(timezone.utc) - timedelta(days=1)
        document = {"report_id": "report", "permissions": {"drilldown": True}, "drilldowns": [{"drilldown_id": "dd", "mode": "snapshot", "data_ref": "rows", "columns": ["amount"]}]}
        await store.persist_version("job", "run", identity, document, {"rows": [{"amount": 42}]}, expired)
        monkeypatch.setattr(route, "get_current_identity", lambda: identity)
        monkeypatch.setattr(route, "get_report_store", lambda: store)
        monkeypatch.setattr(route, "get_report_permission_service", lambda: ReportPermissionService(store))
        app = FastAPI()
        app.include_router(route.router)

        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
            response = await client.post("/report-workbench/reports/report/drilldowns/dd", json={})

        assert response.status_code == 410
        assert "amount" not in response.text

    # 方法作用：验证管理元数据不泄露正文，延期能力不能获得永久权限。
    # Args: self - 测试实例；monkeypatch - 依赖替换工具。
    # Returns: 无返回值。
    async def test_lifecycle_http_permissions_and_restore(self, monkeypatch) -> None:
        """租户授予的延期能力可恢复页面，但无永久能力仍返回 403。"""
        identity = SimpleNamespace(tenant_id=3, user_id=7, role="analyst")
        store = ReportStore(memory=True)
        store._capabilities[(3, 7)] = ["report.menu", "report.extend"]
        expired = datetime.now(timezone.utc) - timedelta(days=1)
        await store.persist_version("job", "run", identity, {"report_id": "report", "sensitive_body": "private"}, {}, expired)
        monkeypatch.setattr(route, "get_current_identity", lambda: identity)
        monkeypatch.setattr(route, "get_report_store", lambda: store)
        monkeypatch.setattr(route, "get_report_permission_service", lambda: ReportPermissionService(store))
        app = FastAPI()
        app.include_router(route.router)

        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
            summary = await client.get("/report-workbench/reports/report/lifecycle")
            forbidden = await client.post("/report-workbench/reports/report/permanent", json={})
            invalid = await client.post("/report-workbench/reports/report/extend", json={"days": 31})
            restored = await client.post("/report-workbench/reports/report/extend", json={"days": 7})
            page = await client.get("/report-workbench/reports/report")

        assert summary.json()["expired"] is True
        assert "sensitive_body" not in summary.text
        assert forbidden.status_code == 403
        assert invalid.status_code == 422
        assert restored.status_code == page.status_code == 200
        assert store._audits[-1]["action"] == "page_lifecycle"

    # 方法作用：验证手动重跑必须传入最新授权，不能继承旧任务权限。
    # Args: self - 测试实例；monkeypatch - 依赖替换工具。
    # Returns: 无返回值。
    async def test_manual_run_revalidates_access(self, monkeypatch) -> None:
        """手动重跑在入队前重新解析数据源权限并传给执行器。"""
        import src.reporting.runner as runner_module

        identity = SimpleNamespace(tenant_id=3, user_id=7, role="analyst")
        store = ReportStore(memory=True)
        plan = ReportPlan(analysis_units=[AnalysisUnit(unit_id="sales", question="销量", datasource="sales")])
        job = await store.create_job(identity, "销量", plan)
        runner = SimpleNamespace(enqueue=Mock())
        access = {"sales": {"allowed_columns": ["amount"], "row_filter_sql": "department_id=3"}}
        monkeypatch.setattr(route, "get_current_identity", lambda: identity)
        monkeypatch.setattr(route, "get_report_store", lambda: store)
        monkeypatch.setattr(route, "get_report_permission_service", lambda: ReportPermissionService(store))
        monkeypatch.setattr(route, "_resolve_plan_access", AsyncMock(return_value=access))
        monkeypatch.setattr(runner_module, "get_report_runner", lambda: runner)

        await route.run_job_now(job.id)

        route._resolve_plan_access.assert_awaited_once_with(identity, job.plan)
        assert runner.enqueue.call_args.kwargs["authorized_datasources"] == access
