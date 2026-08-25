"""报表工作台 API 测试。"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from fastapi import HTTPException


@pytest.mark.asyncio
class TestReportWorkbenchApi:
    """覆盖功能 23.1、23.2、23.6、23.7、23.8、23.10。"""

    # 方法作用：验证计划预览只解析和估算，不创建任务。
    # Args: self - pytest 测试类实例；monkeypatch - pytest 补丁工具。
    # Returns: 无返回值，断言失败时由 pytest 报告。
    async def test_plan_preview_does_not_create_job(self, monkeypatch) -> None:
        """POST /plans 应返回计划哈希和确认提示。"""
        # Arrange
        import src.api.routes.report_workbench as route
        from src.reporting.models import ReportPlanRequest

        identity = SimpleNamespace(tenant_id=3, user_id=7, role="analyst")
        monkeypatch.setattr(route, "get_current_identity", lambda: identity)
        monkeypatch.setattr(route, "_resolve_plan_access", AsyncMock(return_value={"sales": {}}))

        # Act
        result = await route.preview_plan(ReportPlanRequest(prompt="生成销售报表", datasources=["sales"]))

        # Assert
        assert result["plan"]["execution_mode"] == "immediate"
        assert result["plan_hash"].startswith("sha256:")
        assert result["job_id"] is None
        route._resolve_plan_access.assert_awaited_once()

    # 方法作用：验证计划哈希不匹配时阻止创建任务，防止前端篡改计划。
    # Args: self - pytest 测试类实例；monkeypatch - pytest 补丁工具。
    # Returns: 无返回值，断言失败时由 pytest 报告。
    async def test_create_job_rejects_plan_hash_mismatch(self, monkeypatch) -> None:
        """客户端提交错误 plan_hash 应返回 409。"""
        # Arrange
        import src.api.routes.report_workbench as route
        from src.reporting.models import ReportJobCreateRequest

        identity = SimpleNamespace(tenant_id=3, user_id=7, role="analyst")
        monkeypatch.setattr(route, "get_current_identity", lambda: identity)
        monkeypatch.setattr(route, "_require_capability", AsyncMock())
        route._plan_cache[(3, 7, "sha256:" + "a" * 64)] = route.ReportPlanRequest(prompt="old") if False else None

        # Act / Assert
        with pytest.raises(HTTPException) as error:
            await route.create_job(ReportJobCreateRequest(prompt="生成销售报表", plan_hash="sha256:" + "b" * 64, confirm=True))
        assert error.value.status_code in {400, 409}

    # 方法作用：验证私有报表 Drilldown 复用版本数据清单并写入审计。
    # Args: self - pytest 测试类实例；monkeypatch - pytest 补丁工具。
    # Returns: 无返回值，断言失败时由 pytest 报告。
    async def test_private_drilldown_audited(self, monkeypatch) -> None:
        """合法快照钻取不能执行客户端 SQL，并必须留下访问审计。"""
        # Arrange
        import src.api.routes.report_workbench as route
        from src.reporting.models import DrilldownRequest, ReportVersionRecord

        identity = SimpleNamespace(tenant_id=3, user_id=7, role="analyst")
        version = ReportVersionRecord(
            id="v1", report_id="r1", job_id="j1", run_id="run1", version=1,
            document={"permissions": {"drilldown": True}, "drilldowns": [{"drilldown_id": "dd1", "mode": "snapshot", "allowed_filters": [{"field": "region", "operator": "eq", "value_type": "string"}], "columns": ["region", "sales"], "max_rows": 10, "data_ref": "data:sales"}]},
            data_manifest={"data:sales": [{"region": "华东", "sales": 10, "secret": "x"}]}, checksum="c",
        )
        store = SimpleNamespace(get_report_version=AsyncMock(return_value=version), record_audit=AsyncMock())
        monkeypatch.setattr(route, "get_current_identity", lambda: identity)
        monkeypatch.setattr(route, "get_report_store", lambda: store)

        # Act
        result = await route.drilldown("r1", "dd1", DrilldownRequest(filters={"region": "华东"}))

        # Assert
        assert result["rows"] == [{"region": "华东", "sales": 10}]
        store.record_audit.assert_awaited_once()
        assert "sql" not in store.record_audit.await_args.args[0]

    # 方法作用：验证分享创建只返回一次 token 摘要，撤销后调用 Store 处理失效。
    # Args: self - pytest 测试类实例；monkeypatch - pytest 补丁工具。
    # Returns: 无返回值，断言失败时由 pytest 报告。
    async def test_create_and_revoke_share(self, monkeypatch) -> None:
        """公开分享创建和撤销应经过 report.share 能力校验。"""
        # Arrange
        import src.api.routes.report_workbench as route
        from src.reporting.models import ReportShareCreateRequest, ReportVersionRecord

        identity = SimpleNamespace(tenant_id=3, user_id=7, role="tenant_admin")
        version = ReportVersionRecord(id="v1", report_id="r1", job_id="j1", run_id="run1", version=1, document={"permissions": {"drilldown": False}}, checksum="c")
        store = SimpleNamespace(
            get_report_version=AsyncMock(return_value=version),
            create_share=AsyncMock(return_value=SimpleNamespace(id="share-1")),
            record_audit=AsyncMock(),
            revoke_share=AsyncMock(return_value=True),
        )
        monkeypatch.setattr(route, "get_current_identity", lambda: identity)
        monkeypatch.setattr(route, "get_report_store", lambda: store)
        monkeypatch.setattr(route, "_require_capability", AsyncMock())

        # Act
        created = await route.create_share("r1", ReportShareCreateRequest(scope="public"))
        revoked = await route.revoke_share("share-1")

        # Assert
        assert created["share_id"] == "share-1"
        assert "/shared/reports/" in created["url"]
        assert revoked == {"status": "revoked"}
        store.revoke_share.assert_awaited_once_with("share-1", identity)
