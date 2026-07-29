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

    # 方法作用：验证 MCP 失败响应不会持久化原始异常和敏感连接信息。
    # Args: self - pytest 测试类实例。
    # Returns: 无返回值，断言失败时由 pytest 报告。
    def test_agent_failure_does_not_store_raw_exception(self) -> None:
        """MCP 失败摘要和工具输出不得携带内部异常文本。"""
        # Arrange
        from src.graph.nodes.mcp_agent import _mcp_standard_output

        # Act
        result = _mcp_standard_output(
            {"user_query": "分析文件"},
            "postgres://secret-password:internal-path",
            success=False,
        )

        # Assert
        final = result["final_response"]
        assert final["success"] is False
        assert "secret-password" not in final["analysis"]["summary"]
        assert result["mcp_agent_output"] == ""

    # 方法作用：验证预算包装后保留原工具的参数 Schema。
    # Args: self - pytest 测试类实例。
    # Returns: 无返回值，断言失败时由 pytest 报告。
    def test_budget_wrapper_preserves_tool_input_schema(self) -> None:
        """预算包装不能让原工具参数从 Agent 可见 Schema 中消失。"""
        # Arrange
        from langchain_core.tools import BaseTool
        from src.graph.nodes.mcp_agent import _budget_tool

        class EchoTool(BaseTool):
            """带显式 value 参数的同步测试工具。"""

            name: str = "echo"
            description: str = "回显输入"

            # 方法作用：同步回显输入以提供显式工具参数。
            # Args: self - 工具实例；value - 待回显文本。
            # Returns: 原始输入文本。
            def _run(self, value: str) -> str:
                return value

        # Act
        wrapped = _budget_tool(EchoTool(), {"count": 0}, 2)
        fields = wrapped.get_input_schema().model_fields

        # Assert
        assert "value" in fields

    # 方法作用：验证 MCP Agent 不会把 Skill 的显式零预算替换成默认额度。
    # Args: self - pytest 测试类实例。
    # Returns: 无返回值，断言失败时由 pytest 报告。
    def test_skill_tool_zero_budget_is_preserved(self) -> None:
        """存在 Skill 工具时，零预算必须返回零而不是默认 20。"""
        # Arrange
        from src.graph.nodes.mcp_agent import _resolve_tool_limit

        state = {"skill_tool_budget": 0}

        # Act
        limit = _resolve_tool_limit(state, has_skill_tools=True)

        # Assert
        assert limit == 0
