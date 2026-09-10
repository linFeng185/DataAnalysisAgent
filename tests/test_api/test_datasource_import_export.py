"""数据源导入导出、同步状态和应用内配置来源测试。"""

import io
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from fastapi import UploadFile


class TestDatasourceImportExport:
    """覆盖数据源配置导入导出且不泄露密码。"""

    # 方法作用：通过真实 HTTP 路由验证上传与下载的契约。
    # Args: self - 实例；monkeypatch - 依赖替换。
    # Returns: 无返回值。
    async def test_http_upload_download_and_tenant_isolation(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """只导出当前租户，multipart 导入会调用应用 Provider。"""
        from fastapi import FastAPI
        from httpx import ASGITransport, AsyncClient

        from src.api.routes.datasource import router

        # Arrange
        items, persisted = self._setup(monkeypatch)
        items.append({**items[0], "name": "other-tenant", "tenant_id": 3})
        app = FastAPI()
        app.include_router(router)
        # Act
        async with AsyncClient(transport=ASGITransport(app), base_url="http://test") as client:
            exported = await client.get("/datasources/export")
            uploaded = await client.post("/datasources/import", files={"file": ("source.json", json.dumps({"version": 1, "datasources": [{"name": "new", "dialect": "sqlite"}]}), "application/json")})
        # Assert
        assert exported.status_code == 200
        assert [item["name"] for item in exported.json()["datasources"]] == ["sales"]
        assert uploaded.status_code == 201
        assert persisted[0].tenant_id == 2 and persisted[0].owner_user_id == 7

    # 方法作用：验证导入导出边界均校验租户管理权限。
    # Args: self - 实例；monkeypatch - 依赖替换；operation - 导入或导出。
    # Returns: 无返回值。
    @pytest.mark.parametrize("operation", ["import", "export"])
    async def test_transfer_requires_management_permission(self, monkeypatch: pytest.MonkeyPatch, operation: str) -> None:
        """普通用户不能导出连接配置或创建数据源。"""
        from fastapi import HTTPException

        import src.api.auth as auth
        from src.api.routes.datasource import export_datasources, import_datasources

        # Arrange
        gate = auth.require_tenant_admin
        _, persisted = self._setup(monkeypatch)
        monkeypatch.setattr(auth, "require_tenant_admin", gate)
        monkeypatch.setattr(auth, "get_current_role", lambda: "analyst")
        # Act
        with pytest.raises(HTTPException) as caught:
            if operation == "export":
                await export_datasources()
            else:
                await import_datasources(UploadFile(filename="source.json", file=io.BytesIO(b"{}")))
        # Assert
        assert caught.value.status_code == 403
        assert not persisted

    # 方法作用：确认只读浏览表结构不创建连接或启动内省。
    # Args: self - 实例；monkeypatch - 依赖替换。
    # Returns: 无返回值。
    async def test_schema_browse_uses_published_snapshot_only(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """数据源未连接也能查看已发布表结构，浏览不触发同步。"""
        from fastapi import FastAPI
        from httpx import ASGITransport, AsyncClient

        import src.api.routes as routes
        import src.api.routes.schema as schema_route
        from src.datasource.schema_snapshot import SchemaSnapshot, TableSchema

        # Arrange
        source = SimpleNamespace(name="sales", tenant_id=2, schema=None)
        registry = SimpleNamespace(get_provider=lambda name: SimpleNamespace(lookup=AsyncMock(return_value=source)), resolve=AsyncMock())
        service = SimpleNamespace(apply_snapshot=AsyncMock(return_value=SchemaSnapshot([TableSchema("orders")])))
        monkeypatch.setattr(routes, "_registry", lambda: registry)
        monkeypatch.setattr(schema_route, "_resolve_datasource_name", AsyncMock(return_value="sales"))
        monkeypatch.setattr("src.api.auth.get_current_identity", lambda: SimpleNamespace(tenant_id=2))
        monkeypatch.setattr("src.app_context.get_app_context", lambda: SimpleNamespace(get_resource=lambda name: service))
        app = FastAPI()
        app.include_router(schema_route.router)
        # Act
        async with AsyncClient(transport=ASGITransport(app), base_url="http://test") as client:
            response = await client.get("/schema/tables?datasource=sales&cached_only=true")
        # Assert
        assert response.status_code == 200
        assert response.json()["tables"][0]["name"] == "orders"
        registry.resolve.assert_not_awaited()
        service.apply_snapshot.assert_awaited_once_with(source)

    # 方法作用：构造只包含安全摘要的 Provider 和 Registry 测试替身。
    # Args: self - 测试实例；monkeypatch - 运行时依赖替换工具。
    # Returns: 测试替身、数据源列表和持久化记录。
    def _setup(self, monkeypatch):
        import src.api.auth as auth
        import src.api.routes as routes
        import src.api.routes.datasource as datasource_route

        items = [{
            "name": "sales",
            "dialect": "sqlite",
            "version": "",
            "mode": "external",
            "host": "",
            "port": 0,
            "database": "sales.db",
            "username": "",
            "description": "销售库",
            "schema": "",
            "tablespace": "",
            "service_name": "",
            "instance": "",
            "file_path": "sales.db",
            "extra_params": {"connect_timeout": 5, "password": "不要导出"},
            "schema_sync": {"status": "succeeded", "phase": "", "table_count": 3},
        }]
        persisted = []

        class FakeProvider:
            # 方法作用：提供租户原始配置供安全导出筛选。
            # Args: self - Provider；tenant_id - 请求租户。
            # Returns: 配置列表。
            async def list_all(self, *, tenant_id):
                return [SimpleNamespace(**{"tenant_id": 2, "password": "secret", **item}) for item in items]

            # 方法作用：模拟原子导入并在重名时按真实口径追加随机后缀。
            # Args: self - Provider；requests - 请求列表；tenant_id/owner_user_id - 所属身份。
            # Returns: 已保存的数据源列表，顺序与入参一一对应。
            async def import_configs(self, requests, *, tenant_id, owner_user_id):
                from src.datasource.transfer import resolve_available_name

                taken = {
                    str(item["name"]) for item in items
                    if int(item.get("tenant_id", tenant_id)) == tenant_id
                }
                results = []
                for req in requests:
                    resolved = resolve_available_name(req.name, taken.__contains__)
                    taken.add(resolved)
                    if resolved != req.name:
                        req = req.model_copy(update={"name": resolved})
                    results.append(await self.register(req, tenant_id=tenant_id, owner_user_id=owner_user_id))
                return results

            # 方法作用：模拟应用内注册数据源并返回配置对象。
            # Args: self - Provider；req - 已校验导入请求；tenant_id/owner_user_id - 所属身份。
            # Returns: 可持久化的数据源对象。
            async def register(self, req, *, tenant_id, owner_user_id):
                result = SimpleNamespace(
                    name=req.name, dialect=req.dialect, version=req.version,
                    mode="external", host=req.host, port=req.port,
                    database=req.database, username=req.username,
                    password="encrypted", description=req.description,
                    extra_params=dict(req.extra_params), tenant_id=tenant_id,
                    owner_user_id=owner_user_id,
                )
                persisted.append(result)
                return result

            # 方法作用：模拟配置持久化边界。
            # Args: self - Provider；ds - 待持久化数据源；tenant_id/owner_user_id - 所属身份。
            # Returns: 无返回值。
            async def persist(self, ds, *, tenant_id, owner_user_id):
                return None

            # 方法作用：模拟导入失败回滚时移除内存数据源。
            # Args: self - Provider；name - 数据源名；tenant_id - 租户 ID。
            # Returns: 无返回值。
            async def unregister(self, name, *, tenant_id):
                return None

            # 方法作用：模拟查询当前租户已有数据源。
            # Args: self - Provider；name - 数据源名；tenant_id - 租户 ID。
            # Returns: 未找到时返回 None。
            async def lookup(self, name, *, tenant_id):
                return None

        provider = FakeProvider()
        registry = SimpleNamespace(
            list_all=AsyncMock(return_value=items),
            get_provider=lambda name: provider,
            register_provider=lambda name, value: None,
            invalidate=lambda name, tenant_id: None,
        )
        monkeypatch.setattr(auth, "require_tenant_admin", lambda: None)
        monkeypatch.setattr(auth, "get_current_tenant_id", lambda: 2)
        monkeypatch.setattr(auth, "get_current_user_id", lambda: 7)
        monkeypatch.setattr(routes, "_registry", lambda: registry)
        monkeypatch.setattr(datasource_route, "_registry", lambda: registry, raising=False)
        monkeypatch.setattr("src.app_context.get_tenant_policy", lambda: SimpleNamespace(datasource_isolation_enabled=False))
        monkeypatch.setattr("src.security.permission_check.resolve_datasource_access", AsyncMock(return_value={"sales"}))
        monkeypatch.setattr("src.connectors.plugins.require_plugin", lambda dialect: None)
        return items, persisted

    # 方法作用：验证导出响应只保留连接配置，不包含密码和敏感扩展字段。
    # Args: self - 测试实例；monkeypatch - 依赖替换工具。
    # Returns: 无返回值。
    async def test_export_redacts_credentials(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """导出文件可用于迁移配置，但不能成为凭证泄露通道。"""
        from src.api.routes.datasource import export_datasources

        # Arrange
        self._setup(monkeypatch)

        # Act
        response = await export_datasources()
        content = b"".join([chunk async for chunk in response.body_iterator])
        payload = json.loads(content)

        # Assert
        assert payload["version"] == 1
        assert payload["datasources"][0]["name"] == "sales"
        assert "password" not in json.dumps(payload)
        assert "不要导出" not in json.dumps(payload)
        assert response.headers["content-disposition"].endswith('filename="datasources.json"')

    # 方法作用：验证导入文件可以创建应用内数据源且拒绝重复名称。
    # Args: self - 测试实例；monkeypatch - 依赖替换工具。
    # Returns: 无返回值。
    async def test_import_creates_datasources_without_yaml(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """导入只接受 JSON 配置，并由应用 API 写入持久化 Provider。"""
        from src.api.routes.datasource import import_datasources

        # Arrange
        _, persisted = self._setup(monkeypatch)
        upload = UploadFile(
            filename="datasources.json",
            file=io.BytesIO(json.dumps({"version": 1, "datasources": [{
                "name": "warehouse", "dialect": "sqlite", "file_path": "warehouse.db",
                "description": "仓库库", "extra_params": {"connect_timeout": 3},
            }]}).encode()),
        )

        # Act
        result = await import_datasources(upload)

        # Assert
        assert result["imported"] == ["warehouse"]
        assert persisted[0].name == "warehouse"
        assert persisted[0].password == "encrypted"

    # 方法作用：验证导入文件大小和格式边界在写入前被拒绝。
    # Args: self - 测试实例；monkeypatch - 依赖替换工具。
    # Returns: 无返回值。
    @pytest.mark.parametrize("content", [b"{}", b"not-json", b'{"version":2,"datasources":[]}', b'{"version":1,"datasources":[]}'])
    async def test_import_rejects_invalid_payload(self, monkeypatch: pytest.MonkeyPatch, content: bytes) -> None:
        """空对象和非法 JSON 不得产生部分数据源。"""
        from fastapi import HTTPException

        from src.api.routes.datasource import import_datasources

        # Arrange
        _, persisted = self._setup(monkeypatch)
        upload = UploadFile(filename="bad.json", file=io.BytesIO(content))

        # Act / Assert
        with pytest.raises(HTTPException) as caught:
            await import_datasources(upload)
        assert caught.value.status_code == 422
        assert not persisted

    # 方法作用：验证列表使用共享状态并在服务故障时拒绝伪装成空闲。
    # Args: self - 实例；monkeypatch - 依赖替换；status - 共享任务状态。
    # Returns: 无返回值。
    @pytest.mark.parametrize("status", ["running", "succeeded", "failed", "unavailable", "absent"])
    async def test_list_shared_sync_state(self, monkeypatch: pytest.MonkeyPatch, status: str) -> None:
        """刷新页面后可恢复任务状态，状态库故障用 unknown 表达。"""
        from src.api.routes.datasource import list_datasources

        # Arrange
        items, _ = self._setup(monkeypatch)
        state = AsyncMock(return_value={"status": status, "table_count": 3})
        if status == "unavailable":
            state.side_effect = RuntimeError("状态库不可用")
        service = None if status == "absent" else SimpleNamespace(store=SimpleNamespace(state=state))
        monkeypatch.setattr("src.app_context.get_app_context", lambda: SimpleNamespace(get_resource=lambda name: service))
        # Act
        result = await list_datasources(page=1, page_size=20)
        # Assert
        expected = "idle" if status == "absent" else "unknown" if status == "unavailable" else status
        assert result["datasources"][0]["schema_sync"]["status"] == expected
        assert items[0]["schema_sync"]["status"] == "succeeded"
        if status != "absent":
            state.assert_awaited_once_with(2, "sales")

    # 方法作用：验证旧 YAML 和环境变量数据源发现入口已从生产代码移除。
    # Args: self - 测试实例。
    # Returns: 无返回值。
    def test_datasource_sources_are_application_only(self) -> None:
        """Provider 和启动流程只恢复数据库持久化配置。"""
        from pathlib import Path

        from src.datasource.providers.external import ExternalDataSourceProvider

        source = Path("src/datasource/providers/external.py").read_text(encoding="utf-8")
        bootstrap = Path("src/bootstrap.py").read_text(encoding="utf-8")
        assert not hasattr(ExternalDataSourceProvider, "load_yaml")
        assert "from_yaml" not in source
        assert "config/datasources.yaml" not in bootstrap
        embedded = Path("src/datasource/providers/embedded.py").read_text(encoding="utf-8")
        assert "_load_from_env" not in embedded
        assert "_parse_ds_from_prefix" not in embedded

    # 方法作用：验证重复名称和越界字段在原子导入前全部校验。
    # Args: self - 实例；monkeypatch - 依赖替换；records - 导入条目。
    # Returns: 无返回值。
    @pytest.mark.parametrize("records", [
        [{"name": "same", "dialect": "sqlite"}] * 2,
        [{"name": "bad", "dialect": "sqlite", "port": 65536}],
        [{"name": "bad", "dialect": "sqlite", "tenant_id": 99}],
        [{"name": "bad", "dialect": "sqlite", "extra_params": []}],
        [{"name": "bad", "dialect": "sqlite"}] * 101,
    ])
    async def test_import_validation_before_writes(self, monkeypatch: pytest.MonkeyPatch, records: list[dict]) -> None:
        """任一条非法配置都不产生部分写入。"""
        from fastapi import HTTPException

        from src.api.routes.datasource import import_datasources

        # Arrange
        _, persisted = self._setup(monkeypatch)
        upload = UploadFile(filename="datasources.json", file=io.BytesIO(json.dumps({"version": 1, "datasources": records}).encode()))
        # Act
        with pytest.raises(HTTPException) as caught:
            await import_datasources(upload)
        # Assert
        assert caught.value.status_code == 422
        assert not persisted

    # 方法作用：验证上传大小上限在解析及持久化之前生效。
    # Args: self - 实例；monkeypatch - 依赖替换。
    # Returns: 无返回值。
    async def test_import_size_limit(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """超过 1 MiB 的配置返回 413。"""
        from fastapi import HTTPException

        from src.api.routes.datasource import import_datasources

        # Arrange
        _, persisted = self._setup(monkeypatch)
        upload = UploadFile(filename="large.json", file=io.BytesIO(b" " * (1024 * 1024 + 1)))
        # Act
        with pytest.raises(HTTPException) as caught:
            await import_datasources(upload)
        # Assert
        assert caught.value.status_code == 413
        assert not persisted

    # 方法作用：验证重名导入会改名并在响应中回传映射。
    # Args: self - 测试实例；monkeypatch - 依赖替换。
    # Returns: 无返回值。
    async def test_import_returns_renamed_mapping(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """只报告条数会让用户找不到改名后的数据源，等同静默丢失。"""
        from fastapi import FastAPI
        from httpx import ASGITransport, AsyncClient

        from src.api.routes.datasource import router

        # Arrange
        items, _ = self._setup(monkeypatch)
        items.append({**items[0], "name": "sales", "tenant_id": 2})
        app = FastAPI()
        app.include_router(router)
        body = {"version": 1, "datasources": [{"name": "sales", "dialect": "sqlite"}]}
        # Act
        async with AsyncClient(transport=ASGITransport(app), base_url="http://test") as client:
            response = await client.post(
                "/datasources/import",
                files={"file": ("source.json", json.dumps(body), "application/json")},
            )
        # Assert
        assert response.status_code == 201
        payload = response.json()
        assert len(payload["renamed"]) == 1
        assert payload["renamed"][0]["from"] == "sales"
        assert payload["renamed"][0]["to"].startswith("sales_")
        assert payload["imported"] == [payload["renamed"][0]["to"]]

    # 方法作用：验证 names 参数可单源与多源导出，重复名称被去重。
    # Args: self - 测试实例；monkeypatch - 依赖替换；names - 查询参数；expected - 期望名称。
    # Returns: 无返回值。
    @pytest.mark.parametrize(
        ("names", "expected"),
        [
            (["sales"], ["sales"]),
            (["orders", "sales"], ["sales", "orders"]),
            (["sales", "sales"], ["sales"]),
        ],
    )
    async def test_export_filters_by_names(
        self, monkeypatch: pytest.MonkeyPatch, names: list[str], expected: list[str],
    ) -> None:
        """重复名称必须去重，否则产出的文件会被导入侧按重复名整批拒绝。"""
        from fastapi import FastAPI
        from httpx import ASGITransport, AsyncClient

        from src.api.routes.datasource import router

        # Arrange
        items, _ = self._setup(monkeypatch)
        items.append({**items[0], "name": "orders", "tenant_id": 2})
        app = FastAPI()
        app.include_router(router)
        # Act
        async with AsyncClient(transport=ASGITransport(app), base_url="http://test") as client:
            exported = await client.get("/datasources/export", params=[("names", n) for n in names])
        # Assert
        assert exported.status_code == 200
        assert [item["name"] for item in exported.json()["datasources"]] == expected

    # 方法作用：验证未知名称返回 404 且不产出文件，跨租户名称同样不可见。
    # Args: self - 测试实例；monkeypatch - 依赖替换；name - 请求名称。
    # Returns: 无返回值。
    @pytest.mark.parametrize("name", ["missing", "other-tenant"])
    async def test_export_rejects_unknown_names(self, monkeypatch: pytest.MonkeyPatch, name: str) -> None:
        """「不存在」与「属于其他租户」必须返回一致，不泄露跨租户信息。"""
        from fastapi import FastAPI
        from httpx import ASGITransport, AsyncClient

        from src.api.routes.datasource import router

        # Arrange
        items, _ = self._setup(monkeypatch)
        items.append({**items[0], "name": "other-tenant", "tenant_id": 3})
        app = FastAPI()
        app.include_router(router)
        # Act
        async with AsyncClient(transport=ASGITransport(app), base_url="http://test") as client:
            exported = await client.get("/datasources/export", params=[("names", name)])
        # Assert
        assert exported.status_code == 404

    # 方法作用：验证不带参数仍是全量导出，保持既有行为。
    # Args: self - 测试实例；monkeypatch - 依赖替换。
    # Returns: 无返回值。
    async def test_export_without_names_returns_all(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """既有调用方与测试依赖无参全量语义，不能破坏。"""
        # Arrange
        self._setup(monkeypatch)
        # Act
        from src.api.routes.datasource import export_datasources

        response = await export_datasources()
        payload = json.loads(b"".join([chunk async for chunk in response.body_iterator]))
        # Assert
        assert [item["name"] for item in payload["datasources"]] == ["sales"]

    # 方法作用：验证导出条目不含 updated_at 运行时元数据。
    # Args: self - 测试实例；monkeypatch - 依赖替换。
    # Returns: 无返回值。
    async def test_export_excludes_updated_at(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """updated_at 与同步状态同属运行时元数据，不得进入可迁移文件。"""
        # Arrange
        self._setup(monkeypatch)
        # Act
        from src.api.routes.datasource import export_datasources

        response = await export_datasources()
        payload = json.loads(b"".join([chunk async for chunk in response.body_iterator]))
        # Assert
        assert "updated_at" not in json.dumps(payload)
