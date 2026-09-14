"""报表后台生成与报表信息接口测试，覆盖设计 48 第 7.2、7.3 节。"""

from __future__ import annotations

import sys
import types
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, Mock

import httpx
import pytest
from fastapi import FastAPI, HTTPException

import src.api.routes.report_workbench as route
from src.reporting.models import AnalysisUnit, ReportPlan
from src.reporting.plan_worker import ReportPlanWorker, generate_report_title
from src.reporting.quota import QuotaManager, QuotaPolicy, ReportPermissionService
from src.reporting.store import ReportStore

PREFIX = "/report-workbench"
# 7.3 的报表信息 7 列（外加运行上下文列），列表响应必须全部具备。
LIST_FIELDS = {
    "id", "title", "prompt", "datasources", "divergence", "status",
    "runs_started", "next_run_at", "created_at", "execution_mode", "is_paused", "latest_report_id",
}


# 方法作用：构造记录入队请求的假工作器，避免测试触发真实规划与模型调用。
# Args: 无。
# Returns: 具备 enqueue 的假工作器实例。
class _FakePlanWorker:
    """只记录入队参数，是否真正规划由 ReportPlanWorker 的独立用例覆盖。"""

    def __init__(self) -> None:
        self.enqueued: list[dict[str, Any]] = []

    def enqueue(self, report_id: str, identity: Any, *, model_id: str = "", connection_id: int | None = None) -> None:
        """记录入队参数，返回 None 代替 asyncio.Task。"""
        self.enqueued.append({
            "report_id": report_id,
            "user_id": int(getattr(identity, "user_id", 0) or 0),
            "model_id": model_id,
            "connection_id": connection_id,
        })


# 方法作用：构造可注入成功计划或指定异常的假计划解析器。
# Args: plan - 解析返回的计划；error - 需要抛出的异常。
# Returns: 具备 parse/diverge 的假解析器。
class _FakePlanner:
    """worker 只依赖 parse 与 diverge 两个入口，假解析器据此保持最小实现。"""

    def __init__(self, plan: ReportPlan | None = None, error: BaseException | None = None) -> None:
        self.plan = plan
        self.error = error

    async def parse(self, prompt: str, *, datasources: list[str] | None = None, timezone: str = "Asia/Shanghai", divergence: str = "standard") -> ReportPlan:
        """返回预置计划或在配置了异常时抛出。"""
        if self.error is not None:
            raise self.error
        if self.plan is None:
            raise ValueError("测试未提供计划")
        return self.plan.model_copy(deep=True)

    async def diverge(self, plan: ReportPlan, *, datasources: list[str] | None = None, catalog: str = "") -> ReportPlan:
        """发散阶段原样返回，避免引入额外模型依赖。"""
        if self.error is not None:
            raise self.error
        return plan


# 方法作用：把路由模块的全局依赖替换为指定内存 Store 与当前身份。
# Args: monkeypatch - 补丁工具；store - 内存 Store；identity - 当前身份；quota - 可选配额服务；worker - 可选假工作器。
# Returns: 使用的假工作器。
def _bind(monkeypatch: pytest.MonkeyPatch, store: ReportStore, identity: Any, *, quota: QuotaManager | None = None, worker: _FakePlanWorker | None = None) -> _FakePlanWorker:
    resolved_worker = worker or _FakePlanWorker()
    monkeypatch.setattr(route, "get_current_identity", lambda: identity)
    monkeypatch.setattr(route, "get_report_store", lambda: store)
    monkeypatch.setattr(route, "get_report_quota_manager", lambda: quota or QuotaManager(store=store))
    monkeypatch.setattr(route, "get_report_permission_service", lambda: ReportPermissionService(store))
    monkeypatch.setattr(route, "get_report_plan_worker", lambda: resolved_worker)
    return resolved_worker


# 方法作用：构造只挂载报表工作台路由的测试客户端。
# Args: 无。
# Returns: httpx 异步客户端。
def _client() -> httpx.AsyncClient:
    app = FastAPI()
    app.include_router(route.router)
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test")


# 方法作用：注入假通知模块并记录通知调用参数。
# Args: monkeypatch - 补丁工具。
# Returns: 通知调用记录列表。
def _fake_notifications(monkeypatch: pytest.MonkeyPatch) -> list[dict[str, Any]]:
    calls: list[dict[str, Any]] = []
    package = types.ModuleType("src.notifications")
    package.__path__ = []  # 声明为包，满足 from src.notifications.service import 的父包语义
    service = types.ModuleType("src.notifications.service")

    async def notify_user(tenant_id: int, user_id: int, kind: str, title: str, body: str, link: str = "") -> None:
        """记录一次通知调用。"""
        calls.append({"tenant_id": tenant_id, "user_id": user_id, "kind": kind, "title": title, "body": body, "link": link})

    service.notify_user = notify_user
    package.service = service
    monkeypatch.setitem(sys.modules, "src.notifications", package)
    monkeypatch.setitem(sys.modules, "src.notifications.service", service)
    return calls


# 方法作用：构造一份两单元的计划，供裁剪与运行用例复用。
# Args: prompt - 任务描述。
# Returns: 已校验的报表计划。
def _plan(prompt: str = "季度销售分析") -> ReportPlan:
    return ReportPlan(
        requested_datasources=["sales"],
        analysis_units=[
            AnalysisUnit(unit_id="sales_trend", question=f"{prompt}的销售趋势"),
            AnalysisUnit(unit_id="sales_region", question=f"{prompt}的地区分布"),
        ],
    )


# 方法作用：在内存 Store 中直接造出一条处于指定状态的报表记录。
# Args: store - 内存 Store；identity - 记录拥有者；status - 目标状态；prompt - 描述；plan - 可选计划。
# Returns: 报表标识。
async def _make_report(store: ReportStore, identity: Any, status: str = "draft", *, prompt: str = "季度销售分析", plan: ReportPlan | None = None) -> str:
    job, _created = await store.create_planning_job(identity, prompt, datasources=["sales"], divergence="standard")
    if status != "planning":
        resolved = plan or _plan(prompt)
        await store.save_planned_job(job.id, identity, plan=resolved, title=prompt[:20])
    if status == "plan_failed":
        await store.mark_plan_failed(job.id, identity, reason="模型不可用")
    elif status == "queued":
        await store.update_job_status(job.id, identity, "queued")
    elif status == "running":
        await store.update_job_status(job.id, identity, "running")
    return job.id


# 方法作用：构造一个真实的后台规划工作器，并把外呼依赖替换为可控实现。
# Args: monkeypatch - 补丁工具；store - 内存 Store；plan - 解析计划；error - 解析异常。
# Returns: ReportPlanWorker 实例。
def _real_worker(monkeypatch: pytest.MonkeyPatch, store: ReportStore, plan: ReportPlan | None = None, error: BaseException | None = None) -> ReportPlanWorker:
    from src.reporting.runner import ReportRunner

    monkeypatch.setattr(ReportRunner, "_datasource_table_catalog", AsyncMock(return_value=""))
    monkeypatch.setattr(ReportPlanWorker, "_resolve_access", AsyncMock(return_value=None))
    return ReportPlanWorker(store=store, planner=_FakePlanner(plan, error))


class TestReportCreateApi:
    """覆盖 POST /reports：建记录不预占额度、幂等与入参校验。"""

    # 方法作用：验证点「生成计划」只建记录并排队规划，不改变任何额度余额。
    # Args: self - 测试实例；monkeypatch - 补丁工具。
    # Returns: 无返回值。
    async def test_create_report_does_not_reserve_quota(self, monkeypatch) -> None:
        """需求 1.5：建报表前后 jobs_remaining 必须一致。"""
        # Arrange
        store = ReportStore(memory=True)
        analyst = SimpleNamespace(tenant_id=3, user_id=7, role="analyst")
        worker = _bind(monkeypatch, store, analyst)

        # Act
        async with _client() as client:
            before = await client.get(f"{PREFIX}/quotas/me")
            created = await client.post(f"{PREFIX}/reports", json={"prompt": "季度销售分析", "datasources": ["sales"], "divergence": "deep"})
            after = await client.get(f"{PREFIX}/quotas/me")

        # Assert
        assert created.status_code == 202
        body = created.json()
        assert body["status"] == "planning"
        assert before.json()["jobs_remaining"] == after.json()["jobs_remaining"]
        row = store._jobs[body["report_id"]]
        assert row["status"] == "planning" and row["divergence"] == "deep"
        assert row["datasources_json"] == ["sales"] and row["plan_json"] == {} and row["plan_error"] == ""
        assert worker.enqueued == [{"report_id": body["report_id"], "user_id": 7, "model_id": "", "connection_id": None}]

    # 方法作用：验证同一幂等键重复提交只产生一条记录且只入队一次规划。
    # Args: self - 测试实例；monkeypatch - 补丁工具。
    # Returns: 无返回值。
    async def test_create_report_idempotency_key_keeps_single_record(self, monkeypatch) -> None:
        """重复点击生成计划不得产生第二条记录或第二次后台规划。"""
        # Arrange
        store = ReportStore(memory=True)
        analyst = SimpleNamespace(tenant_id=3, user_id=7, role="analyst")
        worker = _bind(monkeypatch, store, analyst)
        payload = {"prompt": "季度销售分析", "idempotency_key": "once-1"}

        # Act
        async with _client() as client:
            first = await client.post(f"{PREFIX}/reports", json=payload)
            second = await client.post(f"{PREFIX}/reports", json=payload)

        # Assert
        assert first.status_code == second.status_code == 202
        assert first.json()["report_id"] == second.json()["report_id"]
        assert len(store._jobs) == 1
        assert len(worker.enqueued) == 1

    # 方法作用：验证空描述与非法发散档位在入口即被拒绝。
    # Args: self - 测试实例；monkeypatch - 补丁工具。
    # Returns: 无返回值。
    async def test_create_report_rejects_invalid_payload(self, monkeypatch) -> None:
        """prompt 为空与 divergence 非枚举值都必须返回 422。"""
        # Arrange
        store = ReportStore(memory=True)
        analyst = SimpleNamespace(tenant_id=3, user_id=7, role="analyst")
        _bind(monkeypatch, store, analyst)

        # Act
        async with _client() as client:
            blank = await client.post(f"{PREFIX}/reports", json={"prompt": "   "})
            bad_divergence = await client.post(f"{PREFIX}/reports", json={"prompt": "销售", "divergence": "wild"})

        # Assert
        assert blank.status_code == 422
        assert bad_divergence.status_code == 422
        assert store._jobs == {}

    # 方法作用：验证只读角色不能创建报表。
    # Args: self - 测试实例；monkeypatch - 补丁工具。
    # Returns: 无返回值。
    async def test_create_report_requires_create_page_capability(self, monkeypatch) -> None:
        """viewer 没有 report.create_page，创建入口必须 403。"""
        # Arrange
        store = ReportStore(memory=True)
        viewer = SimpleNamespace(tenant_id=3, user_id=9, role="viewer")
        _bind(monkeypatch, store, viewer)

        # Act
        async with _client() as client:
            response = await client.post(f"{PREFIX}/reports", json={"prompt": "季度销售分析"})

        # Assert
        assert response.status_code == 403


class TestPlanWorker:
    """覆盖后台规划：成功落库、标题回退、失败状态与通知。"""

    # 方法作用：验证模型不可用时规划仍成功、标题回退为描述前缀并发出就绪通知。
    # Args: self - 测试实例；monkeypatch - 补丁工具。
    # Returns: 无返回值。
    async def test_plan_worker_success_with_title_fallback(self, monkeypatch) -> None:
        """LLM 不可用只影响标题，计划必须照常进入「未生成」。"""
        # Arrange
        store = ReportStore(memory=True)
        analyst = SimpleNamespace(tenant_id=3, user_id=7, role="analyst")
        prompt = "季度销售分析报表"
        report_id = await _make_report(store, analyst, "planning", prompt=prompt)
        worker = _real_worker(monkeypatch, store, _plan(prompt))
        notifications = _fake_notifications(monkeypatch)
        monkeypatch.setattr("src.llm.client.is_task_llm_available", lambda task: False)

        # Act
        result = await worker.plan_report(report_id, analyst)

        # Assert
        assert result["status"] == "draft"
        row = store._jobs[report_id]
        assert row["status"] == "draft" and row["plan_error"] == ""
        assert row["title"] == prompt[:20]
        assert len(row["plan_json"]["analysis_units"]) == 2
        assert row["plan_hash"].startswith("sha256:")
        # 计划就绪属「任务执行完毕的个人通知」，类型固定为 personal，区分放在标题里。
        assert [item["kind"] for item in notifications] == ["personal"]
        assert "计划已生成" in notifications[0]["title"]
        assert notifications[0]["user_id"] == 7 and report_id in notifications[0]["link"]

    # 方法作用：验证模型可用时标题使用 report.title 任务的输出。
    # Args: self - 测试实例；monkeypatch - 补丁工具。
    # Returns: 无返回值。
    async def test_plan_worker_uses_model_title(self, monkeypatch) -> None:
        """标题任务返回结构化标题时不得再走前缀回退。"""
        # Arrange
        from src.llm.output_contracts import ReportTitleOutput

        store = ReportStore(memory=True)
        analyst = SimpleNamespace(tenant_id=3, user_id=7, role="analyst")
        report_id = await _make_report(store, analyst, "planning", prompt="统计各部门季度销售额与同比")
        worker = _real_worker(monkeypatch, store, _plan("统计各部门季度销售额与同比"))
        _fake_notifications(monkeypatch)
        monkeypatch.setattr("src.llm.client.is_task_llm_available", lambda task: True)
        monkeypatch.setattr("src.llm.invocation.invoke_structured", AsyncMock(return_value=ReportTitleOutput(title="季度销售概览")))

        # Act
        result = await worker.plan_report(report_id, analyst)

        # Assert
        assert result["title"] == "季度销售概览"
        assert store._jobs[report_id]["title"] == "季度销售概览"

    # 方法作用：验证标题生成异常时回退前缀且不抛出。
    # Args: self - 测试实例；monkeypatch - 补丁工具。
    # Returns: 无返回值。
    async def test_generate_title_falls_back_on_failure(self, monkeypatch) -> None:
        """标题调用抛异常时必须回退，而不是让计划生成失败。"""
        # Arrange
        monkeypatch.setattr("src.llm.client.is_task_llm_available", Mock(side_effect=RuntimeError("模型服务不可用")))

        # Act
        title = await generate_report_title("统计各部门季度销售额与同比增长情况")

        # Assert
        assert title == "统计各部门季度销售额与同比增长情况"[:20]

    # 方法作用：验证规划失败写入可读原因、切成 plan_failed 并发出失败通知。
    # Args: self - 测试实例；monkeypatch - 补丁工具。
    # Returns: 无返回值。
    async def test_plan_worker_failure_marks_plan_failed(self, monkeypatch) -> None:
        """解析异常必须落成 plan_failed 与可读原因，而不是停在生成中。"""
        # Arrange
        store = ReportStore(memory=True)
        analyst = SimpleNamespace(tenant_id=3, user_id=7, role="analyst")
        report_id = await _make_report(store, analyst, "planning")
        worker = _real_worker(monkeypatch, store, error=ValueError("报表描述不能为空"))
        notifications = _fake_notifications(monkeypatch)

        # Act
        result = await worker.plan_report(report_id, analyst)

        # Assert
        assert result["status"] == "plan_failed"
        row = store._jobs[report_id]
        assert row["status"] == "plan_failed" and row["plan_error"] == "报表描述不能为空"
        # 计划失败同样属个人通知，失败语义由标题与正文承载。
        assert [item["kind"] for item in notifications] == ["personal"]
        assert "失败" in notifications[0]["title"]
        assert notifications[0]["body"] == "报表描述不能为空"

    # 方法作用：验证数据源授权异常的可读文案被写入失败原因。
    # Args: self - 测试实例；monkeypatch - 补丁工具。
    # Returns: 无返回值。
    async def test_plan_worker_maps_http_detail_to_reason(self, monkeypatch) -> None:
        """授权拒绝必须展示 HTTPException.detail，而不是通用兜底文案。"""
        # Arrange
        store = ReportStore(memory=True)
        analyst = SimpleNamespace(tenant_id=3, user_id=7, role="analyst")
        report_id = await _make_report(store, analyst, "planning")
        worker = _real_worker(monkeypatch, store, _plan())
        _fake_notifications(monkeypatch)
        monkeypatch.setattr(ReportPlanWorker, "_resolve_access", AsyncMock(side_effect=HTTPException(403, "无权使用计划中的数据源")))

        # Act
        result = await worker.plan_report(report_id, analyst)

        # Assert
        assert result["reason"] == "无权使用计划中的数据源"
        assert store._jobs[report_id]["plan_error"] == "无权使用计划中的数据源"

    # 方法作用：验证未知异常使用通用兜底文案，不泄露堆栈或连接细节。
    # Args: self - 测试实例；monkeypatch - 补丁工具。
    # Returns: 无返回值。
    async def test_plan_worker_hides_internal_failure(self, monkeypatch) -> None:
        """未知异常只给通用文案，内部信息留在日志。"""
        # Arrange
        store = ReportStore(memory=True)
        analyst = SimpleNamespace(tenant_id=3, user_id=7, role="analyst")
        report_id = await _make_report(store, analyst, "planning")
        worker = _real_worker(monkeypatch, store, error=ConnectionError("postgres://user:pass@host 拒绝连接"))
        _fake_notifications(monkeypatch)

        # Act
        result = await worker.plan_report(report_id, analyst)

        # Assert
        assert result["reason"] == "计划生成失败，请稍后重试"
        assert "postgres" not in store._jobs[report_id]["plan_error"]

    # 方法作用：验证通知服务缺失时规划仍然成功。
    # Args: self - 测试实例；monkeypatch - 补丁工具。
    # Returns: 无返回值。
    async def test_plan_worker_survives_missing_notification_service(self, monkeypatch) -> None:
        """通知模块尚未落地时只能记 warning，不能阻塞计划链路。"""
        # Arrange
        store = ReportStore(memory=True)
        analyst = SimpleNamespace(tenant_id=3, user_id=7, role="analyst")
        report_id = await _make_report(store, analyst, "planning", prompt="季度销售分析")
        worker = _real_worker(monkeypatch, store, _plan("季度销售分析"))
        monkeypatch.setattr("src.llm.client.is_task_llm_available", lambda task: False)
        # 删除假通知模块，模拟 src/notifications/service.py 还不存在。
        monkeypatch.delitem(sys.modules, "src.notifications.service", raising=False)
        monkeypatch.delitem(sys.modules, "src.notifications", raising=False)

        # Act
        result = await worker.plan_report(report_id, analyst)

        # Assert
        assert result["status"] == "draft"
        assert store._jobs[report_id]["status"] == "draft"


class TestReportListApi:
    """覆盖 GET /reports：可见范围、筛选、排序与字段完整性。"""

    # 方法作用：验证非管理员只能看到自己的报表且按创建时间倒序。
    # Args: self - 测试实例；monkeypatch - 补丁工具。
    # Returns: 无返回值。
    async def test_list_reports_scoped_to_owner(self, monkeypatch) -> None:
        """analyst 传别人的 user_id 返回 403，自己的列表不含他人记录。"""
        # Arrange
        store = ReportStore(memory=True)
        owner = SimpleNamespace(tenant_id=3, user_id=7, role="analyst")
        peer = SimpleNamespace(tenant_id=3, user_id=8, role="analyst")
        older = await _make_report(store, owner, prompt="旧报表")
        newer = await _make_report(store, owner, prompt="新报表")
        await _make_report(store, peer, prompt="他人报表")
        store._jobs[older]["created_at"] = datetime.now(timezone.utc) - timedelta(hours=2)
        store._jobs[newer]["created_at"] = datetime.now(timezone.utc)
        _bind(monkeypatch, store, owner)

        # Act
        async with _client() as client:
            listed = await client.get(f"{PREFIX}/reports")
            forbidden = await client.get(f"{PREFIX}/reports", params={"user_id": 8})

        # Assert
        assert listed.status_code == 200
        body = listed.json()
        assert body["total"] == 2
        assert [item["id"] for item in body["reports"]] == [newer, older]
        assert forbidden.status_code == 403

    # 方法作用：验证租户管理员可按用户筛选并能看到全租户记录。
    # Args: self - 测试实例；monkeypatch - 补丁工具。
    # Returns: 无返回值。
    async def test_list_reports_admin_filters_by_user(self, monkeypatch) -> None:
        """管理员不传 user_id 看全租户，传了只看目标用户。"""
        # Arrange
        store = ReportStore(memory=True)
        admin = SimpleNamespace(tenant_id=3, user_id=1, role="tenant_admin")
        peer = SimpleNamespace(tenant_id=3, user_id=8, role="analyst")
        await _make_report(store, admin, prompt="管理员报表")
        await _make_report(store, peer, prompt="成员报表")
        _bind(monkeypatch, store, admin)

        # Act
        async with _client() as client:
            all_rows = await client.get(f"{PREFIX}/reports")
            filtered = await client.get(f"{PREFIX}/reports", params={"user_id": 8})

        # Assert
        assert all_rows.json()["total"] == 2
        assert filtered.json()["total"] == 1
        assert filtered.json()["reports"][0]["user_id"] == 8

    # 方法作用：验证列表响应字段齐全，并带出状态筛选与最新页面标识。
    # Args: self - 测试实例；monkeypatch - 补丁工具。
    # Returns: 无返回值。
    async def test_list_reports_contains_required_columns(self, monkeypatch) -> None:
        """7 列字段必须齐全，且 status 筛选与 latest_report_id 可被前端直接消费。"""
        # Arrange
        store = ReportStore(memory=True)
        owner = SimpleNamespace(tenant_id=3, user_id=7, role="analyst")
        report_id = await _make_report(store, owner, prompt="季度销售分析")
        version = await store.persist_version(report_id, "run-1", owner, {"report_id": report_id}, {}, None)
        _bind(monkeypatch, store, owner)

        # Act
        async with _client() as client:
            listed = await client.get(f"{PREFIX}/reports")
            filtered = await client.get(f"{PREFIX}/reports", params={"status": "plan_failed"})

        # Assert
        item = listed.json()["reports"][0]
        assert LIST_FIELDS.issubset(item.keys())
        assert item["id"] == report_id and item["status"] == "draft"
        assert item["datasources"] == ["sales"] and item["divergence"] == "standard"
        assert item["runs_started"] == 0 and item["latest_report_id"] == version.id
        assert filtered.json()["total"] == 0


class TestReportDetailApi:
    """覆盖 GET /reports/{report_id} 详情与页面列表。"""

    # 方法作用：验证详情返回计划、运行、页面列表，未过期页面同时带出正文。
    # Args: self - 测试实例；monkeypatch - 补丁工具。
    # Returns: 无返回值。
    async def test_report_detail_exposes_plan_and_pages(self, monkeypatch) -> None:
        """详情必须给出计划与页面摘要，页面正文兼容旧查看页。"""
        # Arrange
        store = ReportStore(memory=True)
        owner = SimpleNamespace(tenant_id=3, user_id=7, role="analyst")
        report_id = await _make_report(store, owner, prompt="季度销售分析")
        first = await store.persist_version(report_id, "run-1", owner, {"report_id": report_id, "blocks": []}, {}, None)
        second = await store.persist_version(report_id, "run-2", owner, {"report_id": report_id, "blocks": []}, {}, None)
        _bind(monkeypatch, store, owner)

        # Act
        async with _client() as client:
            detail = await client.get(f"{PREFIX}/reports/{report_id}")
            pages = await client.get(f"{PREFIX}/reports/{report_id}/pages")

        # Assert
        assert detail.status_code == pages.status_code == 200
        body = detail.json()
        assert body["report"]["id"] == report_id
        assert body["plan_error"] == "" and body["report"]["plan_error"] == ""
        assert body["plan"]["analysis_units"][0]["unit_id"] == "sales_trend"
        assert body["latest_report"]["id"] == second.id
        assert [item["id"] for item in body["pages"]] == [second.id, first.id]
        assert body["document"]["report_id"] == report_id
        assert pages.json()["total"] == 2
        assert pages.json()["pages"][0]["version"] == 2 and pages.json()["pages"][0]["is_latest"] is True

    # 方法作用：验证详情遵循归属校验，未授权用户拿不到别人的报表。
    # Args: self - 测试实例；monkeypatch - 补丁工具。
    # Returns: 无返回值。
    async def test_report_detail_enforces_ownership(self, monkeypatch) -> None:
        """非拥有者且无 report.view_all 时返回 404。"""
        # Arrange
        store = ReportStore(memory=True)
        owner = SimpleNamespace(tenant_id=3, user_id=7, role="analyst")
        peer = SimpleNamespace(tenant_id=3, user_id=8, role="analyst")
        report_id = await _make_report(store, owner)
        _bind(monkeypatch, store, peer)

        # Act
        async with _client() as client:
            response = await client.get(f"{PREFIX}/reports/{report_id}")

        # Assert
        assert response.status_code == 404


class TestReportPatchApi:
    """覆盖 PATCH /reports/{report_id}：只允许删减单元与约束重校验。"""

    # 方法作用：验证编辑只删减分析单元并重算计划哈希。
    # Args: self - 测试实例；monkeypatch - 补丁工具。
    # Returns: 无返回值。
    async def test_patch_trims_units_and_recomputes_hash(self, monkeypatch) -> None:
        """保留一个单元后计划只剩一个，被删单元进入 excluded_unit_ids。"""
        # Arrange
        store = ReportStore(memory=True)
        owner = SimpleNamespace(tenant_id=3, user_id=7, role="analyst")
        plan = _plan()
        report_id = await _make_report(store, owner, plan=plan)
        _bind(monkeypatch, store, owner)

        # Act
        async with _client() as client:
            response = await client.patch(f"{PREFIX}/reports/{report_id}", json={"accepted_unit_ids": ["sales_trend"], "page_ttl_days": 3})

        # Assert
        assert response.status_code == 200
        body = response.json()
        assert [unit["unit_id"] for unit in body["plan"]["analysis_units"]] == ["sales_trend"]
        assert body["plan"]["excluded_unit_ids"] == ["sales_region"]
        stored = store._jobs[report_id]
        assert stored["plan_hash"] != plan.canonical_hash()
        assert ReportPlan.model_validate(stored["plan_json"]).canonical_hash() == stored["plan_hash"]
        # 调度列必须与计划同步落库，报表信息列表才能直接出列。
        assert stored["page_ttl_days"] == 3 and stored["execution_mode"] == "immediate" and stored["next_run_at"] is None

    # 方法作用：验证越界单元标识被拒且记录保持原样。
    # Args: self - 测试实例；monkeypatch - 补丁工具。
    # Returns: 无返回值。
    async def test_patch_rejects_unknown_unit_ids(self, monkeypatch) -> None:
        """不属于计划的单元标识必须 422，不能凭空构造执行范围。"""
        # Arrange
        store = ReportStore(memory=True)
        owner = SimpleNamespace(tenant_id=3, user_id=7, role="analyst")
        report_id = await _make_report(store, owner)
        _bind(monkeypatch, store, owner)

        # Act
        async with _client() as client:
            response = await client.patch(f"{PREFIX}/reports/{report_id}", json={"accepted_unit_ids": ["ghost"]})

        # Assert
        assert response.status_code == 422
        assert len(store._jobs[report_id]["plan_json"]["analysis_units"]) == 2

    # 方法作用：验证非 draft 状态不可编辑。
    # Args: self - 测试实例；monkeypatch - 补丁工具。
    # Returns: 无返回值。
    async def test_patch_conflicts_when_not_draft(self, monkeypatch) -> None:
        """已排队或已运行的报表返回 409。"""
        # Arrange
        store = ReportStore(memory=True)
        owner = SimpleNamespace(tenant_id=3, user_id=7, role="analyst")
        report_id = await _make_report(store, owner, "queued")
        _bind(monkeypatch, store, owner)

        # Act
        async with _client() as client:
            response = await client.patch(f"{PREFIX}/reports/{report_id}", json={"page_ttl_days": 3})

        # Assert
        assert response.status_code == 409

    # 方法作用：验证租户管理员能看但不能改成员的报表计划。
    # Args: self - 测试实例；monkeypatch - 补丁工具。
    # Returns: 无返回值。
    async def test_patch_rejects_non_owner_admin(self, monkeypatch) -> None:
        """写操作只属于创建者，与数据库 RLS 的 WITH CHECK 约束保持一致。"""
        # Arrange
        store = ReportStore(memory=True)
        owner = SimpleNamespace(tenant_id=3, user_id=7, role="analyst")
        admin = SimpleNamespace(tenant_id=3, user_id=1, role="tenant_admin")
        report_id = await _make_report(store, owner)
        _bind(monkeypatch, store, admin)

        # Act
        async with _client() as client:
            response = await client.patch(f"{PREFIX}/reports/{report_id}", json={"page_ttl_days": 3})

        # Assert
        assert response.status_code == 403
        assert store._jobs[report_id]["page_ttl_days"] == 7

    # 方法作用：验证改成定时执行必须持有 report.schedule 能力。
    # Args: self - 测试实例；monkeypatch - 补丁工具。
    # Returns: 无返回值。
    async def test_patch_scheduled_requires_capability(self, monkeypatch) -> None:
        """analyst 默认没有 report.schedule，改定时必须 403。"""
        # Arrange
        store = ReportStore(memory=True)
        owner = SimpleNamespace(tenant_id=3, user_id=7, role="analyst")
        report_id = await _make_report(store, owner)
        _bind(monkeypatch, store, owner)

        # Act
        async with _client() as client:
            response = await client.patch(f"{PREFIX}/reports/{report_id}", json={"execution_mode": "scheduled", "frequency": "daily"})

        # Assert
        assert response.status_code == 403
        assert store._jobs[report_id]["execution_mode"] == "immediate"


class TestReportRunApi:
    """覆盖 POST /reports/{report_id}/run：预占额度与入队。"""

    # 方法作用：验证点「生成」才预占 jobs_created 并排队运行。
    # Args: self - 测试实例；monkeypatch - 补丁工具。
    # Returns: 无返回值。
    async def test_run_reserves_jobs_quota(self, monkeypatch) -> None:
        """运行前余额为 N，运行后必须为 N-1，且状态变为 queued。"""
        # Arrange
        import src.reporting.runner as runner_module

        store = ReportStore(memory=True)
        owner = SimpleNamespace(tenant_id=3, user_id=7, role="analyst")
        quota = QuotaManager(store=store)
        report_id = await _make_report(store, owner)
        runner = SimpleNamespace(enqueue=lambda *args, **kwargs: None)
        _bind(monkeypatch, store, owner, quota=quota)
        monkeypatch.setattr(route, "_resolve_plan_access", AsyncMock(return_value={"sales": {}}))
        monkeypatch.setattr(runner_module, "get_report_runner", lambda: runner)
        enqueued: list[dict[str, Any]] = []
        monkeypatch.setattr(runner, "enqueue", lambda *args, **kwargs: enqueued.append(kwargs))

        # Act
        async with _client() as client:
            before = await client.get(f"{PREFIX}/quotas/me")
            response = await client.post(f"{PREFIX}/reports/{report_id}/run")
            after = await client.get(f"{PREFIX}/quotas/me")

        # Assert
        assert response.status_code == 202
        assert before.json()["jobs_remaining"] - after.json()["jobs_remaining"] == 1
        assert response.json()["status"] == "queued" and response.json()["execution_mode"] == "immediate"
        assert store._jobs[report_id]["status"] == "queued"
        assert enqueued and enqueued[0]["authorized_datasources"] == {"sales": {}}

    # 方法作用：验证额度不足时返回 429 且报表状态保持不变。
    # Args: self - 测试实例；monkeypatch - 补丁工具。
    # Returns: 无返回值。
    async def test_run_rejected_when_quota_exhausted(self, monkeypatch) -> None:
        """预占失败必须 429，且记录仍停在「未生成」。"""
        # Arrange
        store = ReportStore(memory=True)
        owner = SimpleNamespace(tenant_id=3, user_id=7, role="analyst")
        quota = QuotaManager(store=store)
        quota.set_policy(3, 7, QuotaPolicy(max_jobs_created=0))
        report_id = await _make_report(store, owner)
        _bind(monkeypatch, store, owner, quota=quota)
        monkeypatch.setattr(route, "_resolve_plan_access", AsyncMock(return_value={}))

        # Act
        async with _client() as client:
            response = await client.post(f"{PREFIX}/reports/{report_id}/run")

        # Assert
        assert response.status_code == 429
        assert store._jobs[report_id]["status"] == "draft"

    # 方法作用：验证非 draft 状态不能运行。
    # Args: self - 测试实例；monkeypatch - 补丁工具。
    # Returns: 无返回值。
    async def test_run_conflicts_when_not_draft(self, monkeypatch) -> None:
        """计划生成中的记录返回 409。"""
        # Arrange
        store = ReportStore(memory=True)
        owner = SimpleNamespace(tenant_id=3, user_id=7, role="analyst")
        report_id = await _make_report(store, owner, "planning")
        _bind(monkeypatch, store, owner)

        # Act
        async with _client() as client:
            response = await client.post(f"{PREFIX}/reports/{report_id}/run")

        # Assert
        assert response.status_code == 409


class TestReportListUsername:
    """覆盖报表列表返回创建者用户名。"""

    # 方法作用：验证列表项带出创建者用户名。
    # Args: self - 测试实例。
    # Returns: 无返回值。
    async def test_list_returns_creator_username(self) -> None:
        """列表只带 user_id 时，管理员认不出哪条报表是谁的。"""
        # Arrange
        store = ReportStore(memory=True)
        owner = SimpleNamespace(tenant_id=3, user_id=7, role="analyst", username="张三")
        await _make_report(store, owner, "draft")

        # Act
        rows, _total = await store.list_reports(owner, view_all=True)

        # Assert
        assert rows[0]["username"] == "张三"

    # 方法作用：验证 PostgreSQL 分支真的 JOIN 了 users 表。
    # Args: self - 测试实例。
    # Returns: 无返回值。
    def test_pg_list_query_joins_users(self) -> None:
        """只改内存分支会让两种部署方式表现不一致，SQL 必须带出用户名。"""
        # Arrange
        source = Path("src/reporting/store.py").read_text(encoding="utf-8")

        # Act / Assert
        assert "LEFT JOIN users u ON u.id=j.user_id" in source
        assert "u.username AS username" in source


class TestRetryAndDeleteApi:
    """覆盖 retry-plan 与删除的状态约束。"""

    # 方法作用：验证只有 plan_failed 可以重试并重新入队规划。
    # Args: self - 测试实例；monkeypatch - 补丁工具。
    # Returns: 无返回值。
    async def test_retry_plan_only_for_plan_failed(self, monkeypatch) -> None:
        """计划失败可重试，草稿状态返回 409。"""
        # Arrange
        store = ReportStore(memory=True)
        owner = SimpleNamespace(tenant_id=3, user_id=7, role="analyst")
        failed = await _make_report(store, owner, "plan_failed")
        draft = await _make_report(store, owner, "draft")
        worker = _bind(monkeypatch, store, owner)

        # Act
        async with _client() as client:
            retried = await client.post(f"{PREFIX}/reports/{failed}/retry-plan")
            rejected = await client.post(f"{PREFIX}/reports/{draft}/retry-plan")

        # Assert
        assert retried.status_code == 202 and retried.json()["status"] == "planning"
        assert store._jobs[failed]["status"] == "planning" and store._jobs[failed]["plan_error"] == ""
        assert rejected.status_code == 409
        assert [item["report_id"] for item in worker.enqueued] == [failed]

    # 方法作用：验证卡死的「计划生成中」可以中断并重新生成。
    # Args: self - 测试实例；monkeypatch - 补丁工具。
    # Returns: 无返回值。
    async def test_retry_plan_allows_stuck_planning(self, monkeypatch) -> None:
        """服务重启会丢掉进程内的后台任务，记录却停在 planning——既不能重试也不能删除。"""
        # Arrange
        from datetime import datetime, timedelta, timezone

        from src.reporting.models import PLAN_STUCK_SECONDS

        store = ReportStore(memory=True)
        owner = SimpleNamespace(tenant_id=3, user_id=7, role="analyst")
        stuck = await _make_report(store, owner, "planning")
        store._jobs[stuck]["updated_at"] = datetime.now(timezone.utc) - timedelta(seconds=PLAN_STUCK_SECONDS + 60)
        worker = _bind(monkeypatch, store, owner)

        # Act
        async with _client() as client:
            response = await client.post(f"{PREFIX}/reports/{stuck}/retry-plan")

        # Assert
        assert response.status_code == 202 and response.json()["status"] == "planning"
        assert store._jobs[stuck]["plan_error"] == ""
        assert [item["report_id"] for item in worker.enqueued] == [stuck]

    # 方法作用：验证仍在正常生成的 planning 不会被打断。
    # Args: self - 测试实例；monkeypatch - 补丁工具。
    # Returns: 无返回值。
    async def test_retry_plan_rejects_fresh_planning(self, monkeypatch) -> None:
        """刚入队还在生成的任务若被误重试，会打断正常的模型拆解。"""
        # Arrange
        store = ReportStore(memory=True)
        owner = SimpleNamespace(tenant_id=3, user_id=7, role="analyst")
        fresh = await _make_report(store, owner, "planning")
        worker = _bind(monkeypatch, store, owner)

        # Act
        async with _client() as client:
            response = await client.post(f"{PREFIX}/reports/{fresh}/retry-plan")

        # Assert
        assert response.status_code == 409
        assert worker.enqueued == []

    # 方法作用：验证运行中的报表不可删除，草稿可删除。
    # Args: self - 测试实例；monkeypatch - 补丁工具。
    # Returns: 无返回值。
    async def test_delete_report_blocks_running_records(self, monkeypatch) -> None:
        """planning/queued/running 返回 409，draft 删除后列表不再出现。"""
        # Arrange
        store = ReportStore(memory=True)
        owner = SimpleNamespace(tenant_id=3, user_id=7, role="analyst")
        planning = await _make_report(store, owner, "planning")
        draft = await _make_report(store, owner, "draft")
        _bind(monkeypatch, store, owner)

        # Act
        async with _client() as client:
            blocked = await client.delete(f"{PREFIX}/reports/{planning}")
            deleted = await client.delete(f"{PREFIX}/reports/{draft}")
            listed = await client.get(f"{PREFIX}/reports")

        # Assert
        assert blocked.status_code == 409
        assert deleted.status_code == 200 and deleted.json()["status"] == "deleted"
        assert [item["id"] for item in listed.json()["reports"]] == [planning]

    # 方法作用：验证租户管理员不能删除成员的报表记录。
    # Args: self - 测试实例；monkeypatch - 补丁工具。
    # Returns: 无返回值。
    async def test_delete_rejects_non_owner_admin(self, monkeypatch) -> None:
        """删除同样只属于创建者，管理员只能查看。"""
        # Arrange
        store = ReportStore(memory=True)
        owner = SimpleNamespace(tenant_id=3, user_id=7, role="analyst")
        admin = SimpleNamespace(tenant_id=3, user_id=1, role="tenant_admin")
        report_id = await _make_report(store, owner, "draft")
        _bind(monkeypatch, store, admin)

        # Act
        async with _client() as client:
            response = await client.delete(f"{PREFIX}/reports/{report_id}")

        # Assert
        assert response.status_code == 403
        assert store._jobs[report_id]["status"] == "draft"


class TestCapabilityKeys:
    """覆盖设计 48 第 7.1 节的新增能力键。"""

    # 方法作用：验证三个子页能力键按角色正确下发。
    # Args: self - 测试实例。
    # Returns: 无返回值。
    async def test_menu_capability_keys_by_role(self) -> None:
        """analyst 可创建与查看，viewer 只能查看报表信息。"""
        # Arrange
        service = ReportPermissionService()
        analyst = SimpleNamespace(tenant_id=3, user_id=7, role="analyst")
        viewer = SimpleNamespace(tenant_id=3, user_id=9, role="viewer")

        # Act
        analyst_caps = set(await service.capabilities(analyst))
        viewer_caps = set(await service.capabilities(viewer))

        # Assert
        assert {"report.create_page", "report.list", "report.link_manage"}.issubset(analyst_caps)
        # viewer 只有 report.list 这一个新键，同时保留 report.menu 以显示父菜单。
        assert {"report.create_page", "report.list", "report.link_manage"} & viewer_caps == {"report.list"}
        assert "report.menu" in viewer_caps
