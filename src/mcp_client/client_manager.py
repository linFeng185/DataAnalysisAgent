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


class MCPClientManager:
    """8.1.1 管理 MCP Client 连接生命周期。

    - connect_all()    → 启动时连接所有 MCP Server
    - get_all_tools()  → 返回 LangChain BaseTool 列表
    - health_check()   → 定期 ping，断线重连
    - close_all()      → 优雅关闭
    """

    def __init__(self, config_path: str = "config/mcp_servers.yaml"):
        self.config_path = config_path
        self.sessions: dict[str, Any] = {}
        self.exit_stack = AsyncExitStack()
        self.langchain_tools: dict[str, BaseTool] = {}
        self._server_configs: dict[str, dict] = {}

    # ── 8.1.2 并发连接 ───────────────────────────────

    async def connect_all(self) -> None:
        """启动时并发连接所有 MCP Server。"""
        cfg_path = Path(self.config_path)
        if not cfg_path.exists():
            logger.info("MCP 配置文件不存在", path=self.config_path)
            return
        config = yaml.safe_load(cfg_path.read_text(encoding="utf-8"))
        servers = config.get("mcp_servers", {})
        if not servers:
            return
        self._server_configs = servers
        tasks = [self._connect_single(n, c) for n, c in servers.items()]
        results = await asyncio.gather(*tasks, return_exceptions=True)
        for name, result in zip(servers.keys(), results):
            if isinstance(result, Exception):
                logger.error(f"MCP Server '{name}' 连接失败", error=str(result))
            else:
                logger.info(f"MCP Server '{name}' 连接成功", tools=len(result))

    # ── 8.1.3 单连接 ────────────────────────────────

    async def _connect_single(self, name: str, config: dict) -> list[BaseTool]:
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

        streams = await self.exit_stack.enter_async_context(transport_ctx)
        session = await self.exit_stack.enter_async_context(ClientSession(*streams))
        await session.initialize()

        mcp_tools = await session.list_tools()
        langchain_tools = []
        for mt in mcp_tools.tools:
            lt = self._mcp_to_langchain_tool(mt, session, name)
            langchain_tools.append(lt)
            self.langchain_tools[f"{name}__{mt.name}"] = lt
        self.sessions[name] = session
        return langchain_tools

    # ── 8.1.4 环境变量解析 ───────────────────────────

    @staticmethod
    def _resolve_env(env: dict) -> dict | None:
        if not env:
            return None
        resolved = {}
        for k, v in env.items():
            if isinstance(v, str) and v.startswith("${") and v.endswith("}"):
                resolved[k] = os.environ.get(v[2:-1], "")
            else:
                resolved[k] = v
        return resolved

    # ── 8.1.5 工具转换 + 8.1.6 Schema ────────────────

    def _mcp_to_langchain_tool(self, mcp_tool, session: Any, namespace: str) -> BaseTool:
        async def _call(**kwargs) -> str:
            result = await session.call_tool(mcp_tool.name, arguments=kwargs)
            return result.content[0].text if result.content else ""

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
            except Exception:
                pass

        return StructuredTool(
            name=f"{namespace}__{mcp_tool.name}",
            description=mcp_tool.description or "",
            coroutine=_call,
            args_schema=schema,
        )

    # ── 8.1.7 获取工具 ──────────────────────────────

    def get_all_tools(self) -> list[BaseTool]:
        return list(self.langchain_tools.values())

    # ── 8.1.8 健康检查 ───────────────────────────────

    async def health_check(self) -> None:
        for name, session in list(self.sessions.items()):
            try:
                await session.send_ping()
            except Exception:
                logger.warning(f"MCP Server '{name}' 断线，尝试重连")
                await self._reconnect(name)

    # ── 8.1.9 重连 + 8.1.12 降级 ───────────────────

    async def _reconnect(self, name: str, retry: int = 0) -> None:
        if retry >= _MAX_RECONNECT_RETRIES:
            logger.error(f"MCP Server '{name}' 降级", retries=_MAX_RECONNECT_RETRIES)
            keys = [k for k in self.langchain_tools if k.startswith(f"{name}__")]
            for k in keys:
                self.langchain_tools.pop(k, None)
            self.sessions.pop(name, None)
            return
        delay = min(2 ** retry, 30)
        await asyncio.sleep(delay)
        try:
            await self._connect_single(name, self._server_configs.get(name, {}))
            logger.info(f"MCP Server '{name}' 重连成功")
        except Exception:
            await self._reconnect(name, retry + 1)

    # ── 8.1.10 关闭 ──────────────────────────────────

    async def close_all(self) -> None:
        await self.exit_stack.aclose()
        self.sessions.clear()
        self.langchain_tools.clear()

    # ── 8.1.11 SSE Client ────────────────────────────

    @staticmethod
    def _sse_client(url: str):
        from mcp.client.sse import sse_client
        return sse_client(url)


    # ── 测试 + PG 重载 ──

    async def test_connection(self, name: str) -> bool:
        """测试 MCP Server 连通性。"""
        cfg = self._server_configs.get(name)
        if not cfg: return False
        try:
            session = await self._create_session(name, cfg)
            await session.send_ping()
            await session.__aexit__(None, None, None)
            return True
        except Exception:
            return False

    async def reload_from_db(self, tenant_id: int = 1) -> int:
        """从 PG 加载租户自定义 MCP 配置。"""
        try:
            import asyncpg, json as _j
            from src.config import get_settings
            url = get_settings().database_url.replace("postgresql+asyncpg://", "postgresql://")
            conn = await asyncpg.connect(url)
            rows = await conn.fetch(
                "SELECT name, transport, command, args, url, env_vars FROM mcp_servers WHERE tenant_id=$1", tenant_id)
            count = 0
            for r in rows:
                if r["name"] not in self._server_configs:
                    cfg = {"transport": r["transport"]}
                    if r["transport"] == "stdio": cfg["command"] = r["command"]; cfg["args"] = (r["args"] or "").split()
                    else: cfg["url"] = r["url"]
                    env = r["env_vars"] or {}
                    if isinstance(env, str): env = _j.loads(env)
                    if env: cfg["env"] = env
                    self._server_configs[r["name"]] = cfg; count += 1
            await conn.close()
            if count: logger.info("PG MCP 配置已加载", tenant_id=tenant_id, count=count)
            return count
        except Exception as e:
            logger.warning("PG MCP 加载失败", error=str(e))
            return 0


def _json_type_to_python(type_name: str) -> type:
    return {"string": str, "integer": int, "number": float, "boolean": bool,
            "array": list, "object": dict}.get(type_name, str)


_client_manager: MCPClientManager | None = None


def get_mcp_client_manager(config_path: str = "config/mcp_servers.yaml") -> MCPClientManager:
    global _client_manager
    if _client_manager is None:
        _client_manager = MCPClientManager(config_path)
    return _client_manager
