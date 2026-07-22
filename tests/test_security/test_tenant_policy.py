"""TenantPolicy 租户策略集中化契约测试。"""

from __future__ import annotations

import ast
import logging
from pathlib import Path

import pytest


logger = logging.getLogger(__name__)
ROOT = Path(__file__).resolve().parents[2]


class TestTenantPolicyMatrix:
    """覆盖功能 20.16：单/多租户身份、过滤和写权限矩阵。"""

    # 方法作用：验证系统、默认租户和匿名用户常量语义稳定。
    # Args: self - pytest 测试类实例。
    # Returns: 无返回值，断言失败时由 pytest 报告。
    def test_identity_constants_are_explicit(self) -> None:
        """0 和 1 不得继续作为散落的魔法数字。"""
        logger.debug("test_identity_constants_are_explicit 入口")
        from src.security.tenant_policy import (
            ANONYMOUS_ROLE,
            ANONYMOUS_USER_ID,
            DEFAULT_TENANT_ID,
            SYSTEM_TENANT_ID,
        )

        assert SYSTEM_TENANT_ID == 0
        assert DEFAULT_TENANT_ID == 1
        assert ANONYMOUS_USER_ID == 0
        assert ANONYMOUS_ROLE == "anonymous"
        logger.info("test_identity_constants_are_explicit 完成")

    # 方法作用：验证单租户匿名兼容和多租户认证门禁。
    # Args: self - pytest 测试类实例。
    # Returns: 无返回值，断言失败时由 pytest 报告。
    @pytest.mark.parametrize(
        ("multi_tenant", "is_probe", "required"),
        [(False, False, False), (False, True, False), (True, False, True), (True, True, False)],
    )
    def test_authentication_requirement_matrix(
        self,
        multi_tenant: bool,
        is_probe: bool,
        required: bool,
    ) -> None:
        """只有多租户普通业务请求必须认证，身份探测始终可访问。"""
        logger.debug("test_authentication_requirement_matrix 入口")
        from src.security.tenant_policy import TenantPolicy

        policy = TenantPolicy(multi_tenant=multi_tenant)

        assert policy.requires_authentication(is_probe=is_probe) is required
        logger.info("test_authentication_requirement_matrix 完成")

    # 方法作用：验证租户过滤条件按部署模式和显式查询统一生成。
    # Args: self - pytest 测试类实例。
    # Returns: 无返回值，断言失败时由 pytest 报告。
    @pytest.mark.parametrize(
        ("multi_tenant", "explicit", "expected"),
        [
            (False, False, {}),
            (False, True, {"tenant_id": "4"}),
            (True, False, {"tenant_id": "4"}),
            (True, True, {"tenant_id": "4"}),
        ],
    )
    def test_tenant_filter_matrix(
        self,
        multi_tenant: bool,
        explicit: bool,
        expected: dict[str, str],
    ) -> None:
        """单租户旧数据默认不加过滤，显式租户和多租户必须精确过滤。"""
        logger.debug("test_tenant_filter_matrix 入口")
        from src.security.tenant_policy import TenantPolicy

        policy = TenantPolicy(multi_tenant=multi_tenant)

        assert policy.tenant_filter(4, explicit=explicit) == expected
        logger.info("test_tenant_filter_matrix 完成")

    # 方法作用：验证多租户拒绝匿名身份，单租户保留匿名兼容。
    # Args: self - pytest 测试类实例。
    # Returns: 无返回值，断言失败时由 pytest 报告。
    def test_identity_validation_fails_closed_in_multi_tenant(self) -> None:
        """多租户请求不能使用 tenant=1/user=0 的匿名兼容身份。"""
        logger.debug("test_identity_validation_fails_closed_in_multi_tenant 入口")
        from src.security.tenant_policy import RequestIdentity, TenantPolicy

        anonymous = RequestIdentity.anonymous()
        assert TenantPolicy(multi_tenant=False).validate_identity(anonymous) is anonymous
        with pytest.raises(PermissionError, match="身份"):
            TenantPolicy(multi_tenant=True).validate_identity(anonymous)
        logger.info("test_identity_validation_fails_closed_in_multi_tenant 完成")

    # 方法作用：验证 system、tenant、private 写权限矩阵集中生效。
    # Args: self - pytest 测试类实例。
    # Returns: 无返回值，断言失败时由 pytest 报告。
    @pytest.mark.parametrize(
        ("scope", "role", "user_id", "expected"),
        [
            ("system", "super_admin", 1, True),
            ("system", "tenant_admin", 1, False),
            ("tenant", "tenant_admin", 1, True),
            ("tenant", "analyst", 1, False),
            ("private", "analyst", 1, True),
            ("private", "anonymous", 0, False),
        ],
    )
    def test_write_scope_matrix(
        self,
        scope: str,
        role: str,
        user_id: int,
        expected: bool,
    ) -> None:
        """作用域写权限不得由各 API 自行解释。"""
        logger.debug("test_write_scope_matrix 入口")
        from src.security.tenant_policy import RequestIdentity, TenantPolicy

        identity = RequestIdentity(tenant_id=4, user_id=user_id, role=role)

        assert TenantPolicy(multi_tenant=True).can_write_scope(scope, identity) is expected
        logger.info("test_write_scope_matrix 完成")


class TestTenantPolicyStaticBoundary:
    """验证业务模块不再直接读取 multi_tenant 配置。"""

    # 方法作用：用 AST 查找业务层直接访问 get_settings().multi_tenant 的残留。
    # Args: self - pytest 测试类实例。
    # Returns: 无返回值，断言失败时由 pytest 报告。
    def test_business_modules_do_not_branch_on_settings_multi_tenant(self) -> None:
        """租户模式判断必须集中到 TenantPolicy。"""
        logger.debug("test_business_modules_do_not_branch_on_settings_multi_tenant 入口")
        allowed = {
            Path("src/config.py"),
            Path("src/app_context.py"),
            Path("src/security/tenant_policy.py"),
        }
        offenders: list[str] = []
        for path in (ROOT / "src").rglob("*.py"):
            relative_path = path.relative_to(ROOT)
            if relative_path in allowed:
                continue
            tree = ast.parse(path.read_text(encoding="utf-8"))
            for node in ast.walk(tree):
                if not isinstance(node, ast.Attribute) or node.attr != "multi_tenant":
                    continue
                value = node.value
                if (
                    isinstance(value, ast.Call)
                    and isinstance(value.func, ast.Name)
                    and value.func.id == "get_settings"
                ):
                    offenders.append(f"{relative_path}:{node.lineno}")

        assert offenders == []
        logger.info("test_business_modules_do_not_branch_on_settings_multi_tenant 完成")

    # 方法作用：验证业务调用读取当前 AppContext 中唯一的 TenantPolicy。
    # Args: self - pytest 测试类实例。
    # Returns: 无返回值，断言失败时由 pytest 报告。
    def test_get_tenant_policy_uses_current_app_context(self) -> None:
        """并发应用不得通过默认配置共享或串用租户策略。"""
        logger.debug("test_get_tenant_policy_uses_current_app_context 入口")
        from types import SimpleNamespace

        from src.app_context import AppContext, get_tenant_policy, use_app_context

        first = AppContext(SimpleNamespace(multi_tenant=False))
        second = AppContext(SimpleNamespace(multi_tenant=True))

        with use_app_context(first):
            assert get_tenant_policy() is first.tenant_policy
        with use_app_context(second):
            assert get_tenant_policy() is second.tenant_policy
        logger.info("test_get_tenant_policy_uses_current_app_context 完成")
