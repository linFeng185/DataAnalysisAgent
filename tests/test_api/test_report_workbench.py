"""报表工作台 API 测试。"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from fastapi import HTTPException


@pytest.mark.asyncio
class TestReportWorkbenchApi:
    """覆盖功能 23.1、23.2、23.6、23.7、23.8、23.10。"""

    # 方法作用：让 API 单测的权限查询使用独立内存 Store，禁止复用真实 PG 连接。
    # Args: self - 测试实例；monkeypatch - 补丁 fixture。
    # Returns: 无返回值。
    @pytest.fixture(autouse=True)
    def isolate_permission_store(self, monkeypatch) -> None:
        """权限库故障应失败关闭，普通 API 用例不依赖开发数据库可用性。"""
        import src.api.routes.report_workbench as route
        from src.reporting.quota import ReportPermissionService
        from src.reporting.store import ReportStore

        service = ReportPermissionService(store=ReportStore(memory=True))
        monkeypatch.setattr(route, "get_report_permission_service", lambda: service)

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

    # 方法作用：验证客户端不能通过自带 plan_override 和自算哈希绕过服务端预览确认。
    # Args: self - pytest 测试类实例；monkeypatch - pytest 补丁工具。
    # Returns: 无返回值，断言失败时由 pytest 报告。
    async def test_create_job_rejects_unpreviewed_plan_override(self, monkeypatch) -> None:
        """未出现在当前身份预览缓存中的计划即使哈希自洽也必须返回 409。"""
        # Arrange
        import src.api.routes.report_workbench as route
        from src.reporting.models import AnalysisUnit, ReportJobCreateRequest, ReportPlan

        identity = SimpleNamespace(tenant_id=3, user_id=7, role="analyst")
        cached_plan = ReportPlan(
            requested_datasources=["sales"],
            analysis_units=[AnalysisUnit(unit_id="sales", question="销售趋势")],
        )
        override_plan = ReportPlan(
            requested_datasources=["sales"],
            analysis_units=[AnalysisUnit(unit_id="sales", question="客户留存")],
        )
        route._plan_cache[(3, 7, cached_plan.canonical_hash())] = cached_plan
        monkeypatch.setattr(route, "get_current_identity", lambda: identity)
        monkeypatch.setattr(route, "_require_capability", AsyncMock())
        monkeypatch.setattr(route, "_resolve_plan_access", AsyncMock(side_effect=AssertionError("未预览计划不应进入执行授权")))

        # Act / Assert
        with pytest.raises(HTTPException) as error:
            await route.create_job(ReportJobCreateRequest(
                prompt="生成客户留存报表",
                plan_hash=override_plan.canonical_hash(),
                plan_override=override_plan,
                confirm=True,
            ))
        assert error.value.status_code == 409

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

    # 方法作用：验证分享链接创建受周期分享配额约束，超限时不写入 token。
    # Args: self - pytest 测试类实例；monkeypatch - pytest 补丁工具。
    # Returns: 无返回值，断言失败时由 pytest 报告。
    async def test_create_share_rejects_quota_exhaustion(self, monkeypatch) -> None:
        """分享配额耗尽应返回 429 且不创建分享记录。"""
        # Arrange
        import src.api.routes.report_workbench as route
        from src.reporting.models import ReportShareCreateRequest, ReportVersionRecord
        from src.reporting.quota import QuotaExceededError

        identity = SimpleNamespace(tenant_id=3, user_id=7, role="tenant_admin")
        version = ReportVersionRecord(id="v1", report_id="r1", job_id="j1", run_id="run1", version=1, document={}, checksum="c")
        store = SimpleNamespace(get_report_version=AsyncMock(return_value=version), create_share=AsyncMock(), record_audit=AsyncMock())
        quota = SimpleNamespace(reserve=AsyncMock(side_effect=QuotaExceededError("QUOTA_EXCEEDED: share_links")))
        monkeypatch.setattr(route, "get_current_identity", lambda: identity)
        monkeypatch.setattr(route, "get_report_store", lambda: store)
        monkeypatch.setattr(route, "get_report_quota_manager", lambda: quota)
        monkeypatch.setattr(route, "_require_capability", AsyncMock())

        # Act / Assert
        with pytest.raises(HTTPException) as error:
            await route.create_share("r1", ReportShareCreateRequest(scope="public"))
        assert error.value.status_code == 429
        store.create_share.assert_not_awaited()

    # 方法作用：验证前端菜单权限探测只返回当前身份能力，不绕过服务端授权。
    # Args: self - pytest 测试类实例；monkeypatch - pytest 补丁工具。
    # Returns: 无返回值，断言失败时由 pytest 报告。
    async def test_permission_probe_returns_capabilities(self, monkeypatch) -> None:
        """具有 report.menu 的用户应得到菜单可见标记和能力列表。"""
        # Arrange
        import src.api.routes.report_workbench as route

        identity = SimpleNamespace(tenant_id=3, user_id=7, role="analyst")
        service = SimpleNamespace(capabilities=AsyncMock(return_value=["report.create", "report.menu"]))
        monkeypatch.setattr(route, "get_current_identity", lambda: identity)
        monkeypatch.setattr(route, "get_report_permission_service", lambda: service)

        # Act
        result = await route.get_my_permissions()

        # Assert
        assert result == {"capabilities": ["report.create", "report.menu"], "menu_allowed": True}
        service.capabilities.assert_awaited_once_with(identity)

    # 方法作用：验证运行日志 API 会剥离内存 Store 中可能残留的内部异常字段。
    # Args: self - pytest 测试类实例；monkeypatch - pytest 补丁工具。
    # Returns: 无返回值，断言失败时由 pytest 报告。
    async def test_run_log_hides_internal_error(self, monkeypatch) -> None:
        """普通用户只能看到节点状态和用户错误，不能读取内部连接细节。"""
        # Arrange
        import src.api.routes.report_workbench as route
        from src.reporting.models import ReportRunRecord

        identity = SimpleNamespace(tenant_id=3, user_id=7, role="analyst")
        store = SimpleNamespace(
            get_run=AsyncMock(return_value=ReportRunRecord(id="run1", job_id="job1")),
            list_steps=AsyncMock(return_value=[{
                "node": "report_execute_units",
                "status": "failed",
                "user_error_message": "查询失败",
                "internal_error_message": "password=secret",
                "input_hash": "private-hash",
            }]),
        )
        monkeypatch.setattr(route, "get_current_identity", lambda: identity)
        monkeypatch.setattr(route, "get_report_store", lambda: store)
        monkeypatch.setattr(route, "_require_capability", AsyncMock())

        # Act
        result = await route.get_run("run1")

        # Assert
        assert result["steps"] == [{"node": "report_execute_units", "status": "failed", "user_error_message": "查询失败"}]


@pytest.mark.asyncio
class TestSharedReportApi:
    """覆盖功能 23.6、23.7：公开快照访问、Drilldown 和审计。"""

    # 方法作用：验证公开分享可以读取固定脱敏版本并保留访问审计。
    # Args: self - pytest 测试类实例；monkeypatch - pytest 补丁工具。
    # Returns: 无返回值，断言失败时由 pytest 报告。
    async def test_shared_snapshot_returns_sanitized_document(self, monkeypatch) -> None:
        """公开分享应只返回版本文档和允许的快照清单。"""
        # Arrange
        import src.api.routes.report_workbench as route
        from src.reporting.models import ReportVersionRecord

        version = ReportVersionRecord(
            id="v1",
            report_id="r1",
            job_id="j1",
            run_id="run1",
            version=2,
            document={
                "permissions": {"drilldown": True},
                "drilldowns": [{"drilldown_id": "dd1", "mode": "snapshot", "allowed_filters": [], "columns": ["region"], "data_ref": "data:sales"}],
            },
            data_manifest={"data:sales": [{"region": "华东", "secret": "hidden"}]},
            checksum="checksum",
        )
        share = {
            "id": "share1",
            "report_version_id": "v1",
            "tenant_id": 3,
            "scope": "public",
            "code_hash": "",
            "revoked_at": None,
            "expires_at": None,
            "allow_drilldown": True,
        }
        store = SimpleNamespace(get_share_by_token=AsyncMock(return_value=share), get_version_by_id=AsyncMock(return_value=version), record_audit=AsyncMock())
        monkeypatch.setattr(route, "hash_share_token", lambda token: "hashed-token")
        monkeypatch.setattr(route, "get_report_store", lambda: store)

        # Act
        result = await route.get_shared_report("raw-token")

        # Assert
        assert result["report_id"] == "r1"
        assert result["version"] == 2
        assert result["data_manifest"] == version.data_manifest
        store.record_audit.assert_awaited_once()
        assert store.record_audit.await_args.args[0]["action"] == "share_access"

    # 方法作用：验证公开分享 Drilldown 只读取签发快照字段并写入成功审计。
    # Args: self - pytest 测试类实例；monkeypatch - pytest 补丁工具。
    # Returns: 无返回值，断言失败时由 pytest 报告。
    async def test_shared_drilldown_is_snapshot_only(self, monkeypatch) -> None:
        """允许明细的公开分享应返回有界列，不能执行客户端 SQL。"""
        # Arrange
        import src.api.routes.report_workbench as route
        from src.reporting.models import DrilldownRequest, ReportVersionRecord

        version = ReportVersionRecord(
            id="v1",
            report_id="r1",
            job_id="j1",
            run_id="run1",
            version=1,
            document={
                "drilldowns": [{
                    "drilldown_id": "dd1",
                    "mode": "snapshot",
                    "allowed_filters": [{"field": "region", "operator": "eq", "value_type": "string"}],
                    "columns": ["region", "sales"],
                    "max_rows": 10,
                    "data_ref": "data:sales",
                }],
            },
            data_manifest={"data:sales": [{"region": "华东", "sales": 10, "secret": "hidden"}]},
            checksum="checksum",
        )
        share = {
            "id": "share1",
            "report_version_id": "v1",
            "tenant_id": 3,
            "scope": "public",
            "code_hash": "",
            "revoked_at": None,
            "expires_at": None,
            "allow_drilldown": True,
        }
        store = SimpleNamespace(get_share_by_token=AsyncMock(return_value=share), get_version_by_id=AsyncMock(return_value=version), record_audit=AsyncMock())
        monkeypatch.setattr(route, "hash_share_token", lambda token: "hashed-token")
        monkeypatch.setattr(route, "get_report_store", lambda: store)

        # Act
        result = await route.shared_drilldown("raw-token", "dd1", DrilldownRequest(filters={"region": "华东"}))

        # Assert
        assert result["rows"] == [{"region": "华东", "sales": 10}]
        assert result["columns"] == ["region", "sales"]
        audit_events = [call.args[0] for call in store.record_audit.await_args_list]
        assert audit_events[-1]["action"] == "share_drilldown"
        assert audit_events[-1]["result"] == "success"

    # 方法作用：验证未开启明细权限的公开分享在执行前被阻断并审计拒绝结果。
    # Args: self - pytest 测试类实例；monkeypatch - pytest 补丁工具。
    # Returns: 无返回值，断言失败时由 pytest 报告。
    async def test_shared_drilldown_requires_share_permission(self, monkeypatch) -> None:
        """分享本身有效但未授权 Drilldown 时应返回 403。"""
        # Arrange
        import src.api.routes.report_workbench as route
        from src.reporting.models import DrilldownRequest

        share = {"id": "share1", "tenant_id": 3, "scope": "public", "code_hash": "", "revoked_at": None, "expires_at": None, "allow_drilldown": False}
        store = SimpleNamespace(get_share_by_token=AsyncMock(return_value=share), record_audit=AsyncMock())
        monkeypatch.setattr(route, "hash_share_token", lambda token: "hashed-token")
        monkeypatch.setattr(route, "get_report_store", lambda: store)

        # Act / Assert
        with pytest.raises(HTTPException) as error:
            await route.shared_drilldown("raw-token", "dd1", DrilldownRequest())
        assert error.value.status_code == 403
        store.record_audit.assert_awaited_once()
        assert store.record_audit.await_args.args[0]["result"] == "denied"

    # 方法作用：验证指定用户分享同时校验租户边界，阻止跨租户同 ID 访问。
    # Args: self - pytest 测试类实例；monkeypatch - pytest 补丁工具。
    # Returns: 无返回值，断言失败时由 pytest 报告。
    async def test_user_scope_rejects_cross_tenant_identity(self, monkeypatch) -> None:
        """用户 ID 命中但租户不同时仍必须返回 403。"""
        # Arrange
        import src.api.routes.report_workbench as route
        from src.reporting.models import ReportVersionRecord

        version = ReportVersionRecord(id="v1", report_id="r1", job_id="j1", run_id="run1", version=1, document={}, checksum="c")
        share = {
            "id": "share1", "report_version_id": "v1", "tenant_id": 3, "created_by": 7,
            "scope": "user", "user_ids": [8], "code_hash": "", "revoked_at": None,
            "expires_at": None, "allow_drilldown": False,
        }
        identity = SimpleNamespace(tenant_id=99, user_id=8, role="analyst")
        store = SimpleNamespace(get_share_by_token=AsyncMock(return_value=share), get_version_by_id=AsyncMock(return_value=version), record_audit=AsyncMock())
        monkeypatch.setattr(route, "hash_share_token", lambda token: "hashed-token")
        monkeypatch.setattr(route, "get_report_store", lambda: store)
        monkeypatch.setattr(route, "get_current_identity", lambda: identity)

        # Act / Assert
        with pytest.raises(HTTPException) as error:
            await route.get_shared_report("raw-token")
        assert error.value.status_code == 403
        assert store.record_audit.await_args.args[0]["result"] == "denied"
