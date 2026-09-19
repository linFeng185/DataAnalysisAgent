"""报表执行明细 API 测试，覆盖 23.35：运行级 LLM/SQL 调用次数与输入输出 token 拆分。"""

from __future__ import annotations

from types import SimpleNamespace

import httpx
import pytest
from fastapi import FastAPI

import src.api.routes.report_workbench as route
from src.reporting.models import AnalysisUnit, ReportPlan
from src.reporting.quota import ReportPermissionService
from src.reporting.snapshots import UnitSnapshot
from src.reporting.store import ReportStore


# 方法作用：构造使用共享内存 Store 的测试应用，并接管身份与权限解析。
# Args: monkeypatch - pytest 补丁工具。
# Returns: (owner 身份, store, 挂载了报表路由的 FastAPI 应用)。
@pytest.fixture
def run_detail_env(monkeypatch):
    """执行明细用例共用同一内存 Store：单测不得访问真实数据库或模型。"""
    owner = SimpleNamespace(tenant_id=3, user_id=7, role="analyst")
    store = ReportStore(memory=True)
    service = ReportPermissionService(store)
    monkeypatch.setattr(route, "get_current_identity", lambda: owner)
    monkeypatch.setattr(route, "get_report_store", lambda: store)
    monkeypatch.setattr(route, "get_report_permission_service", lambda: service)
    app = FastAPI()
    app.include_router(route.router)
    return owner, store, app


# 方法作用：构造 httpx 客户端，屏蔽 ASGI 传输细节。
# Args: app - 挂载了报表路由的 FastAPI 应用。
# Returns: httpx.AsyncClient 实例。
def _client(app: FastAPI) -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test")


# 方法作用：创建一个报表任务并预置一次运行，返回任务与运行标识。
# Args: store - 内存 Store；owner - 归属身份；title - 任务标题；stats - 运行计数更新字段。
# Returns: (job, run)。
async def _seed_run(store: ReportStore, owner, *, title: str, stats: dict | None = None):
    job = await store.create_job(
        owner,
        "生成经营报表",
        ReportPlan(analysis_units=[AnalysisUnit(unit_id="sales", question="销售分析")]),
    )
    # 标题平时由后台规划写回，单测直接落到内存行，避免触发真实模型调用。
    store._jobs[job.id]["title"] = title
    run = await store.create_run(job.id, owner)
    if stats:
        await store.update_run(run.id, owner, **stats)
    return job, run


class TestReportRunDetailApi:
    """覆盖功能 23.35 的执行明细列表、鉴权、过滤与单元快照。"""

    # 方法作用：验证执行明细返回 LLM/SQL 调用次数与输入输出 token 拆分。
    # Args: self - 测试实例；run_detail_env - 内存 Store 环境。
    # Returns: 无返回值。
    async def test_lists_llm_sql_counts_and_split_tokens(self, run_detail_env) -> None:
        """明细行必须给出成功/失败次数和输入、输出 token，而不只是总量。"""
        # Arrange
        owner, store, app = run_detail_env
        store._capabilities[(3, 7)] = ["report.list"]
        _job, run = await _seed_run(
            store,
            owner,
            title="集团经营全景分析报表",
            stats={
                "status": "succeeded",
                "llm_call_count": 42,
                "llm_failure_count": 3,
                "sql_query_count": 21,
                "sql_success_count": 20,
                "sql_failure_count": 1,
                "token_usage": 3000,
                "prompt_token_usage": 2400,
                "completion_token_usage": 600,
            },
        )

        # Act
        async with _client(app) as client:
            response = await client.get("/report-workbench/runs")

        # Assert
        assert response.status_code == 200
        body = response.json()
        assert body["total"] == 1
        row = body["runs"][0]
        assert row["id"] == run.id
        assert row["stats"] == {
            "llm_call_count": 42,
            "llm_failure_count": 3,
            "llm_success_count": 39,
            "llm_token_usage": 3000,
            "llm_prompt_tokens": 2400,
            "llm_completion_tokens": 600,
            "sql_query_count": 21,
            "sql_success_count": 20,
            "sql_failure_count": 1,
            "estimated_query_count": 0,
        }
        # 所属任务标题随行返回，明细页无需按行回查任务。
        assert row["job_title"] == "集团经营全景分析报表"

    # 方法作用：验证缺少 report.list 能力时执行明细返回 403。
    # Args: self - 测试实例；run_detail_env - 内存 Store 环境。
    # Returns: 无返回值。
    async def test_requires_report_list_capability(self, run_detail_env) -> None:
        """未授权身份不得读到任何执行计数。"""
        # Arrange
        owner, store, app = run_detail_env
        store._capabilities[(3, 7)] = ["report.menu"]
        await _seed_run(store, owner, title="报表", stats={"status": "succeeded"})

        # Act
        async with _client(app) as client:
            response = await client.get("/report-workbench/runs")

        # Assert
        assert response.status_code == 403

    # 方法作用：验证按任务与状态过滤只返回命中行，且分页总数是过滤后的全量。
    # Args: self - 测试实例；run_detail_env - 内存 Store 环境。
    # Returns: 无返回值。
    async def test_filters_by_job_and_status_with_total(self, run_detail_env) -> None:
        """过滤条件必须作用在分页之前，否则总数会误导分页器。"""
        # Arrange
        owner, store, app = run_detail_env
        store._capabilities[(3, 7)] = ["report.list"]
        job_a, run_a = await _seed_run(store, owner, title="甲报表", stats={"status": "succeeded"})
        _job_b, _run_b = await _seed_run(store, owner, title="乙报表", stats={"status": "failed"})
        _job_c, _run_c = await _seed_run(store, owner, title="丙报表", stats={"status": "succeeded"})

        # Act
        async with _client(app) as client:
            by_job = await client.get(f"/report-workbench/runs?job_id={job_a.id}")
            by_status = await client.get("/report-workbench/runs?status=succeeded")
            paged = await client.get("/report-workbench/runs?page=1&page_size=2")

        # Assert
        assert by_job.status_code == by_status.status_code == paged.status_code == 200
        assert by_job.json()["total"] == 1
        assert by_job.json()["runs"][0]["id"] == run_a.id
        assert by_status.json()["total"] == 2
        paged_body = paged.json()
        assert paged_body["total"] == 3
        assert len(paged_body["runs"]) == 2
        assert paged_body["page_size"] == 2

    # 方法作用：验证执行明细不返回其他用户（同租户）的运行记录。
    # Args: self - 测试实例；run_detail_env - 内存 Store 环境。
    # Returns: 无返回值。
    async def test_hides_runs_owned_by_other_users(self, run_detail_env) -> None:
        """同租户的普通成员也只能看到自己的运行记录。"""
        # Arrange
        owner, store, app = run_detail_env
        store._capabilities[(3, 7)] = ["report.list"]
        await _seed_run(store, owner, title="自己的报表", stats={"status": "succeeded"})
        other = SimpleNamespace(tenant_id=3, user_id=99, role="analyst")
        await _seed_run(store, other, title="他人的报表", stats={"status": "succeeded"})

        # Act
        async with _client(app) as client:
            response = await client.get("/report-workbench/runs")

        # Assert
        assert response.status_code == 200
        assert response.json()["total"] == 1
        assert response.json()["runs"][0]["job_title"] == "自己的报表"

    # 方法作用：验证运行详情返回单元快照、剥离提示词正文并裁剪超长 SQL。
    # Args: self - 测试实例；run_detail_env - 内存 Store 环境。
    # Returns: 无返回值。
    async def test_run_detail_exposes_units_without_prompt_text(self, run_detail_env) -> None:
        """单元快照用于定位是哪个单元失败的，必须带数据源与模型但不带提示词全文。"""
        # Arrange
        owner, store, app = run_detail_env
        store._capabilities[(3, 7)] = ["report.list"]
        job, run = await _seed_run(store, owner, title="单元明细报表", stats={"status": "partial"})
        long_sql = "SELECT " + ", ".join(f"col_{index}" for index in range(2000)) + " FROM orders"
        await store.save_unit_snapshot(
            UnitSnapshot(
                id="snap-1",
                run_id=run.id,
                report_id=job.id,
                unit_id="sales",
                status="success",
                datasource="postgres",
                dialect="postgres",
                sql_text=long_sql,
                prompt_text="内部提示词全文",
                prompt_id="report.unit",
                model_id="qwen3-32b",
                row_count=12,
                full_count=12,
                truncated=False,
                elapsed_ms=850,
            ),
            owner,
        )

        # Act
        async with _client(app) as client:
            response = await client.get(f"/report-workbench/runs/{run.id}")

        # Assert
        assert response.status_code == 200
        body = response.json()
        assert body["run"]["stats"]["llm_call_count"] == 0
        assert len(body["units"]) == 1
        unit = body["units"][0]
        assert unit["unit_id"] == "sales"
        assert unit["datasource"] == "postgres"
        assert unit["model_id"] == "qwen3-32b"
        assert unit["elapsed_ms"] == 850
        assert "prompt_text" not in unit
        assert unit["sql_truncated"] is True
        assert len(unit["sql_text"]) < len(long_sql)
        assert "内部提示词全文" not in response.text
