"""PgVectorStore Fake PostgreSQL 合约测试。"""

from __future__ import annotations

import json
import logging
import sys
import threading
from types import SimpleNamespace
from unittest.mock import AsyncMock

logger = logging.getLogger(__name__)


class FakeAcquire:
    """返回固定连接的 asyncpg acquire 上下文。"""

    # 方法作用：保存进入上下文时返回的连接。
    # Args: self - 当前上下文；connection - Fake 连接。
    # Returns: 无返回值。
    def __init__(self, connection) -> None:
        logger.debug("FakeAcquire.__init__ 入口")
        self.connection = connection
        logger.info("FakeAcquire.__init__ 完成")

    # 方法作用：进入异步连接上下文。
    # Args: self - 当前上下文。
    # Returns: Fake 连接。
    async def __aenter__(self):
        logger.debug("FakeAcquire.__aenter__ 入口")
        logger.info("FakeAcquire.__aenter__ 完成")
        return self.connection

    # 方法作用：退出异步连接上下文。
    # Args: self - 当前上下文；exc_type/exc/tb - 异常信息。
    # Returns: False，不吞掉异常。
    async def __aexit__(self, exc_type, exc, tb) -> bool:
        logger.debug("FakeAcquire.__aexit__ 入口")
        logger.info("FakeAcquire.__aexit__ 完成")
        return False


class FakePool:
    """提供固定连接的 Fake asyncpg Pool。"""

    # 方法作用：保存固定连接。
    # Args: self - 当前 Pool；connection - Fake 连接。
    # Returns: 无返回值。
    def __init__(self, connection) -> None:
        logger.debug("FakePool.__init__ 入口")
        self.connection = connection
        logger.info("FakePool.__init__ 完成")

    # 方法作用：创建 Fake acquire 上下文。
    # Args: self - 当前 Pool。
    # Returns: FakeAcquire。
    def acquire(self) -> FakeAcquire:
        logger.debug("FakePool.acquire 入口")
        result = FakeAcquire(self.connection)
        logger.info("FakePool.acquire 完成")
        return result


class TestPgVectorStore:
    """覆盖 pgvector 初始化、查询、写入、删除、计数和健康检查。"""

    # 方法作用：构造注入 Fake Pool 和固定嵌入的 Store。
    # Args: self - pytest 测试类实例；monkeypatch - pytest 补丁工具；connection - Fake 连接。
    # Returns: PgVectorStore 实例。
    def _store(self, monkeypatch, connection):
        logger.debug("TestPgVectorStore._store 入口")
        from src.memory.vector_store_pg import PgVectorStore

        store = PgVectorStore("postgresql://test")
        monkeypatch.setattr(store, "_get_pool", AsyncMock(return_value=FakePool(connection)))
        monkeypatch.setattr(store, "_embed", AsyncMock(return_value=[0.1, 0.2]))
        logger.info("TestPgVectorStore._store 完成")
        return store

    # 方法作用：验证 create 执行建表建索引并预热嵌入函数。
    # Args: self - pytest 测试类实例；monkeypatch - pytest 补丁工具。
    # Returns: 无返回值，断言失败时由 pytest 报告。
    async def test_create_initializes_schema(self, monkeypatch) -> None:
        """初始化必须复用应用 Pool，且返回标准化 URL 的 Store。"""
        logger.debug("test_create_initializes_schema 入口")
        import src.memory.vector_store_pg as module

        connection = AsyncMock()
        monkeypatch.setattr(module, "get_pg_pool", AsyncMock(return_value=FakePool(connection)))
        embed = AsyncMock(return_value=lambda text: text)
        monkeypatch.setattr(module.PgVectorStore, "_get_embed_fn", embed)

        store = await module.PgVectorStore.create("postgresql+asyncpg://u:p@host/db")

        assert store._pg_url == "postgresql://u:p@host/db"  # noqa: SLF001
        assert connection.execute.await_count == 3
        embed.assert_awaited_once()
        logger.info("test_create_initializes_schema 完成")

    # 方法作用：验证 pgvector Schema 初始化失败时阻断半初始化 Store 返回。
    # Args: self - pytest 测试类实例；monkeypatch - pytest 补丁工具。
    # Returns: 无返回值，断言失败时由 pytest 报告。
    async def test_create_fails_closed_when_schema_initialization_fails(
        self,
        monkeypatch,
    ) -> None:
        """数据库扩展或建表失败必须抛出，不能延迟到首次查询才暴露。"""
        logger.debug("test_create_fails_closed_when_schema_initialization_fails 入口")
        import pytest

        import src.memory.vector_store_pg as module

        connection = AsyncMock()
        connection.execute.side_effect = RuntimeError("pgvector extension missing")
        monkeypatch.setattr(module, "get_pg_pool", AsyncMock(return_value=FakePool(connection)))
        embed = AsyncMock()
        monkeypatch.setattr(module.PgVectorStore, "_get_embed_fn", embed)

        with pytest.raises(RuntimeError, match="pgvector extension missing"):
            await module.PgVectorStore.create("postgresql://u:p@host/db")

        embed.assert_not_awaited()
        logger.info("test_create_fails_closed_when_schema_initialization_fails 完成")

    # 方法作用：验证搜索、按 ID 与按 metadata 查询的结构转换。
    # Args: self - pytest 测试类实例；monkeypatch - pytest 补丁工具。
    # Returns: 无返回值，断言失败时由 pytest 报告。
    async def test_search_and_reads(self, monkeypatch) -> None:
        """JSON metadata 和相似度必须转换为公共 VectorStore 模型。"""
        logger.debug("test_search_and_reads 入口")
        connection = AsyncMock()
        connection.fetch.return_value = [{
            "id": "v1",
            "content": "content",
            "metadata": json.dumps({"tenant_id": "4"}),
            "score": 0.87654,
        }]
        connection.fetchrow.return_value = {
            "id": "v1",
            "content": "content",
            "metadata": {"tenant_id": "4"},
        }
        store = self._store(monkeypatch, connection)

        results = await store.search("query", top_k=100, filters={"tenant_id": "4"})
        entry = await store.get_by_id("v1")
        entries = await store.get_by_filter({"tenant_id": "4"}, limit=10)

        assert results[0].score == 0.8765
        assert results[0].metadata == {"tenant_id": "4"}
        assert entry.id == "v1"
        assert entries[0].id == "v1"
        assert connection.fetch.call_args.args[-1] == 10
        logger.info("test_search_and_reads 完成")

    # 方法作用：验证批量写入、空输入和删除计数。
    # Args: self - pytest 测试类实例；monkeypatch - pytest 补丁工具。
    # Returns: 无返回值，断言失败时由 pytest 报告。
    async def test_write_and_delete_boundaries(self, monkeypatch) -> None:
        """空列表不得访问数据库，状态字符串末尾计数必须正确解析。"""
        logger.debug("test_write_and_delete_boundaries 入口")
        from src.memory.vector_store import VectorEntry

        connection = AsyncMock()
        connection.execute.return_value = "DELETE 2"
        store = self._store(monkeypatch, connection)

        assert await store.upsert([]) == 0
        assert await store.delete_by_ids([]) == 0
        assert await store.upsert([VectorEntry(id="v1", content="text")]) == 1
        assert await store.delete_by_ids(["v1", "v2"]) == 2
        assert await store.delete_by_filter({"tenant_id": "4"}) == 2
        logger.info("test_write_and_delete_boundaries 完成")

    # 方法作用：验证计数和健康检查正常/异常路径。
    # Args: self - pytest 测试类实例；monkeypatch - pytest 补丁工具。
    # Returns: 无返回值，断言失败时由 pytest 报告。
    async def test_count_and_health_check(self, monkeypatch) -> None:
        """连接可用返回 True，探针异常回退 False。"""
        logger.debug("test_count_and_health_check 入口")
        connection = AsyncMock()
        connection.fetchrow.return_value = (3,)
        connection.fetchval.return_value = 1
        store = self._store(monkeypatch, connection)

        assert await store.count() == 3
        assert await store.count({"tenant_id": "4"}) == 3
        assert await store.health_check() is True

        connection.fetchval.side_effect = RuntimeError("pg unavailable")
        assert await store.health_check() is False
        logger.info("test_count_and_health_check 完成")

    # 方法作用：验证 pgvector 把统一过滤 DSL 编译为参数化 JSONB 谓词。
    # Args: self - pytest 测试类实例。
    # Returns: 无返回值，断言失败时由 pytest 报告。
    def test_filter_dsl_compiles_equal_and_not_equal_predicates(self) -> None:
        """等值、not: 前缀和 $ne 运算符必须同时生效且不得拼接用户值。"""
        logger.debug("test_filter_dsl_compiles_equal_and_not_equal_predicates 入口")
        from src.memory.vector_store_pg import _build_filter_clause

        clause, values = _build_filter_clause({
            "tenant_id": 4,
            "not:status": "deleted",
            "source": {"$ne": "legacy"},
        }, parameter_start=2)

        assert clause == (
            "metadata @> $2::jsonb AND NOT (metadata @> $3::jsonb) "
            "AND NOT (metadata @> $4::jsonb)"
        )
        assert [json.loads(value) for value in values] == [
            {"tenant_id": 4}, {"status": "deleted"}, {"source": "legacy"},
        ]
        logger.info("test_filter_dsl_compiles_equal_and_not_equal_predicates 完成")

    # 方法作用：验证空过滤条件不会触发 pgvector 全表删除。
    # Args: self - pytest 测试类实例；monkeypatch - pytest 补丁工具。
    # Returns: 无返回值，断言失败时由 pytest 报告。
    async def test_delete_by_empty_filter_is_noop(self, monkeypatch) -> None:
        """空过滤是调用方缺少约束，不得被解释为删除全部记录。"""
        logger.debug("test_delete_by_empty_filter_is_noop 入口")
        connection = AsyncMock()
        store = self._store(monkeypatch, connection)

        result = await store.delete_by_filter({})

        assert result == 0
        connection.execute.assert_not_awaited()
        logger.info("test_delete_by_empty_filter_is_noop 完成")

    # 方法作用：验证 SentenceTransformer 加载和 encode 都在线程池执行。
    # Args: self - pytest 测试类实例；monkeypatch - pytest 补丁工具。
    # Returns: 无返回值，断言失败时由 pytest 报告。
    async def test_embedding_work_is_offloaded_from_event_loop(self, monkeypatch) -> None:
        """模型初始化和 CPU 推理不得阻塞 asyncio 事件循环。"""
        logger.debug("test_embedding_work_is_offloaded_from_event_loop 入口")
        import src.memory.vector_store_pg as module

        # Arrange
        thread_ids: list[int] = []

        class EncodedVector:
            """提供 SentenceTransformer encode 结果的最小 tolist 契约。"""

            # 方法作用：返回固定向量。
            # Args: self - 当前模拟结果。
            # Returns: 两维浮点列表。
            def tolist(self) -> list[float]:
                logger.debug("EncodedVector.tolist 入口")
                result = [0.1, 0.2]
                logger.info("EncodedVector.tolist 完成")
                return result

        class FakeSentenceTransformer:
            """记录初始化和推理线程的模拟嵌入模型。"""

            # 方法作用：记录模型初始化线程。
            # Args: self - 模拟模型；path - 模型路径。
            # Returns: 无返回值。
            def __init__(self, path: str) -> None:
                logger.debug("FakeSentenceTransformer.__init__ 入口", extra={"path": path})
                thread_ids.append(threading.get_ident())
                logger.info("FakeSentenceTransformer.__init__ 完成")

            # 方法作用：记录文本编码线程并返回固定向量。
            # Args: self - 模拟模型；text - 待编码文本。
            # Returns: 具有 tolist 方法的固定向量。
            def encode(self, text: str) -> EncodedVector:
                logger.debug("FakeSentenceTransformer.encode 入口", extra={"text": text})
                thread_ids.append(threading.get_ident())
                logger.info("FakeSentenceTransformer.encode 完成")
                return EncodedVector()

        monkeypatch.setitem(
            sys.modules,
            "sentence_transformers",
            SimpleNamespace(SentenceTransformer=FakeSentenceTransformer),
        )
        monkeypatch.setattr(
            module,
            "get_settings",
            lambda: SimpleNamespace(embedding_model_path="fake-model"),
        )
        event_loop_thread = threading.get_ident()
        store = module.PgVectorStore("postgresql://test")

        # Act
        vector = await store._embed("hello")  # noqa: SLF001

        # Assert
        assert vector == [0.1, 0.2]
        assert len(thread_ids) == 2
        assert all(thread_id != event_loop_thread for thread_id in thread_ids)
        logger.info("test_embedding_work_is_offloaded_from_event_loop 完成")
