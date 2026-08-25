"""报表工作台交互、分享、配额和存储测试。"""

from __future__ import annotations

from types import SimpleNamespace

import pytest


class TestSnapshotDrilldown:
    """覆盖功能 23.6：快照 Drilldown 仅访问签发字段。"""

    # 方法作用：验证快照过滤和分页只返回 Drilldown 契约允许的字段。
    # Args: self - pytest 测试类实例。
    # Returns: 无返回值，断言失败时由 pytest 报告。
    def test_snapshot_drilldown_filters_and_paginates(self) -> None:
        """合法 eq 过滤应返回脱敏且有界的明细。"""
        # Arrange
        from src.reporting.drilldown import execute_snapshot_drilldown

        definition = {
            "drilldown_id": "dd-1",
            "mode": "snapshot",
            "allowed_filters": [{"field": "region", "operator": "eq", "value_type": "string"}],
            "columns": ["region", "sales"],
            "max_rows": 2,
            "data_ref": "data:sales",
        }
        manifest = {"data:sales": [
            {"region": "华东", "sales": 10, "secret": "x"},
            {"region": "华南", "sales": 20, "secret": "y"},
            {"region": "华东", "sales": 30, "secret": "z"},
        ]}

        # Act
        result = execute_snapshot_drilldown(definition, manifest, {"region": "华东"}, page=1, page_size=10)

        # Assert
        assert result["total"] == 2
        assert result["rows"] == [{"region": "华东", "sales": 10}, {"region": "华东", "sales": 30}]

    # 方法作用：阻止客户端提交未签发字段或任意过滤操作符。
    # Args: self - pytest 测试类实例。
    # Returns: 无返回值，断言失败时由 pytest 报告。
    def test_snapshot_drilldown_rejects_unknown_filter(self) -> None:
        """未知字段和 gt 操作符不能穿透到快照数据。"""
        # Arrange
        from src.reporting.drilldown import execute_snapshot_drilldown

        definition = {
            "drilldown_id": "dd-1", "mode": "snapshot", "allowed_filters": [],
            "columns": ["region"], "max_rows": 10, "data_ref": "data:sales",
        }

        # Act / Assert
        with pytest.raises(ValueError, match="过滤|字段"):
            execute_snapshot_drilldown(definition, {"data:sales": [{"region": "华东"}]}, {"secret": "x"}, 1, 10)


class TestReportQuota:
    """覆盖功能 23.8：菜单能力和周期配额。"""

    # 方法作用：验证用户配额在周期内达到上限后阻断新任务。
    # Args: self - pytest 测试类实例。
    # Returns: 无返回值，断言失败时由 pytest 报告。
    @pytest.mark.asyncio
    async def test_quota_reservation_is_atomic_in_memory(self) -> None:
        """第二次超出任务上限的预占应返回 QUOTA_EXCEEDED。"""
        # Arrange
        from src.reporting.quota import QuotaExceededError, QuotaManager, QuotaPolicy

        manager = QuotaManager(default_policy=QuotaPolicy(max_jobs_created=1, max_runs_started=1))
        identity = SimpleNamespace(tenant_id=3, user_id=7, role="analyst")

        # Act
        first = await manager.reserve(identity, jobs=1)

        # Assert
        assert first.jobs_remaining == 0
        with pytest.raises(QuotaExceededError, match="QUOTA_EXCEEDED"):
            await manager.reserve(identity, jobs=1)

    # 方法作用：验证默认角色能力和管理员覆盖能力的边界。
    # Args: self - pytest 测试类实例。
    # Returns: 无返回值，断言失败时由 pytest 报告。
    @pytest.mark.asyncio
    async def test_permission_capabilities(self) -> None:
        """analyst 默认可创建，viewer 默认不可创建，显式覆盖可授予菜单。"""
        # Arrange
        from src.reporting.quota import ReportPermissionService

        service = ReportPermissionService()

        # Act / Assert
        assert await service.has(SimpleNamespace(role="analyst", user_id=7, tenant_id=3), "report.create")
        assert not await service.has(SimpleNamespace(role="viewer", user_id=8, tenant_id=3), "report.create")
        service.set_user_capabilities(8, ["report.menu", "report.create"])
        assert await service.has(SimpleNamespace(role="viewer", user_id=8, tenant_id=3), "report.create")


class TestReportStore:
    """覆盖功能 23.2、23.7、23.10：内存 Store 的幂等、运行与审计。"""

    # 方法作用：验证同一幂等键不会重复创建报表任务。
    # Args: self - pytest 测试类实例。
    # Returns: 无返回值，断言失败时由 pytest 报告。
    @pytest.mark.asyncio
    async def test_job_idempotency_and_step_logging(self) -> None:
        """重复提交返回同一个 Job，步骤同时保留用户错误和内部错误字段。"""
        # Arrange
        from src.reporting.models import AnalysisUnit, ReportPlan
        from src.reporting.store import ReportStore

        store = ReportStore()
        identity = SimpleNamespace(tenant_id=3, user_id=7, role="analyst")
        plan = ReportPlan(analysis_units=[AnalysisUnit(question="销售趋势")])

        # Act
        first = await store.create_job(identity, "生成销售报表", plan, "req-1")
        second = await store.create_job(identity, "生成销售报表", plan, "req-1")
        run = await store.create_run(first.id, identity, attempt=1)
        await store.append_step(run.id, "report_plan_queries", "failed", error_code="QUERY_FAILED", internal_error="connection secret", user_error="查询暂时失败")
        steps = await store.list_steps(run.id, identity)

        # Assert
        assert first.id == second.id
        assert len(steps) == 1
        assert steps[0]["user_error_message"] == "查询暂时失败"
        assert steps[0]["internal_error_message"] == "connection secret"
