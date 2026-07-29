"""全测试套件共享的确定性基础设施 fixture。"""

from __future__ import annotations

from collections.abc import AsyncIterator, Callable, Sequence
from uuid import uuid4

import pytest
from langchain_core.language_models.fake_chat_models import FakeListChatModel
from langchain_core.messages import AIMessage

from tests.fixtures.mock_db import SQLiteMemoryDB
from tests.fixtures.mock_llm import make_fake_llm
from tests.fixtures.mock_mcp import StaticMCPTestServer


# 方法作用：提供创建顺序响应 Fake LLM 的工厂。
# Args: 无。
# Returns: 接收响应序列并返回 FakeListChatModel 的函数。
@pytest.fixture
def fake_llm_factory() -> Callable[[Sequence[str | AIMessage]], FakeListChatModel]:
    return make_fake_llm


# 方法作用：创建并在用例结束后释放已 seed 的 SQLite 内存数据库。
# Args: 无。
# Returns: 测试期间可用的 SQLiteMemoryDB。
@pytest.fixture
async def sqlite_memory_db() -> AsyncIterator[SQLiteMemoryDB]:
    database = await SQLiteMemoryDB.create()
    try:
        yield database
    finally:
        await database.close()


# 方法作用：提供不依赖网络和子进程的静态 MCP Server。
# Args: 无。
# Returns: 已注册 echo 工具的 Server。
@pytest.fixture
def static_mcp_server() -> StaticMCPTestServer:
    return StaticMCPTestServer()


# 方法作用：创建测试隔离的 Chroma EphemeralClient 和 VectorStore。
# Args: 无。
# Returns: 用例结束后自动关闭并删除 Collection 的 ChromaVectorStore。
@pytest.fixture
async def chroma_store() -> AsyncIterator[object]:
    import chromadb

    from src.memory.vector_store_chroma import ChromaVectorStore

    client = chromadb.EphemeralClient()
    collection_name = f"test_{uuid4().hex}"
    collection = client.create_collection(
        collection_name,
        embedding_function=None,
        metadata={"hnsw:space": "cosine"},
    )

    # 方法作用：为测试查询返回稳定 4 维向量。
    # Args: text - 查询文本。
    # Returns: 固定 4 维向量。
    def embed(text: str) -> list[float]:
        del text
        return [1.0, 0.0, 0.0, 0.0]

    store = ChromaVectorStore(collection, embedding_fn=embed, client=client)
    try:
        yield store
    finally:
        await store.close()
        client.delete_collection(collection_name)
