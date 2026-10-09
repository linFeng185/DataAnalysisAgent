"""功能27.1.6独立审核：外部MCP故障不能阻断本地知识工具。"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

from langchain_core.messages import AIMessage
from langchain_core.tools import StructuredTool


class TestKnowledgeMcpReview:
    """覆盖本地只读工具独立于外部MCP可用性的边界。"""

    # 验证外部服务不可用但本地知识工具存在时仍进入Agent。
    # Args: monkeypatch - 测试替换工具。
    # Returns: 无，断言本地工具路径能正常完成。
    async def test_external_mcp_failure_preserves_local_knowledge_agent(self, monkeypatch) -> None:
        """加载顺序不能在知识工具构建之前就因外部故障退出。"""
        import langgraph.prebuilt

        import src.graph.nodes.mcp_agent as node
        import src.llm.client as client
        import src.llm.knowledge_loop as loop
        import src.mcp_client.client_manager as manager_module

        # Arrange：只保留一个本地只读工具，外部服务初始化明确失败。
        manager = SimpleNamespace(
            ensure_scoped_servers=AsyncMock(side_effect=RuntimeError("synthetic MCP offline")),
            get_all_tools=MagicMock(return_value=[]),
        )

        # 提供不访问向量库的本地工具，当前用例只测试加载与降级分支。
        # Args: query - 查询文本。
        # Returns: 空知识结果。
        async def local_search(query: str) -> dict:
            return {"status": "empty", "evidence": [], "citations": []}

        tool = StructuredTool.from_function(coroutine=local_search, name="search_knowledge", description="本地只读检索")
        agent = SimpleNamespace(ainvoke=AsyncMock(return_value={
            "messages": [AIMessage(content='{"answer":"本地知识路径可用"}')],
        }))
        factory = MagicMock(return_value=agent)
        monkeypatch.setattr(manager_module, "get_mcp_client_manager", lambda: manager)
        monkeypatch.setattr(loop, "build_knowledge_tools_for_call", lambda *args, **kwargs: [tool])
        monkeypatch.setattr(client, "is_task_llm_available", lambda task: True)
        monkeypatch.setattr(client, "get_task_llm", lambda *args, **kwargs: object())
        monkeypatch.setattr(langgraph.prebuilt, "create_react_agent", factory)

        # Act：本地工具路径不依赖外部MCP加载成功。
        result = await node.mcp_agent_node({"tenant_id": 2, "user_id": 7, "user_query": "查询字段口径"})

        # Assert：真正执行Agent，而非直接返回失败契约。
        assert result["final_response"]["success"] is True
        agent.ainvoke.assert_awaited_once()
        assert any(item.name == "search_knowledge" for item in factory.call_args.args[1])
