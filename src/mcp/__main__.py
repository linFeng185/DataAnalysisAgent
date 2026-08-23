"""通过 `python -m src.mcp` 启动 FastMCP Server。"""

from __future__ import annotations

from src.logging_config import get_logger
from src.mcp_client.server import create_mcp_server

logger = get_logger(__name__)


# 方法作用：创建并以前台模式运行 FastMCP Server。
# Args: 无。
# Returns: 无返回值，Server 停止后结束进程。
def main() -> None:
    """执行功能 8.2.6 声明的模块启动入口。"""
    logger.debug("MCP Server 模块启动入口")
    try:
        create_mcp_server().run()
    except Exception as exc:
        logger.error(
            "MCP Server 模块启动失败",
            error_type=type(exc).__name__,
            exc_info=True,
        )
        raise
    logger.info("MCP Server 模块已停止")


if __name__ == "__main__":
    main()
