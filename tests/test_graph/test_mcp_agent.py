"""MCP Agent 工作流节点直接单元测试。"""

from __future__ import annotations

import logging
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

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

    # 方法作用：验证请求级附件工具拒绝读取未在 file_ids 白名单中的文件。
    # Args: self - pytest 测试类实例；monkeypatch - pytest 补丁工具。
    # Returns: 无返回值，断言失败时由 pytest 报告。
    async def test_attachment_tool_rejects_file_outside_request(self, monkeypatch) -> None:
        """附件授权必须同时通过请求白名单和 FileStore 身份过滤。"""
        # Arrange
        import src.knowledge.file_store as file_store_module
        from src.graph.nodes.mcp_agent import _build_attachment_tools

        store = SimpleNamespace(get=AsyncMock())
        monkeypatch.setattr(file_store_module, "get_file_store", lambda: store)
        tool = _build_attachment_tools({"file_ids": [7]})[0]

        # Act / Assert
        with pytest.raises(PermissionError, match="当前请求"):
            await tool.ainvoke({"file_id": 8})
        store.get.assert_not_awaited()

    # 方法作用：验证 CSV 附件返回真实 profile、预览和文件引用。
    # Args: self - pytest 测试类实例；monkeypatch - pytest 补丁工具。
    # Returns: 无返回值，断言失败时由 pytest 报告。
    async def test_attachment_tool_returns_csv_profile_preview_and_citation(self, monkeypatch) -> None:
        """结构化附件不得退化为自由文本，且引用必须来自工具真实输出。"""
        # Arrange
        import src.knowledge.file_store as file_store_module
        from src.graph.nodes.mcp_agent import _build_attachment_tools

        store = SimpleNamespace(get=AsyncMock(return_value={
            "id": 7,
            "filename": "orders.csv",
            "file_data": b"id,amount\n1,12.5\n2,9.0\n",
        }))
        monkeypatch.setattr(file_store_module, "get_file_store", lambda: store)
        tool = _build_attachment_tools({"file_ids": [7]})[0]

        # Act
        result = await tool.ainvoke({"file_id": 7})

        # Assert
        assert result["kind"] == "structured"
        assert result["profile"]["row_count"] == 2
        assert result["preview"][0] == {"id": 1, "amount": 12.5}
        assert result["citation"] == {
            "source_id": "file:7",
            "locator": {"file_id": 7, "filename": "orders.csv"},
        }

    # 方法作用：验证不存在或跨用户附件在 FileStore 边界失败关闭。
    # Args: self - pytest 测试类实例；monkeypatch - pytest 补丁工具。
    # Returns: 无返回值，断言失败时由 pytest 报告。
    async def test_attachment_tool_fails_closed_when_file_is_not_visible(self, monkeypatch) -> None:
        """FileStore 返回 None 时不得向模型暴露任何附件内容。"""
        # Arrange
        import src.knowledge.file_store as file_store_module
        from src.graph.nodes.mcp_agent import _build_attachment_tools

        store = SimpleNamespace(get=AsyncMock(return_value=None))
        monkeypatch.setattr(file_store_module, "get_file_store", lambda: store)
        tool = _build_attachment_tools({"file_ids": [7]})[0]

        # Act / Assert
        with pytest.raises(FileNotFoundError, match="不存在或无权访问"):
            await tool.ainvoke({"file_id": 7})
