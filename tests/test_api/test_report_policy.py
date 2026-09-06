"""租户报表策略管理的身份和事务边界测试。"""

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from fastapi import HTTPException

from src.reporting.models import QuotaPolicyRequest


class TestTenantReportPolicy:
    """覆盖功能 23.8：租户配额管理、越权阻断及失败不写缓存。"""

    # 方法作用：验证租户管理员可以保存本租户用户的周期额度。
    # Args: self - 测试实例；monkeypatch - 补丁 fixture。
    # Returns: 无返回值。
    async def test_tenant_admin_saves_policy(self, monkeypatch) -> None:
        """策略落库成功后才应更新缓存并返回已保存值。"""
        import src.api.routes.report_workbench as route
        from src.reporting.quota import QuotaManager, ReportPermissionService

        identity = SimpleNamespace(tenant_id=3, user_id=7, role="tenant_admin")
        store = SimpleNamespace(save_user_policy=AsyncMock(), record_audit=AsyncMock())
        quota = QuotaManager()
        permissions = ReportPermissionService()
        monkeypatch.setattr(route, "get_current_identity", lambda: identity)
        monkeypatch.setattr(route, "get_report_store", lambda: store)
        monkeypatch.setattr(route, "get_report_quota_manager", lambda: quota)
        monkeypatch.setattr(route, "get_report_permission_service", lambda: permissions)
        result = await route.set_user_policy(9, QuotaPolicyRequest(max_jobs_created=30, capabilities=["report.menu"]))
        assert result["policy"]["max_jobs_created"] == 30
        assert quota._policies[(3, 9)].max_jobs_created == 30
        store.save_user_policy.assert_awaited_once()

    # 方法作用：验证平台超级管理员和普通用户不能配置租户业务额度。
    # Args: self - 测试实例；monkeypatch - 补丁 fixture；role - 待测试角色。
    # Returns: 无返回值。
    @pytest.mark.parametrize("role", ["super_admin", "analyst", "viewer"])
    async def test_policy_rejects_non_tenant_admin(self, monkeypatch, role: str) -> None:
        """业务额度只由当前租户管理员管理。"""
        import src.api.routes.report_workbench as route

        monkeypatch.setattr(route, "get_current_identity", lambda: SimpleNamespace(tenant_id=3, user_id=7, role=role))
        with pytest.raises(HTTPException) as error:
            await route.set_user_policy(9, QuotaPolicyRequest())
        assert error.value.status_code == 403

    # 方法作用：验证数据库拒绝不存在或跨租户的目标时不能提前更新缓存。
    # Args: self - 测试实例；monkeypatch - 补丁 fixture。
    # Returns: 无返回值。
    async def test_foreign_user_does_not_mutate_policy_cache(self, monkeypatch) -> None:
        """目标用户无权访问时返回 404 并保留原额度。"""
        import src.api.routes.report_workbench as route
        from src.reporting.quota import QuotaManager

        quota = QuotaManager()
        store = SimpleNamespace(save_user_policy=AsyncMock(side_effect=LookupError("目标用户不存在")))
        monkeypatch.setattr(route, "get_current_identity", lambda: SimpleNamespace(tenant_id=3, user_id=7, role="tenant_admin"))
        monkeypatch.setattr(route, "get_report_store", lambda: store)
        monkeypatch.setattr(route, "get_report_quota_manager", lambda: quota)
        with pytest.raises(HTTPException) as error:
            await route.set_user_policy(99, QuotaPolicyRequest(max_jobs_created=300))
        assert error.value.status_code == 404
        assert (3, 99) not in quota._policies

    # 方法作用：验证管理页读取目标用户的有效策略和当前用量。
    # Args: self - 测试实例；monkeypatch - 补丁 fixture。
    # Returns: 无返回值。
    async def test_get_policy_returns_subject_quota(self, monkeypatch) -> None:
        """查询必须使用目标用户身份，而不是管理员自己的额度。"""
        import src.api.routes.report_workbench as route
        from src.reporting.quota import QuotaManager, ReportPermissionService

        identity = SimpleNamespace(tenant_id=3, user_id=7, role="tenant_admin")
        store = SimpleNamespace(
            get_policy_subject=AsyncMock(return_value={"role": "analyst"}),
            get_quota_policy=AsyncMock(return_value={"max_jobs_created": 30}),
        )
        quota = QuotaManager()
        monkeypatch.setattr(route, "get_current_identity", lambda: identity)
        monkeypatch.setattr(route, "get_report_store", lambda: store)
        monkeypatch.setattr(route, "get_report_quota_manager", lambda: quota)
        monkeypatch.setattr(route, "get_report_permission_service", lambda: ReportPermissionService())
        result = await route.get_user_policy(9)
        assert result["user_id"] == 9
        assert result["period"] == "calendar_month"
        assert result["policy"]["max_jobs_created"] == 30
        store.get_quota_policy.assert_awaited_once_with(3, 9)

    # 方法作用：验证写入失败时整个策略操作回滚且不更新运行缓存。
    # Args: self - 测试实例；monkeypatch - 补丁 fixture。
    # Returns: 无返回值。
    async def test_storage_failure_preserves_cache(self, monkeypatch) -> None:
        """持久化异常应向错误边界传播，不提前修改本地配额。"""
        import src.api.routes.report_workbench as route
        from src.reporting.quota import QuotaManager

        quota = QuotaManager()
        store = SimpleNamespace(save_user_policy=AsyncMock(side_effect=RuntimeError("事务失败")))
        monkeypatch.setattr(route, "get_current_identity", lambda: SimpleNamespace(tenant_id=3, user_id=7, role="tenant_admin"))
        monkeypatch.setattr(route, "get_report_store", lambda: store)
        monkeypatch.setattr(route, "get_report_quota_manager", lambda: quota)
        with pytest.raises(RuntimeError, match="事务失败"):
            await route.set_user_policy(9, QuotaPolicyRequest(max_jobs_created=300))
        assert (3, 9) not in quota._policies
