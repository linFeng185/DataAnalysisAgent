"""兼容旧的 `python -m src.mcp_client` 启动方式。"""

from __future__ import annotations

from src.mcp.__main__ import main

if __name__ == "__main__":
    main()
