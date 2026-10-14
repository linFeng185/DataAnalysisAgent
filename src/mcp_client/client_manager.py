"""8.1 MCP Client Manager — 管理 MCP Server 连接与工具转换。

依据: SPEC §3.9.1
"""

from __future__ import annotations

import asyncio
import ipaddress
import os
import socket
from pathlib import Path
from typing import Any

import yaml
from langchain_core.tools import BaseTool, StructuredTool

from src.logging_config import get_logger
from src.mcp_client.owner_task import OwnedMCPConnection

logger = get_logger(__name__)

_MAX_RECONNECT_RETRIES = 5

# 拆除与降级时关闭旧连接的有界预算（秒）。
_TEARDOWN_CLOSE_TIMEOUT = 5.0


class MCPResetError(Exception):
    """MCP Server 重置发生未分类内部失败。"""


class MCPResetUnavailableError(MCPResetError):
    """MCP Server 未注册、配置已移除或不可用于重建。"""


class MCPResetBlockedError(MCPResetError):
    """MCP Server 配置存在但被停用、安全阻断或校验拒绝。"""


class MCPResetTimeoutError(MCPResetError):
    """MCP Server 重置超出总预算。"""


class _ServerGate:
    """为每个内部 Server 名维护独立锁：按服务串行、跨服务并行。"""

    # 方法作用：初始化按名称惰性创建锁的容器。
    # Args: 无。
    # Returns: 无返回值。
    def __init__(self) -> None:
        self._locks: dict[str, asyncio.Lock] = {}

    # 方法作用：返回指定 Server 的锁，首次访问时创建。
    # Args: name - 内部 Server 名。
    # Returns: 该 Server 对应的独占锁。
    def lock(self, name: str) -> asyncio.Lock:
        lock = self._locks.get(name)
        if lock is None:
            lock = asyncio.Lock()
            self._locks[name] = lock
        return lock


# 方法作用：解析远程主机并确认所有候选地址均为公网地址。
# Args: host - 待解析主机名或 IP；port - 远程服务端口。
# Returns: 至少解析到一个地址且全部为公网地址时返回 True。
def _host_resolves_to_public_addresses(host: str, port: int) -> bool:
    try:
        addresses = [ipaddress.ip_address(host.split("%", maxsplit=1)[0])]
    except ValueError:
        try:
            resolved = socket.getaddrinfo(host, port, type=socket.SOCK_STREAM)
            addresses = [
                ipaddress.ip_address(str(item[4][0]).split("%", maxsplit=1)[0])
                for item in resolved
            ]
        except (OSError, ValueError) as exc:
            logger.warning("受管 MCP 主机解析失败", host=host, error=str(exc))
            return False
    if not addresses:
        logger.warning("受管 MCP 主机未解析到地址", host=host)
        return False
    allowed = all(address.is_global for address in addresses)
    if not allowed:
        logger.warning(
            "受管 MCP 私网地址已阻断",
            host=host,
            address_count=len(addresses),
        )
    return allowed


# 方法作用：验证数据库受管 MCP 配置只能连接 allowlist 中的远程 SSE 主机。
# Args: config - mcp_servers 数据库行或等价映射。
# Returns: 配置安全时返回 True；stdio、非法 URL 或未授权主机返回 False。
def _is_managed_remote_config_allowed(config: Any) -> bool:
    """静态 YAML 仍可使用受信任 stdio，数据库动态配置必须限制为远程 SSE。

    Args:
        config: 数据库读取的 MCP 配置。

    Returns:
        是否允许加载该受管配置。
    """
    from urllib.parse import urlparse

    from src.config import get_settings

    transport = str(config["transport"] or "").strip().lower()
    url = str(config["url"] or "").strip()
    parsed = urlparse(url)
    host = (parsed.hostname or "").strip().lower()
    allowlist = {
        value.strip().lower()
        for value in getattr(get_settings(), "mcp_remote_host_allowlist", "").split(",")
        if value.strip()
    }
    logger.debug("校验受管 MCP 数据库配置入口", transport=transport, host=host)
    allowed = bool(
        transport == "sse"
        and parsed.scheme in {"http", "https"}
        and host
        and not parsed.username
        and not parsed.password
        and host in allowlist
        and _host_resolves_to_public_addresses(
            host,
            parsed.port or (443 if parsed.scheme == "https" else 80),
        )
    )
    if not allowed:
        logger.warning("受管 MCP 数据库配置跳过", transport=transport, host=host)
    logger.info("校验受管 MCP 数据库配置完成", allowed=allowed, host=host)
    return allowed


class MCPClientManager:
    """8.1.1 管理 MCP Client 连接生命周期。

    - connect_all()    → 启动时连接所有 MCP Server
    - get_all_tools()  → 返回 LangChain BaseTool 列表
    - health_check()   → 定期 ping，断线重连
    - close_all()      → 优雅关闭
    """

    # 方法作用：初始化 MCP 连接、工具、作用域和生命周期缓存。
    # Args: config_path - 系统 MCP YAML 配置路径。
    # Returns: 无返回值。
    def __init__(self, config_path: str = "config/app.yaml"):
        logger.debug("初始化 MCPClientManager 入口", config_path=config_path)
        self.config_path = config_path
        self.sessions: dict[str, Any] = {}
        self.langchain_tools: dict[str, BaseTool] = {}
        self._server_configs: dict[str, dict] = {}
        self._server_tenants: dict[str, int] = {}
        self._server_owners: dict[str, int] = {}
        self._server_scopes: dict[str, str] = {}
        self._server_builtins: dict[str, bool] = {}
        self._connections: dict[str, OwnedMCPConnection] = {}
        self._gates = _ServerGate()
        self._epochs: dict[str, int] = {}
        # 删除墓碑：记录 remove 时的 epoch，未被后续注册/重建刷新前禁止重新注册。
        self._removed_marks: dict[str, int] = {}
        self._loaded_identities: set[tuple[int, int]] = set()
        self._configured_system_servers: dict[str, dict] = {}
        logger.info("初始化 MCPClientManager 完成", config_path=config_path)

    # ── 8.1.2 并发连接 ───────────────────────────────

    # 方法作用：读取系统 YAML 并并发连接全部启用的 system MCP Server。
    # Args: 无。
    # Returns: 无返回值。
    async def connect_all(self) -> None:
        """启动时并发连接所有启用的 MCP Server。

        Args:
            无。

        Returns:
            无返回值。
        """
        logger.debug("connect_all 入口", config_path=self.config_path)
        cfg_path = Path(self.config_path)
        if not cfg_path.exists():
            self._configured_system_servers = {}
            logger.info("MCP 配置文件不存在", path=self.config_path)
            return
        config = yaml.safe_load(cfg_path.read_text(encoding="utf-8"))
        configured_servers = config.get("mcp_servers", {})
        self._configured_system_servers = {
            name: server_config
            for name, server_config in configured_servers.items()
            if isinstance(server_config, dict)
        }
        servers = {
            name: server_config
            for name, server_config in configured_servers.items()
            if isinstance(server_config, dict) and server_config.get("enabled", True)
        }
        disabled = sorted(set(configured_servers) - set(servers))
        if disabled:
            logger.info("MCP Server 已禁用", servers=disabled)
        if not servers:
            self._server_configs = {}
            logger.info("无启用的 MCP Server")
            return
        self._server_configs = servers
        tasks = [
            self._connect_single(n, c, scope="system", tenant_id=0, owner_user_id=0)
            for n, c in servers.items()
        ]
        results = await asyncio.gather(*tasks, return_exceptions=True)
        for name, result in zip(servers.keys(), results, strict=False):
            if isinstance(result, Exception):
                logger.error(f"MCP Server '{name}' 连接失败", error=str(result))
            else:
                logger.info(f"MCP Server '{name}' 连接成功", tools=len(result))
        logger.info("connect_all 完成", connected=len(self.sessions), configured=len(servers))

    # 方法作用：幂等执行 MCP system/tenant/private 三级作用域数据库迁移。
    # Args: 无。
    # Returns: 迁移成功返回 True，数据库不可用时返回 False。
    async def ensure_schema(self) -> bool:
        """启动时确保 mcp_servers 表、索引和 RLS 策略存在。"""
        logger.debug("初始化 MCP 数据库结构入口")
        try:
            from src.memory.pg_pool import get_pg_pool

            migration_path = Path(__file__).resolve().parents[2] / "migrations" / "004_resource_scopes.sql"
            migration_sql = migration_path.read_text(encoding="utf-8")
            pool = await get_pg_pool()
            async with pool.acquire() as connection:
                await connection.execute(migration_sql)
            logger.info("初始化 MCP 数据库结构完成", migration=migration_path.name)
            return True
        except Exception as exc:
            logger.error("初始化 MCP 数据库结构失败", error=str(exc), exc_info=True)
            return False

    # ── 8.1.3 单连接 ────────────────────────────────

    # 方法作用：建立单个 MCP 连接并注册其工具与可信作用域。
    # Args: name - 内部名称；config - 连接配置；scope - 作用域；tenant_id - 租户；
    #       owner_user_id - 所有者；timeout - 建立连接的最长等待秒数。
    # Returns: 转换后的 LangChain 工具列表。
    async def _connect_single(
        self,
        name: str,
        config: dict,
        scope: str = "system",
        tenant_id: int = 0,
        owner_user_id: int = 0,
        *,
        timeout: float | None = None,  # noqa: ASYNC109  兼容 spec/56 §4 连接建立契约参数名，等待在 owner task 内有界
    ) -> list[BaseTool]:
        """在 owner task 内建立单个 MCP 连接并原子注册工具与元数据。"""
        logger.debug(
            "连接单个 MCP Server 入口",
            server=name,
            scope=scope,
            tenant_id=tenant_id,
            owner_user_id=owner_user_id,
            timeout=timeout,
        )
        transport = self._build_transport_context(config)
        connection = OwnedMCPConnection(name, transport, self._session_factory)
        mcp_tools = await connection.start(timeout=timeout)
        new_entries: dict[str, BaseTool] = {}
        langchain_tools: list[BaseTool] = []
        for mt in mcp_tools:
            lt = self._mcp_to_langchain_tool(mt, connection, name)
            new_entries[f"{name}__{mt.name}"] = lt
            langchain_tools.append(lt)
        # 原子提交（无 await）：观察者只会看到旧集合或新集合，不会看到混合状态。
        self._connections[name] = connection
        self.sessions[name] = connection
        self.langchain_tools.update(new_entries)
        self._server_scopes[name] = scope
        self._server_tenants[name] = tenant_id
        self._server_owners[name] = owner_user_id
        self._server_builtins[name] = scope == "system" and not name.startswith("system_0_0_")
        logger.info(
            "连接单个 MCP Server 完成",
            server=name,
            scope=scope,
            tools=len(langchain_tools),
        )
        return langchain_tools

    # 方法作用：按配置创建 MCP transport 异步上下文，供 owner task 进入。
    # Args: config - 连接配置（transport/command/args/env/url）。
    # Returns: 可进入的 transport 上下文；不支持的 transport 抛 ValueError。
    def _build_transport_context(self, config: dict) -> Any:
        """受信任 YAML 可使用 stdio；数据库受管配置只有 SSE 会走到这里。"""
        logger.debug("创建 MCP transport 入口", transport=config.get("transport", "stdio"))
        transport = config.get("transport", "stdio")
        if transport == "stdio":
            from mcp.client.stdio import stdio_client

            from mcp import StdioServerParameters

            server_params = StdioServerParameters(
                command=config["command"],
                args=config.get("args", []),
                env=self._resolve_env(config.get("env", {})),
            )
            return stdio_client(server_params)
        if transport == "sse":
            return self._sse_client(config["url"])
        raise ValueError(f"不支持的 transport: {transport}")

    # 方法作用：在 owner task 内创建 MCP ClientSession（测试可注入替身）。
    # Args: name - 连接名；streams - transport 提供的流对。
    # Returns: 可进入的会话上下文。
    def _session_factory(self, name: str, streams: Any) -> Any:
        """默认返回真实 ClientSession；测试替换为确定性替身。"""
        from mcp import ClientSession

        del name
        return ClientSession(*streams)

    # ── 8.1.4 环境变量解析 ───────────────────────────

    @staticmethod
    # 方法作用：解析 MCP 环境变量占位符但不记录敏感值。
    # Args: env - 原始环境变量映射。
    # Returns: 解析后的映射；空输入返回 None。
    def _resolve_env(env: dict) -> dict | None:
        logger.debug("解析 MCP 环境变量入口", key_count=len(env))
        if not env:
            logger.info("解析 MCP 环境变量完成", key_count=0)
            return None
        resolved = {}
        for k, v in env.items():
            if isinstance(v, str) and v.startswith("${") and v.endswith("}"):
                resolved[k] = os.environ.get(v[2:-1], "")
            else:
                resolved[k] = v
        logger.info("解析 MCP 环境变量完成", key_count=len(resolved))
        return resolved

    # ── 8.1.5 工具转换 + 8.1.6 Schema ────────────────

    # 方法作用：把 MCP Tool Schema 转换为带命名空间的 LangChain StructuredTool。
    # Args: mcp_tool - MCP 工具描述；session - 所属会话；namespace - Server 内部名称。
    # Returns: 可供 Agent 调用的 BaseTool。
    def _mcp_to_langchain_tool(self, mcp_tool, session: Any, namespace: str) -> BaseTool:
        logger.debug("转换 MCP Tool 入口", namespace=namespace, tool=mcp_tool.name)

        # 方法作用：通过已授权 MCP 会话调用当前工具。
        # Args: kwargs - MCP Tool 的结构化参数。
        # Returns: MCP 返回的首段文本内容。
        async def _call(**kwargs) -> str:
            logger.debug("调用 MCP Tool 入口", namespace=namespace, tool=mcp_tool.name)
            try:
                await self.authorize_runtime_server(namespace)
                result = await session.call_tool(mcp_tool.name, arguments=kwargs)
                text = result.content[0].text if result.content else ""
                logger.info(
                    "调用 MCP Tool 完成", namespace=namespace, tool=mcp_tool.name,
                    output_chars=len(text),
                )
                return text
            except Exception:
                logger.error(
                    "调用 MCP Tool 失败", namespace=namespace, tool=mcp_tool.name,
                    exc_info=True,
                )
                raise

        schema = None
        raw_schema = getattr(mcp_tool, 'inputSchema', {}) or {}
        if raw_schema.get("properties"):
            try:
                from pydantic import Field, create_model
                fields = {}
                for pn, pp in raw_schema.get("properties", {}).items():
                    pt = _json_type_to_python(pp.get("type", "string"))
                    req = pn in raw_schema.get("required", [])
                    fields[pn] = (pt, Field(default=..., description=pp.get("description", ""))) if req else (pt | None, Field(None, description=pp.get("description", "")))
                if fields:
                    schema = create_model("ToolArgs", **fields)
            except Exception as exc:
                logger.warning(
                    "转换 MCP Tool Schema 失败",
                    namespace=namespace,
                    tool=mcp_tool.name,
                    error=str(exc),
                )

        result = StructuredTool(
            name=f"{namespace}__{mcp_tool.name}",
            description=mcp_tool.description or "",
            coroutine=_call,
            args_schema=schema,
        )
        logger.info("转换 MCP Tool 完成", namespace=namespace, tool=mcp_tool.name)
        return result

    # ── 8.1.7 获取工具 ──────────────────────────────

    # 方法作用：按当前租户和用户过滤可调用的 MCP 工具。
    # Args: tenant_id - 当前租户；user_id - 当前用户。
    # Returns: system + 当前 tenant + 本人 private 工具列表。
    def get_all_tools(
        self, tenant_id: int | None = None, user_id: int | None = None,
    ) -> list[BaseTool]:
        """返回当前身份可见的 system/tenant/private 工具。

        Args:
            tenant_id: 当前请求租户；None 表示无租户身份，只允许系统工具。
            user_id: 当前请求用户；private 工具必须精确匹配。

        Returns:
            经过租户边界过滤的 LangChain Tool 列表。
        """
        logger.debug(
            "获取 MCP 工具入口", tenant_id=tenant_id, user_id=user_id,
            total=len(self.langchain_tools),
        )
        from src.api.auth import get_current_role

        platform = get_current_role() == "super_admin"
        tools: list[BaseTool] = []
        for key, tool in self.langchain_tools.items():
            server_name = key.rsplit("__", 1)[0]
            owner_tenant = self._server_tenants.get(server_name, 0)
            scope = self._server_scopes.get(
                server_name, "system" if owner_tenant == 0 else "tenant",
            )
            owner_user = self._server_owners.get(server_name, 0)
            visible = (
                scope == "system"
                or (scope == "tenant" and tenant_id is not None and owner_tenant == tenant_id)
                or (
                    scope == "private"
                    and tenant_id is not None
                    and user_id is not None
                    and owner_tenant == tenant_id
                    and owner_user == user_id
                )
            )
            if visible and (not platform or scope == "system"):
                tools.append(tool)
        logger.info(
            "获取 MCP 工具完成", tenant_id=tenant_id, user_id=user_id, count=len(tools),
        )
        return tools

    # 方法作用：列出配置文件或系统受管来源中的 system MCP Server 元数据。
    # Args: 无。
    # Returns: 不包含环境变量密文的系统 Server 列表。
    def list_system_servers(self) -> list[dict]:
        """为管理界面补充不存储在租户数据库中的系统 MCP 配置。"""
        logger.debug("列出系统 MCP Server 入口")
        servers = []
        for name, config in self._configured_system_servers.items():
            servers.append({
                "name": name,
                "scope": "system",
                "tenant_id": 0,
                "owner_user_id": 0,
                "transport": config.get("transport", "stdio"),
                "command": config.get("command", ""),
                "args": " ".join(config.get("args", []) or []),
                "url": config.get("url", ""),
                "description": config.get("description", ""),
                "is_builtin": True,
                "enabled": bool(config.get("enabled", True)),
            })
        result = sorted(servers, key=lambda item: item["name"])
        logger.info("列出系统 MCP Server 完成", count=len(result))
        return result

    # 方法作用：按显示名称和可选作用域选择已加载的最高优先级 MCP Server。
    # Args: name - 对外 Server 名称；scope - 可选 system/tenant/private 精确作用域。
    # Returns: 内部名称和作用域；没有匹配项时返回 None。
    def resolve_loaded_server(
        self,
        name: str,
        scope: str | None = None,
    ) -> tuple[str, str] | None:
        logger.debug("解析已加载 MCP Server 入口", name=name, scope=scope or "")
        from src.api.auth import get_current_role, get_current_tenant_id, get_current_user_id

        tenant_id, user_id = get_current_tenant_id(), get_current_user_id()
        platform = get_current_role() == "super_admin"
        priority = {"private": 3, "tenant": 2, "system": 1}
        candidates = [
            (internal_name, self._server_scopes.get(internal_name, "system"))
            for internal_name in self.sessions
            if self._server_scopes.get(internal_name, "system") == "system" or (
                not platform
                and self._server_tenants.get(internal_name) == tenant_id
                and (self._server_scopes.get(internal_name) == "tenant" or self._server_owners.get(internal_name) == user_id)
            )
            if (internal_name == self.scoped_server_name(name, self._server_scopes.get(internal_name, "system"), self._server_tenants.get(internal_name, 0), self._server_owners.get(internal_name, 0)) or internal_name == name)
            and (
                not scope
                or self._server_scopes.get(internal_name, "system") == scope
            )
        ]
        result = max(
            candidates,
            key=lambda item: priority.get(item[1], 0),
            default=None,
        )
        logger.info(
            "解析已加载 MCP Server 完成",
            name=name,
            found=result is not None,
            resolved_scope=result[1] if result else "",
        )
        return result

    # ── 8.1.8 健康检查 ───────────────────────────────

    # 方法作用：在远程调用前重新验证当前身份、连接状态和共享安全阻断。
    # Args: name - 已加载连接的内部名称。
    # Returns: 授权成功无返回值，拒绝或数据库失效不调用远端。
    async def authorize_runtime_server(self, name: str) -> None:
        """工具缓存不构成永久授权，跨 Worker 安全状态每次查库。"""
        from src.api.auth import get_current_role, get_current_tenant_id, get_current_user_id
        from src.memory.pg_pool import pg_connection

        scope = self._server_scopes.get(name, "system")
        tenant_id, user_id, role = get_current_tenant_id(), get_current_user_id(), get_current_role()
        owner_tenant, owner_user = self._server_tenants.get(name, 0), self._server_owners.get(name, 0)
        if scope != "system" and (role == "super_admin" or tenant_id != owner_tenant or (scope == "private" and user_id != owner_user)):
            logger.warning("MCP 运行时身份阻断", scope=scope, tenant_id=tenant_id, user_id=user_id)
            raise PermissionError("无权调用该 MCP 连接")
        if name in self._configured_system_servers:
            return
        if name not in self._server_configs:
            raise PermissionError("MCP 连接未注册")
        public_name = name.removeprefix(f"{scope}_{owner_tenant}_{owner_user}_")
        async with pg_connection(tenant_id=tenant_id, user_id=user_id, role=role) as connection:
            row = await connection.fetchrow(
                "SELECT url,enabled FROM mcp_servers WHERE name=$1 AND scope=$2 AND COALESCE(tenant_id,0)=$3 AND owner_user_id=$4",
                public_name, scope, owner_tenant, owner_user,
            )
            blocked = await connection.fetchval(
                "SELECT blocked FROM mcp_security_blocks WHERE name=$1 AND scope=$2 AND tenant_id=$3 AND owner_user_id=$4",
                public_name, scope, owner_tenant, owner_user,
            )
        if not row or not row["enabled"] or blocked or row["url"] != self._server_configs[name].get("url", ""):
            logger.warning("MCP 运行时安全阻断", server=name, blocked=bool(blocked))
            raise PermissionError("MCP 连接已停用、删除或配置已变更，请重新加载")

    # 方法作用：对全部已连接 MCP Server 执行 ping，并尝试恢复降级配置。
    # Args: 无。
    # Returns: 无返回值。
    async def health_check(self) -> None:
        """已连接服务 ping 失败重连；有配置无会话的降级服务尝试回填。"""
        logger.debug("MCP 健康检查入口", server_count=len(self.sessions))
        attempted: set[str] = set()
        for name, session in list(self.sessions.items()):
            try:
                await session.send_ping()
            except Exception:
                attempted.add(name)
                logger.info("MCP Server 断线，尝试重连", server=name)
                await self._reconnect(name)
        for name in list(self._server_configs):
            if name in attempted or name in self.sessions:
                continue
            logger.info("MCP Server 处于降级状态，尝试恢复", server=name)
            await self._reconnect(name)
        logger.info("MCP 健康检查完成", server_count=len(self.sessions))

    # 方法作用：指数退避等待，独立方法便于测试注入确定性等待。
    # Args: retry - 当前重试轮次。
    # Returns: 无返回值。
    async def _reconnect_backoff_wait(self, retry: int) -> None:
        """默认指数退避（单次最长 30s）；测试注入零等待或删除钩子。"""
        await asyncio.sleep(min(2 ** retry, 30))

    # 方法作用：按轮次重建单个 MCP Server 连接，耗尽后进入降级状态。
    # Args: name - 内部 Server 名称；retry - 起始重试轮次。
    # Returns: 无返回值。
    async def _reconnect(self, name: str, retry: int = 0) -> None:
        """退避在锁外；醒来后在锁内复核 epoch 与配置，避免复活已删除服务。"""
        logger.debug("MCP 重连入口", server=name, retry=retry)
        attempt = retry
        while attempt < _MAX_RECONNECT_RETRIES:
            epoch_before = self._epochs.get(name, 0)
            await self._reconnect_backoff_wait(attempt)
            async with self._gates.lock(name):
                if self._epochs.get(name, 0) != epoch_before:
                    logger.info("MCP 重连已取消", server=name, reason="epoch 变化")
                    return
                if name not in self._server_configs:
                    logger.info("MCP 重连已取消", server=name, reason="配置已移除")
                    return
                config = dict(self._server_configs[name])
                scope = self._server_scopes.get(name, "system")
                tenant_id = self._server_tenants.get(name, 0)
                owner_user_id = self._server_owners.get(name, 0)
                try:
                    await self._teardown_locked(name)
                    await self._connect_single(
                        name,
                        config,
                        scope=scope,
                        tenant_id=tenant_id,
                        owner_user_id=owner_user_id,
                    )
                    logger.info("MCP Server 重连成功", server=name, retry=attempt)
                    return
                except Exception as exc:
                    logger.warning(
                        "MCP Server 重连失败",
                        server=name,
                        retry=attempt,
                        error_type=type(exc).__name__,
                    )
                    attempt += 1
        async with self._gates.lock(name):
            if name not in self._server_configs:
                logger.info("MCP 降级已取消", server=name, reason="配置已移除")
                return
            await self._degrade_locked(name)
            logger.error("MCP Server 已降级", server=name, retries=_MAX_RECONNECT_RETRIES)

    # ── 8.1.10 关闭 ──────────────────────────────────

    # 方法作用：关闭全部 MCP 连接并清理运行时缓存。
    # Args: 无。
    # Returns: 无返回值。
    async def close_all(self) -> None:
        """关闭所有 MCP 连接并清空运行时与作用域元数据。"""
        logger.debug("关闭全部 MCP Server 入口", count=len(self._connections))
        names = set(self._connections) | set(self.sessions) | set(self._server_configs)
        for name in sorted(names):
            try:
                await self.remove_server(name)
            except Exception:
                logger.error("关闭单个 MCP Server 失败", server=name, exc_info=True)
        self.sessions.clear()
        self.langchain_tools.clear()
        self._connections.clear()
        self._server_configs.clear()
        self._server_tenants.clear()
        self._server_owners.clear()
        self._server_scopes.clear()
        self._server_builtins.clear()
        self._loaded_identities.clear()
        self._epochs.clear()
        self._removed_marks.clear()
        logger.info("关闭全部 MCP Server 完成")

    # 方法作用：关闭并移除一个 MCP Server 的运行时连接、工具和全部元数据。
    # Args: name - 内部唯一 Server 名称。
    # Returns: 找到运行时资源时返回 True。
    async def remove_server(self, name: str) -> bool:
        """终态删除：epoch 递增，在途的重连与 reload 复核后不得复活该服务。"""
        logger.debug("移除 MCP Server 入口", server=name)
        async with self._gates.lock(name):
            existed = name in self._server_configs or name in self.sessions
            await self._teardown_locked(name)
            self._server_configs.pop(name, None)
            self._server_tenants.pop(name, None)
            self._server_owners.pop(name, None)
            self._server_scopes.pop(name, None)
            self._server_builtins.pop(name, None)
            self._bump_epoch(name)
            # 记录删除墓碑：后续注册必须由新的 reload/重建刷新 epoch 才可继续。
            self._removed_marks[name] = self._epochs[name]
        logger.info("移除 MCP Server 完成", server=name, removed=existed)
        return existed

    # 方法作用：摘除工具、会话与连接引用并关闭旧 owner 连接（调用方持锁）。
    # Args: name - 内部唯一 Server 名称；close_timeout - 关闭旧连接的有界预算。
    # Returns: 无返回值。
    async def _teardown_locked(
        self,
        name: str,
        *,
        close_timeout: float = _TEARDOWN_CLOSE_TIMEOUT,
    ) -> None:
        """先摘工具、再摘会话，最后关闭连接，失败路径上不留旧越权工具。"""
        keys = [key for key in self.langchain_tools if key.startswith(f"{name}__")]
        for key in keys:
            self.langchain_tools.pop(key, None)
        connection = self._connections.pop(name, None)
        self.sessions.pop(name, None)
        if connection is not None:
            await connection.close(timeout=close_timeout)

    # 方法作用：将服务标记为降级：摘除工具与会话，保留配置以便重试。
    # Args: name - 内部唯一 Server 名称。
    # Returns: 无返回值。
    async def _degrade_locked(self, name: str) -> None:
        """降级不删除配置；随后健康检查或管理员 reset 都可再尝试恢复。"""
        await self._teardown_locked(name)

    # 方法作用：把某服务的重连代号加一，使在途操作复核后放弃。
    # Args: name - 内部唯一 Server 名称。
    # Returns: 无返回值。
    def _bump_epoch(self, name: str) -> None:
        self._epochs[name] = self._epochs.get(name, 0) + 1

    # ── 11.1.12 手动重置 ─────────────────────────────

    # 方法作用：判断内部名是否指向受信任 YAML 内置服务。
    # Args: name - 内部 Server 名称。
    # Returns: 是内置 YAML 服务返回 True。
    def is_builtin_server(self, name: str) -> bool:
        """内置服务不在数据库，仅平台超级管理员可重置。"""
        return name in self._configured_system_servers

    # 方法作用：按显示名称与可选作用域解析可重置的服务（含降级无会话）。
    # Args: name - 对外 Server 名称；scope - 可选 system/tenant/private 精确作用域。
    # Returns: (内部名称, 作用域)；没有匹配或对当前身份不可见时返回 None。
    def resolve_resettable_server(
        self,
        name: str,
        scope: str | None = None,
    ) -> tuple[str, str] | None:
        """可见性规则与 resolve_loaded_server 一致，但候选来自保留的配置。"""
        logger.debug("解析可重置 MCP Server 入口", name=name, scope=scope or "")
        from src.api.auth import (
            get_current_role,
            get_current_tenant_id,
            get_current_user_id,
        )

        tenant_id, user_id = get_current_tenant_id(), get_current_user_id()
        platform = get_current_role() == "super_admin"
        priority = {"private": 3, "tenant": 2, "system": 1}
        candidates: list[tuple[str, str]] = []
        for internal_name in list(self._server_configs):
            internal_scope = self._server_scopes.get(internal_name, "system")
            if internal_scope == "system":
                visible = True
            elif platform:
                visible = False
            elif self._server_tenants.get(internal_name) != tenant_id:
                visible = False
            elif internal_scope == "tenant":
                visible = True
            else:
                visible = self._server_owners.get(internal_name) == user_id
            if not visible or (scope and internal_scope != scope):
                continue
            expected = self.scoped_server_name(
                name,
                internal_scope,
                self._server_tenants.get(internal_name, 0),
                self._server_owners.get(internal_name, 0),
            )
            if internal_name not in (name, expected):
                continue
            candidates.append((internal_name, internal_scope))
        for internal_name in self._configured_system_servers:
            if scope and scope != "system":
                continue
            if internal_name == name:
                candidates.append((internal_name, "system"))
        result = max(
            candidates,
            key=lambda item: priority.get(item[1], 0),
            default=None,
        )
        logger.debug(
            "解析可重置 MCP Server 完成",
            name=name,
            found=result is not None,
            resolved_scope=result[1] if result else "",
        )
        return result

    # 方法作用：为路由已授权的单个受管目标补登记配置与身份元数据（不建立连接）。
    # Args: name - 内部唯一 Server 名；config - 路由已授权配置（transport/url）；
    #       scope - 作用域；tenant_id - 资源租户；owner_user_id - 所有者。
    # Returns: 无返回值；目标已有注册时幂等返回。
    async def prepare_authorized_server(
        self,
        name: str,
        *,
        config: dict,
        scope: str,
        tenant_id: int,
        owner_user_id: int,
    ) -> None:
        """首次重置尚未加载的数据库目标时只准备该服务，不整租户加载。"""
        logger.debug("准备已授权 MCP Server 入口", server=name, scope=scope)
        async with self._gates.lock(name):
            if name in self._server_configs or name in self._configured_system_servers:
                logger.info("准备已授权 MCP Server 完成", server=name, registered=False)
                return
            # 删除墓碑仍有效（epoch 未被后续注册刷新）：查库后删除落地，不得复活。
            mark = self._removed_marks.get(name)
            if mark is not None and self._epochs.get(name, 0) == mark:
                logger.warning("准备已授权 MCP Server 拒绝", server=name, reason="删除终态")
                raise MCPResetUnavailableError(f"MCP Server '{name}' 已删除")
            self._server_configs[name] = dict(config)
            self._server_scopes[name] = scope
            self._server_tenants[name] = tenant_id
            self._server_owners[name] = owner_user_id
            self._server_builtins[name] = False
        logger.info("准备已授权 MCP Server 完成", server=name, registered=True)

    # 方法作用：管理员手动重置单个 MCP Server（破坏式重建并原子替换）。
    # Args: name - 内部唯一 Server 名称；timeout - 整段重置预算秒数；
    #       authorized_config - 路由已授权的受管配置（可选）。
    # Returns: {"scope": 作用域, "tools": 新工具数}。
    async def reset_server(
        self,
        name: str,
        *,
        timeout: float = 30.0,  # noqa: ASYNC109  兼容 spec/56 §5.5 审定的公开契约参数名，不可改名；总预算已用 asyncio.timeout 实现
        authorized_config: dict | None = None,
    ) -> dict:
        """失败后服务处于 unavailable、配置保留，可再次 reset 恢复。"""
        logger.debug("重置 MCP Server 入口", server=name, timeout=timeout)
        loop = asyncio.get_running_loop()
        # 总预算从调用开始计时并用 asyncio.timeout 统一施加：锁等待、拆旧、重建全部计入。
        started = loop.time()
        lock = self._gates.lock(name)
        lock_acquired = False
        try:
            async with asyncio.timeout(timeout):
                # 同服务持锁时排队等待同样消耗总预算，不能拿锁以后才开始计时。
                await lock.acquire()
                lock_acquired = True
                scope, tenant_id, owner_user_id, config = self._resolve_reset_config(
                    name, authorized_config,
                )
                await self._teardown_locked(name)
                remaining = timeout - (loop.time() - started)
                tools = await self._connect_single(
                    name,
                    config,
                    scope=scope,
                    tenant_id=tenant_id,
                    owner_user_id=owner_user_id,
                    timeout=max(remaining, 0.0),
                )
                self._bump_epoch(name)
        except TimeoutError as exc:
            # asyncio.timeout 到点会取消锁等待/拆旧/重建；owner task 收尾由 start/close 内部有界完成。
            logger.info("重置 MCP Server 超时", server=name)
            raise MCPResetTimeoutError(f"MCP Server '{name}' 重置超时") from exc
        except MCPResetError:
            raise
        except Exception as exc:
            # 此处只做异常归类并向上转换，不记录 error：原始故障位置已由 owner task
            # 边界（启动失败）单次记录，路由异常映射负责最外层单次记录；同一次故障
            # 若在 owner→manager→route 链路上重复落盘会放大敏感正文的暴露面。
            raise MCPResetError(f"MCP Server '{name}' 重建失败") from exc
        finally:
            # 只有真正持有锁才释放；锁等待被超时取消时不得误释放他人持有的锁。
            if lock_acquired:
                lock.release()
        duration_ms = round((loop.time() - started) * 1000)
        logger.info(
            "重置 MCP Server 完成",
            server=name,
            scope=scope,
            tools=len(tools),
            duration_ms=duration_ms,
        )
        return {"scope": scope, "tools": len(tools)}

    # 方法作用：解析重置目标的作用域、所有者与可用配置，不可用时拒绝重建。
    # Args: name - 内部唯一 Server 名称；authorized_config - 路由已授权配置（可选）。
    # Returns: (scope, tenant_id, owner_user_id, config)；不可用时抛 MCPReset 异常。
    def _resolve_reset_config(
        self,
        name: str,
        authorized_config: dict | None,
    ) -> tuple[str, int, int, dict]:
        """内置服务用 YAML 配置；受管服务要求已注册并通过安全校验。"""
        if name in self._configured_system_servers:
            config = dict(self._configured_system_servers[name])
            if not config.get("enabled", True):
                raise MCPResetBlockedError(f"MCP Server '{name}' 已停用")
            return (
                self._server_scopes.get(name, "system"),
                self._server_tenants.get(name, 0),
                self._server_owners.get(name, 0),
                config,
            )
        if name not in self._server_configs:
            raise MCPResetUnavailableError(f"MCP Server '{name}' 未注册或已删除")
        candidate = (
            dict(authorized_config)
            if authorized_config
            else dict(self._server_configs[name])
        )
        if not _is_managed_remote_config_allowed(candidate):
            raise MCPResetBlockedError(f"MCP Server '{name}' 配置未通过安全校验")
        self._server_configs[name] = candidate
        return (
            self._server_scopes.get(name, "private"),
            self._server_tenants.get(name, 0),
            self._server_owners.get(name, 0),
            candidate,
        )

    # ── 8.1.11 SSE Client ────────────────────────────

    @staticmethod
    # 方法作用：创建 SSE transport 异步上下文。
    # Args: url - MCP SSE 服务地址。
    # Returns: MCP sse_client 上下文管理器。
    def _sse_client(url: str):
        logger.debug("创建 MCP SSE Client 入口")
        from mcp.client.sse import sse_client
        result = sse_client(url)
        logger.info("创建 MCP SSE Client 完成")
        return result


    # ── 测试 + PG 重载 ──

    # 方法作用：测试已加载或临时探测的 MCP Server 连通性。
    # Args: name - 内部 Server 名称。
    # Returns: ping 成功返回 True。
    async def test_connection(self, name: str) -> bool:
        """测试已加载 MCP Server 是否可响应 ping。"""
        logger.debug("测试 MCP Server 入口", server=name)
        session = self.sessions.get(name)
        if session is not None:
            try:
                await session.send_ping()
                logger.info("测试 MCP Server 完成", server=name, success=True)
                return True
            except Exception:
                logger.error("测试 MCP Server 失败", server=name, exc_info=True)
                return False
        cfg = self._server_configs.get(name)
        if not cfg:
            logger.warning("测试 MCP Server 跳过", server=name, reason="配置未加载")
            return False
        probe_name = f"probe_{name}"
        try:
            await self._connect_single(
                probe_name, cfg, scope="private", tenant_id=-1, owner_user_id=-1,
            )
            await self.sessions[probe_name].send_ping()
            await self.remove_server(probe_name)
            logger.info("测试 MCP Server 完成", server=name, success=True)
            return True
        except Exception:
            await self.remove_server(probe_name)
            logger.error("测试 MCP Server 失败", server=name, exc_info=True)
            return False

    # 方法作用：从 PostgreSQL 加载当前身份可见的 MCP 配置并建立连接。
    # Args: tenant_id - 当前租户；user_id - 当前用户。
    # Returns: 建立连接数量；数据库失败返回 -1。
    async def reload_from_db(
        self, tenant_id: int | None = None, user_id: int | None = None,
    ) -> int:
        """从 PG 加载全部或指定身份可访问的启用 MCP 配置并建立连接。"""
        logger.debug("从数据库重载 MCP 入口", tenant_id=tenant_id, user_id=user_id)
        try:
            import json as _j

            from src.memory.pg_pool import pg_connection

            # 读库前的整体快照：查询期间发生的删除/重置会让对应行过期。
            epoch_snapshot = dict(self._epochs)
            async with pg_connection(
                tenant_id=tenant_id or 0,
                user_id=user_id or 0,
                role="analyst",
            ) as conn:
                if tenant_id is None:
                    rows = await conn.fetch(
                        "SELECT name, scope, tenant_id, owner_user_id, transport, command, args, "
                        "url, env_vars FROM mcp_servers WHERE enabled=TRUE AND scope='system'",
                    )
                else:
                    rows = await conn.fetch(
                        "SELECT name, scope, tenant_id, owner_user_id, transport, command, args, "
                        "url, env_vars FROM mcp_servers WHERE enabled=TRUE AND NOT EXISTS ("
                        "SELECT 1 FROM mcp_security_blocks b WHERE b.tenant_id=mcp_servers.tenant_id AND b.scope=mcp_servers.scope "
                        "AND b.owner_user_id=mcp_servers.owner_user_id AND b.name=mcp_servers.name AND b.blocked) AND "
                        "(scope='system' OR (scope='tenant' AND tenant_id=$1) OR "
                        "(scope='private' AND tenant_id=$1 AND owner_user_id=$2))",
                        tenant_id, user_id or 0,
                    )
            count = 0
            for r in rows:
                if not _is_managed_remote_config_allowed(r):
                    continue
                scope = str(r["scope"] or "tenant")
                row_tenant = int(r["tenant_id"] or 0)
                row_owner = int(r["owner_user_id"] or 0)
                internal_name = self.scoped_server_name(
                    str(r["name"]), scope, row_tenant, row_owner,
                )
                cfg = {"transport": r["transport"]}
                if r["transport"] == "stdio":
                    cfg["command"] = r["command"]
                    cfg["args"] = (r["args"] or "").split()
                else:
                    cfg["url"] = r["url"]
                env = r["env_vars"] or {}
                if isinstance(env, str):
                    env = _j.loads(env)
                if env:
                    cfg["env"] = env
                async with self._gates.lock(internal_name):
                    if self._epochs.get(internal_name, 0) != epoch_snapshot.get(
                        internal_name, 0,
                    ):
                        logger.info(
                            "数据库 MCP 配置已过期，跳过",
                            server=internal_name,
                            reason="epoch 变化",
                        )
                        continue
                    unchanged = (
                        internal_name in self.sessions
                        and self._server_configs.get(internal_name) == cfg
                        and self._server_scopes.get(internal_name) == scope
                        and self._server_tenants.get(internal_name) == row_tenant
                        and self._server_owners.get(internal_name) == row_owner
                    )
                    if unchanged:
                        logger.debug("数据库 MCP 配置未变化，复用连接", server=internal_name)
                        continue
                    await self._teardown_locked(internal_name)
                    self._server_configs[internal_name] = cfg
                    self._server_scopes[internal_name] = scope
                    self._server_tenants[internal_name] = row_tenant
                    self._server_owners[internal_name] = row_owner
                    self._server_builtins[internal_name] = False
                    try:
                        await self._connect_single(
                            internal_name, cfg, scope=scope, tenant_id=row_tenant,
                            owner_user_id=row_owner,
                        )
                        count += 1
                    except Exception:
                        logger.error(
                            "数据库 MCP 连接失败", server=internal_name, exc_info=True,
                        )
                    self._bump_epoch(internal_name)
            logger.info("PG MCP 配置已加载", tenant_id=tenant_id, user_id=user_id, count=count)
            return count
        except Exception as e:
            logger.error("PG MCP 加载失败", error=str(e), exc_info=True)
            return -1

    # 方法作用：向 PostgreSQL 连接注入 RLS 所需的认证上下文。
    # Args: connection - asyncpg 连接；tenant_id - 当前租户；user_id - 当前用户；role - 当前角色。
    # Returns: 无返回值。
    @staticmethod
    async def _set_connection_identity(
        connection: Any, *, tenant_id: int, user_id: int, role: str,
    ) -> None:
        """使用事务局部 set_config 注入 RLS 身份。"""
        logger.debug(
            "注入 MCP 数据库身份入口", tenant_id=tenant_id, user_id=user_id, role=role,
        )
        await connection.execute(
            "SELECT set_config('app.current_tenant_id', $1, true), "
            "set_config('app.current_user_id', $2, true), "
            "set_config('app.current_role', $3, true)",
            str(tenant_id), str(user_id), role,
        )
        logger.info("注入 MCP 数据库身份完成", tenant_id=tenant_id, user_id=user_id, role=role)

    # 方法作用：确保当前租户和用户可见的数据库 MCP 配置在本进程中完成加载。
    # Args: tenant_id - 当前租户；user_id - 当前用户；force - 是否强制重新加载。
    # Returns: 本次新建立或重建的连接数量。
    async def ensure_scoped_servers(
        self, tenant_id: int, user_id: int, *, force: bool = False,
    ) -> int:
        """按身份惰性加载 MCP 配置，避免启动时连接所有租户私有服务。"""
        logger.debug(
            "确保身份 MCP 已加载入口", tenant_id=tenant_id, user_id=user_id, force=force,
        )
        from src.api.auth import get_current_role

        if get_current_role() == "super_admin":
            tenant_id, user_id = 0, 0
        identity = (tenant_id, user_id)
        if identity in self._loaded_identities and not force:
            logger.info("确保身份 MCP 已加载完成", tenant_id=tenant_id, user_id=user_id, cached=True)
            return 0
        count = await self.reload_from_db(tenant_id=tenant_id, user_id=user_id)
        if count >= 0:
            self._loaded_identities.add(identity)
        logger.info(
            "确保身份 MCP 已加载完成",
            tenant_id=tenant_id,
            user_id=user_id,
            cached=False,
            count=count,
        )
        return max(count, 0)

    # 方法作用：生成带作用域和所有者的 MCP Server 内部唯一名称。
    # Args: name - 对外名称；scope - 作用域；tenant_id - 租户；owner_user_id - 所有者。
    # Returns: 运行时内部名称。
    @staticmethod
    def scoped_server_name(
        name: str, scope: str, tenant_id: int, owner_user_id: int,
    ) -> str:
        """防止不同作用域的同名 Server 覆盖运行时连接。"""
        logger.debug(
            "生成 MCP 内部名称入口", name=name, scope=scope,
            tenant_id=tenant_id, owner_user_id=owner_user_id,
        )
        result = f"{scope}_{tenant_id}_{owner_user_id}_{name}"
        logger.info("生成 MCP 内部名称完成", internal_name=result)
        return result


# 方法作用：把 JSON Schema 基础类型映射为 Python 类型。
# Args: type_name - JSON Schema type 名称。
# Returns: 对应 Python 类型，未知类型回退 str。
def _json_type_to_python(type_name: str) -> type:
    logger.debug("映射 JSON Schema 类型入口", type_name=type_name)
    result = {"string": str, "integer": int, "number": float, "boolean": bool,
              "array": list, "object": dict}.get(type_name, str)
    logger.info("映射 JSON Schema 类型完成", type_name=type_name, python_type=result.__name__)
    return result


# 方法作用：从当前 AppContext 获取 MCPClientManager。
# Args: config_path - 首次初始化使用的 YAML 配置路径。
# Returns: 当前应用独享的 MCPClientManager 实例。
def get_mcp_client_manager(config_path: str = "config/app.yaml") -> MCPClientManager:
    from functools import partial

    from src.app_context import get_app_context

    logger.debug("获取 MCPClientManager 入口", config_path=config_path)
    result = get_app_context().get_or_create(
        "mcp_client_manager",
        partial(MCPClientManager, config_path),
        closer=MCPClientManager.close_all,
    )
    logger.debug("获取 MCPClientManager 完成")
    return result
