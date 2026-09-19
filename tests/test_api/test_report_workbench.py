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
        import src.llm.tenant_config as tenant_config
        from src.reporting.quota import ReportPermissionService
        from src.reporting.store import ReportStore

        service = ReportPermissionService(store=ReportStore(memory=True))
        monkeypatch.setattr(route, "get_report_permission_service", lambda: service)

        # 单测不访问真实 LLM 配置库，默认视为未配置，预览走规则回退。
        # 方法作用：模拟租户尚未配置模型时的解析失败。
        # Args: kwargs - 透传的租户与模型选择参数。
        # Returns: 不返回，直接抛出未配置异常。
        async def _no_tenant_llm(**kwargs):
            raise LookupError("测试环境未配置租户 LLM")

        monkeypatch.setattr(tenant_config, "resolve_tenant_llm_selection", _no_tenant_llm)

    # 方法作用：验证预览路由把客户端显式选择的命名连接与模型透传给模型解析。
    # Args: self - pytest 测试类实例；monkeypatch - pytest 补丁工具。
    # Returns: 无返回值，断言失败时由 pytest 报告。
    async def test_plan_forwards_selected_model_connection(self, monkeypatch) -> None:
        """请求显式选择命名连接时，预览必须用该连接而不是租户默认连接。"""
        # Arrange
        import src.api.routes.report_workbench as route
        import src.llm.client as llm_client
        import src.llm.tenant_config as tenant_config
        from src.reporting.models import ReportPlanRequest

        captured: list[dict[str, object]] = []
        identity = SimpleNamespace(tenant_id=3, user_id=7, role="analyst")
        monkeypatch.setattr(route, "get_current_identity", lambda: identity)
        monkeypatch.setattr(route, "_resolve_plan_access", AsyncMock(return_value={"sales": {}}))
        # 关闭模型可用性，确保用例只验证参数透传，不触发任何真实模型调用。
        monkeypatch.setattr(llm_client, "is_task_llm_available", lambda task: False)

        # 方法作用：记录模型解析入参并返回未配置，避免访问真实配置库。
        # Args: kwargs - resolve_tenant_llm_selection 的关键字参数。
        # Returns: 始终为 None，表示未取到可用选择。
        async def fake_resolve(**kwargs):
            captured.append(kwargs)
            return None

        monkeypatch.setattr(tenant_config, "resolve_tenant_llm_selection", fake_resolve)

        # Act
        await route.preview_plan(
            ReportPlanRequest(
                prompt="生成销售报表",
                datasources=["sales"],
                model_id="gpt-4o",
                llm_connection_id=11,
            )
        )

        # Assert
        assert captured, "预览路由必须调用租户模型解析"
        assert captured[0]["tenant_id"] == 3
        assert captured[0]["connection_id"] == 11
        assert captured[0]["model_id"] == "gpt-4o"

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
            list_unit_snapshots=AsyncMock(return_value=[]),
        )
        monkeypatch.setattr(route, "get_current_identity", lambda: identity)
        monkeypatch.setattr(route, "get_report_store", lambda: store)
        monkeypatch.setattr(route, "_has_any_capability", AsyncMock(return_value=True))

        # Act
        result = await route.get_run("run1")

        # Assert
        assert result["steps"] == [{"node": "report_execute_units", "status": "failed", "user_error_message": "查询失败"}]
        # 单元快照随详情一并返回，明细页才能定位到具体单元。
        assert result["units"] == []


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

    # 方法作用：验证未授权钻取的分享仍下发受限脱敏清单，页面表格不再恒为空。
    # Args: self - pytest 测试类实例；monkeypatch - pytest 补丁工具。
    # Returns: 无返回值，断言失败时由 pytest 报告。
    async def test_shared_snapshot_without_drilldown_still_returns_manifest(self, monkeypatch) -> None:
        """实机取证：allow_drilldown=false 时 data_manifest 直接回 {}，表格块永远只有表头。"""
        # Arrange
        import src.api.routes.report_workbench as route
        from src.reporting.models import ReportVersionRecord

        rows = [{"region": f"区域{index}", "sales": index} for index in range(150)]
        version = ReportVersionRecord(
            id="v1",
            report_id="r1",
            job_id="j1",
            run_id="run1",
            version=3,
            document={
                "permissions": {"drilldown": True},
                "drilldowns": [{"drilldown_id": "dd1", "mode": "snapshot", "columns": ["region"], "data_ref": "data:sales"}],
            },
            data_manifest={"data:sales": rows},
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
            "allow_drilldown": False,
        }
        store = SimpleNamespace(get_share_by_token=AsyncMock(return_value=share), get_version_by_id=AsyncMock(return_value=version), record_audit=AsyncMock())
        monkeypatch.setattr(route, "hash_share_token", lambda token: "hashed-token")
        monkeypatch.setattr(route, "get_report_store", lambda: store)

        # Act
        result = await route.get_shared_report("raw-token")

        # Assert：清单仍下发且按上限裁剪，渲染表格块有行可用
        assert result["data_manifest"]["data:sales"]
        assert len(result["data_manifest"]["data:sales"]) == route._SHARE_MANIFEST_ROW_LIMIT
        # 明细入口继续关闭：drilldowns 清空、权限位为 false
        assert result["document"]["drilldowns"] == []
        assert result["document"]["permissions"]["drilldown"] is False
        # 不下发 SQL 文本或凭证字段
        payload = str(result)
        assert "SELECT" not in payload
        assert "code_hash" not in payload
        assert "password" not in payload

    # 方法作用：验证清单裁剪对异常输入失败关闭，不抛错也不透传。
    # Args: self - pytest 测试类实例。
    # Returns: 无返回值，断言失败时由 pytest 报告。
    async def test_shared_manifest_tolerates_non_mapping(self) -> None:
        """版本记录里的清单字段可能是 null 或异常结构，不能让分享页面整体 500。"""
        # Arrange
        import src.api.routes.report_workbench as route

        # Act / Assert
        assert route._shared_manifest(None) == {}
        assert route._shared_manifest("not-a-mapping") == {}
        assert route._shared_manifest({"data:sales": None}) == {"data:sales": []}

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
