"""8.1 MCP Client Manager — 管理 MCP Server 连接与工具转换。

依据: SPEC §3.9.1
"""

from __future__ import annotations

import asyncio
import os
from contextlib import AsyncExitStack
from pathlib import Path
from typing import Any

import yaml
from langchain_core.tools import BaseTool, StructuredTool

from src.logging_config import get_logger

logger = get_logger(__name__)

_MAX_RECONNECT_RETRIES = 5


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
    def __init__(self, config_path: str = "config/mcp_servers.yaml"):
        logger.debug("初始化 MCPClientManager 入口", config_path=config_path)
        self.config_path = config_path
        self.sessions: dict[str, Any] = {}
        self.exit_stack = AsyncExitStack()
        self.langchain_tools: dict[str, BaseTool] = {}
        self._server_configs: dict[str, dict] = {}
        self._server_tenants: dict[str, int] = {}
        self._server_owners: dict[str, int] = {}
        self._server_scopes: dict[str, str] = {}
        self._server_builtins: dict[str, bool] = {}
        self._server_stacks: dict[str, AsyncExitStack] = {}
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
        for name, result in zip(servers.keys(), results):
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
            import asyncpg
            from src.config import get_settings

            migration_path = Path(__file__).resolve().parents[2] / "migrations" / "004_resource_scopes.sql"
            migration_sql = migration_path.read_text(encoding="utf-8")
            url = get_settings().database_url.replace("postgresql+asyncpg://", "postgresql://")
            connection = await asyncpg.connect(url)
            try:
                await connection.execute(migration_sql)
            finally:
                await connection.close()
            logger.info("初始化 MCP 数据库结构完成", migration=migration_path.name)
            return True
        except Exception as exc:
            logger.error("初始化 MCP 数据库结构失败", error=str(exc), exc_info=True)
            return False

    # ── 8.1.3 单连接 ────────────────────────────────

    # 方法作用：建立单个 MCP 连接并注册其工具与可信作用域。
    # Args: name - 内部名称；config - 连接配置；scope - 作用域；tenant_id - 租户；owner_user_id - 所有者。
    # Returns: 转换后的 LangChain 工具列表。
    async def _connect_single(
        self,
        name: str,
        config: dict,
        scope: str = "system",
        tenant_id: int = 0,
        owner_user_id: int = 0,
    ) -> list[BaseTool]:
        """建立单个 MCP 连接并记录可信作用域元数据。"""
        logger.debug(
            "连接单个 MCP Server 入口",
            server=name,
            scope=scope,
            tenant_id=tenant_id,
            owner_user_id=owner_user_id,
        )
        from mcp import ClientSession, StdioServerParameters
        from mcp.client.stdio import stdio_client

        transport = config.get("transport", "stdio")
        if transport == "stdio":
            server_params = StdioServerParameters(
                command=config["command"], args=config.get("args", []),
                env=self._resolve_env(config.get("env", {})),
            )
            transport_ctx = stdio_client(server_params)
        elif transport == "sse":
            transport_ctx = self._sse_client(config["url"])
        else:
            raise ValueError(f"不支持的 transport: {transport}")

        server_stack = AsyncExitStack()
        try:
            streams = await server_stack.enter_async_context(transport_ctx)
            session = await server_stack.enter_async_context(ClientSession(*streams))
            await session.initialize()
        except Exception:
            await server_stack.aclose()
            logger.error("连接单个 MCP Server 失败", server=name, exc_info=True)
            raise

        mcp_tools = await session.list_tools()
        langchain_tools = []
        for mt in mcp_tools.tools:
            lt = self._mcp_to_langchain_tool(mt, session, name)
            langchain_tools.append(lt)
            self.langchain_tools[f"{name}__{mt.name}"] = lt
        self.sessions[name] = session
        self._server_stacks[name] = server_stack
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
                from pydantic import BaseModel, Field, create_model
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
            if visible:
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

    # ── 8.1.8 健康检查 ───────────────────────────────

    # 方法作用：对全部已连接 MCP Server 执行 ping 并触发断线重连。
    # Args: 无。
    # Returns: 无返回值。
    async def health_check(self) -> None:
        logger.debug("MCP 健康检查入口", server_count=len(self.sessions))
        for name, session in list(self.sessions.items()):
            try:
                await session.send_ping()
            except Exception:
                logger.warning(f"MCP Server '{name}' 断线，尝试重连")
                await self._reconnect(name)
        logger.info("MCP 健康检查完成", server_count=len(self.sessions))

    # ── 8.1.9 重连 + 8.1.12 降级 ───────────────────

    # 方法作用：使用指数退避重建单个 MCP Server 连接。
    # Args: name - 内部 Server 名称；retry - 当前重试次数。
    # Returns: 无返回值。
    async def _reconnect(self, name: str, retry: int = 0) -> None:
        logger.debug("MCP 重连入口", server=name, retry=retry)
        if retry >= _MAX_RECONNECT_RETRIES:
            logger.error(f"MCP Server '{name}' 降级", retries=_MAX_RECONNECT_RETRIES)
            keys = [k for k in self.langchain_tools if k.startswith(f"{name}__")]
            for k in keys:
                self.langchain_tools.pop(k, None)
            self.sessions.pop(name, None)
            return
        delay = min(2 ** retry, 30)
        await asyncio.sleep(delay)
        config = dict(self._server_configs.get(name, {}))
        scope = self._server_scopes.get(name, "system")
        tenant_id = self._server_tenants.get(name, 0)
        owner_user_id = self._server_owners.get(name, 0)
        try:
            await self.remove_server(name)
            self._server_configs[name] = config
            self._server_scopes[name] = scope
            self._server_tenants[name] = tenant_id
            self._server_owners[name] = owner_user_id
            await self._connect_single(
                name,
                config,
                scope=scope,
                tenant_id=tenant_id,
                owner_user_id=owner_user_id,
            )
            logger.info(f"MCP Server '{name}' 重连成功")
        except Exception as exc:
            logger.warning("MCP Server 重连失败", server=name, retry=retry, error=str(exc))
            await self._reconnect(name, retry + 1)

    # ── 8.1.10 关闭 ──────────────────────────────────

    # 方法作用：关闭全部 MCP 连接并清理运行时缓存。
    # Args: 无。
    # Returns: 无返回值。
    async def close_all(self) -> None:
        """关闭所有 MCP 连接并清空作用域元数据。"""
        logger.debug("关闭全部 MCP Server 入口", count=len(self._server_stacks))
        for name in list(self._server_stacks):
            await self.remove_server(name)
        await self.exit_stack.aclose()
        self.sessions.clear()
        self.langchain_tools.clear()
        self._server_tenants.clear()
        self._server_owners.clear()
        self._server_scopes.clear()
        self._server_builtins.clear()
        self._loaded_identities.clear()
        logger.info("关闭全部 MCP Server 完成")

    # 方法作用：关闭并移除一个 MCP Server 的运行时连接、工具和作用域元数据。
    # Args: name - 内部唯一 Server 名称。
    # Returns: 找到运行时资源时返回 True。
    async def remove_server(self, name: str) -> bool:
        """安全移除单个 MCP Server，不影响其他连接。"""
        logger.debug("移除 MCP Server 入口", server=name)
        existed = name in self._server_configs or name in self.sessions
        keys = [key for key in self.langchain_tools if key.startswith(f"{name}__")]
        for key in keys:
            self.langchain_tools.pop(key, None)
        stack = self._server_stacks.pop(name, None)
        if stack is not None:
            try:
                await stack.aclose()
            except Exception:
                logger.error("关闭 MCP Server 连接失败", server=name, exc_info=True)
        self.sessions.pop(name, None)
        self._server_configs.pop(name, None)
        self._server_tenants.pop(name, None)
        self._server_owners.pop(name, None)
        self._server_scopes.pop(name, None)
        self._server_builtins.pop(name, None)
        logger.info("移除 MCP Server 完成", server=name, removed=existed)
        return existed

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
            import asyncpg, json as _j
            from src.config import get_settings
            url = get_settings().database_url.replace("postgresql+asyncpg://", "postgresql://")
            conn = await asyncpg.connect(url)
            try:
                await self._set_connection_identity(
                    conn, tenant_id=tenant_id or 0, user_id=user_id or 0, role="analyst",
                )
                if tenant_id is None:
                    rows = await conn.fetch(
                        "SELECT name, scope, tenant_id, owner_user_id, transport, command, args, "
                        "url, env_vars FROM mcp_servers WHERE enabled=TRUE",
                    )
                else:
                    rows = await conn.fetch(
                        "SELECT name, scope, tenant_id, owner_user_id, transport, command, args, "
                        "url, env_vars FROM mcp_servers WHERE enabled=TRUE AND "
                        "(scope='system' OR (scope='tenant' AND tenant_id=$1) OR "
                        "(scope='private' AND tenant_id=$1 AND owner_user_id=$2))",
                        tenant_id, user_id or 0,
                    )
            finally:
                await conn.close()
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
                if internal_name in self.sessions:
                    await self.remove_server(internal_name)
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
        """使用 set_config 设置连接级 RLS 身份，连接关闭后自动释放。"""
        logger.debug(
            "注入 MCP 数据库身份入口", tenant_id=tenant_id, user_id=user_id, role=role,
        )
        await connection.execute(
            "SELECT set_config('app.current_tenant_id', $1, false), "
            "set_config('app.current_user_id', $2, false), "
            "set_config('app.current_role', $3, false)",
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


_client_manager: MCPClientManager | None = None


# 方法作用：获取全局 MCPClientManager 单例。
# Args: config_path - 首次初始化使用的 YAML 配置路径。
# Returns: MCPClientManager 全局单例。
def get_mcp_client_manager(config_path: str = "config/mcp_servers.yaml") -> MCPClientManager:
    logger.debug("获取 MCPClientManager 单例入口", config_path=config_path)
    global _client_manager
    if _client_manager is None:
        _client_manager = MCPClientManager(config_path)
    logger.info("获取 MCPClientManager 单例完成")
    return _client_manager
