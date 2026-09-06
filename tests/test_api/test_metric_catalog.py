"""业务指标 API 的 HTTP 契约和租户权限回归。"""

from unittest.mock import AsyncMock

from fastapi import FastAPI
from fastapi.testclient import TestClient

from src.knowledge.metric_catalog import MetricConflict
from src.security.tenant_policy import RequestIdentity


class TestMetricApi:
    """覆盖 11.1.13：独立路由、错误映射、参数验证及身份传递。"""

    # 方法作用：验证 HTTP 新建使用服务端身份而非请求体租户。
    # Args: self - 实例；monkeypatch - 边界补丁。
    # Returns: 无返回值。
    def test_create_and_conflict_contract(self, monkeypatch) -> None:
        """保存冲突返回 409，不能吞异常返回成功。"""
        import src.api.routes.metric_catalog as route

        identity = RequestIdentity(tenant_id=3, user_id=7, role="tenant_admin")
        store = AsyncMock()
        store.save_revision.return_value = {"id": "version-1", "version": 1}
        monkeypatch.setattr(route, "get_current_identity", lambda: identity)
        monkeypatch.setattr(route, "get_metric_catalog", lambda: store)
        monkeypatch.setattr(route, "metric_datasource_dialect", AsyncMock(return_value="sqlite"))
        app = FastAPI()
        app.include_router(route.router, prefix="/api/v1")
        client = TestClient(app)
        payload = {"definition": {"code": "gmv", "name": "GMV", "datasource": "sales", "expression": "SUM(amount)"}, "expected_version": 0}
        response = client.post("/api/v1/metric-catalog", json=payload)
        assert response.status_code == 200
        assert store.save_revision.await_args.args[0] == identity
        assert store.save_revision.await_args.args[1].definition.dialect == "sqlite"
        store.save_revision.side_effect = MetricConflict("版本冲突")
        assert client.post("/api/v1/metric-catalog", json=payload).status_code == 409
        payload["tenant_id"] = 9
        assert client.post("/api/v1/metric-catalog", json=payload).status_code == 422

    # 方法作用：验证目录读取、历史和生效接口的权限错误及分页约束。
    # Args: self - 实例；monkeypatch - 边界补丁。
    # Returns: 无返回值。
    def test_read_and_review_http_boundaries(self, monkeypatch) -> None:
        """所有业务接口均映射服务层权限异常，不改变 Prometheus 路由。"""
        import src.api.routes.metric_catalog as route

        store = AsyncMock()
        store.list_metrics.return_value = {"items": [], "total": 0}
        store.versions.return_value = []
        store.effective.side_effect = LookupError("尚未生效")
        store.transition.side_effect = PermissionError("需要租户管理员")
        monkeypatch.setattr(route, "get_current_identity", lambda: RequestIdentity(tenant_id=3, user_id=8, role="viewer"))
        monkeypatch.setattr(route, "get_metric_catalog", lambda: store)
        app = FastAPI()
        app.include_router(route.router)
        client = TestClient(app)
        assert client.get("/metric-catalog").json() == {"items": [], "total": 0}
        assert client.get("/metric-catalog?limit=101").status_code == 422
        assert client.get("/metric-catalog/gmv/versions").json() == {"items": []}
        assert client.get("/metric-catalog/gmv/effective").status_code == 404
        assert client.post("/metric-catalog/record/transition", json={"action": "approve"}).status_code == 403
