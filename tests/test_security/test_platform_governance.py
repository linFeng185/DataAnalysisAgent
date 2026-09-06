"""G24.1/G24.3/G24.4/G24.5 平台职责边界回归。"""

from types import SimpleNamespace

import pytest
from fastapi import HTTPException

from src.knowledge.governance import can_manage_knowledge_resource, can_write_knowledge_scope
from src.reporting.quota import ReportPermissionService


class TestPlatformGovernance:
    """覆盖 G24.1、G24.3、G24.4 和 G24.5 的拒绝边界。"""

    # 验证旧能力覆盖不能恢复平台账号的报表业务权。
    # Args: self - 测试实例。
    # Returns: 无返回值。
    async def test_super_admin_report_override_is_rejected(self) -> None:
        """超管即使有旧覆盖也不能获得菜单或创建能力。"""
        service = ReportPermissionService()
        identity = SimpleNamespace(tenant_id=1, user_id=1, role="super_admin")
        service._user_capabilities[(1, 1)] = frozenset({"report.create", "report.menu"})
        assert not await service.has(identity, "report.create")
        assert await service.capabilities(identity) == []

    # 验证平台身份只管理系统知识。
    # Args: self - 测试实例；scope - 知识范围。
    # Returns: 无返回值。
    @pytest.mark.parametrize("scope", ["tenant", "private"])
    def test_super_admin_cannot_manage_business_knowledge(self, scope: str) -> None:
        """同租户、本人旧资源也不再授予平台业务能力。"""
        assert not can_write_knowledge_scope(scope, role="super_admin", user_id=1, multi_tenant=True)
        assert not can_manage_knowledge_resource(
            scope, role="super_admin", current_tenant_id=1, resource_tenant_id=1,
            current_user_id=1, owner_user_id=1,
        )

    # 验证旧模型入口不再触发外部调用。
    # Args: self - 测试实例。
    # Returns: 无返回值。
    async def test_legacy_model_probe_is_gone(self) -> None:
        """旧接口统一返回可迁移的 410。"""
        from src.api.routes.management import test_model
        from src.api.schemas import ModelTestRequest

        with pytest.raises(HTTPException) as error:
            await test_model(ModelTestRequest(model_id="legacy"))
        assert error.value.status_code == 410

    # 验证平台普通知识检索不会携带租户或个人过滤分支。
    # Args: self - 测试实例；monkeypatch - 平台身份。
    # Returns: 无返回值。
    def test_super_admin_retrieval_only_system(self, monkeypatch) -> None:
        """旧检索测试入口与三范围入口都只检索系统知识。"""
        from src.knowledge.retrieval import (
            build_accessible_knowledge_filters,
            build_knowledge_filters,
        )

        monkeypatch.setattr("src.api.auth.get_current_role", lambda: "super_admin")
        assert build_knowledge_filters(datasource="sales") == {"visibility": "system", "datasource": "sales"}
        assert build_accessible_knowledge_filters(tenant_id=2, user_id=7) == [{"visibility": "system"}]
