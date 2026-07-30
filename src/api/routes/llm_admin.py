"""平台 LLM 目录与租户命名连接管理路由。"""

from __future__ import annotations

import json
from typing import Literal

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

from src.datasource.credential_manager import CredentialManager
from src.llm.capability_schema import (
    CapabilityFormSchema,
    default_capability_schema,
    normalize_json_object,
    validate_capability_values,
)
from src.llm.tenant_config import resolve_tenant_llm_selection
from src.logging_config import get_logger
from src.memory.pg_pool import get_pg_pool

logger = get_logger(__name__)
router = APIRouter(prefix="/admin/llm", tags=["llm-governance"])


class ProviderCatalogCreateRequest(BaseModel):
    """平台厂商目录创建请求。"""

    code: str = Field(..., min_length=1, max_length=64, pattern=r"^[A-Za-z0-9][A-Za-z0-9_-]*$")
    display_name: str = Field(..., min_length=1, max_length=128)
    protocol: Literal["openai_compatible", "anthropic"]
    default_base_url: str = Field(default="", max_length=2048)
    capability_schema: CapabilityFormSchema = Field(
        default_factory=lambda: CapabilityFormSchema.model_validate(
            default_capability_schema(),
        ),
    )


class ProviderCatalogUpdateRequest(BaseModel):
    """平台厂商目录更新请求。"""

    display_name: str | None = Field(default=None, min_length=1, max_length=128)
    default_base_url: str | None = Field(default=None, max_length=2048)
    capability_schema: CapabilityFormSchema | None = None
    is_active: bool | None = None


class ModelCatalogCreateRequest(BaseModel):
    """平台模型目录创建请求。"""

    model_id: str = Field(..., min_length=1, max_length=128)
    display_name: str = Field(..., min_length=1, max_length=128)
    capabilities: dict = Field(default_factory=dict)


class ModelCatalogUpdateRequest(BaseModel):
    """平台模型目录更新请求。"""

    display_name: str | None = Field(default=None, min_length=1, max_length=128)
    capabilities: dict | None = None
    is_active: bool | None = None


class TenantConnectionCreateRequest(BaseModel):
    """租户命名连接创建请求。"""

    provider_id: int = Field(..., ge=1)
    name: str = Field(..., min_length=1, max_length=128)
    base_url: str = Field(default="", max_length=2048)
    api_key: str = Field(..., min_length=1, max_length=4096)
    model_catalog_ids: list[int] = Field(..., min_length=1, max_length=100)


class TenantConnectionUpdateRequest(BaseModel):
    """租户命名连接更新请求，空 API Key 表示沿用原凭证。"""

    name: str | None = Field(default=None, min_length=1, max_length=128)
    base_url: str | None = Field(default=None, max_length=2048)
    api_key: str | None = Field(default=None, max_length=4096)
    is_active: bool | None = None
    model_catalog_ids: list[int] | None = Field(default=None, min_length=1, max_length=100)


class TenantLLMDefaultRequest(BaseModel):
    """租户默认连接和模型请求。"""

    connection_id: int = Field(..., ge=1)
    model_catalog_id: int = Field(..., ge=1)


# 方法作用：把数据库连接行转换为不含凭证的公开响应。
# Args: row - 数据库连接记录；api_key_configured - 是否已保存凭证。
# Returns: 不含 API Key 的连接摘要。
def _public_connection(row, *, api_key_configured: bool) -> dict:
    return {
        "id": int(row["id"]),
        "tenant_id": int(row["tenant_id"]),
        "provider_id": int(row["provider_id"]),
        "name": str(row["name"]),
        "base_url": str(row["base_url"] or ""),
        "is_active": bool(row["is_active"]),
        "api_key_configured": api_key_configured,
    }


# 方法作用：把厂商数据库行转换为 capability_schema 恒为对象的公开响应。
# Args: row - asyncpg 厂商记录。
# Returns: 可供管理端动态渲染的厂商字典。
def _public_provider(row) -> dict:
    result = dict(row)
    result["capability_schema"] = normalize_json_object(
        result.get("capability_schema"),
        field_name="capability_schema",
    )
    return result


# 方法作用：把模型数据库行转换为 capabilities 恒为对象的公开响应。
# Args: row - asyncpg 模型记录。
# Returns: 可供模型表单和对话选择使用的模型字典。
def _public_model(row) -> dict:
    result = dict(row)
    result["capabilities"] = normalize_json_object(
        result.get("capabilities"),
        field_name="capabilities",
    )
    return result


@router.get("/providers")
# 方法作用：列出平台支持的厂商目录。
# Args: 无。
# Returns: 厂商目录列表。
async def list_provider_catalog() -> dict:
    from src.api.auth import require_tenant_admin

    require_tenant_admin()
    pool = await get_pg_pool()
    async with pool.acquire() as connection:
        rows = await connection.fetch(
            "SELECT id, code, display_name, protocol, default_base_url, "
            "capability_schema, is_active "
            "FROM llm_provider_catalog ORDER BY display_name",
        )
    return {"providers": [_public_provider(row) for row in rows]}


@router.post("/providers", status_code=201)
# 方法作用：由超级管理员创建支持数据驱动扩展的厂商目录。
# Args: request - 厂商编码、协议和默认地址。
# Returns: 新建厂商摘要。
async def create_provider_catalog_entry(request: ProviderCatalogCreateRequest) -> dict:
    from src.api.auth import require_super_admin

    require_super_admin()
    logger.debug("平台 LLM 厂商创建入口", code=request.code, protocol=request.protocol)
    try:
        pool = await get_pg_pool()
        async with pool.acquire() as connection:
            row = await connection.fetchrow(
                "INSERT INTO llm_provider_catalog "
                "(code, display_name, protocol, default_base_url, capability_schema) "
                "VALUES ($1, $2, $3, $4, $5::jsonb) "
                "RETURNING id, code, display_name, protocol, default_base_url, "
                "capability_schema, is_active",
                request.code.strip().lower(),
                request.display_name.strip(),
                request.protocol,
                request.default_base_url.strip(),
                json.dumps(request.capability_schema.model_dump(by_alias=True)),
            )
        logger.info("平台 LLM 厂商创建完成", provider_id=row["id"], code=row["code"])
        return _public_provider(row)
    except Exception as exc:
        if type(exc).__name__ == "UniqueViolationError":
            raise HTTPException(409, "厂商编码已存在") from exc
        logger.error("平台 LLM 厂商创建失败", error=str(exc), exc_info=True)
        raise HTTPException(500, "厂商创建失败") from exc


@router.patch("/providers/{provider_id}")
# 方法作用：更新平台厂商展示信息、默认地址或启用状态。
# Args: provider_id - 厂商目录 ID；request - 更新字段。
# Returns: 更新后的厂商摘要。
async def update_provider_catalog_entry(
    provider_id: int,
    request: ProviderCatalogUpdateRequest,
) -> dict:
    from src.api.auth import require_super_admin

    require_super_admin()
    pool = await get_pg_pool()
    async with pool.acquire() as connection:
        if request.capability_schema is not None:
            existing_models = await connection.fetch(
                "SELECT model_id, capabilities FROM llm_model_catalog WHERE provider_id=$1",
                provider_id,
            )
            try:
                for model in existing_models:
                    validate_capability_values(
                        request.capability_schema.model_dump(by_alias=True),
                        model["capabilities"],
                    )
            except ValueError as exc:
                logger.warning(
                    "平台 LLM 厂商能力表单更新阻断",
                    provider_id=provider_id,
                    model_id=str(model["model_id"]),
                    reason=str(exc),
                )
                raise HTTPException(
                    422,
                    f"现有模型 {model['model_id']} 不符合新能力表单: {exc}",
                ) from exc
        row = await connection.fetchrow(
            "UPDATE llm_provider_catalog SET "
            "display_name=COALESCE($1, display_name), "
            "default_base_url=COALESCE($2, default_base_url), "
            "capability_schema=COALESCE($3::jsonb, capability_schema), "
            "is_active=COALESCE($4, is_active), updated_at=NOW() "
            "WHERE id=$5 RETURNING id, code, display_name, protocol, "
            "default_base_url, capability_schema, is_active",
            request.display_name,
            request.default_base_url,
            (
                json.dumps(request.capability_schema.model_dump(by_alias=True))
                if request.capability_schema is not None
                else None
            ),
            request.is_active,
            provider_id,
        )
    if row is None:
        raise HTTPException(404, "厂商不存在")
    return _public_provider(row)


@router.delete("/providers/{provider_id}")
# 方法作用：物理删除未被租户连接引用的模型厂商及其模型目录。
# Args: provider_id - 厂商目录 ID。
# Returns: 被删除厂商 ID；存在租户引用时返回 409。
async def delete_provider_catalog_entry(provider_id: int) -> dict:
    from src.api.auth import require_super_admin

    require_super_admin()
    logger.debug("平台 LLM 厂商删除入口", provider_id=provider_id)
    try:
        pool = await get_pg_pool()
        async with pool.acquire() as connection:
            async with connection.transaction():
                connection_count = int(await connection.fetchval(
                    "SELECT COUNT(*) FROM tenant_llm_connections WHERE provider_id=$1",
                    provider_id,
                ) or 0)
                if connection_count:
                    logger.warning(
                        "平台 LLM 厂商删除阻断",
                        provider_id=provider_id,
                        tenant_connection_count=connection_count,
                    )
                    raise HTTPException(409, "厂商仍被租户连接使用，请先删除相关租户连接")
                await connection.execute(
                    "DELETE FROM llm_model_catalog WHERE provider_id=$1",
                    provider_id,
                )
                status = await connection.execute(
                    "DELETE FROM llm_provider_catalog WHERE id=$1",
                    provider_id,
                )
    except HTTPException:
        raise
    except Exception as exc:
        if type(exc).__name__ == "ForeignKeyViolationError":
            logger.warning(
                "平台 LLM 厂商删除阻断",
                provider_id=provider_id,
                reason="厂商或模型出现并发租户引用",
            )
            raise HTTPException(409, "厂商仍被租户连接使用，请先删除相关租户连接") from exc
        logger.error("平台 LLM 厂商删除失败", error=str(exc), exc_info=True)
        raise HTTPException(500, "厂商删除失败") from exc
    if status == "DELETE 0":
        raise HTTPException(404, "厂商不存在")
    logger.info("平台 LLM 厂商删除完成", provider_id=provider_id)
    return {"status": "ok", "provider_id": provider_id}


@router.get("/providers/{provider_id}/models")
# 方法作用：列出指定平台厂商的模型目录。
# Args: provider_id - 厂商目录 ID。
# Returns: 模型目录列表。
async def list_model_catalog(provider_id: int) -> dict:
    from src.api.auth import require_tenant_admin

    require_tenant_admin()
    pool = await get_pg_pool()
    async with pool.acquire() as connection:
        rows = await connection.fetch(
            "SELECT id, provider_id, model_id, display_name, capabilities, is_active "
            "FROM llm_model_catalog WHERE provider_id=$1 ORDER BY display_name",
            provider_id,
        )
    logger.info(
        "平台模型目录读取边界",
        provider_id=provider_id,
        model_count=len(rows),
        capability_types=sorted({type(row["capabilities"]).__name__ for row in rows}),
    )
    return {"models": [_public_model(row) for row in rows]}


@router.post("/providers/{provider_id}/models", status_code=201)
# 方法作用：向平台厂商目录增加无需代码分支的新模型。
# Args: provider_id - 厂商目录 ID；request - 模型标识和能力。
# Returns: 新建模型摘要。
async def create_model_catalog_entry(
    provider_id: int,
    request: ModelCatalogCreateRequest,
) -> dict:
    from src.api.auth import require_super_admin

    require_super_admin()
    pool = await get_pg_pool()
    try:
        async with pool.acquire() as connection:
            capability_schema = await connection.fetchval(
                "SELECT capability_schema FROM llm_provider_catalog WHERE id=$1",
                provider_id,
            )
            if capability_schema is None:
                raise HTTPException(404, "厂商不存在")
            capabilities = validate_capability_values(
                capability_schema,
                request.capabilities,
            )
            row = await connection.fetchrow(
                "INSERT INTO llm_model_catalog "
                "(provider_id, model_id, display_name, capabilities) "
                "SELECT $1, $2, $3, $4::jsonb FROM llm_provider_catalog "
                "WHERE id=$1 RETURNING id, provider_id, model_id, display_name, "
                "capabilities, is_active",
                provider_id,
                request.model_id.strip(),
                request.display_name.strip(),
                json.dumps(capabilities),
            )
    except HTTPException:
        raise
    except ValueError as exc:
        logger.warning(
            "平台模型能力校验拒绝",
            provider_id=provider_id,
            model_id=request.model_id,
            reason=str(exc),
        )
        raise HTTPException(422, str(exc)) from exc
    except Exception as exc:
        if type(exc).__name__ == "UniqueViolationError":
            raise HTTPException(409, "该厂商模型已存在") from exc
        logger.error("平台模型创建失败", error=str(exc), exc_info=True)
        raise HTTPException(500, "模型创建失败") from exc
    if row is None:
        raise HTTPException(404, "厂商不存在")
    return _public_model(row)


@router.patch("/models/{model_catalog_id}")
# 方法作用：更新平台模型展示、能力或启用状态。
# Args: model_catalog_id - 模型目录 ID；request - 更新字段。
# Returns: 更新后的模型摘要。
async def update_model_catalog_entry(
    model_catalog_id: int,
    request: ModelCatalogUpdateRequest,
) -> dict:
    import json

    from src.api.auth import require_super_admin

    require_super_admin()
    pool = await get_pg_pool()
    async with pool.acquire() as connection:
        capability_schema = await connection.fetchval(
            "SELECT p.capability_schema FROM llm_model_catalog m "
            "JOIN llm_provider_catalog p ON p.id=m.provider_id WHERE m.id=$1",
            model_catalog_id,
        )
        if capability_schema is None:
            raise HTTPException(404, "模型不存在")
        try:
            capabilities = (
                validate_capability_values(capability_schema, request.capabilities)
                if request.capabilities is not None
                else None
            )
        except ValueError as exc:
            logger.warning(
                "平台模型能力更新拒绝",
                model_catalog_id=model_catalog_id,
                reason=str(exc),
            )
            raise HTTPException(422, str(exc)) from exc
        row = await connection.fetchrow(
            "UPDATE llm_model_catalog SET display_name=COALESCE($1, display_name), "
            "capabilities=COALESCE($2::jsonb, capabilities), "
            "is_active=COALESCE($3, is_active), updated_at=NOW() "
            "WHERE id=$4 RETURNING id, provider_id, model_id, display_name, "
            "capabilities, is_active",
            request.display_name,
            json.dumps(capabilities) if capabilities is not None else None,
            request.is_active,
            model_catalog_id,
        )
    if row is None:
        raise HTTPException(404, "模型不存在")
    return _public_model(row)


@router.delete("/models/{model_catalog_id}")
# 方法作用：物理删除未被租户连接或默认值引用的平台模型。
# Args: model_catalog_id - 模型目录 ID。
# Returns: 被删除模型 ID；存在租户引用时返回 409。
async def delete_model_catalog_entry(model_catalog_id: int) -> dict:
    from src.api.auth import require_super_admin

    require_super_admin()
    logger.debug("平台 LLM 模型删除入口", model_catalog_id=model_catalog_id)
    try:
        pool = await get_pg_pool()
        async with pool.acquire() as connection:
            status = await connection.execute(
                "DELETE FROM llm_model_catalog WHERE id=$1",
                model_catalog_id,
            )
    except Exception as exc:
        if type(exc).__name__ == "ForeignKeyViolationError":
            logger.warning(
                "平台 LLM 模型删除阻断",
                model_catalog_id=model_catalog_id,
                reason="模型仍被租户连接引用",
            )
            raise HTTPException(409, "模型仍被租户连接或默认值使用") from exc
        logger.error("平台 LLM 模型删除失败", error=str(exc), exc_info=True)
        raise HTTPException(500, "模型删除失败") from exc
    if status == "DELETE 0":
        raise HTTPException(404, "模型不存在")
    logger.info("平台 LLM 模型删除完成", model_catalog_id=model_catalog_id)
    return {"status": "ok", "model_catalog_id": model_catalog_id}


@router.get("/connections")
# 方法作用：列出当前租户命名连接和已启用模型，凭证只返回配置状态。
# Args: 无。
# Returns: 当前租户连接列表。
async def list_tenant_connections() -> dict:
    from src.api.auth import get_current_tenant_id, require_tenant_user_admin

    require_tenant_user_admin()
    tenant_id = get_current_tenant_id()
    pool = await get_pg_pool()
    async with pool.acquire() as connection:
        rows = await connection.fetch(
            "SELECT c.id, c.tenant_id, c.provider_id, c.name, c.base_url, "
            "c.is_active, c.encrypted_api_key, p.code AS provider_code, "
            "p.display_name AS provider_name, "
            "COALESCE(array_agg(cm.model_catalog_id) FILTER (WHERE cm.model_catalog_id IS NOT NULL), '{}') AS model_catalog_ids "
            "FROM tenant_llm_connections c "
            "JOIN llm_provider_catalog p ON p.id=c.provider_id "
            "LEFT JOIN tenant_llm_connection_models cm ON cm.connection_id=c.id "
            "WHERE c.tenant_id=$1 "
            "GROUP BY c.id, c.tenant_id, c.provider_id, c.name, c.base_url, c.is_active, c.encrypted_api_key, p.code, p.display_name "
            "ORDER BY c.name",
            tenant_id,
        )
    return {
        "connections": [
            {
                **_public_connection(row, api_key_configured=bool(row["encrypted_api_key"])),
                "provider_code": str(row["provider_code"]),
                "provider_name": str(row["provider_name"]),
                "model_catalog_ids": [int(value) for value in (row["model_catalog_ids"] or [])],
            }
            for row in rows
        ],
    }


@router.post("/connections", status_code=201)
# 方法作用：为当前租户创建独立地址和加密凭证的命名连接。
# Args: request - 厂商、连接名、地址、API Key 和启用模型。
# Returns: 不含凭证的连接摘要。
async def create_tenant_connection(request: TenantConnectionCreateRequest) -> dict:
    from src.api.auth import get_current_tenant_id, require_tenant_user_admin

    require_tenant_user_admin()
    tenant_id = get_current_tenant_id()
    encrypted_key = CredentialManager().encrypt(request.api_key)
    logger.debug(
        "租户 LLM 连接创建入口",
        tenant_id=tenant_id,
        provider_id=request.provider_id,
        connection_name=request.name,
        model_count=len(request.model_catalog_ids),
    )
    try:
        pool = await get_pg_pool()
        async with pool.acquire() as connection:
            async with connection.transaction():
                provider = await connection.fetchrow(
                    "SELECT id, protocol, is_active FROM llm_provider_catalog WHERE id=$1",
                    request.provider_id,
                )
                if provider is None or not bool(provider["is_active"]):
                    raise HTTPException(409, "厂商不存在或已停用")
                row = await connection.fetchrow(
                    "INSERT INTO tenant_llm_connections "
                    "(tenant_id, provider_id, name, base_url, encrypted_api_key) "
                    "VALUES ($1, $2, $3, $4, $5) "
                    "RETURNING id, tenant_id, provider_id, name, base_url, is_active",
                    tenant_id,
                    request.provider_id,
                    request.name.strip(),
                    request.base_url.strip(),
                    encrypted_key,
                )
                for model_catalog_id in dict.fromkeys(request.model_catalog_ids):
                    status = await connection.execute(
                        "INSERT INTO tenant_llm_connection_models "
                        "(connection_id, model_catalog_id) "
                        "SELECT $1, id FROM llm_model_catalog "
                        "WHERE id=$2 AND provider_id=$3 AND is_active=TRUE",
                        row["id"],
                        model_catalog_id,
                        request.provider_id,
                    )
                    if status == "INSERT 0 0":
                        raise HTTPException(400, "连接包含无效或跨厂商模型")
        logger.info("租户 LLM 连接创建完成", tenant_id=tenant_id, connection_id=row["id"])
        return _public_connection(row, api_key_configured=True)
    except HTTPException:
        raise
    except Exception as exc:
        if type(exc).__name__ == "UniqueViolationError":
            raise HTTPException(409, "当前租户连接名称已存在") from exc
        logger.error("租户 LLM 连接创建失败", error=str(exc), exc_info=True)
        raise HTTPException(500, "租户 LLM 连接创建失败") from exc


@router.patch("/connections/{connection_id}")
# 方法作用：更新当前租户连接并在非空 API Key 时替换加密凭证。
# Args: connection_id - 连接 ID；request - 可选更新字段。
# Returns: 不含凭证的连接摘要。
async def update_tenant_connection(
    connection_id: int,
    request: TenantConnectionUpdateRequest,
) -> dict:
    from src.api.auth import get_current_tenant_id, require_tenant_user_admin

    require_tenant_user_admin()
    tenant_id = get_current_tenant_id()
    encrypted_key = (
        CredentialManager().encrypt(request.api_key)
        if request.api_key
        else None
    )
    pool = await get_pg_pool()
    async with pool.acquire() as connection:
        async with connection.transaction():
            row = await connection.fetchrow(
                "UPDATE tenant_llm_connections SET name=COALESCE($1, name), "
                "base_url=COALESCE($2, base_url), "
                "encrypted_api_key=COALESCE($3, encrypted_api_key), "
                "is_active=COALESCE($4, is_active), updated_at=NOW() "
                "WHERE id=$5 AND tenant_id=$6 "
                "RETURNING id, tenant_id, provider_id, name, base_url, is_active, "
                "encrypted_api_key",
                request.name,
                request.base_url,
                encrypted_key,
                request.is_active,
                connection_id,
                tenant_id,
            )
            if row is None:
                raise HTTPException(404, "连接不存在")
            if request.model_catalog_ids is not None:
                await connection.execute(
                    "DELETE FROM tenant_llm_connection_models WHERE connection_id=$1",
                    connection_id,
                )
                for model_catalog_id in dict.fromkeys(request.model_catalog_ids):
                    status = await connection.execute(
                        "INSERT INTO tenant_llm_connection_models "
                        "(connection_id, model_catalog_id) "
                        "SELECT $1, id FROM llm_model_catalog "
                        "WHERE id=$2 AND provider_id=$3 AND is_active=TRUE",
                        connection_id,
                        model_catalog_id,
                        row["provider_id"],
                    )
                    if status == "INSERT 0 0":
                        raise HTTPException(400, "连接包含无效或跨厂商模型")
    return _public_connection(row, api_key_configured=bool(row["encrypted_api_key"]))


@router.delete("/connections/{connection_id}")
# 方法作用：删除当前租户连接并级联清理模型绑定和默认值。
# Args: connection_id - 连接 ID。
# Returns: 删除状态。
async def delete_tenant_connection(connection_id: int) -> dict:
    from src.api.auth import get_current_tenant_id, require_tenant_user_admin

    require_tenant_user_admin()
    tenant_id = get_current_tenant_id()
    pool = await get_pg_pool()
    async with pool.acquire() as connection:
        status = await connection.execute(
            "DELETE FROM tenant_llm_connections WHERE id=$1 AND tenant_id=$2",
            connection_id,
            tenant_id,
        )
    if status == "DELETE 0":
        raise HTTPException(404, "连接不存在")
    logger.info("租户 LLM 连接删除完成", tenant_id=tenant_id, connection_id=connection_id)
    return {"status": "ok", "connection_id": connection_id}


@router.put("/default")
# 方法作用：设置当前租户默认连接和默认对话模型。
# Args: request - 连接 ID 和模型目录 ID。
# Returns: 当前默认选择摘要。
async def set_tenant_llm_default(request: TenantLLMDefaultRequest) -> dict:
    from src.api.auth import get_current_tenant_id, require_tenant_user_admin

    require_tenant_user_admin()
    tenant_id = get_current_tenant_id()
    pool = await get_pg_pool()
    async with pool.acquire() as connection:
        valid = await connection.fetchval(
            "SELECT 1 FROM tenant_llm_connections c "
            "JOIN tenant_llm_connection_models cm ON cm.connection_id=c.id "
            "WHERE c.id=$1 AND c.tenant_id=$2 AND cm.model_catalog_id=$3 "
            "AND c.is_active=TRUE AND cm.is_enabled=TRUE",
            request.connection_id,
            tenant_id,
            request.model_catalog_id,
        )
        if valid is None:
            raise HTTPException(409, "默认连接或模型不可用")
        await connection.execute(
            "INSERT INTO tenant_llm_defaults "
            "(tenant_id, connection_id, model_catalog_id) VALUES ($1, $2, $3) "
            "ON CONFLICT (tenant_id) DO UPDATE SET "
            "connection_id=EXCLUDED.connection_id, "
            "model_catalog_id=EXCLUDED.model_catalog_id, updated_at=NOW()",
            tenant_id,
            request.connection_id,
            request.model_catalog_id,
        )
    logger.info(
        "租户默认 LLM 设置完成",
        tenant_id=tenant_id,
        connection_id=request.connection_id,
        model_catalog_id=request.model_catalog_id,
    )
    return {
        "connection_id": request.connection_id,
        "model_catalog_id": request.model_catalog_id,
    }


@router.post("/connections/{connection_id}/test")
# 方法作用：使用最小输出测试当前租户连接和指定模型。
# Args: connection_id - 连接 ID；model_id - 厂商模型标识。
# Returns: 连通状态，不返回上游正文或凭证。
async def test_tenant_connection(connection_id: int, model_id: str) -> dict:
    import time

    from src.api.auth import get_current_tenant_id, require_tenant_user_admin
    from src.llm.client import get_provider

    require_tenant_user_admin()
    selection = await resolve_tenant_llm_selection(
        tenant_id=get_current_tenant_id(),
        connection_id=connection_id,
        model_id=model_id,
    )
    started = time.monotonic()
    try:
        provider = get_provider(
            model_id=selection.model_id,
            provider_name=selection.protocol,
            base_url=selection.base_url,
            api_key=selection.api_key,
        )
        await provider.agenerate([{"role": "user", "content": "ping"}], max_tokens=1)
        return {"ok": True, "latency_ms": round((time.monotonic() - started) * 1000)}
    except Exception as exc:
        logger.error(
            "租户 LLM 连接测试失败",
            tenant_id=selection.tenant_id,
            connection_id=connection_id,
            error=str(exc),
            exc_info=True,
        )
        return {"ok": False, "error": "模型连接测试失败"}
