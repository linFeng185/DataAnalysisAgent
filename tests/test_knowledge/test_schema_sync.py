"""25.6 表结构同步互斥、取消及缓存保留。"""

import asyncio
from contextlib import asynccontextmanager
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest
from fastapi import HTTPException

from src.datasource.schema_snapshot import SchemaSnapshot, TableSchema


class FakeSyncStore:
    """模拟共享数据库，多个服务实例共享互斥与状态。"""

    # 保存共享活动状态，模拟事务提交而非每 Worker 的计数。
    # Args: self - 实例。
    # Returns: 无返回值。
    def __init__(self) -> None:
        self.locked = False
        self.row = {"status": "idle"}
        self.published = SchemaSnapshot([TableSchema("old")])

    # 在拥有者生命周期内保持互斥，退出后才释放。
    # Args: self - 实例；tenant/name - 数据源作用域。
    # Returns: 模拟租约。
    @asynccontextmanager
    async def owner(self, tenant: int, name: str):
        if self.locked:
            raise HTTPException(409, "正在同步")
        self.locked = True
        try:
            yield AsyncMock()
        finally:
            self.locked = False

    # 发布启动状态。
    # Args: self - 实例；tenant/name/run_id - 任务键。
    # Returns: 无返回值。
    async def begin(self, tenant: int, name: str, run_id: str) -> None:
        self.row = {"status": "running", "phase": "collecting", "run_id": run_id}

    # 读取当前共享状态。
    # Args: self - 实例；tenant/name - 作用域。
    # Returns: 状态副本。
    async def state(self, tenant: int, name: str) -> dict:
        return dict(self.row)

    # 模拟工作进程心跳并读取取消信号。
    # Args: self - 实例；tenant/name/run_id - 任务键。
    # Returns: 当前状态。
    async def heartbeat(self, tenant: int, name: str, run_id: str) -> dict:
        return dict(self.row)

    # 原子进入不可中断的发布阶段。
    # Args: self - 实例；tenant/name/run_id - 任务键。
    # Returns: 是否允许发布。
    async def publishing(self, tenant: int, name: str, run_id: str) -> bool:
        if self.row["status"] != "running":
            return False
        self.row["phase"] = "publishing"
        return True

    # 原子提交新的规范快照和完成状态。
    # Args: self - 实例；tenant/name/run_id - 任务键；snapshot - 新快照；fingerprint - 配置签名。
    # Returns: 无返回值。
    async def publish(self, tenant: int, name: str, run_id: str, snapshot: SchemaSnapshot, fingerprint: str) -> None:
        self.published = snapshot

    # 记录终态，保留旧快照。
    # Args: self - 实例；tenant/name/run_id - 任务键；status/error - 结果。
    # Returns: 无返回值。
    async def finish(self, tenant: int, name: str, run_id: str, status: str, error: str = "") -> None:
        self.row.update(status=status, error=error)

    # 请求取消但不立即释放运行任务的锁。
    # Args: self - 实例；tenant/name - 作用域。
    # Returns: 当前状态。
    async def cancel(self, tenant: int, name: str) -> dict:
        if self.row.get("phase") == "publishing":
            raise HTTPException(409, "发布中")
        if self.row["status"] == "running":
            self.row["status"] = "cancelling"
        return dict(self.row)


class TestSchemaSync:
    """覆盖 25.6 多实例互斥、正常同步和取消保留。"""

    # 验证成功同步发布快照后释放租约。
    # Args: self - 实例。
    # Returns: 无返回值。
    async def test_success(self) -> None:
        """内省完成才更新缓存，不需要连接真实数据库。"""
        from src.knowledge.schema_sync import SchemaSyncService
        store = FakeSyncStore()
        snapshot = SchemaSnapshot([TableSchema("new")])
        manager = SimpleNamespace(inspect_live_schema=AsyncMock(return_value=snapshot), refresh_from_snapshot=AsyncMock())
        service = SchemaSyncService(store=store, manager=manager, fingerprint=AsyncMock(return_value="signature"))
        await service.start(2, "sales", wait=True)
        assert store.row["status"] == "succeeded"
        assert store.published is snapshot
        assert not store.locked
        await service.close()

    # 验证另一 Worker 的重复启动被拒绝，取消后旧快照仍可读取。
    # Args: self - 实例。
    # Returns: 无返回值。
    async def test_duplicate_and_cancel_keep_previous_snapshot(self) -> None:
        """取消必须等采集协程清理完成后才释放互斥。"""
        from src.knowledge.schema_sync import SchemaSyncService
        store = FakeSyncStore()
        entered, released = asyncio.Event(), asyncio.Event()
        # 保持内省挂起，记录取消真正到达驱动边界。
        # Args: args/kwargs - 数据源和租户。
        # Returns: 永不正常完成。
        async def inspect(*args, **kwargs):
            entered.set()
            try:
                await asyncio.Event().wait()
            finally:
                released.set()
        manager = SimpleNamespace(inspect_live_schema=inspect, refresh_from_snapshot=AsyncMock())
        first = SchemaSyncService(store=store, manager=manager, fingerprint=AsyncMock(return_value="signature"))
        second = SchemaSyncService(store=store, manager=manager, fingerprint=AsyncMock(return_value="signature"))
        await first.start(2, "sales")
        await entered.wait()
        with pytest.raises(HTTPException) as error:
            await second.start(2, "sales")
        assert error.value.status_code == 409
        await store.cancel(2, "sales")
        assert store.locked
        await asyncio.wait_for(released.wait(), 3)
        await first.close()
        assert store.row["status"] == "cancelled"
        assert store.published.tables[0].name == "old"
        manager.refresh_from_snapshot.assert_not_awaited()
        assert not store.locked


class TestPublishedSchema:
    """覆盖 25.6 规范快照与既有文档、缓存的交互边界。"""

    # 验证已发布结构仍合并当前请求的文档备注，不污染规范快照。
    # Args: self - 测试实例；monkeypatch - 依赖替换工具。
    # Returns: 无返回值。
    async def test_published_snapshot_keeps_document_overlay(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """规范结构命中时仍保留人工文档的高优先级字段备注。"""
        from src.app_context import AppContext, use_app_context
        from src.datasource.schema_snapshot import ColumnInfo
        from src.knowledge.models import KnowledgeEntry, KnowledgeSource
        from src.knowledge.schema_manager import SchemaManager
        from src.logging_config import get_logger

        # Arrange：结构快照和文档描述使用相同的租户字段标识。
        published = SchemaSnapshot([TableSchema("orders", columns=[ColumnInfo("amount", "DECIMAL", comment="原始金额")])])
        manager = SchemaManager(datasource_cache=SimpleNamespace())
        monkeypatch.setattr(manager, "_resolve_datasource", AsyncMock(return_value=SimpleNamespace()))
        docs = [KnowledgeEntry(
            id=manager._scoped_entry_id("column", "sales", "orders", "amount", tenant_id=2),
            content="orders.amount - 含税金额", source=KnowledgeSource.MANUAL_DOC,
            category="column", table_name="orders", column_name="amount",
            metadata={"type": "DECIMAL", "comment": "含税金额"},
        )]
        load_docs = Mock(return_value=docs)
        monkeypatch.setattr(manager, "_load_from_docs", load_docs)
        context = AppContext(SimpleNamespace(multi_tenant=True))
        context.set_resource("schema_sync", SimpleNamespace(apply_snapshot=AsyncMock(return_value=published)))
        logger = get_logger(__name__)
        logger.info("文档覆盖复现输入", document_count=len(docs), tenant_id=2)

        # Act：通过生产读取入口获取结构，不调用真实数据库。
        with use_app_context(context):
            result = await manager.get_or_fetch_schema("sales", tenant_id=2)
        logger.info("文档覆盖复现输出", document_loads=load_docs.call_count, comment_matches=result.tables[0].columns[0].comment == "含税金额")

        # Assert：当前请求带文档覆盖，其他请求共享的规范快照不变。
        assert result.tables[0].columns[0].comment == "含税金额"
        assert published.tables[0].columns[0].comment == "原始金额"
        load_docs.assert_called_once_with("sales")

    # 验证规范快照发布的缓存替换已清除旧条目，无需重复运行内省。
    # Args: self - 实例；monkeypatch - 数据库与缓存依赖替换工具。
    # Returns: 无返回值。
    async def test_published_snapshot_replaces_expired_cache(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """空的新结构同样删除旧自动缓存，避免已删除表重新出现在知识检索。"""
        from src.knowledge.schema_manager import SchemaManager

        # Arrange：模拟旧快照缓存，不连接实际向量数据库。
        manager = SchemaManager(datasource_cache=SimpleNamespace())
        monkeypatch.setattr(manager, "_resolve_datasource", AsyncMock(return_value=None))
        monkeypatch.setattr(manager, "_delete_shared_cache", AsyncMock())
        monkeypatch.setattr(manager, "_query_cache", AsyncMock(return_value=[SimpleNamespace(id="table:2:sales.old")]))
        monkeypatch.setattr(manager, "_upsert_to_cache", AsyncMock())
        monkeypatch.setattr(manager, "_write_shared_cache", AsyncMock())
        store = SimpleNamespace(delete_by_ids=AsyncMock(return_value=1))
        monkeypatch.setattr("src.memory.vector_store.get_vector_store", AsyncMock(return_value=store))

        # Act
        snapshot = await manager.refresh_from_snapshot("sales", SchemaSnapshot([]), tenant_id=2)

        # Assert：不会再次访问真实数据源，也不会把空快照回退为旧结构。
        assert snapshot.tables == []
        store.delete_by_ids.assert_awaited_once_with(["table:2:sales.old"])
        manager._delete_shared_cache.assert_awaited_once()
        manager._upsert_to_cache.assert_not_awaited()
