"""API 访问策略匹配、客户端 IP 解析和数据库快照管理。"""

from __future__ import annotations

import ipaddress
import re
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any, Pattern

from starlette.datastructures import Headers
from starlette.routing import compile_path
from starlette.types import Scope

from src.config import ApiAccessConfig, ApiAccessRouteConfig
from src.logging_config import get_logger


logger = get_logger(__name__)
IpAddress = ipaddress.IPv4Address | ipaddress.IPv6Address
IpNetwork = ipaddress.IPv4Network | ipaddress.IPv6Network
_PROTECTED_ADMIN_PREFIX = "/api/v1/admin/access-"


class AuthMode(StrEnum):
    """接口支持的认证模式。"""

    PUBLIC = "public"
    OPTIONAL = "optional"
    JWT = "jwt"
    JWT_OR_ADMIN_KEY = "jwt_or_admin_key"
    SUPER_ADMIN = "super_admin"


class AccessLogMode(StrEnum):
    """接口访问摘要的日志模式。"""

    STANDARD = "standard"
    SECURITY = "security"
    AUDIT = "audit"
    NONE = "none"


class PathType(StrEnum):
    """接口路径匹配方式。"""

    EXACT = "exact"
    TEMPLATE = "template"


class IpRuleAction(StrEnum):
    """接口 IP 规则动作。"""

    ALLOW = "allow"
    DENY = "deny"


@dataclass(frozen=True, slots=True)
class AccessPolicy:
    """已编译且可在请求热路径匹配的访问策略。"""

    policy_key: str
    path: str
    path_type: PathType
    methods: tuple[str, ...]
    auth_mode: AuthMode
    access_log_mode: AccessLogMode
    source: str
    policy_id: int | None = None
    priority: int = 0
    enabled: bool = True
    description: str = ""
    path_regex: Pattern[str] | None = field(default=None, repr=False, compare=False)

    # 方法作用：判断当前方法和路径是否命中策略。
    # Args: path - 请求路径；method - HTTP 方法。
    # Returns: 同时匹配方法和路径时返回 True。
    def matches(self, path: str, method: str) -> bool:
        if not self.enabled or method.upper() not in self.methods:
            return False
        if self.path_type is PathType.EXACT:
            return path == self.path
        return self.path_regex is not None and self.path_regex.fullmatch(path) is not None

    # 方法作用：转换为平台管理 API 可序列化的策略摘要。
    # Args: self - 当前策略。
    # Returns: 不包含编译正则的字典。
    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.policy_id,
            "policy_key": self.policy_key,
            "path": self.path,
            "path_type": self.path_type.value,
            "methods": list(self.methods),
            "auth_mode": self.auth_mode.value,
            "access_log_mode": self.access_log_mode.value,
            "source": self.source,
            "priority": self.priority,
            "enabled": self.enabled,
            "description": self.description,
        }


@dataclass(frozen=True, slots=True)
class IpRule:
    """已解析为网络对象的接口 IP 规则。"""

    rule_id: int
    policy_key: str
    action: IpRuleAction
    network: IpNetwork
    enabled: bool = True
    description: str = ""

    # 方法作用：判断客户端地址是否命中当前 CIDR。
    # Args: address - 客户端 IPv4 或 IPv6 地址。
    # Returns: 地址版本一致且位于网络内时返回 True。
    def matches(self, address: IpAddress) -> bool:
        return self.enabled and address.version == self.network.version and address in self.network

    # 方法作用：转换为平台管理 API 可序列化的 IP 规则。
    # Args: self - 当前规则。
    # Returns: 包含规范化 CIDR 的字典。
    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.rule_id,
            "policy_key": self.policy_key,
            "action": self.action.value,
            "cidr": str(self.network),
            "enabled": self.enabled,
            "description": self.description,
        }


@dataclass(frozen=True, slots=True)
class AccessDecision:
    """单次请求的访问策略和 IP 判定结果。"""

    policy: AccessPolicy
    allowed: bool
    client_ip: str
    denial_reason: str = ""


# 方法作用：把配置或数据库路径编译为可重复使用的匹配正则。
# Args: path - 精确路径或 FastAPI 模板；path_type - 匹配类型。
# Returns: 模板类型返回编译正则，精确类型返回 None。
def _compile_path_regex(path: str, path_type: PathType) -> Pattern[str] | None:
    if path_type is PathType.EXACT:
        return None
    regex, _, _ = compile_path(path)
    return regex


# 方法作用：从直接连接和可信代理转发链解析真实客户端 IP。
# Args: scope - 当前 ASGI 请求作用域；trusted_proxy_cidrs - 可信代理 CIDR。
# Returns: 可信解析后的客户端地址，缺失或非法时返回未指定地址。
def resolve_client_ip(scope: Scope | dict[str, Any], trusted_proxy_cidrs: tuple[str, ...]) -> IpAddress:
    client = scope.get("client")
    direct_text = str(client[0]) if isinstance(client, (tuple, list)) and client else "0.0.0.0"
    try:
        direct = ipaddress.ip_address(direct_text.split("%", maxsplit=1)[0])
    except ValueError:
        logger.warning("客户端直连 IP 无效", client_ip=direct_text)
        return ipaddress.ip_address("0.0.0.0")
    trusted = tuple(ipaddress.ip_network(value, strict=False) for value in trusted_proxy_cidrs)
    if not any(direct.version == network.version and direct in network for network in trusted):
        return direct
    forwarded = Headers(scope=scope).get("x-forwarded-for", "")
    if not forwarded:
        return direct
    chain: list[IpAddress] = []
    try:
        chain = [
            ipaddress.ip_address(item.strip().split("%", maxsplit=1)[0])
            for item in forwarded.split(",")
            if item.strip()
        ]
    except ValueError:
        logger.warning("忽略非法 X-Forwarded-For", direct_ip=str(direct))
        return direct
    for address in reversed(chain):
        is_trusted = any(
            address.version == network.version and address in network for network in trusted
        )
        if not is_trusted:
            return address
    return chain[0] if chain else direct


class ApiAccessPolicyManager:
    """持有 YAML 基线与 PostgreSQL 动态策略的原子内存快照。"""

    # 方法作用：从当前 Settings 编译不可变 YAML 基线和紧急 IP 黑名单。
    # Args: settings - 当前应用配置。
    # Returns: 无返回值。
    def __init__(self, settings: Any) -> None:
        logger.debug("ApiAccessPolicyManager.__init__ 入口")
        raw_config = getattr(settings, "api_access", None)
        self.config = (
            raw_config if isinstance(raw_config, ApiAccessConfig)
            else ApiAccessConfig.model_validate(raw_config or {})
        )
        self.trusted_proxy_cidrs = tuple(self.config.trusted_proxy_cidrs)
        self._emergency_denies = tuple(
            ipaddress.ip_network(value, strict=False) for value in self.config.emergency_ip_deny
        )
        self._bootstrap = tuple(self._from_bootstrap(item) for item in self.config.bootstrap_policies)
        self._dynamic: tuple[AccessPolicy, ...] = ()
        self._ip_rules: tuple[IpRule, ...] = ()
        self._default = AccessPolicy(
            policy_key="__default__",
            path="*",
            path_type=PathType.EXACT,
            methods=(),
            auth_mode=AuthMode(self.config.default_auth),
            access_log_mode=AccessLogMode(self.config.default_access_log),
            source="default",
            description="默认失败关闭策略",
        )
        self._admin_policy = AccessPolicy(
            policy_key="__access_policy_admin__",
            path=f"{_PROTECTED_ADMIN_PREFIX}*",
            path_type=PathType.EXACT,
            methods=("GET", "POST", "PATCH", "DELETE"),
            auth_mode=AuthMode.SUPER_ADMIN,
            access_log_mode=AccessLogMode.AUDIT,
            source="system",
            priority=2_147_483_647,
            description="访问策略管理自保护",
        )
        logger.info("ApiAccessPolicyManager.__init__ 完成", bootstrap_count=len(self._bootstrap))

    # 方法作用：把 YAML 配置转换为已编译策略。
    # Args: self - 当前管理器；item - YAML 路由策略。
    # Returns: 已编译启动策略。
    def _from_bootstrap(self, item: ApiAccessRouteConfig) -> AccessPolicy:
        path_type = PathType(item.path_type)
        return AccessPolicy(
            policy_key=item.id,
            path=item.path,
            path_type=path_type,
            methods=tuple(item.methods),
            auth_mode=AuthMode(item.auth),
            access_log_mode=AccessLogMode(item.access_log),
            source="yaml",
            description=item.description,
            path_regex=_compile_path_regex(item.path, path_type),
        )

    # 方法作用：把数据库记录转换为已编译动态策略。
    # Args: self - 当前管理器；row - asyncpg Record 或字典。
    # Returns: 已编译动态策略。
    def _from_dynamic(self, row: Any) -> AccessPolicy:
        data = dict(row)
        path_type = PathType(str(data.get("path_type", "exact")))
        methods = tuple(str(value).upper() for value in data.get("methods", []))
        return AccessPolicy(
            policy_id=int(data["id"]),
            policy_key=str(data["policy_key"]),
            path=str(data["path"]),
            path_type=path_type,
            methods=methods,
            auth_mode=AuthMode(str(data["auth_mode"])),
            access_log_mode=AccessLogMode(str(data["access_log_mode"])),
            source="database",
            priority=int(data.get("priority", 0)),
            enabled=bool(data.get("enabled", True)),
            description=str(data.get("description", "")),
            path_regex=_compile_path_regex(str(data["path"]), path_type),
        )

    # 方法作用：把数据库记录转换为已解析 CIDR 规则。
    # Args: self - 当前管理器；row - asyncpg Record 或字典。
    # Returns: 已解析 IP 规则。
    def _from_ip_rule(self, row: Any) -> IpRule:
        data = dict(row)
        return IpRule(
            rule_id=int(data["id"]),
            policy_key=str(data["policy_key"]),
            action=IpRuleAction(str(data["action"])),
            network=ipaddress.ip_network(str(data["cidr"]), strict=False),
            enabled=bool(data.get("enabled", True)),
            description=str(data.get("description", "")),
        )

    # 方法作用：原子替换数据库动态策略和 IP 规则快照。
    # Args: policies - 数据库策略记录；ip_rules - 数据库 IP 规则记录。
    # Returns: 无返回值。
    def replace_dynamic(self, policies: list[Any], ip_rules: list[Any]) -> None:
        logger.debug("替换 API 动态策略入口", policy_count=len(policies), rule_count=len(ip_rules))
        compiled_policies = tuple(
            sorted(
                (self._from_dynamic(row) for row in policies),
                key=lambda policy: (-policy.priority, policy.policy_id or 0),
            )
        )
        compiled_rules = tuple(self._from_ip_rule(row) for row in ip_rules)
        self._dynamic = compiled_policies
        self._ip_rules = compiled_rules
        logger.info("替换 API 动态策略完成", policy_count=len(policies), rule_count=len(ip_rules))

    # 方法作用：从 PostgreSQL 重新加载动态策略和 IP 规则并原子切换快照。
    # Args: self - 当前管理器。
    # Returns: 无返回值。
    async def refresh(self) -> None:
        logger.debug("刷新 API 访问策略入口")
        from src.memory.pg_pool import pg_connection
        from src.security.tenant_policy import DEFAULT_TENANT_ID, SUPER_ADMIN_USER_ID

        async with pg_connection(
            tenant_id=DEFAULT_TENANT_ID,
            user_id=SUPER_ADMIN_USER_ID,
            role="super_admin",
        ) as connection:
            policies = await connection.fetch(
                "SELECT id, policy_key, path, path_type, methods, auth_mode, access_log_mode, "
                "priority, enabled, description FROM api_access_policies ORDER BY priority DESC, id"
            )
            rules = await connection.fetch(
                "SELECT id, policy_key, action, cidr::text AS cidr, enabled, description "
                "FROM api_ip_rules ORDER BY id"
            )
        self.replace_dynamic(list(policies), list(rules))
        logger.info("刷新 API 访问策略完成", policy_count=len(policies), rule_count=len(rules))

    # 方法作用：判断合并快照中是否存在可被 IP 规则引用的策略键。
    # Args: self - 当前管理器；policy_key - 策略稳定编号。
    # Returns: YAML 或数据库策略存在时返回 True。
    def has_policy(self, policy_key: str) -> bool:
        return any(
            policy.policy_key == policy_key for policy in (*self._bootstrap, *self._dynamic)
        )

    # 方法作用：按固定优先级解析请求策略并执行紧急及接口级 IP 规则。
    # Args: self - 当前管理器；path - 请求路径；method - HTTP 方法；client_ip - 客户端地址。
    # Returns: 包含匹配策略、客户端 IP 和允许状态的决策。
    def resolve(self, path: str, method: str, client_ip: str | IpAddress) -> AccessDecision:
        address = client_ip if isinstance(client_ip, (ipaddress.IPv4Address, ipaddress.IPv6Address)) else ipaddress.ip_address(client_ip)
        if any(address.version == network.version and address in network for network in self._emergency_denies):
            return AccessDecision(self._default, False, str(address), "emergency_deny")
        if path.startswith(_PROTECTED_ADMIN_PREFIX):
            policy = self._admin_policy
        else:
            policy = next(
                (item for item in self._bootstrap if item.matches(path, method)),
                next((item for item in self._dynamic if item.matches(path, method)), self._default),
            )
        rules = [
            rule for rule in self._ip_rules if rule.enabled and rule.policy_key == policy.policy_key
        ]
        if any(rule.action is IpRuleAction.DENY and rule.matches(address) for rule in rules):
            return AccessDecision(policy, False, str(address), "policy_deny")
        allows = [rule for rule in rules if rule.action is IpRuleAction.ALLOW]
        if allows and not any(rule.matches(address) for rule in allows):
            return AccessDecision(policy, False, str(address), "allowlist_miss")
        return AccessDecision(policy, True, str(address))

    # 方法作用：导出平台管理页使用的合并策略和 IP 规则快照。
    # Args: self - 当前管理器。
    # Returns: 策略、IP 规则和默认模式字典。
    def export_snapshot(self) -> dict[str, Any]:
        return {
            "policies": [policy.to_dict() for policy in (*self._bootstrap, *self._dynamic)],
            "ip_rules": [rule.to_dict() for rule in self._ip_rules],
            "defaults": {
                "auth_mode": self._default.auth_mode.value,
                "access_log_mode": self._default.access_log_mode.value,
            },
        }


# 方法作用：获取当前 AppContext 内唯一的 API 访问策略管理器。
# Args: 无。
# Returns: 已存在或按当前配置创建的管理器。
def get_api_access_policy_manager() -> ApiAccessPolicyManager:
    from src.app_context import get_app_context

    context = get_app_context()
    return context.get_or_create(
        "api_access_policy_manager",
        lambda: ApiAccessPolicyManager(context.settings),
    )


# 方法作用：创建管理器并加载 PostgreSQL 动态访问策略。
# Args: 无。
# Returns: 完成数据库刷新后的管理器。
async def initialize_api_access_policy_manager() -> ApiAccessPolicyManager:
    manager = get_api_access_policy_manager()
    await manager.refresh()
    return manager
