"""MCP Server 管理路由。"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any

from fastapi import APIRouter, HTTPException

from src.api.routes._helpers import _authorize_extension_scope
from src.api.schemas import MCPServerCreate
from src.logging_config import get_logger
from src.security.mcp_safety import (
    MCPSecurityBlockRequest,
    list_mcp_security_blocks,
    set_mcp_security_block,
)

logger = get_logger(__name__)
router = APIRouter()


# 读取不含租户连接配置的平台安全状态。
# Args: 无。
# Returns: 安全阻断元数据。
@router.get("/mcp/security-blocks")
async def list_security_blocks() -> dict:
    """平台安全操作与租户日常管理分离。"""
    return {"items": await list_mcp_security_blocks()}


# 设置独立安全阻断，不修改租户连接启用状态。
# Args: request - 明确资源、理由和阻断状态。
# Returns: 更新后的安全状态。
@router.post("/mcp/security-blocks")
async def set_security_block(request: MCPSecurityBlockRequest) -> dict:
    """安全解除不等同于租户启用。"""
    return await set_mcp_security_block(request)
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
    if scope in {"tenant", "private"} and not is_tenant_admin(role):
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


# 方法作用：构造脱敏异常三元组：固定文案新异常承载原始堆栈，排除敏感正文。
# Args: exc - 捕获到的原始异常；message - 固定脱敏异常文案。
# Returns: (type, value, traceback) 三元组，可直接作为 exc_info 传给 logger。
def _exception_triple(exc: BaseException, message: str) -> tuple[type, BaseException, Any]:
    # 底层异常可能携带 URL/凭证/数据库细节；日志只保留类型名与原始堆栈位置。
    safe_error = RuntimeError(message)
    return (type(safe_error), safe_error, exc.__traceback__)


# 方法作用：在事务内借用已注入当前认证身份的连接供 MCP RLS 查询使用。
# Args: 无。
# Returns: 事务范围内设置 tenant_id/user_id/role 的 asyncpg 连接。
@asynccontextmanager
async def _connect_scoped_mcp_db() -> AsyncIterator:
    """所有 MCP 管理 SQL 必须经过事务局部 RLS 身份注入。"""
    logger.debug("连接 MCP 作用域数据库入口")
    from src.api.auth import (
        get_current_role,
        get_current_tenant_id,
        get_current_user_id,
    )
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
    from src.api.auth import (
        get_current_tenant_id,
        get_current_user_id,
        is_platform_super_admin,
    )
    from src.knowledge.governance import normalize_knowledge_scope

    tenant_id = get_current_tenant_id()
    user_id = get_current_user_id()
    platform_admin = is_platform_super_admin()
    normalized_scope = None
    if scope:
        try:
            normalized_scope = normalize_knowledge_scope(scope).value
        except ValueError as exc:
            raise HTTPException(400, str(exc)) from exc
    from src.mcp_client.client_manager import get_mcp_client_manager
    runtime_system = (
        get_mcp_client_manager().list_system_servers()
        if platform_admin and normalized_scope in (None, "system") else []
    )
    try:
        import src.api.routes as routes_package

        async with routes_package._connect_scoped_mcp_db() as conn:
            if platform_admin:
                rows = await conn.fetch(
                    "SELECT name, scope, tenant_id, owner_user_id, transport, command, args, "
                    "url, description, is_builtin, enabled FROM mcp_servers WHERE scope='system' "
                    "ORDER BY scope, name",
                )
            else:
                rows = await conn.fetch(
                    "SELECT name, scope, tenant_id, owner_user_id, transport, command, args, "
                    "url, description, is_builtin, enabled FROM mcp_servers WHERE "
                    "(scope='tenant' AND tenant_id=$1) OR "
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
        logger.error(
            "创建 MCP Server 失败",
            name=req.name,
            error_type=type(exc).__name__,
            exc_info=True,
        )
        raise HTTPException(500, "创建 MCP Server 失败") from exc


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
                "(scope='private' AND tenant_id=$3 AND owner_user_id=$4)) AND "
                "($5<>'super_admin' OR scope='system') "
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
        logger.error(
            "删除 MCP Server 失败",
            name=name,
            error_type=type(exc).__name__,
            exc_info=True,
        )
        raise HTTPException(500, "删除 MCP Server 失败") from exc


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
        resolved = mgr.resolve_loaded_server(name, scope)
        if resolved is None:
            raise HTTPException(404, f"MCP Server '{name}' 未找到")
        internal_name, resolved_scope = resolved
        await mgr.authorize_runtime_server(internal_name)
        ok = await mgr.test_connection(internal_name)
        result = {"name": name, "scope": resolved_scope, "ok": ok}
        logger.info("测试 MCP Server API 完成", name=name, ok=ok)
        return result
    except HTTPException:
        raise
    except Exception as exc:
        logger.error(
            "测试 MCP Server API 失败",
            name=name,
            error_type=type(exc).__name__,
            exc_info=True,
        )
        return {"name": name, "ok": False, "error": "MCP Server 连通性测试失败"}


# 重置总预算（秒）：覆盖拆除旧连接与重建，超时映射为 HTTP 504。
_MCP_RESET_TIMEOUT_SECONDS = 30.0


@router.post("/mcp/{name}/reset")
# 方法作用：管理员手动重置 MCP Server，授权与安全检查先于任何连接。
# Args: name - 对外 Server 名称；scope - 可选 system/tenant/private 精确作用域消歧。
# Returns: 200 契约响应；不可见 404、无权/停用/阻断 403、存储不可用 503、超时 504、其他 500。
async def reset_mcp_server(name: str, scope: str | None = None) -> dict[str, Any]:
    """重置 degraded/unavailable 的 MCP Server；身份一律取自认证上下文。

    Args:
        name: 对外 Server 名称。
        scope: 可选精确作用域，用于同名多作用域消歧。

    Returns:
        {"status": "ok", "name": 公开名, "scope": 实际作用域, "tools": 新工具数}。
    """
    logger.debug("重置 MCP Server API 入口", name=name, scope=scope or "")
    from src.api.auth import get_current_role, get_current_tenant_id, get_current_user_id
    from src.knowledge.governance import (
        can_manage_knowledge_resource,
        is_super_admin,
        normalize_knowledge_scope,
    )
    from src.mcp_client.client_manager import (
        MCPResetBlockedError,
        MCPResetError,
        MCPResetTimeoutError,
        MCPResetUnavailableError,
        get_mcp_client_manager,
    )
    from src.mcp_client.owner_task import is_error_logged

    tenant_id = get_current_tenant_id()
    user_id = get_current_user_id()
    role = get_current_role()
    normalized_scope = None
    if scope:
        try:
            normalized_scope = normalize_knowledge_scope(scope).value
        except ValueError as exc:
            raise HTTPException(400, str(exc)) from exc

    # 第 1 步：授权查库先于任何连接：存储不可用返回 503，命中停用/安全阻断留待授权阶段拒绝。
    try:
        import src.api.routes as routes_package

        async with routes_package._connect_scoped_mcp_db() as conn:
            row = await conn.fetchrow(
                "SELECT id, scope, tenant_id, owner_user_id, transport, url, enabled, is_builtin "
                "FROM mcp_servers WHERE name=$1 AND ($2::text IS NULL OR scope=$2) AND "
                "(scope='system' OR (scope='tenant' AND tenant_id=$3) OR "
                "(scope='private' AND tenant_id=$3 AND owner_user_id=$4)) AND "
                "($5<>'super_admin' OR scope='system') "
                "ORDER BY CASE scope WHEN 'private' THEN 3 WHEN 'tenant' THEN 2 ELSE 1 END DESC "
                "LIMIT 1",
                name, normalized_scope, tenant_id, user_id, role,
            )
            blocked = False
            if row:
                blocked = bool(await conn.fetchval(
                    "SELECT blocked FROM mcp_security_blocks WHERE name=$1 AND scope=$2 AND "
                    "COALESCE(tenant_id,0)=$3 AND owner_user_id=$4",
                    name, str(row["scope"]), int(row["tenant_id"] or 0),
                    int(row["owner_user_id"] or 0),
                ))
    except HTTPException:
        raise
    except Exception as exc:
        # 授权查库是本路由的实际处理边界：固定文案 + error_type + 原堆栈单次记录，
        # 数据库异常正文（可能含连接串/凭证）不落盘。
        logger.error(
            "重置 MCP Server 授权查询失败",
            name=name,
            error_type=type(exc).__name__,
            exc_info=_exception_triple(exc, "授权查库失败，原始异常正文不记录"),
        )
        raise HTTPException(503, "MCP Server 配置存储不可用") from exc

    manager = get_mcp_client_manager()

    # 第 2 步：纯内存消歧（不建立连接、不按租户全量加载）。
    resolved = manager.resolve_resettable_server(name, normalized_scope)

    # 第 3 步：可见性、角色、停用与阻断校验全部通过前不得建立任何连接。
    authorized_config: dict | None = None
    prepare_kwargs: dict | None = None
    if resolved is not None and manager.is_builtin_server(resolved[0]):
        internal_name = resolved[0]
        # 内置 YAML 服务不在数据库，仅平台超级管理员可重置。
        if not is_super_admin(role):
            raise HTTPException(403, "内置 MCP Server 仅平台超级管理员可重置")
    else:
        if row is None:
            # 未知、跨租户不可见、已删除或运行时残留：统一 404，删除为终态不得重建。
            raise HTTPException(404, f"MCP Server '{name}' 未找到")
        row_scope = str(row["scope"])
        row_tenant = int(row["tenant_id"] or 0)
        row_owner = int(row["owner_user_id"] or 0)
        expected = manager.scoped_server_name(name, row_scope, row_tenant, row_owner)
        if resolved is not None and (
            resolved[1] != row_scope or resolved[0] != expected
        ):
            # 同名消歧结果与数据库行不一致：按不可见处理。
            raise HTTPException(404, f"MCP Server '{name}' 未找到")
        # 数据库合法但尚未加载的目标：按行构造内部名，随后只准备该目标。
        internal_name = expected
        if not row["enabled"] or blocked:
            raise HTTPException(403, "MCP Server 已停用或存在安全阻断")
        if not can_manage_knowledge_resource(
            row_scope,
            role=role,
            current_tenant_id=tenant_id,
            resource_tenant_id=row_tenant,
            current_user_id=user_id,
            owner_user_id=row_owner,
        ):
            raise HTTPException(403, "无权重置该 MCP Server")
        authorized_config = {"transport": row["transport"], "url": row["url"]}
        # 已授权目标的准备参数：授权已全部通过，随后只准备该目标。
        prepare_kwargs = {
            "config": authorized_config,
            "scope": row_scope,
            "tenant_id": row_tenant,
            "owner_user_id": row_owner,
        }

    # 第 4 步：目标准备、锁等待、拆旧与重建共享同一个剩余总 deadline；
    # 准备失败与重置失败走同一套脱敏映射，不得漏成未处理异常。
    loop = asyncio.get_running_loop()
    started = loop.time()
    try:
        async with asyncio.timeout(_MCP_RESET_TIMEOUT_SECONDS):
            if prepare_kwargs is not None:
                # 只准备已授权目标：不加载租户其它服务、不建立其它连接。
                await manager.prepare_authorized_server(internal_name, **prepare_kwargs)
            remaining = _MCP_RESET_TIMEOUT_SECONDS - (loop.time() - started)
            result = await manager.reset_server(
                internal_name,
                timeout=max(remaining, 0.0),
                authorized_config=authorized_config,
            )
    except (MCPResetTimeoutError, TimeoutError) as exc:
        # 路由侧预算到点与 manager 内部超时统一映射 504（受控边界，一次脱敏记录）。
        logger.error("重置 MCP Server API 超时", name=name, error_type=type(exc).__name__)
        raise HTTPException(504, "重置 MCP Server 超时") from exc
    except MCPResetUnavailableError as exc:
        raise HTTPException(404, f"MCP Server '{name}' 未找到") from exc
    except MCPResetBlockedError as exc:
        logger.warning("重置 MCP Server API 拒绝", name=name, reason="安全校验或停用")
        raise HTTPException(403, "MCP Server 配置未通过安全校验或已停用") from exc
    except MCPResetError as exc:
        # 最外层处理边界单次记录：用__cause__（owner/重建的真实故障位置）构造脱敏
        # 三元组；异常链已在 owner 边界落盘时不再重复写同一堆栈，只做状态映射。
        origin = exc.__cause__ or exc
        if not is_error_logged(origin):
            logger.error(
                "重置 MCP Server API 失败",
                name=name,
                error_type=type(origin).__name__,
                exc_info=_exception_triple(origin, "重置 MCP Server 失败，原始异常正文不记录"),
            )
        raise HTTPException(500, "重置 MCP Server 失败") from exc
    except Exception as exc:
        # 路由直接处理（目标准备等）的未分类失败：同样固定文案 + error_type + 原堆栈；
        # 异常链已被上游边界记录（同一故障跨层传播）时不再重复落盘。
        if not is_error_logged(exc):
            logger.error(
                "重置 MCP Server API 内部失败",
                name=name,
                error_type=type(exc).__name__,
                exc_info=_exception_triple(exc, "重置 MCP Server 内部失败，原始异常正文不记录"),
            )
        raise HTTPException(500, "重置 MCP Server 失败") from exc

    response = {
        "status": "ok",
        "name": name,
        "scope": result["scope"],
        "tools": result["tools"],
    }
    logger.info(
        "重置 MCP Server API 完成",
        name=name,
        scope=result["scope"],
        tools=result["tools"],
    )
    return response
