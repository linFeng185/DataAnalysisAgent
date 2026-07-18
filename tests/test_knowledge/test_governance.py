"""三范围知识权限与角色边界测试，覆盖 6.9.1 和 12.4.6。"""

from __future__ import annotations

import logging

import pytest

logger = logging.getLogger(__name__)


class TestKnowledgeGovernance:
    """验证平台管理员、租户管理员和普通用户的知识写权限。"""

    # 验证三范围写权限严格区分平台管理员和租户管理员。
    # Args: self - pytest 测试类实例；scope/role/user_id/allowed - 权限场景。
    # Returns: 无返回值，断言失败时由 pytest 报告。
    @pytest.mark.parametrize(
        ("scope", "role", "user_id", "allowed"),
        [
            ("system", "super_admin", 1, True),
            ("system", "tenant_admin", 2, False),
            ("system", "analyst", 3, False),
            ("tenant", "super_admin", 1, True),
            ("tenant", "tenant_admin", 2, True),
            ("tenant", "analyst", 3, False),
            ("private", "super_admin", 1, True),
            ("private", "tenant_admin", 2, True),
            ("private", "analyst", 3, True),
            ("private", "anonymous", 0, False),
        ],
    )
    def test_scope_write_permission_matrix(
        self,
        scope: str,
        role: str,
        user_id: int,
        allowed: bool,
    ) -> None:
        """只有对应角色才能写入 system、tenant、private 范围。"""
        logger.debug(
            "test_scope_write_permission_matrix 入口",
            extra={"scope": scope, "role": role},
        )
        try:
            # Arrange / Act：按角色和身份检查目标范围写权限。
            from src.knowledge.governance import can_write_knowledge_scope

            result = can_write_knowledge_scope(
                scope,
                role=role,
                user_id=user_id,
                multi_tenant=True,
            )

            # Assert：平台和租户管理边界不可互相替代。
            assert result is allowed
            logger.info("test_scope_write_permission_matrix 完成")
        except Exception as exc:
            logger.error(
                "test_scope_write_permission_matrix 异常: %s",
                exc,
                exc_info=True,
            )
            raise

    # 验证单租户开发模式继续允许匿名用户写个人知识。
    # Args: self - pytest 测试类实例。
    # Returns: 无返回值，断言失败时由 pytest 报告。
    def test_anonymous_private_upload_only_allowed_in_single_tenant(self) -> None:
        """匿名兼容仅限 multi_tenant=false，不能扩大生产权限。"""
        logger.debug("test_anonymous_private_upload_only_allowed_in_single_tenant 入口")
        try:
            # Arrange / Act：分别检查单租户和多租户匿名身份。
            from src.knowledge.governance import can_write_knowledge_scope

            single_tenant = can_write_knowledge_scope(
                "private", role="anonymous", user_id=0, multi_tenant=False,
            )
            multi_tenant = can_write_knowledge_scope(
                "private", role="anonymous", user_id=0, multi_tenant=True,
            )

            # Assert：只保留本地开发兼容路径。
            assert single_tenant is True
            assert multi_tenant is False
            logger.info("test_anonymous_private_upload_only_allowed_in_single_tenant 完成")
        except Exception as exc:
            logger.error(
                "test_anonymous_private_upload_only_allowed_in_single_tenant 异常: %s",
                exc,
                exc_info=True,
            )
            raise

    # 验证未知知识范围必须失败关闭。
    # Args: self - pytest 测试类实例。
    # Returns: 无返回值，断言失败时由 pytest 报告。
    def test_unknown_scope_is_rejected(self) -> None:
        """未声明范围不能回退为公共知识。"""
        logger.debug("test_unknown_scope_is_rejected 入口")
        try:
            # Arrange / Act / Assert：非法范围应抛出数据校验错误。
            from src.knowledge.governance import normalize_knowledge_scope

            with pytest.raises(ValueError):
                normalize_knowledge_scope("public")
            logger.info("test_unknown_scope_is_rejected 完成")
        except Exception as exc:
            logger.error("test_unknown_scope_is_rejected 异常: %s", exc, exc_info=True)
            raise
