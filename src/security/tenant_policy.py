"""租户身份常量与单/多租户行为策略。"""

from __future__ import annotations

from dataclasses import dataclass

from src.logging_config import get_logger


logger = get_logger(__name__)

SYSTEM_TENANT_ID = 0
DEFAULT_TENANT_ID = 1
ANONYMOUS_USER_ID = 0
ANONYMOUS_ROLE = "anonymous"


@dataclass(frozen=True, slots=True)
class RequestIdentity:
    """请求级用户、租户和角色身份快照。"""

    tenant_id: int
    user_id: int
    role: str

    # 方法作用：构造单租户兼容的匿名身份。
    # Args: 无。
    # Returns: default 租户下的匿名身份。
    @classmethod
    def anonymous(cls) -> "RequestIdentity":
        logger.debug("构造匿名身份入口")
        result = cls(
            tenant_id=DEFAULT_TENANT_ID,
            user_id=ANONYMOUS_USER_ID,
            role=ANONYMOUS_ROLE,
        )
        logger.info("构造匿名身份完成")
        return result

    # 方法作用：构造无请求用户的系统后台身份。
    # Args: role - 系统后台角色，默认 super_admin。
    # Returns: system 租户下的后台身份。
    @classmethod
    def system(cls, role: str = "super_admin") -> "RequestIdentity":
        logger.debug("构造系统身份入口", role=role)
        result = cls(
            tenant_id=SYSTEM_TENANT_ID,
            user_id=ANONYMOUS_USER_ID,
            role=normalize_role(role),
        )
        logger.info("构造系统身份完成", role=result.role)
        return result


# 方法作用：统一角色大小写和空值，避免权限判断出现不同语义。
# Args: role - JWT、数据库或调用方提供的原始角色。
# Returns: 规范化角色，空值回退 anonymous。
def normalize_role(role: str | None) -> str:
    logger.debug("租户策略角色规范化入口", role=role)
    result = str(role or ANONYMOUS_ROLE).strip().lower() or ANONYMOUS_ROLE
    logger.info("租户策略角色规范化完成", role=result)
    return result


@dataclass(frozen=True, slots=True)
class TenantPolicy:
    """集中定义单租户兼容和多租户失败关闭规则。"""

    multi_tenant: bool

    @property
    def datasource_isolation_enabled(self) -> bool:
        """返回数据源权限表是否必须参与授权。"""
        logger.debug("读取数据源隔离策略入口", multi_tenant=self.multi_tenant)
        result = self.multi_tenant
        logger.info("读取数据源隔离策略完成", enabled=result)
        return result

    @property
    def knowledge_isolation_enabled(self) -> bool:
        """返回知识库是否必须附加租户过滤。"""
        logger.debug("读取知识隔离策略入口", multi_tenant=self.multi_tenant)
        result = self.multi_tenant
        logger.info("读取知识隔离策略完成", enabled=result)
        return result

    # 方法作用：判断当前端点是否必须具备有效 JWT 身份。
    # Args: is_probe - 是否为允许匿名读取认证状态的探测端点。
    # Returns: 需要认证时返回 True。
    def requires_authentication(self, *, is_probe: bool = False) -> bool:
        logger.debug(
            "判断租户认证门禁入口",
            multi_tenant=self.multi_tenant,
            is_probe=is_probe,
        )
        result = self.multi_tenant and not is_probe
        logger.info("判断租户认证门禁完成", required=result)
        return result

    # 方法作用：生成知识库或向量库使用的租户精确过滤条件。
    # Args: tenant_id - 当前或显式租户；explicit - 是否为调用方显式指定租户。
    # Returns: 需要隔离时返回 tenant_id 字符串过滤，否则返回空字典。
    def tenant_filter(self, tenant_id: int, *, explicit: bool = False) -> dict[str, str]:
        logger.debug(
            "生成租户过滤入口",
            tenant_id=tenant_id,
            explicit=explicit,
            multi_tenant=self.multi_tenant,
        )
        if tenant_id < 0:
            logger.error("生成租户过滤失败", tenant_id=tenant_id)
            raise ValueError("tenant_id 不能为负数")
        result = (
            {"tenant_id": str(tenant_id)}
            if self.multi_tenant or explicit
            else {}
        )
        logger.info("生成租户过滤完成", applied=bool(result), tenant_id=tenant_id)
        return result

    # 方法作用：校验请求身份是否满足当前部署模式的最小安全要求。
    # Args: identity - 待校验身份快照。
    # Returns: 校验通过的原身份对象。
    def validate_identity(self, identity: RequestIdentity) -> RequestIdentity:
        logger.debug(
            "校验租户身份入口",
            tenant_id=identity.tenant_id,
            user_id=identity.user_id,
            role=identity.role,
            multi_tenant=self.multi_tenant,
        )
        role = normalize_role(identity.role)
        invalid = self.multi_tenant and (
            identity.user_id <= ANONYMOUS_USER_ID
            or role == ANONYMOUS_ROLE
            or (identity.tenant_id <= SYSTEM_TENANT_ID and role != "super_admin")
        )
        if invalid:
            logger.warning(
                "租户身份校验拒绝",
                tenant_id=identity.tenant_id,
                user_id=identity.user_id,
                role=role,
            )
            raise PermissionError("多租户请求身份无效")
        logger.info("校验租户身份完成", tenant_id=identity.tenant_id, user_id=identity.user_id)
        return identity

    # 方法作用：集中判断当前身份能否写入 system、tenant 或 private 作用域。
    # Args: scope - 目标作用域；identity - 当前请求身份。
    # Returns: 允许写入时返回 True。
    def can_write_scope(self, scope: str, identity: RequestIdentity) -> bool:
        normalized_scope = str(scope or "").strip().lower()
        role = normalize_role(identity.role)
        logger.debug(
            "判断租户作用域写权限入口",
            scope=normalized_scope,
            role=role,
            tenant_id=identity.tenant_id,
            user_id=identity.user_id,
        )
        if normalized_scope == "system":
            result = role == "super_admin"
        elif normalized_scope == "tenant":
            result = (
                role in {"super_admin", "tenant_admin"}
                and identity.tenant_id >= DEFAULT_TENANT_ID
            )
        elif normalized_scope == "private":
            result = identity.user_id > ANONYMOUS_USER_ID or (
                not self.multi_tenant and role == ANONYMOUS_ROLE
            )
        else:
            logger.error("判断租户作用域写权限失败", scope=normalized_scope)
            raise ValueError(f"不支持的资源作用域: {scope}")
        if not result:
            logger.warning(
                "租户作用域写权限拒绝",
                scope=normalized_scope,
                role=role,
                tenant_id=identity.tenant_id,
                user_id=identity.user_id,
            )
        logger.info("判断租户作用域写权限完成", scope=normalized_scope, allowed=result)
        return result
