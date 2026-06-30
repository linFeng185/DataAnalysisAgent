"""8.2.6 MCP Server 启动入口 — `python -m src.mcp`。"""

from __future__ import annotations

from src.mcp.server import create_mcp_server

if __name__ == "__main__":
    create_mcp_server().run()
