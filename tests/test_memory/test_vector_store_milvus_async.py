"""19.17 / 25.6：Milvus 同步 SDK 隔离、有限超时与请求并发回归。"""

import asyncio
import threading
from functools import partial
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import httpx
import pytest
from fastapi import FastAPI

from src.memory.vector_store import VectorEntry
from src.memory.vector_store_milvus import MilvusVectorStore


class TestMilvusAsyncBoundary:
    """覆盖 19.17 所有远程操作不占用事件循环，RPC 显式指定有限超时。"""

    # 验证首次模型导入也离开事件循环，多个请求只初始化一次。
    # Args: self - 实例；monkeypatch - 补丁工具。
    # Returns: 无返回值。
    async def test_embedding_import_and_initialization_are_off_loop(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """防止只将构造器 to_thread，但先在主线程执行昂贵的 transformers 导入。"""
        import builtins

        # Arrange
        main_thread = threading.get_ident()
        original_import = builtins.__import__
        model = SimpleNamespace(encode=MagicMock())
        constructor = MagicMock(return_value=model)
        modules = {
            "sentence_transformers": SimpleNamespace(SentenceTransformer=constructor),
            "transformers.utils": SimpleNamespace(logging=SimpleNamespace(disable_progress_bar=MagicMock())),
        }

        # 在真实 import 边界检查线程，而非仅验证构造器调用。
        # Args: name - 导入名；args/kwargs - import 参数。
        # Returns: 模型替身或原始导入结果。
        def import_probe(name: str, *args: Any, **kwargs: Any) -> Any:
            if name in modules:
                assert threading.get_ident() != main_thread, name
                return modules[name]
            return original_import(name, *args, **kwargs)

        monkeypatch.setattr(builtins, "__import__", import_probe)
        store = MilvusVectorStore()
        # Act
        results = await asyncio.gather(store._get_embed_fn(), store._get_embed_fn())
        # Assert
        assert results == [model.encode, model.encode]
        constructor.assert_called_once()

    # 验证每个公开操作的 SDK 调用在线程中并有有限超时。
    # Args: self - 实例；monkeypatch - 补丁工具；operation - 被测操作。
    # Returns: 无返回值。
    @pytest.mark.parametrize("operation", ["create", "search", "get_by_id", "get_by_filter", "upsert", "delete_by_ids", "delete_by_filter", "count", "health_check"])
    async def test_sdk_calls_are_off_loop_and_bounded(self, monkeypatch: pytest.MonkeyPatch, operation: str) -> None:
        """嵌入使用替身，所有 SDK 边界均验证线程和 deadline，不接真实 Milvus。"""
        pymilvus = pytest.importorskip("pymilvus")

        # Arrange
        main_thread = threading.get_ident()
        called = []
        collection = MagicMock()
        iterator = SimpleNamespace(next=MagicMock(return_value=[]), close=MagicMock())

        # 记录 SDK 边界，避免只验证 asyncio 包装名称而遗漏实际阻塞调用。
        # Args: name - 操作名；args/kwargs - SDK 参数。
        # Returns: 确定性 SDK 响应。
        def rpc(rpc_name: str, *args: Any, **kwargs: Any) -> Any:
            called.append(rpc_name)
            assert threading.get_ident() != main_thread, rpc_name
            if rpc_name != "has_connection":
                assert 0 < kwargs.get("timeout", 0) <= 30, (rpc_name, kwargs)
            if rpc_name == "Collection":
                return collection
            if rpc_name == "query_iterator":
                return iterator
            if rpc_name in {"has_connection", "has_collection"}:
                return True
            if rpc_name == "search":
                return [[]]
            return [{"count(*)": 0}] if operation == "count" and rpc_name == "query" else []

        monkeypatch.setattr(pymilvus, "Collection", partial(rpc, "Collection"))
        for name in ("load", "query", "search", "query_iterator", "insert", "delete", "flush"):
            getattr(collection, name).side_effect = partial(rpc, name)
        monkeypatch.setattr(pymilvus.connections, "connect", partial(rpc, "connect"))
        monkeypatch.setattr(pymilvus.connections, "has_connection", partial(rpc, "has_connection"))
        monkeypatch.setattr(pymilvus.utility, "has_collection", partial(rpc, "has_collection"))
        monkeypatch.setattr(MilvusVectorStore, "_get_embed_fn", AsyncMock())
        store = MilvusVectorStore()
        monkeypatch.setattr(store, "_embed", AsyncMock(return_value=[0.1]))
        arguments = {
            "search": ("query",), "get_by_id": ("entry",), "get_by_filter": ({"tenant_id": 1},),
            "upsert": ([VectorEntry("entry", "body", embedding=[0.1])],),
            "delete_by_ids": (["entry"],), "delete_by_filter": ({"tenant_id": 1},),
            "count": (), "health_check": (),
        }
        # Act
        if operation == "create":
            result = await MilvusVectorStore.create("http://milvus.invalid:19530")
        else:
            result = await getattr(store, operation)(*arguments[operation])
        # Assert
        assert called
        if operation == "health_check":
            assert result is True

    # 验证慢加载期间真实 ASGI 请求仍可被事件循环处理。
    # Args: self - 实例；monkeypatch - 补丁工具。
    # Returns: 无返回值。
    async def test_slow_collection_load_keeps_http_responsive(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """复现发布索引时 SDK 等待；健康和身份请求不能排队等待它结束。"""
        # Arrange
        entered = threading.Event()
        released = threading.Event()
        store = MilvusVectorStore()
        app = FastAPI()

        # 用有限等待模拟挂起的 Milvus，不让错误实现永久卡死测试进程。
        # Args: 无。
        # Returns: 总是抛出连接超时。
        def slow_load() -> Any:
            entered.set()
            released.wait(1)
            raise TimeoutError("collection load timeout")

        # 返回独立接口的响应，验证请求处理没有被同步 SDK 阻塞。
        # Args: 无。
        # Returns: 服务状态。
        @app.get("/health")
        @app.get("/auth/me")
        async def probe() -> dict[str, bool]:
            return {"ok": True}

        monkeypatch.setattr(store, "_get_collection", slow_load)
        task = asyncio.create_task(store.get_by_filter({"tenant_id": 1}))
        try:
            assert await asyncio.to_thread(entered.wait, 2)
            # Act
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
                responses = await asyncio.gather(client.get("/health"), client.get("/auth/me"))
            # Assert
            assert all(response.status_code == 200 for response in responses)
            assert not task.done(), "SDK 等待期间其他请求应先完成"
        finally:
            released.set()
            results = await asyncio.gather(task, return_exceptions=True)
        assert isinstance(results[0], TimeoutError)

    # 验证取消写任务后，写锁直到后台同步操作真正退出才释放。
    # Args: self - 实例；monkeypatch - 补丁工具。
    # Returns: 无返回值。
    async def test_cancelled_write_keeps_lock_until_rpc_exits(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """防止 to_thread 取消只影响等待者，导致第二个写操作越过仍运行的第一个。"""
        # Arrange
        entered = threading.Event()
        released = threading.Event()
        collection = MagicMock()
        collection.query.return_value = []
        store = MilvusVectorStore()

        # 用有限等待模拟尚未提交完的同步写入。
        # Args: args/kwargs - 写入 SDK 参数。
        # Returns: 无返回值。
        def insert(*args: Any, **kwargs: Any) -> None:
            entered.set()
            released.wait(1)

        collection.insert.side_effect = insert
        monkeypatch.setattr(store, "_get_collection", lambda: collection)
        first = asyncio.create_task(store.upsert([VectorEntry("first", "body", embedding=[0.1])]))
        second = None
        try:
            assert await asyncio.to_thread(entered.wait, 2)
            # Act
            first.cancel()
            second = asyncio.create_task(store.delete_by_ids(["second"]))
            await asyncio.sleep(0.02)
            # Assert
            assert store._write_lock.locked()
            assert not first.done()
            collection.delete.assert_not_called()
        finally:
            released.set()
            results = await asyncio.gather(first, *([second] if second else []), return_exceptions=True)
        assert isinstance(results[0], asyncio.CancelledError)
        assert results[1] == 1
        assert not store._write_lock.locked()

    # 验证派生向量索引超时不让已经提交的表结构任务永久停在发布中。
    # Args: self - 实例；monkeypatch - 补丁工具。
    # Returns: 无返回值。
    async def test_sync_publish_finishes_when_index_times_out(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """规范快照成功后，索引失败是可降级边界，清理完毕再写入同步终态。"""
        from src.datasource.schema_snapshot import SchemaSnapshot, TableSchema
        from src.knowledge import schema_sync
        from tests.test_knowledge.test_schema_sync import FakeSyncStore

        # Arrange
        cleaned = asyncio.Event()

        # 模拟派生索引长期等待，并证明超时后收尾确实执行。
        # Args: args/kwargs - 索引刷新参数。
        # Returns: 无返回值。
        async def refresh(*args: Any, **kwargs: Any) -> None:
            try:
                await asyncio.Event().wait()
            finally:
                cleaned.set()

        monkeypatch.setattr(schema_sync, "_INDEX_REFRESH_TIMEOUT_SECONDS", 0.02, raising=False)
        store = FakeSyncStore()
        service = schema_sync.SchemaSyncService(store=store, manager=SimpleNamespace(refresh_from_snapshot=refresh))
        snapshot = SchemaSnapshot([TableSchema("new")])
        # Act
        await asyncio.wait_for(service._publish(2, "sales", "run", snapshot, "fingerprint"), timeout=0.5)
        # Assert
        assert cleaned.is_set()
        assert store.published is snapshot
        assert store.row["status"] == "succeeded"
