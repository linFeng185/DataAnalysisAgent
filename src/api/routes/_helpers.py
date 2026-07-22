"""API 路由共享依赖与授权辅助函数。"""

from __future__ import annotations

import time

from fastapi import APIRouter, HTTPException

from src.logging_config import get_logger

logger = get_logger(__name__)
router = APIRouter()
_started_at = time.time()


def _app():
    from src.graph.workflow import app
    return app


def _registry():
    from src.datasource.registry import get_registry
    return get_registry()

# ---- MCP Server 管理 ----


# 方法作用：规范化扩展资源作用域并校验当前身份写权限。
# Args: scope - system/tenant/private 作用域。
# Returns: 规范化作用域、当前租户、当前用户和当前角色。
def _authorize_extension_scope(scope: str) -> tuple[str, int, int, str]:
    """Skill 和 MCP 共用与知识库一致的三级写权限。"""
    logger.debug("扩展资源作用域授权入口", scope=scope)
    from src.api.auth import (
        get_current_identity, get_current_role, get_current_tenant_id, get_current_user_id,
    )
    from src.app_context import get_tenant_policy
    from src.knowledge.governance import normalize_knowledge_scope

    try:
        normalized = normalize_knowledge_scope(scope).value
    except ValueError as exc:
        logger.warning("扩展资源作用域无效", scope=scope)
        raise HTTPException(400, str(exc)) from exc
    tenant_id = get_current_tenant_id()
    user_id = get_current_user_id()
    role = get_current_role()
    if not get_tenant_policy().can_write_scope(
        normalized,
        get_current_identity(),
    ):
        logger.warning(
            "扩展资源作用域授权拒绝",
            scope=normalized,
            role=role,
            tenant_id=tenant_id,
            user_id=user_id,
        )
        raise HTTPException(403, f"当前角色无权写入 {normalized} 扩展资源")
    logger.info(
        "扩展资源作用域授权完成",
        scope=normalized,
        role=role,
        tenant_id=tenant_id,
        user_id=user_id,
    )
    return normalized, tenant_id, user_id, role
