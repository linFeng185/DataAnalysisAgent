"""知识库三范围权限模型，集中约束平台、租户和个人知识操作。"""

from __future__ import annotations

from enum import StrEnum

from src.logging_config import get_logger

logger = get_logger(__name__)


class KnowledgeScope(StrEnum):
    """知识内容可见范围。"""

    SYSTEM = "system"
    TENANT = "tenant"
    PRIVATE = "private"


# 规范化并校验知识范围，未知值失败关闭。
# Args: scope - 待校验的范围字符串或枚举。
# Returns: 合法的 KnowledgeScope 枚举。
def normalize_knowledge_scope(scope: str | KnowledgeScope) -> KnowledgeScope:
    logger.debug("规范化知识范围入口", scope=str(scope))
    try:
        result = scope if isinstance(scope, KnowledgeScope) else KnowledgeScope(str(scope).strip().lower())
        logger.info("规范化知识范围完成", scope=result.value)
        return result
    except (TypeError, ValueError) as exc:
        logger.error("规范化知识范围失败", scope=str(scope), error=str(exc), exc_info=True)
        raise ValueError(f"不支持的知识范围: {scope}") from exc


# 判断角色是否为平台超级管理员。
# Args: role - 当前用户角色。
# Returns: 是超级管理员时返回 True。
def is_super_admin(role: str) -> bool:
    logger.debug("检查超级管理员入口", role=role)
    result = str(role).strip().lower() == "super_admin"
    logger.info("检查超级管理员完成", role=role, allowed=result)
    return result


# 判断角色是否为租户管理员。
# Args: role - 当前用户角色。
# Returns: 是租户管理员时返回 True。
def is_tenant_admin(role: str) -> bool:
    logger.debug("检查租户管理员入口", role=role)
    result = str(role).strip().lower() == "tenant_admin"
    logger.info("检查租户管理员完成", role=role, allowed=result)
    return result


# 判断当前身份是否可以写入指定知识范围。
# Args: scope - system/tenant/private；role - 当前角色；user_id - 当前用户 ID；
#       multi_tenant - 是否启用多租户认证。
# Returns: 允许写入时返回 True。
def can_write_knowledge_scope(
    scope: str | KnowledgeScope,
    *,
    role: str,
    user_id: int,
    multi_tenant: bool,
) -> bool:
    logger.debug(
        "检查知识范围写权限入口",
        scope=str(scope),
        role=role,
        user_id=user_id,
        multi_tenant=multi_tenant,
    )
    normalized = normalize_knowledge_scope(scope)
    if normalized is KnowledgeScope.SYSTEM:
        result = is_super_admin(role)
    elif normalized is KnowledgeScope.TENANT:
        result = is_super_admin(role) or is_tenant_admin(role)
    else:
        result = user_id > 0 or (not multi_tenant and role == "anonymous")
    if not result:
        logger.warning(
            "知识范围写权限拒绝",
            scope=normalized.value,
            role=role,
            user_id=user_id,
        )
    logger.info("检查知识范围写权限完成", scope=normalized.value, role=role, allowed=result)
    return result


# 判断当前身份是否可以管理指定所有者的知识。
# Args: scope - 知识范围；role - 当前角色；current_tenant_id - 当前租户；
#       resource_tenant_id - 资源租户；current_user_id - 当前用户；owner_user_id - 资源所有者。
# Returns: 允许修改或删除时返回 True。
def can_manage_knowledge_resource(
    scope: str | KnowledgeScope,
    *,
    role: str,
    current_tenant_id: int,
    resource_tenant_id: int | None,
    current_user_id: int,
    owner_user_id: int | None,
) -> bool:
    logger.debug(
        "检查知识资源管理权限入口",
        scope=str(scope),
        role=role,
        current_tenant_id=current_tenant_id,
        resource_tenant_id=resource_tenant_id,
        current_user_id=current_user_id,
        owner_user_id=owner_user_id,
    )
    normalized = normalize_knowledge_scope(scope)
    if is_super_admin(role):
        result = True
    elif normalized is KnowledgeScope.SYSTEM:
        result = False
    elif resource_tenant_id != current_tenant_id:
        result = False
    elif normalized is KnowledgeScope.TENANT:
        result = is_tenant_admin(role)
    else:
        result = owner_user_id == current_user_id
    if not result:
        logger.warning(
            "知识资源管理权限拒绝",
            scope=normalized.value,
            role=role,
            current_tenant_id=current_tenant_id,
            resource_tenant_id=resource_tenant_id,
        )
    logger.info("检查知识资源管理权限完成", scope=normalized.value, allowed=result)
    return result
