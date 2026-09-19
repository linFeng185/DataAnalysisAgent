# tests/test_api/test_report_live_template_parse.py
"""实时钻取模板不可解析时的接口契约：返回 4xx，不得变成 500。"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock

import httpx
from fastapi import FastAPI
from sqlglot.errors import ParseError

import src.api.routes.report_workbench as route
from src.reporting.models import AnalysisUnit, ReportPlan
from src.reporting.quota import QuotaManager, ReportPermissionService
from src.reporting.store import ReportStore

# 多源合并文本的真实形态：两段来源头 + 各自语句，sqlglot 无法当单条 SQL 解析。
_MERGED_SQL = "-- oracle\nSELECT sales FROM orders\n\n-- ch\nSELECT attributed FROM attribution"


class TestDrilldownTemplateParseFailure:
    """覆盖 drilldown 端点对坏模板的 4xx 契约与单源正常路径。"""

    # 方法作用：搭建带私有模板的报表页面，返回 (store, quota, owner) 三元组。
    # Args: sql - 存入 report_live_queries 的模板 SQL；datasource - 模板所属数据源；dialect - 模板方言。
    # Returns: 内存 Store、配额管理器和执行身份。
    async def _fixture(self, sql: str, datasource: str = "oracle", dialect: str = "mysql", columns: list[str] | None = None):
        owner = SimpleNamespace(tenant_id=3, user_id=7, role="tenant_admin")
        store = ReportStore(memory=True)
        quota = QuotaManager(store=store)
        job = await store.create_job(owner, "销售", ReportPlan(analysis_units=[AnalysisUnit(unit_id="sales", question="销售")]))
        run = await store.create_run(job.id, owner)
        query_id = await store.save_live_query(run.id, owner, datasource, dialect, sql)
        definition = {"drilldown_id": "live", "mode": "live", "query_ref": query_id, "columns": list(columns or ["sales"]), "allowed_filters": []}
        await store.persist_version(job.id, run.id, owner, {"report_id": "report", "permissions": {"drilldown": True}, "drilldowns": [definition]}, {}, None)
        return store, quota, owner

    # 方法作用：把路由挂到测试客户端上，并替换身份、Store、配额与数据源授权解析。
    # Args: self - 测试实例；monkeypatch - 补丁工具；store - 内存 Store；quota - 配额管理器；owner - 执行身份；access - 数据源授权映射。
    # Returns: 无返回值；客户端由调用方在 with 中创建。
    def _patch_route(self, monkeypatch, store, quota, owner, access) -> None:
        monkeypatch.setattr(route, "get_current_identity", lambda: owner)
        monkeypatch.setattr(route, "get_report_store", lambda: store)
        monkeypatch.setattr(route, "get_report_quota_manager", lambda: quota)
        monkeypatch.setattr(route, "get_report_permission_service", lambda: ReportPermissionService(store))
        monkeypatch.setattr(route, "_resolve_plan_access", AsyncMock(return_value=access))

    # 方法作用：验证历史签发的多段合并模板被点击时返回 422 且不触碰数据库。
    # Args: self - 测试实例；monkeypatch - 补丁工具。
    # Returns: 无返回值。
    async def test_merged_template_returns_422_not_500(self, monkeypatch) -> None:
        """模板不可解析是可预期的数据问题：4xx、可读信息、零次真实执行。"""
        import src.security.sql_execution as execution

        store, quota, owner = await self._fixture(_MERGED_SQL)
        self._patch_route(monkeypatch, store, quota, owner, {"oracle": {}})
        executor = AsyncMock()
        monkeypatch.setattr(execution, "validate_and_execute_sql", executor)
        app = FastAPI()
        app.include_router(route.router)

        transport = httpx.ASGITransport(app=app, raise_app_exceptions=False)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            response = await client.post("/report-workbench/reports/report/drilldowns/live", json={})

        assert response.status_code != 500
        assert response.status_code == 422
        assert "模板" in response.json()["detail"]
        executor.assert_not_awaited()

    # 方法作用：验证直接冒出的 ParseError 也在路由边界被映射为 4xx。
    # Args: self - 测试实例；monkeypatch - 补丁工具。
    # Returns: 无返回值。
    async def test_raw_parse_error_is_mapped_to_422(self, monkeypatch) -> None:
        """sqlglot.errors.ParseError 不是 ValueError，边界必须显式兜住，不能退化成 500。"""
        import src.reporting.live_drilldown as live

        store, quota, owner = await self._fixture("SELECT sales FROM orders")
        self._patch_route(monkeypatch, store, quota, owner, {"oracle": {}})
        monkeypatch.setattr(live, "build_live_sql", lambda *args, **kwargs: (_ for _ in ()).throw(ParseError("Found multiple 'WITH' clauses")))
        app = FastAPI()
        app.include_router(route.router)

        transport = httpx.ASGITransport(app=app, raise_app_exceptions=False)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            response = await client.post("/report-workbench/reports/report/drilldowns/live", json={})

        assert response.status_code == 422
        assert "模板" in response.json()["detail"]

    # 方法作用：验证单源可解析模板照旧返回脱敏明细，防止修复把单源一起关掉。
    # Args: self - 测试实例；monkeypatch - 补丁工具。
    # Returns: 无返回值。
    async def test_single_source_template_still_serves_rows(self, monkeypatch) -> None:
        """单源 live 钻取是既有用户可见能力，必须保持 200 与脱敏行。"""
        import src.security.sql_execution as execution

        store, quota, owner = await self._fixture(
            "SELECT region, amount FROM sales",
            datasource="sales",
            dialect="postgres",
            columns=["region", "amount"],
        )
        self._patch_route(monkeypatch, store, quota, owner, {"sales": {"allowed_columns": ["region", "amount"]}})
        executor = AsyncMock(return_value=SimpleNamespace(success=True, data=[{"region": "east", "amount": 3}], truncated=False))
        monkeypatch.setattr(execution, "validate_and_execute_sql", executor)
        app = FastAPI()
        app.include_router(route.router)

        transport = httpx.ASGITransport(app=app, raise_app_exceptions=False)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            response = await client.post("/report-workbench/reports/report/drilldowns/live", json={})

        assert response.status_code == 200
        body = response.json()
        assert body["mode"] == "live"
        assert body["rows"] == [{"region": "east", "amount": 3}]
        assert executor.await_count == 1
