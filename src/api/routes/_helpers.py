"""API 路由共享依赖与授权辅助函数。"""

from __future__ import annotations

import inspect
import time
from typing import Any

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


# 方法作用：调用支持可选租户参数的异步 Registry 方法，并安全兼容旧签名。
# Args: method - 待调用的方法；args - 位置参数；tenant_id - 当前租户 ID。
# Returns: 被调用方法的返回值；同步返回值也会原样返回。
async def _call_with_optional_tenant(
    method: Any,
    *args: Any,
    tenant_id: int | None,
) -> Any:
    """只在方法签名明确不支持租户参数时走旧 Provider 兼容路径。"""
    try:
        signature = inspect.signature(method)
        parameters = signature.parameters.values()
        tenant_parameter = signature.parameters.get("tenant_id")
        supports_positional = bool(
            tenant_parameter
            and tenant_parameter.kind is inspect.Parameter.POSITIONAL_ONLY
        )
        supports_keyword = (
            bool(tenant_parameter) and not supports_positional
        ) or any(
            parameter.kind is inspect.Parameter.VAR_KEYWORD
            for parameter in parameters
        )
    except (TypeError, ValueError):
        supports_positional = False
        supports_keyword = None

    logger.debug(
        "API 租户参数兼容调用入口",
        method=getattr(method, "__qualname__", repr(method)),
        tenant_id=tenant_id,
        supports_positional=supports_positional,
        supports_keyword=supports_keyword,
    )
    if supports_positional:
        result = method(*args, tenant_id)
        return await result if inspect.isawaitable(result) else result
    if supports_keyword is False:
        result = method(*args)
        return await result if inspect.isawaitable(result) else result

    try:
        result = method(*args, tenant_id=tenant_id)
    except TypeError as exc:
        # 未知签名时仅识别明确的未知关键字错误，避免吞掉方法体内部异常。
        message = str(exc).lower()
        if supports_keyword is True or "unexpected keyword argument" not in message:
            raise
        logger.info(
            "API 租户参数兼容回退",
            method=getattr(method, "__qualname__", repr(method)),
            reason="旧方法签名不接受 tenant_id",
        )
        result = method(*args)
    return await result if inspect.isawaitable(result) else result

# ---- MCP Server 管理 ----


# 方法作用：规范化扩展资源作用域并校验当前身份写权限。
# Args: scope - system/tenant/private 作用域。
# Returns: 规范化作用域、当前租户、当前用户和当前角色。
def _authorize_extension_scope(scope: str) -> tuple[str, int, int, str]:
    """Skill 和 MCP 共用与知识库一致的三级写权限。"""
    logger.debug("扩展资源作用域授权入口", scope=scope)
    from src.api.auth import (
        get_current_identity,
        get_current_role,
        get_current_tenant_id,
        get_current_user_id,
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
