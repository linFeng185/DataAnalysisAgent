"""MCP Agent 工作流节点直接单元测试。"""

from __future__ import annotations

import logging
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock


logger = logging.getLogger(__name__)


class TestMCPAgentNode:
    """覆盖功能 20.8：MCP Agent 授权边界与降级输出。"""

    # 方法作用：验证模型不可用时仍输出统一失败契约并传递身份。
    # Args: self - pytest 测试类实例；monkeypatch - pytest 补丁工具。
    # Returns: 无返回值，断言失败时由 pytest 报告。
    async def test_model_unavailable_returns_standard_failure(self, monkeypatch) -> None:
        """节点降级时必须保留统一响应结构和当前请求身份。"""
        logger.debug("test_model_unavailable_returns_standard_failure 入口")
        try:
            # Arrange
            import src.graph.nodes.mcp_agent as node_module
            import src.llm.client as llm_client
            import src.mcp_client.client_manager as manager_module

            manager = SimpleNamespace(
                ensure_scoped_servers=AsyncMock(),
                get_all_tools=MagicMock(return_value=[]),
            )
            monkeypatch.setattr(manager_module, "get_mcp_client_manager", lambda: manager)
            monkeypatch.setattr(llm_client, "is_task_llm_available", lambda task: False)

            # Act
            result = await node_module.mcp_agent_node({
                "tenant_id": 4,
                "user_id": 7,
                "user_query": "分析文件",
                "skill_tools": [],
            })

            # Assert
            manager.ensure_scoped_servers.assert_awaited_once_with(4, 7)
            manager.get_all_tools.assert_called_once_with(tenant_id=4, user_id=7)
            assert result["final_response"]["success"] is False
            assert result["final_response"]["source"] == "mcp_agent"
            logger.info("test_model_unavailable_returns_standard_failure 完成")
        except Exception as exc:
            logger.error(
                "test_model_unavailable_returns_standard_failure 异常: %s",
                exc,
                exc_info=True,
            )
            raise
