"""租户 LLM 连接、默认模型和请求级选择解析。"""

from __future__ import annotations

from contextlib import contextmanager
from contextvars import ContextVar, Token
from dataclasses import dataclass
from typing import Iterator

from src.datasource.credential_manager import CredentialManager
from src.logging_config import get_logger
from src.memory.pg_pool import get_pg_pool

logger = get_logger(__name__)


@dataclass(frozen=True, slots=True)
class TenantLLMSelection:
    """一次已完成租户授权和凭证解密的 LLM 选择。"""

    tenant_id: int
    connection_id: int
    connection_name: str
    provider_code: str
    protocol: str
    model_catalog_id: int
    model_id: str
    base_url: str
    api_key: str

    # 方法作用：返回不含明文或密文 API Key 的对话选择摘要。
    # Args: self - 当前租户 LLM 选择。
    # Returns: 可安全返回给前端或写入追踪元数据的字典。
    def to_public_dict(self) -> dict:
        return {
            "tenant_id": self.tenant_id,
            "connection_id": self.connection_id,
            "connection_name": self.connection_name,
            "provider_code": self.provider_code,
            "protocol": self.protocol,
            "model_catalog_id": self.model_catalog_id,
            "model_id": self.model_id,
            "base_url": self.base_url,
        }


_current_selection: ContextVar[TenantLLMSelection | None] = ContextVar(
    "current_tenant_llm_selection",
    default=None,
)


# 方法作用：读取当前请求已经授权的租户 LLM 选择。
# Args: 无。
# Returns: 当前选择；请求未绑定时返回 None。
def get_current_tenant_llm_selection() -> TenantLLMSelection | None:
    return _current_selection.get()


# 方法作用：在当前异步执行上下文中绑定已授权的租户 LLM 选择。
# Args: selection - 当前请求选择；单租户兼容回退时为 None。
# Returns: 用于精确恢复上一个 ContextVar 值的 Token。
def _bind_tenant_llm_selection(
    selection: TenantLLMSelection | None,
) -> Token[TenantLLMSelection | None]:
    token = _current_selection.set(selection)
    logger.debug(
        "绑定租户 LLM 选择入口",
        tenant_id=selection.tenant_id if selection else 0,
        connection_id=selection.connection_id if selection else 0,
        model_id=selection.model_id if selection else "",
    )
    return token


# 方法作用：使用绑定时生成的 Token 恢复上一个租户 LLM 选择。
# Args: token - `_bind_tenant_llm_selection` 返回的 ContextVar Token。
# Returns: 无返回值。
def _reset_tenant_llm_selection(
    token: Token[TenantLLMSelection | None],
) -> None:
    _current_selection.reset(token)
    logger.debug("绑定租户 LLM 选择已恢复")


# 方法作用：在当前协程范围内绑定租户 LLM 选择并在退出时精确恢复。
# Args: selection - 已授权且已解密的租户连接选择。
# Returns: 供 with 使用的上下文管理器。
@contextmanager
def use_tenant_llm_selection(
    selection: TenantLLMSelection | None,
) -> Iterator[TenantLLMSelection | None]:
    token = _bind_tenant_llm_selection(selection)
    try:
        yield selection
    finally:
        _reset_tenant_llm_selection(token)


# 方法作用：从数据库解析当前租户的显式选择或默认 LLM 连接。
# Args: tenant_id - 当前认证租户；connection_id - 可选显式连接；model_id - 可选显式模型标识。
# Returns: 已授权并解密凭证的 TenantLLMSelection。
async def resolve_tenant_llm_selection(
    *,
    tenant_id: int,
    connection_id: int | None = None,
    model_id: str = "",
) -> TenantLLMSelection:
    explicit = connection_id is not None or bool(model_id.strip())
    if explicit and (connection_id is None or not model_id.strip()):
        raise ValueError("连接和模型必须同时选择")
    logger.info(
        "租户 LLM 选择解析边界",
        tenant_id=tenant_id,
        explicit=explicit,
        connection_id=connection_id or 0,
        model_id=model_id,
    )
    pool = await get_pg_pool()
    async with pool.acquire() as connection:
        if explicit:
            row = await connection.fetchrow(
                "SELECT c.id AS connection_id, c.name AS connection_name, "
                "p.code AS provider_code, p.protocol, "
                "m.id AS model_catalog_id, m.model_id, "
                "COALESCE(NULLIF(c.base_url, ''), p.default_base_url) AS base_url, "
                "c.encrypted_api_key FROM tenant_llm_connections c "
                "JOIN llm_provider_catalog p ON p.id=c.provider_id "
                "JOIN tenant_llm_connection_models cm ON cm.connection_id=c.id "
                "JOIN llm_model_catalog m ON m.id=cm.model_catalog_id "
                "WHERE c.tenant_id=$1 AND c.id=$2 AND m.model_id=$3 "
                "AND c.is_active=TRUE AND p.is_active=TRUE "
                "AND cm.is_enabled=TRUE AND m.is_active=TRUE",
                int(tenant_id),
                int(connection_id),
                model_id.strip(),
            )
        else:
            row = await connection.fetchrow(
                "SELECT c.id AS connection_id, c.name AS connection_name, "
                "p.code AS provider_code, p.protocol, "
                "m.id AS model_catalog_id, m.model_id, "
                "COALESCE(NULLIF(c.base_url, ''), p.default_base_url) AS base_url, "
                "c.encrypted_api_key FROM tenant_llm_defaults d "
                "JOIN tenant_llm_connections c ON c.id=d.connection_id "
                "JOIN llm_provider_catalog p ON p.id=c.provider_id "
                "JOIN llm_model_catalog m ON m.id=d.model_catalog_id "
                "JOIN tenant_llm_connection_models cm ON cm.connection_id=c.id "
                "AND cm.model_catalog_id=m.id "
                "WHERE d.tenant_id=$1 AND c.tenant_id=$1 "
                "AND c.is_active=TRUE AND p.is_active=TRUE "
                "AND cm.is_enabled=TRUE AND m.is_active=TRUE",
                int(tenant_id),
            )
    if row is None:
        logger.warning(
            "租户 LLM 选择解析失败",
            tenant_id=tenant_id,
            connection_id=connection_id or 0,
            reason="选择不存在或不可用",
        )
        raise LookupError("当前租户未配置可用的 LLM 连接和模型")
    api_key = CredentialManager().decrypt(str(row["encrypted_api_key"] or ""))
    if not api_key:
        raise ValueError("当前 LLM 连接未配置 API Key")
    selection = TenantLLMSelection(
        tenant_id=int(tenant_id),
        connection_id=int(row["connection_id"]),
        connection_name=str(row["connection_name"]),
        provider_code=str(row["provider_code"]),
        protocol=str(row["protocol"]),
        model_catalog_id=int(row["model_catalog_id"]),
        model_id=str(row["model_id"]),
        base_url=str(row["base_url"] or ""),
        api_key=api_key,
    )
    logger.info(
        "租户 LLM 选择解析完成",
        tenant_id=tenant_id,
        connection_id=selection.connection_id,
        provider_code=selection.provider_code,
        model_id=selection.model_id,
    )
    return selection


# 方法作用：列出当前租户所有启用连接模型及默认选择。
# Args: tenant_id - 当前认证租户。
# Returns: 扁平模型选项和默认 connection_id/model_id。
async def list_tenant_model_options(*, tenant_id: int) -> dict:
    logger.debug("租户模型选项列表入口", tenant_id=tenant_id)
    pool = await get_pg_pool()
    async with pool.acquire() as connection:
        rows = await connection.fetch(
            "SELECT c.id AS connection_id, c.name AS connection_name, "
            "p.code AS provider_code, m.id AS model_catalog_id, "
            "m.model_id, m.display_name, m.capabilities "
            "FROM tenant_llm_connections c "
            "JOIN llm_provider_catalog p ON p.id=c.provider_id "
            "JOIN tenant_llm_connection_models cm ON cm.connection_id=c.id "
            "JOIN llm_model_catalog m ON m.id=cm.model_catalog_id "
            "WHERE c.tenant_id=$1 AND c.is_active=TRUE AND p.is_active=TRUE "
            "AND cm.is_enabled=TRUE AND m.is_active=TRUE "
            "ORDER BY c.name, m.display_name",
            int(tenant_id),
        )
        default_row = await connection.fetchrow(
            "SELECT d.connection_id, d.model_catalog_id, m.model_id "
            "FROM tenant_llm_defaults d "
            "JOIN llm_model_catalog m ON m.id=d.model_catalog_id "
            "WHERE d.tenant_id=$1",
            int(tenant_id),
        )
    models = [
        {
            "connection_id": int(row["connection_id"]),
            "connection_name": str(row["connection_name"]),
            "provider": str(row["provider_code"]),
            "model_catalog_id": int(row["model_catalog_id"]),
            "id": str(row["model_id"]),
            "name": str(row["display_name"]),
            "capabilities": dict(row["capabilities"] or {}),
        }
        for row in rows
    ]
    default = (
        {
            "connection_id": int(default_row["connection_id"]),
            "model_id": str(default_row["model_id"]),
        }
        if default_row is not None
        else {}
    )
    logger.info("租户模型选项列表完成", tenant_id=tenant_id, model_count=len(models))
    return {"models": models, "default": default}
