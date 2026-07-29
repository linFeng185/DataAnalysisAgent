"""共享测试 fixture 契约测试，覆盖功能 16.2.1-16.2.4。"""

from __future__ import annotations

from langchain_core.messages import AIMessage


class TestSharedFixtures:
    """验证 Fake LLM、SQLite、MCP 与 Chroma fixture 的公共行为。"""

    # 方法作用：验证 Fake LLM 按预设顺序返回消息且支持异步调用。
    # Args: self - pytest 测试类实例；fake_llm_factory - Fake LLM 工厂 fixture。
    # Returns: 无返回值，断言失败时由 pytest 报告。
    async def test_fake_llm_returns_scripted_responses(self, fake_llm_factory) -> None:
        """所有 LLM Node 测试应复用可重复且不联网的模型替身。"""
        # Arrange
        model = fake_llm_factory([AIMessage(content="第一条"), "第二条"])

        # Act
        first = await model.ainvoke("query-1")
        second = await model.ainvoke("query-2")

        # Assert
        assert first.content == "第一条"
        assert second.content == "第二条"

    # 方法作用：验证 SQLite fixture 已创建固定订单样本并支持真实异步 SQL。
    # Args: self - pytest 测试类实例；sqlite_memory_db - SQLite 测试数据库 fixture。
    # Returns: 无返回值，断言失败时由 pytest 报告。
    async def test_sqlite_memory_db_is_seeded(self, sqlite_memory_db) -> None:
        """工作流集成测试必须使用真实 SQL 执行而非返回值 Mock。"""
        # Act
        rows = await sqlite_memory_db.fetch_all(
            "SELECT COUNT(*) AS total_orders FROM orders",
        )

        # Assert
        assert rows == [{"total_orders": 3}]
        assert sqlite_memory_db.datasource.dialect == "sqlite"
        assert sqlite_memory_db.datasource.schema.tables[0].name == "orders"

    # 方法作用：验证内存 MCP Server 固定工具的发现、调用和 ping 协议。
    # Args: self - pytest 测试类实例；static_mcp_server - MCP 测试服务 fixture。
    # Returns: 无返回值，断言失败时由 pytest 报告。
    async def test_static_mcp_server_calls_registered_tool(self, static_mcp_server) -> None:
        """MCP 测试不应依赖外部进程或网络端口。"""
        # Act
        tools = await static_mcp_server.list_tools()
        result = await static_mcp_server.call_tool("echo", {"text": "hello"})
        await static_mcp_server.send_ping()

        # Assert
        assert [tool.name for tool in tools.tools] == ["echo"]
        assert result.content[0].text == "hello"

    # 方法作用：验证 Chroma EphemeralClient fixture 可以写入和相似检索。
    # Args: self - pytest 测试类实例；chroma_store - 临时 Chroma VectorStore fixture。
    # Returns: 无返回值，断言失败时由 pytest 报告。
    async def test_ephemeral_chroma_store_round_trip(self, chroma_store) -> None:
        """临时 Collection 必须在单测内完成真实 metadata 过滤和向量检索。"""
        # Arrange
        from src.memory.vector_store import VectorEntry

        await chroma_store.upsert([VectorEntry(
            id="orders-doc",
            content="订单金额和订单数量",
            metadata={"tenant_id": 4, "category": "schema"},
            embedding=[1.0, 0.0, 0.0, 0.0],
        )])

        # Act
        results = await chroma_store.search(
            "订单",
            top_k=1,
            filters={"tenant_id": 4},
        )

        # Assert
        assert [item.id for item in results] == ["orders-doc"]
