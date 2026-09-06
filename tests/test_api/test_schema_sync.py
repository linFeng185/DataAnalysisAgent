"""25.6 表结构同步的 HTTP 授权、互斥与兼容接口测试。"""

import asyncio
from collections.abc import AsyncIterator
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from src.datasource.schema_snapshot import SchemaSnapshot, TableSchema
from src.knowledge.schema_sync import SchemaSyncService
from tests.test_knowledge.test_schema_sync import FakeSyncStore


# 通过真实路由与任务服务隔离数据库和当前身份，不连接业务数据源。
# Args: monkeypatch - 当前测试依赖替换工具。
# Returns: HTTP 客户端、同步服务和可控的驱动边界。
@pytest.fixture
async def sync_api(monkeypatch: pytest.MonkeyPatch) -> AsyncIterator[SimpleNamespace]:
    import src.api.auth as auth
    import src.api.routes as routes
    import src.api.routes.schema as schema_route
    import src.knowledge.schema_sync as sync_module

    collecting, cleanup, finish = asyncio.Event(), asyncio.Event(), asyncio.Event()

    # 模拟取消后仍需清理的驱动，确保终止信号不能提前释放互斥。
    # Args: name - 数据源名；tenant_id - 当前租户。
    # Returns: 清理结束后的完整快照。
    async def inspect(name: str, *, tenant_id: int) -> SchemaSnapshot:
        collecting.set()
        try:
            await finish.wait()
        finally:
            cleanup.set()
            await finish.wait()
        return SchemaSnapshot([TableSchema("orders")])

    store = FakeSyncStore()
    manager = SimpleNamespace(inspect_live_schema=inspect, refresh_from_snapshot=AsyncMock())
    service = SchemaSyncService(store=store, manager=manager, fingerprint=AsyncMock(return_value="signature"))
    monkeypatch.setattr(sync_module, "get_schema_sync", lambda: service)
    monkeypatch.setattr(auth, "get_current_tenant_id", lambda: 2)
    monkeypatch.setattr(auth, "get_current_user_id", lambda: 7)
    monkeypatch.setattr(auth, "get_current_role", lambda: "tenant_admin")
    monkeypatch.setattr(schema_route, "_resolve_datasource_name", AsyncMock(return_value="sales"))
    monkeypatch.setattr(routes, "_registry", lambda: SimpleNamespace(resolve=AsyncMock()))
    app = FastAPI()
    app.include_router(schema_route.router)
    async with AsyncClient(transport=ASGITransport(app), base_url="http://test") as client:
        try:
            yield SimpleNamespace(client=client, service=service, store=store, collecting=collecting, cleanup=cleanup, finish=finish, manager=manager)
        finally:
            finish.set()
            await service.close()


class TestSchemaSyncAPI:
    """覆盖 25.6：新旧入口共享互斥，取消清理完成前不能再次同步。"""

    # 验证启动和取消都执行写权限校验，不能依赖前端隐藏按钮。
    # Args: self - 实例；sync_api - 路由与服务；monkeypatch - 身份替换；path - 写接口。
    # Returns: 无返回值。
    @pytest.mark.parametrize("path", ["/schema/sync", "/schema/sync/cancel", "/schema/refresh"])
    async def test_write_requires_tenant_admin(self, sync_api: SimpleNamespace, monkeypatch: pytest.MonkeyPatch, path: str) -> None:
        """普通租户成员不能启动或取消同步。"""
        import src.api.auth as auth

        # Arrange
        monkeypatch.setattr(auth, "get_current_role", lambda: "analyst")
        # Act
        response = await sync_api.client.post(path, params={"datasource": "sales"})
        # Assert
        assert response.status_code == 403
        assert not sync_api.service.tasks

    # 验证同步中和取消清理中的重复请求都返回冲突。
    # Args: self - 实例；sync_api - 路由与服务。
    # Returns: 无返回值。
    async def test_duplicate_and_legacy_entry_share_cancellation_lock(self, sync_api: SimpleNamespace) -> None:
        """清理退出前新旧入口均不得启动第二个任务。"""
        # Arrange
        client = sync_api.client
        params = {"datasource": "sales"}
        # Act
        started = await client.post("/schema/sync", params=params)
        await asyncio.wait_for(sync_api.collecting.wait(), 2)
        duplicate = await client.post("/schema/sync", params=params)
        legacy = await client.post("/schema/refresh", params=params)
        cancelled = await client.post("/schema/sync/cancel", params=params)
        await asyncio.wait_for(sync_api.cleanup.wait(), 2)
        during_cleanup = await client.post("/schema/sync", params=params)
        status = await client.get("/schema/sync", params=params)
        # Assert
        assert started.status_code == 202
        assert duplicate.status_code == legacy.status_code == during_cleanup.status_code == 409
        assert cancelled.json()["status"] == status.json()["status"] == "cancelling"
        assert sync_api.store.locked
        assert sync_api.store.published.tables[0].name == "old"
        sync_api.manager.refresh_from_snapshot.assert_not_awaited()

    # 验证发布阶段拒绝取消且最终状态由真实发布结果决定。
    # Args: self - 实例；sync_api - 路由与服务。
    # Returns: 无返回值。
    async def test_publishing_cannot_be_cancelled(self, sync_api: SimpleNamespace) -> None:
        """发布中返回 409，清理结束后旧接口仍返回兼容的成功格式。"""
        # Arrange
        publishing, release = asyncio.Event(), asyncio.Event()

        # 挂起实际索引发布边界，使 HTTP 取消发生在发布阶段。
        # Args: args/kwargs - 数据源、快照与租户。
        # Returns: 无返回值。
        async def refresh(*args: object, **kwargs: object) -> None:
            publishing.set()
            await release.wait()

        sync_api.manager.refresh_from_snapshot = refresh
        sync_api.finish.set()
        # Act
        await sync_api.client.post("/schema/sync", params={"datasource": "sales"})
        await asyncio.wait_for(publishing.wait(), 2)
        try:
            response = await sync_api.client.post("/schema/sync/cancel", params={"datasource": "sales"})
            assert response.status_code == 409
        finally:
            release.set()
            await asyncio.gather(*sync_api.service.tasks)
        legacy = await sync_api.client.post("/schema/refresh", params={"datasource": "sales"})
        # Assert
        assert legacy.status_code == 200
        assert legacy.json()["status"] == "ok"
        assert not sync_api.store.locked

    # 验证缺失数据源参数不能创建或操作任务。
    # Args: self - 实例；sync_api - 路由与服务；path - 新接口。
    # Returns: 无返回值。
    @pytest.mark.parametrize("path", ["/schema/sync", "/schema/sync/cancel"])
    async def test_empty_datasource_is_rejected(self, sync_api: SimpleNamespace, path: str) -> None:
        """空数据源应由请求验证返回 422。"""
        # Arrange / Act
        response = await sync_api.client.post(path, params={"datasource": ""})
        # Assert
        assert response.status_code == 422
        assert not sync_api.service.tasks
