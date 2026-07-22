"""MCP Server 管理路由。"""

from __future__ import annotations

import io
import html
import json
import os
import time
import uuid
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import APIRouter, Body, File, HTTPException, Query, UploadFile

from src.api.schemas import (
    ChatRequest, ChatResponse, ColumnCommentRequest,
    DataSourceCreateRequest, DataSourceInfo, HealthResponse, KnowledgeTagCreateRequest,
    KnowledgeTagStatusRequest, MCPServerCreate, TableInfo,
)
from src.exceptions import DataSourceNotFoundError
from src.llm.client import is_llm_available
from src.logging_config import get_logger
from src.api.routes._helpers import _app, _authorize_extension_scope, _registry

logger = get_logger(__name__)
router = APIRouter()
_started_at = time.time()


# 方法作用：把作用域和当前身份转换为 MCP 数据库存储所有者字段。
# Args: scope - 已规范化作用域；tenant_id - 当前租户；user_id - 当前用户。
# Returns: 数据库 tenant_id 与 owner_user_id。
def _mcp_owner_fields(scope: str, tenant_id: int, user_id: int) -> tuple[int | None, int]:
    """system 不绑定租户，tenant 不绑定个人，private 同时绑定租户和用户。"""
    logger.debug(
        "计算 MCP 所有者字段入口", scope=scope, tenant_id=tenant_id, user_id=user_id,
    )
    if scope == "system":
        result = (None, 0)
    elif scope == "tenant":
        result = (tenant_id, 0)
    else:
        result = (tenant_id, user_id)
    logger.info(
        "计算 MCP 所有者字段完成",
        scope=scope,
        resource_tenant_id=result[0],
        owner_user_id=result[1],
    )
    return result


# 方法作用：校验数据库受管 MCP 的角色、传输方式和远程主机边界。
# Args: req - MCP 创建请求；scope - 已规范化作用域；role - 当前角色。
# Returns: 校验通过时返回 None，否则抛出 HTTP 400/403。
def _validate_managed_mcp_request(req: MCPServerCreate, scope: str, role: str) -> None:
    """阻断受管配置中的本地进程执行和任意 URL 访问。

    Args:
        req: MCP Server 配置请求。
        scope: system/tenant/private 作用域。
        role: 当前认证角色。

    Returns:
        校验通过时返回 None。
    """
    from urllib.parse import urlparse

    from src.config import get_settings
    from src.knowledge.governance import is_super_admin, is_tenant_admin

    logger.debug("校验受管 MCP 请求入口", name=req.name, scope=scope, transport=req.transport, role=role)
    if scope == "system" and not is_super_admin(role):
        logger.warning("受管 MCP 请求拒绝", reason="system 需要超级管理员", role=role)
        raise HTTPException(403, "system MCP 需要超级管理员权限")
    if scope in {"tenant", "private"} and not (is_super_admin(role) or is_tenant_admin(role)):
        logger.warning("受管 MCP 请求拒绝", reason="需要租户管理员", role=role)
        raise HTTPException(403, "受管 MCP 需要租户管理员权限")
    if str(req.transport or "").strip().lower() != "sse":
        logger.warning("受管 MCP 请求拒绝", reason="禁止 stdio", transport=req.transport)
        raise HTTPException(400, "受管 MCP 仅允许 SSE transport，禁止 stdio")
    if req.command.strip() or req.args.strip() or req.env_vars:
        logger.warning("受管 MCP 请求拒绝", reason="SSE 配置包含进程参数", name=req.name)
        raise HTTPException(400, "SSE MCP 不允许 command、args 或 env_vars")
    parsed = urlparse(req.url.strip())
    host = (parsed.hostname or "").strip().lower()
    if parsed.scheme not in {"http", "https"} or not host or parsed.username or parsed.password:
        logger.warning("受管 MCP 请求拒绝", reason="URL 格式非法", url_preview=req.url[:120])
        raise HTTPException(400, "MCP SSE URL 必须是无认证信息的 HTTP/HTTPS 地址")
    allowlist = {
        value.strip().lower()
        for value in getattr(get_settings(), "mcp_remote_host_allowlist", "").split(",")
        if value.strip()
    }
    if host not in allowlist:
        logger.warning("受管 MCP 请求拒绝", reason="host 不在 allowlist", host=host)
        raise HTTPException(400, f"MCP SSE host '{host}' 不在 allowlist")
    logger.info("校验受管 MCP 请求完成", name=req.name, scope=scope, host=host)


# 方法作用：在事务内借用已注入当前认证身份的连接供 MCP RLS 查询使用。
# Args: 无。
# Returns: 事务范围内设置 tenant_id/user_id/role 的 asyncpg 连接。
@asynccontextmanager
async def _connect_scoped_mcp_db() -> AsyncIterator:
    """所有 MCP 管理 SQL 必须经过事务局部 RLS 身份注入。"""
    logger.debug("连接 MCP 作用域数据库入口")
    from src.api.auth import get_current_role, get_current_tenant_id, get_current_user_id
    from src.memory.pg_pool import pg_connection

    try:
        async with pg_connection(
            tenant_id=get_current_tenant_id(),
            user_id=get_current_user_id(),
            role=get_current_role(),
        ) as connection:
            yield connection
        logger.info("连接 MCP 作用域数据库完成")
    except Exception:
        logger.error("连接 MCP 作用域数据库失败", exc_info=True)
        raise


@router.get("/mcp/servers")
# 方法作用：列出当前身份可见的 MCP Server。
# Args: scope - 可选 system/tenant/private 过滤范围。
# Returns: MCP Server 列表和总数。
async def list_mcp_servers(scope: str | None = None):
    """列出当前身份可见的 system/tenant/private MCP Server。"""
    logger.debug("MCP Server 列表入口", scope=scope or "")
    from src.api.auth import get_current_role, get_current_tenant_id, get_current_user_id
    from src.knowledge.governance import is_super_admin, normalize_knowledge_scope

    tenant_id = get_current_tenant_id()
    user_id = get_current_user_id()
    role = get_current_role()
    normalized_scope = None
    if scope:
        try:
            normalized_scope = normalize_knowledge_scope(scope).value
        except ValueError as exc:
            raise HTTPException(400, str(exc)) from exc
    from src.mcp_client.client_manager import get_mcp_client_manager
    runtime_system = (
        get_mcp_client_manager().list_system_servers()
        if normalized_scope in (None, "system") else []
    )
    try:
        import src.api.routes as routes_package

        async with routes_package._connect_scoped_mcp_db() as conn:
            if is_super_admin(role):
                rows = await conn.fetch(
                    "SELECT name, scope, tenant_id, owner_user_id, transport, command, args, "
                    "url, description, is_builtin, enabled FROM mcp_servers "
                    "ORDER BY scope, name",
                )
            else:
                rows = await conn.fetch(
                    "SELECT name, scope, tenant_id, owner_user_id, transport, command, args, "
                    "url, description, is_builtin, enabled FROM mcp_servers WHERE "
                    "scope='system' OR (scope='tenant' AND tenant_id=$1) OR "
                    "(scope='private' AND tenant_id=$1 AND owner_user_id=$2) "
                    "ORDER BY scope, name",
                    tenant_id, user_id,
                )
        servers = list(runtime_system)
        for row in rows:
            row_scope = str(row["scope"] or "tenant")
            if normalized_scope and row_scope != normalized_scope:
                continue
            servers.append({
                "name": row["name"], "scope": row_scope,
                "tenant_id": int(row["tenant_id"] or 0),
                "owner_user_id": int(row["owner_user_id"] or 0),
                "transport": row["transport"], "command": row["command"],
                "args": row["args"], "url": row["url"],
                "description": row["description"], "is_builtin": row["is_builtin"],
                "enabled": row["enabled"],
            })
        result = {"servers": servers, "total": len(servers)}
        logger.info(
            "MCP Server 列表完成", tenant_id=tenant_id, user_id=user_id, total=len(servers),
        )
        return result
    except Exception as exc:
        logger.error("MCP Server 数据库列表失败", error=str(exc), exc_info=True)
        raise HTTPException(503, "MCP Server 配置存储不可用") from exc


@router.post("/mcp/servers", status_code=201)
# 方法作用：按认证身份创建或更新指定作用域 MCP Server。
# Args: req - MCP Server 连接配置和目标作用域。
# Returns: 创建状态、作用域和运行时加载数量。
async def create_mcp_server(req: MCPServerCreate):
    """按当前身份创建或更新 system/tenant/private MCP Server。"""
    logger.debug("创建 MCP Server 入口", name=req.name, scope=req.scope)
    from src.api.auth import get_current_role

    normalized_scope, tenant_id, user_id, _ = _authorize_extension_scope(req.scope)
    _validate_managed_mcp_request(req, normalized_scope, get_current_role())
    resource_tenant_id, owner_user_id = _mcp_owner_fields(
        normalized_scope, tenant_id, user_id,
    )
    try:
        import json
        import src.api.routes as routes_package

        async with routes_package._connect_scoped_mcp_db() as conn:
            existing = await conn.fetchval(
                "SELECT id FROM mcp_servers WHERE name=$1 AND scope=$2 "
                "AND COALESCE(tenant_id, 0)=COALESCE($3::int, 0) AND owner_user_id=$4",
                req.name, normalized_scope, resource_tenant_id, owner_user_id,
            )
            if existing:
                await conn.execute(
                    "UPDATE mcp_servers SET transport=$1, command=$2, args=$3, url=$4, "
                    "env_vars=$5, description=$6, enabled=$7 WHERE id=$8",
                    req.transport, req.command, req.args, req.url,
                    json.dumps(req.env_vars), req.description, req.enabled, existing,
                )
            else:
                await conn.execute(
                    "INSERT INTO mcp_servers (name, scope, tenant_id, owner_user_id, transport, "
                    "command, args, url, env_vars, description, enabled) "
                    "VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11)",
                    req.name, normalized_scope, resource_tenant_id, owner_user_id,
                    req.transport, req.command, req.args, req.url,
                    json.dumps(req.env_vars), req.description, req.enabled,
                )
        from src.mcp_client.client_manager import get_mcp_client_manager
        loaded = await get_mcp_client_manager().ensure_scoped_servers(
            tenant_id, user_id, force=True,
        )
        result = {
            "status": "ok", "name": req.name, "scope": normalized_scope,
            "runtime_loaded": loaded,
        }
        logger.info(
            "创建 MCP Server 完成", name=req.name, scope=normalized_scope,
            tenant_id=tenant_id, user_id=user_id,
        )
        return result
    except HTTPException:
        raise
    except Exception as exc:
        logger.error("创建 MCP Server 失败", name=req.name, error=str(exc), exc_info=True)
        raise HTTPException(500, f"创建失败: {exc}") from exc


@router.delete("/mcp/servers/{name}")
# 方法作用：删除当前身份有权管理的 MCP Server。
# Args: name - Server 名称；scope - 可选精确作用域。
# Returns: 删除状态、名称和作用域。
async def delete_mcp_server(name: str, scope: str | None = None):
    """删除当前身份有权管理的 MCP Server，内置 YAML 服务不可删除。"""
    logger.debug("删除 MCP Server 入口", name=name, scope=scope or "")
    from src.api.auth import get_current_role, get_current_tenant_id, get_current_user_id
    from src.knowledge.governance import can_manage_knowledge_resource, normalize_knowledge_scope

    tenant_id = get_current_tenant_id()
    user_id = get_current_user_id()
    role = get_current_role()
    normalized_scope = None
    if scope:
        try:
            normalized_scope = normalize_knowledge_scope(scope).value
        except ValueError as exc:
            raise HTTPException(400, str(exc)) from exc
    try:
        import src.api.routes as routes_package

        async with routes_package._connect_scoped_mcp_db() as conn:
            row = await conn.fetchrow(
                "SELECT id, scope, tenant_id, owner_user_id, is_builtin FROM mcp_servers "
                "WHERE name=$1 AND ($2::text IS NULL OR scope=$2) AND "
                "(scope='system' OR (scope='tenant' AND tenant_id=$3) OR "
                "(scope='private' AND tenant_id=$3 AND owner_user_id=$4) OR $5='super_admin') "
                "ORDER BY CASE scope WHEN 'private' THEN 3 WHEN 'tenant' THEN 2 ELSE 1 END DESC "
                "LIMIT 1",
                name, normalized_scope, tenant_id, user_id, role,
            )
            if not row:
                raise HTTPException(404, f"MCP Server '{name}' 未找到")
            row_scope = str(row["scope"])
            row_tenant = int(row["tenant_id"] or 0)
            row_owner = int(row["owner_user_id"] or 0)
            if row["is_builtin"]:
                raise HTTPException(403, "内置 MCP Server 不可删除")
            if not can_manage_knowledge_resource(
                row_scope, role=role, current_tenant_id=tenant_id,
                resource_tenant_id=row_tenant, current_user_id=user_id,
                owner_user_id=row_owner,
            ):
                raise HTTPException(403, "无权删除该 MCP Server")
            await conn.execute("DELETE FROM mcp_servers WHERE id=$1", row["id"])
        from src.mcp_client.client_manager import get_mcp_client_manager
        manager = get_mcp_client_manager()
        internal_name = manager.scoped_server_name(name, row_scope, row_tenant, row_owner)
        await manager.remove_server(internal_name)
        logger.info("删除 MCP Server 完成", name=name, scope=row_scope)
        return {"status": "ok", "name": name, "scope": row_scope}
    except HTTPException:
        raise
    except Exception as exc:
        logger.error("删除 MCP Server 失败", name=name, error=str(exc), exc_info=True)
        raise HTTPException(500, f"删除失败: {exc}") from exc


@router.post("/mcp/servers/{name}/test")
# 方法作用：测试当前身份可见 MCP Server 的连通性。
# Args: name - Server 名称；scope - 可选精确作用域。
# Returns: 测试结果和错误信息。
async def test_mcp_server(name: str, scope: str | None = None):
    """测试当前身份可见的 MCP Server 连通性。"""
    logger.debug("测试 MCP Server API 入口", name=name, scope=scope or "")
    try:
        from src.api.auth import get_current_tenant_id, get_current_user_id
        from src.mcp_client.client_manager import get_mcp_client_manager
        mgr = get_mcp_client_manager()
        tenant_id = get_current_tenant_id()
        user_id = get_current_user_id()
        await mgr.ensure_scoped_servers(tenant_id, user_id)
        candidates = []
        for internal_name in mgr.sessions:
            server_scope = mgr._server_scopes.get(internal_name, "system")  # noqa: SLF001
            if scope and server_scope != scope:
                continue
            if internal_name.endswith(f"_{name}") or internal_name == name:
                candidates.append(internal_name)
        if not candidates:
            raise HTTPException(404, f"MCP Server '{name}' 未找到")
        priority = {"private": 3, "tenant": 2, "system": 1}
        internal_name = max(
            candidates,
            key=lambda item: priority.get(mgr._server_scopes.get(item, "system"), 0),  # noqa: SLF001
        )
        ok = await mgr.test_connection(internal_name)
        result = {"name": name, "scope": mgr._server_scopes.get(internal_name, "system"), "ok": ok}  # noqa: SLF001
        logger.info("测试 MCP Server API 完成", name=name, ok=ok)
        return result
    except HTTPException:
        raise
    except Exception as e:
        logger.error("测试 MCP Server API 失败", name=name, error=str(e), exc_info=True)
        return {"name": name, "ok": False, "error": str(e)}
