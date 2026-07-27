"""平台超级管理员维护 API 访问策略与接口 IP 规则的路由。"""

from __future__ import annotations

import ipaddress
from typing import Literal

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field, field_validator, model_validator

from src.logging_config import get_logger


logger = get_logger(__name__)
router = APIRouter(prefix="/admin", tags=["api-access-policy"])
ProtectedAuthMode = Literal["jwt", "jwt_or_admin_key", "super_admin"]
AccessLogModeValue = Literal["standard", "security", "audit", "none"]
PathTypeValue = Literal["exact", "template"]


class AccessPolicyCreateRequest(BaseModel):
    """创建数据库动态访问策略的请求。"""

    policy_key: str = Field(..., min_length=2, max_length=64, pattern=r"^[a-z][a-z0-9_-]+$")
    path: str = Field(..., min_length=1, max_length=512, pattern=r"^/")
    path_type: PathTypeValue = "exact"
    methods: list[str] = Field(min_length=1)
    auth_mode: ProtectedAuthMode
    access_log_mode: AccessLogModeValue = "standard"
    priority: int = Field(default=0, ge=-100_000, le=100_000)
    enabled: bool = True
    description: str = Field(default="", max_length=500)

    # 方法作用：规范化动态策略 HTTP 方法并拒绝空值。
    # Args: methods - 请求中的 HTTP 方法列表。
    # Returns: 去重后的大写方法列表。
    @field_validator("methods")
    @classmethod
    def normalize_methods(cls, methods: list[str]) -> list[str]:
        result: list[str] = []
        for method in methods:
            value = str(method).strip().upper()
            if not value or not value.replace("-", "").isalpha():
                raise ValueError("HTTP 方法格式无效")
            if value not in result:
                result.append(value)
        if not result:
            raise ValueError("至少需要一个 HTTP 方法")
        return result


class AccessPolicyUpdateRequest(BaseModel):
    """修改数据库动态访问策略的请求。"""

    path: str | None = Field(default=None, min_length=1, max_length=512, pattern=r"^/")
    path_type: PathTypeValue | None = None
    methods: list[str] | None = None
    auth_mode: ProtectedAuthMode | None = None
    access_log_mode: AccessLogModeValue | None = None
    priority: int | None = Field(default=None, ge=-100_000, le=100_000)
    enabled: bool | None = None
    description: str | None = Field(default=None, max_length=500)

    # 方法作用：规范化可选 HTTP 方法列表。
    # Args: methods - 可选方法列表。
    # Returns: 空值或去重后的大写方法列表。
    @field_validator("methods")
    @classmethod
    def normalize_optional_methods(cls, methods: list[str] | None) -> list[str] | None:
        if methods is None:
            return None
        return AccessPolicyCreateRequest.normalize_methods(methods)

    # 方法作用：拒绝没有任何更新字段的空请求。
    # Args: self - 已完成字段校验的更新请求。
    # Returns: 至少包含一个字段的请求。
    @model_validator(mode="after")
    def validate_not_empty(self) -> "AccessPolicyUpdateRequest":
        if not self.model_fields_set:
            raise ValueError("没有可更新字段")
        return self


class IpRuleCreateRequest(BaseModel):
    """创建接口 CIDR 黑白名单的请求。"""

    action: Literal["allow", "deny"]
    cidr: str
    enabled: bool = True
    description: str = Field(default="", max_length=500)

    # 方法作用：把单 IP 或 CIDR 规范化为网络地址。
    # Args: cidr - IPv4、IPv6 或 CIDR 字符串。
    # Returns: 规范化 CIDR。
    @field_validator("cidr")
    @classmethod
    def normalize_cidr(cls, cidr: str) -> str:
        return str(ipaddress.ip_network(cidr.strip(), strict=False))


class IpRuleUpdateRequest(BaseModel):
    """修改接口 IP 规则的请求。"""

    action: Literal["allow", "deny"] | None = None
    cidr: str | None = None
    enabled: bool | None = None
    description: str | None = Field(default=None, max_length=500)

    # 方法作用：规范化可选 CIDR。
    # Args: cidr - 可选 IPv4、IPv6 或 CIDR。
    # Returns: 空值或规范化 CIDR。
    @field_validator("cidr")
    @classmethod
    def normalize_optional_cidr(cls, cidr: str | None) -> str | None:
        return None if cidr is None else str(ipaddress.ip_network(cidr.strip(), strict=False))

    # 方法作用：拒绝没有任何更新字段的空 IP 规则请求。
    # Args: self - 已完成字段校验的更新请求。
    # Returns: 至少包含一个字段的请求。
    @model_validator(mode="after")
    def validate_not_empty(self) -> "IpRuleUpdateRequest":
        if not self.model_fields_set:
            raise ValueError("没有可更新字段")
        return self


# 方法作用：把数据库唯一约束等写入异常转换为稳定 HTTP 错误。
# Args: exc - 数据库异常；operation - 当前操作说明。
# Returns: 始终抛出 HTTPException。
def _raise_policy_write_error(exc: Exception, operation: str) -> None:
    if type(exc).__name__ in {"UniqueViolationError", "ForeignKeyViolationError"}:
        logger.warning("API 访问策略写入冲突", operation=operation)
        raise HTTPException(409, f"{operation}冲突") from exc
    logger.error("API 访问策略写入失败", operation=operation, error=str(exc), exc_info=True)
    raise HTTPException(500, f"{operation}失败") from exc


# 方法作用：刷新当前进程的 API 访问策略内存快照。
# Args: 无。
# Returns: 无返回值。
async def _refresh_snapshot() -> None:
    from src.security.api_access_policy import get_api_access_policy_manager

    await get_api_access_policy_manager().refresh()


@router.get("/access-policies")
# 方法作用：返回 YAML 与数据库合并策略以及全部 IP 规则。
# Args: 无。
# Returns: 当前原子策略快照。
async def list_access_policies() -> dict:
    """YAML 策略标记为只读，数据库策略带有可修改编号。"""
    from src.api.auth import require_super_admin
    from src.security.api_access_policy import get_api_access_policy_manager

    logger.debug("访问策略列表入口")
    require_super_admin()
    result = get_api_access_policy_manager().export_snapshot()
    logger.info(
        "访问策略列表完成",
        policy_count=len(result["policies"]),
        rule_count=len(result["ip_rules"]),
    )
    return result


@router.post("/access-policies", status_code=201)
# 方法作用：创建只能收紧或保持认证边界的数据库动态策略。
# Args: request - 动态路径、认证和日志配置。
# Returns: 新建策略记录。
async def create_access_policy(request: AccessPolicyCreateRequest) -> dict:
    """public 与 optional 不在请求枚举中，避免数据库扩大匿名面。"""
    from src.api.auth import get_current_user_id, require_super_admin
    from src.memory.pg_pool import pg_connection
    from src.security.tenant_policy import DEFAULT_TENANT_ID

    logger.debug("创建 API 访问策略入口", policy_key=request.policy_key)
    require_super_admin()
    try:
        async with pg_connection(
            tenant_id=DEFAULT_TENANT_ID,
            user_id=get_current_user_id(),
            role="super_admin",
        ) as connection:
            row = await connection.fetchrow(
                "INSERT INTO api_access_policies "
                "(policy_key, path, path_type, methods, auth_mode, access_log_mode, priority, "
                "enabled, description, created_by) VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10) "
                "RETURNING id, policy_key, path, path_type, methods, auth_mode, "
                "access_log_mode, priority, enabled, description",
                request.policy_key,
                request.path,
                request.path_type,
                request.methods,
                request.auth_mode,
                request.access_log_mode,
                request.priority,
                request.enabled,
                request.description,
                get_current_user_id(),
            )
        await _refresh_snapshot()
        result = dict(row)
        logger.info("创建 API 访问策略完成", policy_id=result["id"], policy_key=request.policy_key)
        return result
    except HTTPException:
        raise
    except Exception as exc:
        _raise_policy_write_error(exc, "创建访问策略")


@router.patch("/access-policies/{policy_id}")
# 方法作用：修改数据库动态策略并刷新内存快照。
# Args: policy_id - 数据库策略编号；request - 可选更新字段。
# Returns: 更新后的策略记录。
async def update_access_policy(policy_id: int, request: AccessPolicyUpdateRequest) -> dict:
    """YAML 策略没有数据库编号，不能通过此接口修改。"""
    from src.api.auth import get_current_user_id, require_super_admin
    from src.memory.pg_pool import pg_connection
    from src.security.tenant_policy import DEFAULT_TENANT_ID

    logger.debug("更新 API 访问策略入口", policy_id=policy_id)
    require_super_admin()
    try:
        async with pg_connection(
            tenant_id=DEFAULT_TENANT_ID,
            user_id=get_current_user_id(),
            role="super_admin",
        ) as connection:
            row = await connection.fetchrow(
                "UPDATE api_access_policies SET path=COALESCE($1,path), "
                "path_type=COALESCE($2,path_type), methods=COALESCE($3,methods), "
                "auth_mode=COALESCE($4,auth_mode), access_log_mode=COALESCE($5,access_log_mode), "
                "priority=COALESCE($6,priority), enabled=COALESCE($7,enabled), "
                "description=COALESCE($8,description), updated_at=NOW() WHERE id=$9 "
                "RETURNING id, policy_key, path, path_type, methods, auth_mode, "
                "access_log_mode, priority, enabled, description",
                request.path,
                request.path_type,
                request.methods,
                request.auth_mode,
                request.access_log_mode,
                request.priority,
                request.enabled,
                request.description,
                policy_id,
            )
        if row is None:
            raise HTTPException(404, "动态访问策略不存在")
        await _refresh_snapshot()
        result = dict(row)
        logger.info("更新 API 访问策略完成", policy_id=policy_id)
        return result
    except HTTPException:
        raise
    except Exception as exc:
        _raise_policy_write_error(exc, "更新访问策略")


@router.delete("/access-policies/{policy_id}")
# 方法作用：删除数据库动态策略及引用其策略键的 IP 规则。
# Args: policy_id - 数据库策略编号。
# Returns: 删除状态和策略编号。
async def delete_access_policy(policy_id: int) -> dict:
    """删除使用事务避免留下仅引用已删除动态策略的规则。"""
    from src.api.auth import get_current_user_id, require_super_admin
    from src.memory.pg_pool import pg_connection
    from src.security.tenant_policy import DEFAULT_TENANT_ID

    logger.debug("删除 API 访问策略入口", policy_id=policy_id)
    require_super_admin()
    try:
        async with pg_connection(
            tenant_id=DEFAULT_TENANT_ID,
            user_id=get_current_user_id(),
            role="super_admin",
        ) as connection:
            row = await connection.fetchrow(
                "DELETE FROM api_access_policies WHERE id=$1 RETURNING id, policy_key",
                policy_id,
            )
            if row is None:
                raise HTTPException(404, "动态访问策略不存在")
            await connection.execute(
                "DELETE FROM api_ip_rules WHERE policy_key=$1",
                row["policy_key"],
            )
        await _refresh_snapshot()
        logger.info("删除 API 访问策略完成", policy_id=policy_id)
        return {"status": "deleted", "policy_id": policy_id}
    except HTTPException:
        raise
    except Exception as exc:
        _raise_policy_write_error(exc, "删除访问策略")


@router.post("/access-policies/{policy_key}/ip-rules", status_code=201)
# 方法作用：为 YAML 或数据库策略创建接口级 CIDR 黑白名单。
# Args: policy_key - 合并快照策略键；request - 动作和 CIDR。
# Returns: 新建 IP 规则。
async def create_ip_rule(policy_key: str, request: IpRuleCreateRequest) -> dict:
    """写入前验证策略存在，避免无效孤立规则。"""
    from src.api.auth import get_current_user_id, require_super_admin
    from src.memory.pg_pool import pg_connection
    from src.security.api_access_policy import get_api_access_policy_manager
    from src.security.tenant_policy import DEFAULT_TENANT_ID

    logger.debug("创建 API IP 规则入口", policy_key=policy_key, action=request.action)
    require_super_admin()
    manager = get_api_access_policy_manager()
    if not manager.has_policy(policy_key):
        raise HTTPException(404, "访问策略不存在")
    try:
        async with pg_connection(
            tenant_id=DEFAULT_TENANT_ID,
            user_id=get_current_user_id(),
            role="super_admin",
        ) as connection:
            row = await connection.fetchrow(
                "INSERT INTO api_ip_rules "
                "(policy_key, action, cidr, enabled, description, created_by) "
                "VALUES ($1,$2,$3::cidr,$4,$5,$6) "
                "RETURNING id, policy_key, action, cidr::text AS cidr, enabled, description",
                policy_key,
                request.action,
                request.cidr,
                request.enabled,
                request.description,
                get_current_user_id(),
            )
        await _refresh_snapshot()
        result = dict(row)
        logger.info("创建 API IP 规则完成", rule_id=result["id"], policy_key=policy_key)
        return result
    except HTTPException:
        raise
    except Exception as exc:
        _raise_policy_write_error(exc, "创建 IP 规则")


@router.patch("/access-ip-rules/{rule_id}")
# 方法作用：修改接口 IP 规则并刷新内存快照。
# Args: rule_id - IP 规则编号；request - 可选更新字段。
# Returns: 更新后的 IP 规则。
async def update_ip_rule(rule_id: int, request: IpRuleUpdateRequest) -> dict:
    """CIDR 更新通过 PostgreSQL cidr 类型再次校验。"""
    from src.api.auth import get_current_user_id, require_super_admin
    from src.memory.pg_pool import pg_connection
    from src.security.tenant_policy import DEFAULT_TENANT_ID

    logger.debug("更新 API IP 规则入口", rule_id=rule_id)
    require_super_admin()
    try:
        async with pg_connection(
            tenant_id=DEFAULT_TENANT_ID,
            user_id=get_current_user_id(),
            role="super_admin",
        ) as connection:
            row = await connection.fetchrow(
                "UPDATE api_ip_rules SET action=COALESCE($1,action), "
                "cidr=COALESCE($2::cidr,cidr), enabled=COALESCE($3,enabled), "
                "description=COALESCE($4,description), updated_at=NOW() WHERE id=$5 "
                "RETURNING id, policy_key, action, cidr::text AS cidr, enabled, description",
                request.action,
                request.cidr,
                request.enabled,
                request.description,
                rule_id,
            )
        if row is None:
            raise HTTPException(404, "IP 规则不存在")
        await _refresh_snapshot()
        result = dict(row)
        logger.info("更新 API IP 规则完成", rule_id=rule_id)
        return result
    except HTTPException:
        raise
    except Exception as exc:
        _raise_policy_write_error(exc, "更新 IP 规则")


@router.delete("/access-ip-rules/{rule_id}")
# 方法作用：删除接口 IP 规则并刷新内存快照。
# Args: rule_id - IP 规则编号。
# Returns: 删除状态和规则编号。
async def delete_ip_rule(rule_id: int) -> dict:
    """不存在的规则返回 404。"""
    from src.api.auth import get_current_user_id, require_super_admin
    from src.memory.pg_pool import pg_connection
    from src.security.tenant_policy import DEFAULT_TENANT_ID

    logger.debug("删除 API IP 规则入口", rule_id=rule_id)
    require_super_admin()
    try:
        async with pg_connection(
            tenant_id=DEFAULT_TENANT_ID,
            user_id=get_current_user_id(),
            role="super_admin",
        ) as connection:
            row = await connection.fetchrow(
                "DELETE FROM api_ip_rules WHERE id=$1 RETURNING id",
                rule_id,
            )
        if row is None:
            raise HTTPException(404, "IP 规则不存在")
        await _refresh_snapshot()
        logger.info("删除 API IP 规则完成", rule_id=rule_id)
        return {"status": "deleted", "rule_id": rule_id}
    except HTTPException:
        raise
    except Exception as exc:
        _raise_policy_write_error(exc, "删除 IP 规则")
