"""报表页面独立访问、逐页生命周期操作与永久报表上限，覆盖需求 2.3、2.4（设计 48 第 7.4 节）。"""

from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from typing import Any

import httpx
from fastapi import FastAPI

import src.api.routes.report_workbench as route
from src.reporting.models import AnalysisUnit, ReportPlan
from src.reporting.quota import ReportPermissionService
from src.reporting.store import ReportStore


# 方法作用：构造只挂载报表工作台路由的隔离应用，并按既有测试方式替换身份与 Store。
# Args: monkeypatch - pytest 依赖替换工具；identity - 当前请求身份；store - 报表 Store。
# Returns: 可直接用 httpx ASGITransport 调用的 FastAPI 应用。
def build_report_app(monkeypatch: Any, identity: Any, store: ReportStore) -> FastAPI:
    monkeypatch.setattr(route, "get_current_identity", lambda: identity)
    monkeypatch.setattr(route, "get_report_store", lambda: store)
    monkeypatch.setattr(route, "get_report_permission_service", lambda: ReportPermissionService(store))
    app = FastAPI()
    app.include_router(route.router)
    return app


# 方法作用：创建一条属于该身份的报表记录，供页面版本挂载。
# Args: store - 报表 Store；identity - 报表拥有者身份；prompt - 报表描述。
# Returns: 已创建的报表任务记录。
async def create_owner_job(store: ReportStore, identity: Any, prompt: str = "销量") -> Any:
    plan = ReportPlan(analysis_units=[AnalysisUnit(unit_id="sales", question="销量")])
    return await store.create_job(identity, prompt, plan)


class TestPageIndependentAccess:
    """覆盖需求 2.3：每次运行产生的页面独立可访问，非最新页面也能按自己的 id 取到正文。"""

    # 方法作用：验证按页面版本 id 能取到非最新页面的正文，任务 id 仍回退到最新页。
    # Args: self - 测试实例；monkeypatch - 依赖替换工具。
    # Returns: 无返回值。
    async def test_non_latest_page_is_reachable_by_version_id(self, monkeypatch: Any) -> None:
        """点 2.3 的核心语义：第二版存在时，第一版仍要能独立访问。"""
        # Arrange
        identity = SimpleNamespace(tenant_id=3, user_id=7, role="analyst")
        store = ReportStore(memory=True)
        job = await create_owner_job(store, identity)
        soon = datetime.now(timezone.utc) + timedelta(days=3)
        first = await store.persist_version(job.id, "run-1", identity, {"report_id": job.id, "title": "第一版"}, {"d": [1]}, soon)
        second = await store.persist_version(job.id, "run-2", identity, {"report_id": job.id, "title": "第二版"}, {"d": [2]}, soon)
        app = build_report_app(monkeypatch, identity, store)

        # Act
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
            non_latest = await client.get(f"/report-workbench/reports/{first.id}")
            latest = await client.get(f"/report-workbench/reports/{second.id}")
            by_job = await client.get(f"/report-workbench/reports/{job.id}")
            pages = await client.get(f"/report-workbench/reports/{job.id}/pages")

        # Assert
        assert non_latest.status_code == 200
        assert non_latest.json()["report"] is None, "按页面 id 读取必须走页面正文分支"
        assert non_latest.json()["document"]["title"] == "第一版"
        assert non_latest.json()["version"] == 1
        assert latest.status_code == 200
        assert latest.json()["document"]["title"] == "第二版"
        assert by_job.status_code == 200
        assert by_job.json()["document"]["title"] == "第二版", "任务 id 必须回退到最新页面"
        # 页面列表按版本倒序且只有第二版是最新
        assert [item["id"] for item in pages.json()["pages"]] == [second.id, first.id]
        assert [item["is_latest"] for item in pages.json()["pages"]] == [True, False]

    # 方法作用：验证 Store 层同时支持页面版本 id 与任务 id 两种入参。
    # Args: self - 测试实例。
    # Returns: 无返回值。
    async def test_store_resolves_page_id_before_job_id(self) -> None:
        """同一方法要能吃两种标识：页面版本 id 精确命中，任务 id 取最新。"""
        # Arrange
        identity = SimpleNamespace(tenant_id=3, user_id=7, role="analyst")
        store = ReportStore(memory=True)
        job = await create_owner_job(store, identity)
        soon = datetime.now(timezone.utc) + timedelta(days=3)
        first = await store.persist_version(job.id, "run-1", identity, {"report_id": job.id, "title": "第一版"}, {}, soon)
        second = await store.persist_version(job.id, "run-2", identity, {"report_id": job.id, "title": "第二版"}, {}, soon)

        # Act
        by_page = await store.get_report_version(first.id, identity)
        by_job = await store.get_report_version(job.id, identity)
        missing = await store.get_report_version("not-a-uuid", identity)

        # Assert
        assert by_page is not None and by_page.id == first.id and by_page.document["title"] == "第一版"
        assert by_job is not None and by_job.id == second.id and by_job.document["title"] == "第二版"
        assert missing is None


class TestExpiredPageManagement:
    """覆盖需求 2.3 的过期语义：正文 410，但生命周期仍可管理。"""

    # 方法作用：验证过期页面查看返回 410，按页面 id 仍能延期恢复。
    # Args: self - 测试实例；monkeypatch - 依赖替换工具。
    # Returns: 无返回值。
    async def test_expired_page_is_410_and_still_extendable(self, monkeypatch: Any) -> None:
        """过期只是正文不可读，不能连延期入口一起失效。"""
        # Arrange
        identity = SimpleNamespace(tenant_id=3, user_id=7, role="analyst")
        store = ReportStore(memory=True)
        job = await create_owner_job(store, identity)
        expired = datetime.now(timezone.utc) - timedelta(days=1)
        page = await store.persist_version(job.id, "run-1", identity, {"report_id": job.id, "title": "过期页"}, {}, expired)
        app = build_report_app(monkeypatch, identity, store)

        # Act
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
            blocked = await client.get(f"/report-workbench/reports/{page.id}")
            lifecycle = await client.get(f"/report-workbench/reports/{page.id}/lifecycle")
            extended = await client.post(f"/report-workbench/reports/{page.id}/extend", json={"days": 7})
            restored = await client.get(f"/report-workbench/reports/{page.id}")

        # Assert
        assert blocked.status_code == 410
        assert lifecycle.status_code == 200
        assert lifecycle.json()["expired"] is True
        assert lifecycle.json()["can_extend"] is True
        assert extended.status_code == 200
        assert extended.json()["permanent"] is False
        assert restored.status_code == 200
        assert restored.json()["document"]["title"] == "过期页"

    # 方法作用：验证单次延期不得超过三十天。
    # Args: self - 测试实例；monkeypatch - 依赖替换工具。
    # Returns: 无返回值。
    async def test_extend_rejects_more_than_thirty_days(self, monkeypatch: Any) -> None:
        """界面按后端约束提示「每次最多 30 天」，超限必须在契约层被拒绝。"""
        # Arrange
        identity = SimpleNamespace(tenant_id=3, user_id=7, role="analyst")
        store = ReportStore(memory=True)
        job = await create_owner_job(store, identity)
        soon = datetime.now(timezone.utc) + timedelta(days=1)
        page = await store.persist_version(job.id, "run-1", identity, {"report_id": job.id}, {}, soon)
        app = build_report_app(monkeypatch, identity, store)

        # Act
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
            rejected = await client.post(f"/report-workbench/reports/{page.id}/extend", json={"days": 31})
            accepted = await client.post(f"/report-workbench/reports/{page.id}/extend", json={"days": 30})

        # Assert
        assert rejected.status_code == 422
        assert accepted.status_code == 200


class TestPermanentReportLimit:
    """覆盖需求 2.4：租户级 max_permanent_reports_per_user 限制每用户永久报表数量。"""

    # 方法作用：验证达到上限后转永久返回 409，且不改动任何页面数据。
    # Args: self - 测试实例；monkeypatch - 依赖替换工具。
    # Returns: 无返回值。
    async def test_limit_reached_returns_409_without_changing_data(self, monkeypatch: Any) -> None:
        """409 只拒绝请求：有效期、校验摘要与永久计数都必须保持原样。"""
        # Arrange
        identity = SimpleNamespace(tenant_id=3, user_id=7, role="analyst")
        store = ReportStore(memory=True)
        await store.save_tenant_settings(3, {"max_permanent_reports_per_user": 2}, 1)
        job = await create_owner_job(store, identity)
        soon = datetime.now(timezone.utc) + timedelta(days=2)
        for index in range(2):
            await store.persist_version(job.id, f"run-{index}", identity, {"report_id": job.id, "title": f"永久{index}"}, {}, None)
        finite = await store.persist_version(job.id, "run-finite", identity, {"report_id": job.id, "title": "有限期"}, {}, soon)
        before = await store.get_report_version(finite.id, identity)
        app = build_report_app(monkeypatch, identity, store)

        # Act
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
            response = await client.post(f"/report-workbench/reports/{finite.id}/permanent", json={})

        # Assert
        assert response.status_code == 409
        assert "永久报表已达上限 2，请联系租户管理员调整" in response.json()["detail"]
        after = await store.get_report_version(finite.id, identity)
        assert after is not None and after.expires_at is not None
        assert after.checksum == before.checksum
        assert await store.count_permanent_versions_for_user(identity, 7) == 2
        assert store._audits[-1]["result"] == "denied"
        assert store._audits[-1]["action"] == "page_lifecycle"

    # 方法作用：验证上限为 0 时不限制，且 PUT lifecycle 与 POST permanent 走同一套校验。
    # Args: self - 测试实例；monkeypatch - 依赖替换工具。
    # Returns: 无返回值。
    async def test_zero_limit_means_unlimited(self, monkeypatch: Any) -> None:
        """设置行不存在或配置 0 都按不限制处理。"""
        # Arrange
        identity = SimpleNamespace(tenant_id=3, user_id=7, role="analyst")
        store = ReportStore(memory=True)
        job = await create_owner_job(store, identity)
        soon = datetime.now(timezone.utc) + timedelta(days=2)
        first = await store.persist_version(job.id, "run-1", identity, {"report_id": job.id}, {}, soon)
        second = await store.persist_version(job.id, "run-2", identity, {"report_id": job.id}, {}, soon)
        app = build_report_app(monkeypatch, identity, store)

        # Act：未配置任何设置行时先转永久
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
            default_limit = await client.post(f"/report-workbench/reports/{first.id}/permanent", json={})
            # 显式配置 0（不限制）后继续转第二个页面
            await store.save_tenant_settings(3, {"max_permanent_reports_per_user": 0}, 1)
            explicit_zero = await client.put(f"/report-workbench/reports/{second.id}/lifecycle", json={"permanent": True})

        # Assert
        assert default_limit.status_code == 200
        assert explicit_zero.status_code == 200
        assert explicit_zero.json()["permanent"] is True
        assert await store.count_permanent_versions_for_user(identity, 7) == 2

    # 方法作用：验证已是永久的页面重复调用不报 409，计数也不增加。
    # Args: self - 测试实例；monkeypatch - 依赖替换工具。
    # Returns: 无返回值。
    async def test_repeat_permanent_on_permanent_page_is_idempotent(self, monkeypatch: Any) -> None:
        """只有「由有限期转为永久」才计数校验，已永久的页面重复调用必须幂等。"""
        # Arrange
        identity = SimpleNamespace(tenant_id=3, user_id=7, role="analyst")
        store = ReportStore(memory=True)
        await store.save_tenant_settings(3, {"max_permanent_reports_per_user": 1}, 1)
        job = await create_owner_job(store, identity)
        page = await store.persist_version(job.id, "run-1", identity, {"report_id": job.id}, {}, None)
        app = build_report_app(monkeypatch, identity, store)

        # Act：上限 1 且已有 1 个永久页面，重复调用同一个永久页面
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
            response = await client.post(f"/report-workbench/reports/{page.id}/permanent", json={})

        # Assert
        assert response.status_code == 200
        assert response.json()["permanent"] is True
        assert await store.count_permanent_versions_for_user(identity, 7) == 1
        assert store._audits[-1]["result"] == "success"

    # 方法作用：验证租户管理员本人不受永久上限约束。
    # Args: self - 测试实例；monkeypatch - 依赖替换工具。
    # Returns: 无返回值。
    async def test_tenant_admin_is_exempt_from_limit(self, monkeypatch: Any) -> None:
        """上限由租户管理员配置，管理员不能被自己配置的上限锁死。"""
        # Arrange
        identity = SimpleNamespace(tenant_id=3, user_id=9, role="tenant_admin")
        store = ReportStore(memory=True)
        await store.save_tenant_settings(3, {"max_permanent_reports_per_user": 1}, 1)
        job = await create_owner_job(store, identity)
        for index in range(2):
            await store.persist_version(job.id, f"run-{index}", identity, {"report_id": job.id}, {}, None)
        soon = datetime.now(timezone.utc) + timedelta(days=2)
        finite = await store.persist_version(job.id, "run-finite", identity, {"report_id": job.id}, {}, soon)
        app = build_report_app(monkeypatch, identity, store)

        # Act
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
            response = await client.post(f"/report-workbench/reports/{finite.id}/permanent", json={})

        # Assert
        assert response.status_code == 200
        assert await store.count_permanent_versions_for_user(identity, 9) == 3

    # 方法作用：验证其他租户的永久页面不计入本租户用户的额度。
    # Args: self - 测试实例；monkeypatch - 依赖替换工具。
    # Returns: 无返回值。
    async def test_other_tenant_pages_do_not_consume_quota(self, monkeypatch: Any) -> None:
        """计数必须限定当前租户，跨租户页面不得占用额度。"""
        # Arrange
        identity = SimpleNamespace(tenant_id=3, user_id=7, role="analyst")
        neighbor = SimpleNamespace(tenant_id=4, user_id=7, role="analyst")
        store = ReportStore(memory=True)
        await store.save_tenant_settings(3, {"max_permanent_reports_per_user": 1}, 1)
        neighbor_job = await create_owner_job(store, neighbor, "他租户报表")
        await store.persist_version(neighbor_job.id, "run-n", neighbor, {"report_id": neighbor_job.id}, {}, None)
        job = await create_owner_job(store, identity)
        soon = datetime.now(timezone.utc) + timedelta(days=2)
        page = await store.persist_version(job.id, "run-1", identity, {"report_id": job.id}, {}, soon)
        app = build_report_app(monkeypatch, identity, store)
        # 前置断言：他租户的永久页面不能被算进本租户用户的额度
        assert await store.count_permanent_versions_for_user(identity, 7) == 0
        assert await store.count_permanent_versions_for_user(neighbor, 7) == 1

        # Act
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
            response = await client.post(f"/report-workbench/reports/{page.id}/permanent", json={})

        # Assert
        assert response.status_code == 200
        assert await store.count_permanent_versions_for_user(identity, 7) == 1


class TestPageLifecycleCapabilities:
    """覆盖需求 2.4：延期与永久能力下放给 analyst，viewer 不获得。"""

    # 方法作用：验证 analyst 具备延期与永久能力。
    # Args: self - 测试实例。
    # Returns: 无返回值。
    async def test_analyst_can_extend_and_permanent(self) -> None:
        """能力下放后分析员不再需要租户管理员代劳。"""
        # Arrange
        service = ReportPermissionService()
        analyst = SimpleNamespace(tenant_id=3, user_id=7, role="analyst")

        # Act
        extend = await service.has(analyst, "report.extend")
        permanent = await service.has(analyst, "report.permanent")

        # Assert
        assert extend is True
        assert permanent is True

    # 方法作用：验证 viewer 只保留菜单与列表能力，不具备延期与永久。
    # Args: self - 测试实例。
    # Returns: 无返回值。
    async def test_viewer_has_no_extend_or_permanent(self) -> None:
        """只看报表的角色不得改动任何页面的生命周期。"""
        # Arrange
        service = ReportPermissionService()
        viewer = SimpleNamespace(tenant_id=3, user_id=8, role="viewer")

        # Act
        extend = await service.has(viewer, "report.extend")
        permanent = await service.has(viewer, "report.permanent")
        list_allowed = await service.has(viewer, "report.list")

        # Assert
        assert extend is False
        assert permanent is False
        assert list_allowed is True

    # 方法作用：验证租户管理员原有能力不因下放而减少。
    # Args: self - 测试实例。
    # Returns: 无返回值。
    async def test_tenant_admin_keeps_full_capabilities(self) -> None:
        """下放只做加法：管理员能力集合必须仍然覆盖延期与永久。"""
        # Arrange
        service = ReportPermissionService()
        admin = SimpleNamespace(tenant_id=3, user_id=9, role="tenant_admin")

        # Act
        capabilities = await service.capabilities(admin)

        # Assert
        assert {"report.extend", "report.permanent", "report.view_all", "report.link_manage"}.issubset(set(capabilities))

    # 方法作用：验证无能力身份调用永久接口被 403 拒绝，不会走到 409 上限分支。
    # Args: self - 测试实例；monkeypatch - 依赖替换工具。
    # Returns: 无返回值。
    async def test_viewer_permanent_request_is_forbidden(self, monkeypatch: Any) -> None:
        """能力校验先于上限校验：viewer 拿到 403 而不是 409。"""
        # Arrange
        identity = SimpleNamespace(tenant_id=3, user_id=8, role="viewer")
        store = ReportStore(memory=True)
        await store.save_tenant_settings(3, {"max_permanent_reports_per_user": 1}, 1)
        job = await create_owner_job(store, identity)
        soon = datetime.now(timezone.utc) + timedelta(days=2)
        page = await store.persist_version(job.id, "run-1", identity, {"report_id": job.id}, {}, soon)
        app = build_report_app(monkeypatch, identity, store)

        # Act
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
            response = await client.post(f"/report-workbench/reports/{page.id}/permanent", json={})

        # Assert
        assert response.status_code == 403
        assert "report.permanent" in response.json()["detail"]
