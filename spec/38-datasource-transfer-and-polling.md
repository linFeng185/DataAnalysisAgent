# 数据源导出选择、导入改名、列表与轮询 实现计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 让数据源导出支持单选/多选、导出后重名导入自动追加随机后缀并可追溯、列表按更新时间倒序，并修掉两处空转轮询。

**Architecture:** 后端保持「服务端生成导出文件」的既有边界，只在导出路由增加可选的 `names` 过滤；导入冲突由整批拒绝改为有界重试的随机后缀改名，改名映射随响应回传；`updated_at` 复用已存在的数据库列，贯通到列表并按它倒序。前端只负责选择与展示，不参与敏感字段判定。

**Tech Stack:** Python 3.14 / FastAPI / Pydantic v2 / asyncpg / pytest；React 19 + TypeScript + Ant Design；浏览器验收为 Playwright 驱动的 Python 测试。

依据设计：`spec/37-runtime-platform-and-ui.md`（本轮已更新第 39/41/42 行并新增轮询纪律与改名回传约束）。

## Global Constraints

- 所有注释与 docstring 使用中文；标识符用英文小写下划线。
- 每个方法必须写三要素注释（作用 / Args / Returns）并按项目规范打日志：入口 `debug`、出口 `info`、异常 `error`（含 `exc_info=True`）、分支回退 `info`/`warning`；禁止 `print()`。
- Python 使用 `async/await`；Pydantic 做校验；类型注解完整（Python 3.14 语法）。
- 前端为双语，新增用户可见文案**必须**同时补 `frontend/src/i18n/en.ts`，否则中文会直接漏给英文用户。
- 单元测试禁止调用真实 LLM / 真实业务数据库，使用 Fake 或 Mock。
- 导出文件名固定 `datasources.json`；导出文件**不得**包含 `updated_at`、密码、归属/权限、同步状态。
- 随机后缀固定为 `_` + 6 位 base36 小写；改名重试上限 3 次。
- 数据源名称上限 64 字符，后缀占 7 字符（`_` + 6），超长原名需截断。

---

### Task 1: 导入重名自动追加随机后缀

把「同名冲突整批拒绝」改为「自动改名 + 有界重试」，并回传改名映射。这是本轮唯一的**契约变更**，会推翻 `tests/test_datasource/test_transfer.py` 中 `test_duplicate_keeps_original` 的既有断言。

**Files:**
- Modify: `src/datasource/transfer.py`（新增随机后缀与改名解析函数）
- Modify: `src/datasource/providers/external.py:299-338`（`import_configs`）
- Modify: `src/api/routes/datasource.py:53-91`（导入路由响应）
- Test: `tests/test_datasource/test_transfer.py`
- Test: `tests/test_api/test_datasource_import_export.py`

**Interfaces:**
- Consumes: 既有 `DataSourceConflictError`、`ImportDataSource`、`ExternalDataSourceProvider._sources`（键为 `(tenant_id, name)`）。
- Produces:
  - `src/datasource/transfer.py::random_suffix() -> str`
  - `src/datasource/transfer.py::resolve_available_name(name: str, taken: Callable[[str], bool], *, max_attempts: int = 3) -> str`
  - `ExternalDataSourceProvider.import_configs(...)` **返回值顺序保证与入参 `requests` 一一对应**（路由据此推导改名映射）。
  - 导入响应新增 `renamed: list[dict[str, str]]`。

- [ ] **Step 1: 写失败的测试（改名解析函数）**

追加到 `tests/test_datasource/test_transfer.py`：

```python
class TestResolveAvailableName:
    """覆盖 25.7 改名解析：随机后缀、长度截断与重试上限。"""

    # 方法作用：验证名称未被占用时原样返回，不产生多余改名。
    # Args: self - 测试类实例。
    # Returns: 无返回值。
    def test_keeps_name_when_free(self) -> None:
        """不冲突就不改名，避免导入结果出现无意义后缀。"""
        logger.debug("test_keeps_name_when_free 入口")
        from src.datasource.transfer import resolve_available_name

        assert resolve_available_name("sales", lambda candidate: False) == "sales"
        logger.info("test_keeps_name_when_free 完成")

    # 方法作用：验证冲突时追加 6 位 base36 后缀且结果为未占用名称。
    # Args: self - 测试类实例。
    # Returns: 无返回值。
    def test_appends_six_char_suffix_on_conflict(self) -> None:
        """后缀格式固定为下划线加 6 位小写 base36，便于排查与 URL 安全。"""
        logger.debug("test_appends_six_char_suffix_on_conflict 入口")
        import re

        from src.datasource.transfer import resolve_available_name

        taken = {"sales"}
        result = resolve_available_name("sales", taken.__contains__)
        assert re.fullmatch(r"sales_[0-9a-z]{6}", result), result
        logger.info("test_appends_six_char_suffix_on_conflict 完成")

    # 方法作用：验证超长名称截断后仍满足 64 字符上限。
    # Args: self - 测试类实例。
    # Returns: 无返回值。
    def test_truncates_long_name_to_limit(self) -> None:
        """原名可达 64 字符，追加后缀必须截断而非超出数据库列长度。"""
        logger.debug("test_truncates_long_name_to_limit 入口")
        from src.datasource.transfer import resolve_available_name

        long_name = "a" * 64
        result = resolve_available_name(long_name, lambda candidate: candidate == long_name)
        assert result != long_name, "原名已占用时必须改名"
        assert len(result) == 64
        assert result.startswith("a" * 57)
        logger.info("test_truncates_long_name_to_limit 完成")

    # 方法作用：验证一直冲突时超过重试上限抛错，不无限循环。
    # Args: self - 测试类实例。
    # Returns: 无返回值。
    def test_raises_after_max_attempts(self) -> None:
        """候选名全部被占用时必须失败，不能死循环。"""
        logger.debug("test_raises_after_max_attempts 入口")
        import pytest as _pytest

        from src.datasource.transfer import DataSourceConflictError, resolve_available_name

        with _pytest.raises(DataSourceConflictError):
            resolve_available_name("sales", lambda candidate: True, max_attempts=3)
        logger.info("test_raises_after_max_attempts 完成")
```

- [ ] **Step 2: 运行测试确认失败**

Run: `.venv/Scripts/python.exe -m pytest tests/test_datasource/test_transfer.py -k ResolveAvailableName -v`
Expected: FAIL，`ImportError: cannot import name 'resolve_available_name'`

- [ ] **Step 3: 实现改名解析**

在 `src/datasource/transfer.py` 顶部补 `import secrets` 与 `from collections.abc import Callable`，并在 `MAX_IMPORT_BYTES` 附近加入常量与函数：

```python
_MAX_NAME_LENGTH = 64
_SUFFIX_ALPHABET = "0123456789abcdefghijklmnopqrstuvwxyz"
_SUFFIX_LENGTH = 6
_MAX_RENAME_ATTEMPTS = 3


# 方法作用：生成重名改名用的随机后缀，避免依赖需要并发保护的递增序号。
# Args: 无。
# Returns: 6 位 base36 小写随机串。
def random_suffix() -> str:
    """递增序号需先查询现有名称求最大值，并发导入会选中同一序号。"""
    return "".join(secrets.choice(_SUFFIX_ALPHABET) for _ in range(_SUFFIX_LENGTH))


# 方法作用：返回未被占用的名称，冲突时追加随机后缀并重试。
# Args: name - 原始名称；taken - 判断候选名是否已被占用的回调；max_attempts - 最大尝试次数。
# Returns: 未冲突时返回原名，否则返回截断后带后缀的新名称。
# Raises: DataSourceConflictError - 超过尝试次数仍无可用名称。
def resolve_available_name(
    name: str,
    taken: Callable[[str], bool],
    *,
    max_attempts: int = _MAX_RENAME_ATTEMPTS,
) -> str:
    """名称与后缀合计不超过 64 字符，后缀固定为下划线加 6 位 base36。"""
    if not taken(name):
        return name
    suffix_width = _SUFFIX_LENGTH + 1
    for _ in range(max_attempts):
        candidate = f"{name[: _MAX_NAME_LENGTH - suffix_width]}_{random_suffix()}"
        if not taken(candidate):
            return candidate
    raise DataSourceConflictError("同名数据源过多，未能生成可用后缀")
```

- [ ] **Step 4: 运行测试确认通过**

Run: `.venv/Scripts/python.exe -m pytest tests/test_datasource/test_transfer.py -k ResolveAvailableName -v`
Expected: PASS（4 passed）

- [ ] **Step 5: 写失败的测试（import_configs 改名行为）**

替换 `tests/test_datasource/test_transfer.py` 中的 `test_duplicate_keeps_original`，改为：

替换 `tests/test_datasource/test_transfer.py` 中 `TestDataSourceTransfer`（第 16 行）里的 `test_duplicate_keeps_original`（第 115 行）：

```python
    # 方法作用：验证同租户重名改为追加随机后缀，且不覆盖原配置。
    # Args: self - 实例；monkeypatch - 依赖替换。
    # Returns: 无返回值。
    async def test_duplicate_renames_instead_of_failing(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """导入不再整批拒绝；原名保留，新条目带后缀且不覆盖已存在配置。"""
        # Arrange
        import re

        provider = ExternalDataSourceProvider()
        source = DataSourceConfig(name="sales", dialect="sqlite", mode="external", tenant_id=2)
        provider._register(source)
        writes: list[tuple[str, tuple]] = []

        # 方法作用：提供可回滚的内存事务边界。
        # Args: 无。
        # Returns: 模拟事务上下文。
        @asynccontextmanager
        async def transaction():
            try:
                yield
            except Exception:
                writes.clear()
                raise

        # 方法作用：记录事务写入。
        # Args: sql - 写入语句；values - 参数。
        # Returns: 无返回值。
        async def execute(sql: str, *values: object) -> None:
            writes.append((sql, values))

        connection = SimpleNamespace(transaction=transaction, execute=execute)

        # 方法作用：模拟借用状态库连接。
        # Args: 无。
        # Returns: 测试连接。
        @asynccontextmanager
        async def acquire():
            yield connection

        monkeypatch.setattr("src.memory.pg_pool.get_pg_pool", AsyncMock(return_value=SimpleNamespace(acquire=acquire)))
        # Act
        result = await provider.import_configs(
            [ImportDataSource(name="sales", dialect="sqlite")], tenant_id=2, owner_user_id=7,
        )
        # Assert
        assert len(result) == 1
        assert re.fullmatch(r"sales_[0-9a-z]{6}", result[0].name), result[0].name
        assert writes[0][1][0] == result[0].name
        assert provider._sources[(2, "sales")] is source
```

> `transaction` / `acquire` 必须在本测试内定义（该文件每个测试各自定义局部 `@asynccontextmanager`，见第 69 与 91 行），不能跨测试复用。

- [ ] **Step 6: 运行测试确认失败**

Run: `.venv/Scripts/python.exe -m pytest tests/test_datasource/test_transfer.py -k test_duplicate -v`
Expected: FAIL，当前实现抛 `DataSourceConflictError`

- [ ] **Step 7: 实现 import_configs 改名**

把 `src/datasource/providers/external.py` 的 `import_configs` 中冲突检查段替换为：

```python
        from src.datasource.transfer import resolve_available_name

        logger.debug("数据源原子导入入口", tenant_id=tenant_id, count=len(requests))
        # 服务端仍是唯一改名权威；DB 唯一约束继续作为并发兜底。
        # Args: candidate - 候选名称。
        # Returns: 该租户下名称是否已被占用。
        def is_taken(candidate: str) -> bool:
            return (tenant_id, candidate) in self._sources

        reserved = {(tenant_id, request.name) for request in requests}
        sources = []
        for request in requests:
            # 同批次内先解析的名称也要占用，避免批次内互相撞名。
            resolved = resolve_available_name(
                request.name, lambda c: is_taken(c) or (tenant_id, c) in reserved,
            )
            request = request.model_copy(update={"name": resolved})
            source = self._build_config(request, encrypt_password=True)
            source.tenant_id, source.owner_user_id = tenant_id, owner_user_id
            reserved.add((tenant_id, resolved))
            sources.append(source)
        if len({source.name for source in sources}) != len(sources):
            raise DataSourceConflictError("同批次改名后仍存在重名")
```

并把该方法的 docstring 改为：

```python
        """通过唯一约束防止并发导入覆盖同租户配置；返回值顺序与入参一一对应。"""
```

- [ ] **Step 8: 运行测试确认通过**

Run: `.venv/Scripts/python.exe -m pytest tests/test_datasource/test_transfer.py -v`
Expected: PASS（含原有事务回滚用例）

- [ ] **Step 9: 导入路由回传改名映射**

`src/api/routes/datasource.py` 导入路由末尾，把返回改为：

```python
    requested = [item.name for item in bundle.datasources]
    returned = [source.name for source in sources]
    renamed = [
        {"from": original, "to": current}
        for original, current in zip(requested, returned, strict=True)
        if original != current
    ]
    logger.info("数据源导入完成", tenant_id=tenant_id, count=len(sources), renamed_count=len(renamed))
    return {"imported": returned, "renamed": renamed}
```

- [ ] **Step 10: 写并通过路由层测试**

追加到 `tests/test_api/test_datasource_import_export.py`：

```python
    # 方法作用：验证重名导入会改名并在响应中回传映射。
    # Args: self - 测试实例；monkeypatch - 依赖替换。
    # Returns: 无返回值。
    async def test_import_returns_renamed_mapping(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """只报告条数会让用户找不到改名后的数据源，等同静默丢失。"""
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
```

> `_setup` 返回 `(items, persisted)` 二元组（见 `tests/test_api/test_datasource_import_export.py:25-26` 的 `items, persisted = self._setup(monkeypatch)`），故用 `items, _ =` 解包；`items` 是可变 list，直接 append 追加行即可。

- [ ] **Step 11: 运行并提交**

Run: `.venv/Scripts/python.exe -m pytest tests/test_datasource/test_transfer.py tests/test_api/test_datasource_import_export.py -v`
Expected: PASS

```bash
git add src/datasource/transfer.py src/datasource/providers/external.py src/api/routes/datasource.py tests/test_datasource/test_transfer.py tests/test_api/test_datasource_import_export.py
git commit -m "feat: 数据源导入重名自动追加随机后缀并回传改名映射"
```

---

### Task 2: 导出支持单选/多选

**Files:**
- Modify: `src/api/routes/datasource.py:30-47`（导出路由）
- Test: `tests/test_api/test_datasource_import_export.py`

**Interfaces:**
- Consumes: `export_config()` 保持原样，不新增字段。
- Produces: `GET /api/v1/datasources/export?names=a&names=b`，`names` 可选可重复。

- [ ] **Step 1: 写失败的测试**

追加到 `tests/test_api/test_datasource_import_export.py`：

```python
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
```

- [ ] **Step 2: 运行测试确认失败**

Run: `.venv/Scripts/python.exe -m pytest tests/test_api/test_datasource_import_export.py -k "export_filters_by_names or export_rejects_unknown" -v`
Expected: FAIL，`names` 参数不存在，过滤与 404 都不生效

- [ ] **Step 3: 实现导出过滤**

`src/api/routes/datasource.py` 顶部补 `from typing import Annotated`，替换导出路由：

```python
# 方法作用：导出当前租户应用内数据源的无凭证配置，可按名称筛选子集。
# Args: names - 可选的数据源名称，可重复；不传或为空即导出全部。
# Returns: 可下载的 JSON 配置文件；名称不可用时返回 404 且不产出文件。
@router.get("/datasources/export")
async def export_datasources(names: Annotated[list[str] | None, Query()] = None) -> StreamingResponse:
    """导出不包含密码，导入后需在应用中补充凭证。"""
    import src.api.routes as routes_package
    from src.api.auth import get_current_tenant_id, require_tenant_admin
    from src.datasource.transfer import export_config

    require_tenant_admin()
    tenant_id = get_current_tenant_id()
    logger.debug("数据源导出入口", tenant_id=tenant_id, requested=len(names or []))
    provider = routes_package._registry().get_provider("external")
    sources = await provider.list_all(tenant_id=tenant_id) if provider else []
    available = [source for source in sources if int(source.tenant_id) == tenant_id]
    if names:
        # 去重后按 Provider 顺序过滤：重复名称会产出重复条目，导入侧将整批拒绝。
        wanted = set(names)
        if not wanted <= {source.name for source in available}:
            logger.warning("数据源导出请求包含不可用名称", tenant_id=tenant_id, requested=len(wanted))
            raise HTTPException(404, "指定的数据源不存在")
        available = [source for source in available if source.name in wanted]
    configs = [export_config(source) for source in available]
    payload = json.dumps({"version": 1, "datasources": configs}, ensure_ascii=False, indent=2).encode("utf-8")
    logger.info("数据源导出完成", tenant_id=tenant_id, count=len(configs), filtered=bool(names))
    return StreamingResponse(io.BytesIO(payload), media_type="application/json", headers={
        "Content-Disposition": 'attachment; filename="datasources.json"', "Cache-Control": "no-store",
    })
```

> **必须使用 `Annotated[list[str] | None, Query()] = None`**，不能写成 `names: list[str] | None = Query(default=None)`：后者在直接调用 `export_datasources()` 时会把 `Query` 对象当作实参，而 `test_export_redacts_credentials` 与 `test_transfer_requires_management_permission` 正是直接调用该函数。

- [ ] **Step 4: 运行测试确认通过**

Run: `.venv/Scripts/python.exe -m pytest tests/test_api/test_datasource_import_export.py -v`
Expected: PASS（含既有 `test_export_redacts_credentials`，其断言文件名仍为 `datasources.json`）

- [ ] **Step 5: 提交**

```bash
git add src/api/routes/datasource.py tests/test_api/test_datasource_import_export.py
git commit -m "feat: 数据源导出支持按名称单选与多选"
```

---

### Task 3: `updated_at` 贯通与列表倒序

`datasource_configs.updated_at` 列**已存在**（`migrations/006_platform_admin.sql:61`，`TIMESTAMPTZ NOT NULL DEFAULT NOW()`），编辑路径已在维护它（`ON CONFLICT ... SET updated_at=NOW()`）。本任务**不需要新迁移**，只把值读出来、透出并据此排序。

**Files:**
- Modify: `src/datasource/config.py:9-31`（`DataSourceConfig`）
- Modify: `src/datasource/providers/external.py`（`load_persisted`、`import_configs`、持久化 upsert、`list_all`）
- Modify: `src/datasource/registry.py:642-660`（摘要字典）
- Modify: `src/api/schemas.py:209-227`（`DataSourceInfo`）
- Modify: `src/api/routes/datasource.py::_datasource_info`
- Test: `tests/test_datasource/test_transfer.py`

**Interfaces:**
- Produces:
  - `DataSourceConfig.updated_at: datetime | None = None`
  - 数据源摘要字典与 `DataSourceInfo` 新增 `updated_at: str`（ISO-8601，无值为空串）
  - `ExternalDataSourceProvider.list_all` 返回**按 `updated_at` 倒序**排列

- [ ] **Step 1: 写失败的测试**

追加到 `tests/test_datasource/test_transfer.py`：

```python
class TestListOrdering:
    """覆盖 25.7 列表排序：按 updated_at 倒序，缺失值排最后。"""

    # 方法作用：验证列表按 updated_at 倒序返回。
    # Args: self - 测试类实例。
    # Returns: 无返回值。
    async def test_list_all_orders_by_updated_at_desc(self) -> None:
        """最近更新的数据源必须排在最前，用户才能看到刚导入的对象。"""
        logger.debug("test_list_all_orders_by_updated_at_desc 入口")
        from datetime import UTC, datetime, timedelta

        provider = ExternalDataSourceProvider()
        now = datetime.now(UTC)
        for offset, name in ((2, "oldest"), (1, "middle"), (0, "newest")):
            provider._register(DataSourceConfig(
                name=name, dialect="sqlite", mode="external", tenant_id=2,
                updated_at=now - timedelta(hours=offset),
            ))
        # Act
        result = await provider.list_all(tenant_id=2)
        # Assert
        assert [item.name for item in result] == ["newest", "middle", "oldest"]
        logger.info("test_list_all_orders_by_updated_at_desc 完成")

    # 方法作用：验证缺失 updated_at 的条目排在最后且不抛异常。
    # Args: self - 测试类实例。
    # Returns: 无返回值。
    async def test_list_all_places_missing_updated_at_last(self) -> None:
        """朴素与带时区 datetime 不可直接比较，实现必须避开该陷阱。"""
        logger.debug("test_list_all_places_missing_updated_at_last 入口")
        from datetime import UTC, datetime

        provider = ExternalDataSourceProvider()
        provider._register(DataSourceConfig(
            name="undated", dialect="sqlite", mode="external", tenant_id=2))
        provider._register(DataSourceConfig(
            name="dated", dialect="sqlite", mode="external", tenant_id=2,
            updated_at=datetime.now(UTC)))
        # Act
        result = await provider.list_all(tenant_id=2)
        # Assert
        assert [item.name for item in result] == ["dated", "undated"]
        logger.info("test_list_all_places_missing_updated_at_last 完成")
```

- [ ] **Step 2: 运行测试确认失败**

Run: `.venv/Scripts/python.exe -m pytest tests/test_datasource/test_transfer.py -k TestListOrdering -v`
Expected: FAIL，`DataSourceConfig` 不接受 `updated_at`，顺序为插入顺序

- [ ] **Step 3: 扩展 DataSourceConfig**

`src/datasource/config.py` 顶部补 `from datetime import datetime`，在 `extra_params` 之后加入字段：

```python
    # 列表排序用的运行时元数据，不进入导出文件。
    updated_at: datetime | None = None
```

- [ ] **Step 4: 实现 list_all 倒序**

`src/datasource/providers/external.py::list_all` 改为：

```python
    async def list_all(self, *, tenant_id: int | None = None) -> list[DataSourceConfig]:
        """按 updated_at 倒序返回，最近更新的数据源排在最前。"""
        if not self._isolation_enabled():
            tenant_id = None
        elif tenant_id is None:
            tenant_id = self._effective_tenant_id(None)
        items = [
            value for (source_tenant, _), value in self._sources.items()
            if tenant_id is None or source_tenant == int(tenant_id)
        ]
        # 用时间戳比较，避开朴素与带时区 datetime 不可比较的陷阱；缺失值排最后。
        items.sort(key=lambda ds: ds.updated_at.timestamp() if ds.updated_at else 0.0, reverse=True)
        logger.debug("列出外部数据源完成", tenant_id=tenant_id, count=len(items))
        return items
```

- [ ] **Step 5: 运行测试确认通过**

Run: `.venv/Scripts/python.exe -m pytest tests/test_datasource/test_transfer.py -k TestListOrdering -v`
Expected: PASS

- [ ] **Step 6: 读库时带回 updated_at**

`src/datasource/providers/external.py::load_persisted` 的 SQL 与构造改为：

```python
                rows = await connection.fetch(
                    "SELECT name, tenant_id, owner_user_id, dialect, version, host, port, "
                    "database_name, username, encrypted_password, description, extra_params, updated_at "
                    "FROM datasource_configs ORDER BY updated_at DESC, id DESC",
                )
```

并在 `DataSourceConfig(...)` 构造中补：

```python
                    updated_at=row["updated_at"],
```

- [ ] **Step 7: 写入路径回填 updated_at**

`import_configs` 的 INSERT 改为 `fetchrow` 以取回数据库时间，保证内存值与库内一致：

```python
                    row = await connection.fetchrow(
                        "INSERT INTO datasource_configs "
                        "(name,tenant_id,owner_user_id,dialect,version,host,port,database_name,username,encrypted_password,description,extra_params) "
                        "VALUES($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11,$12::jsonb) "
                        "RETURNING updated_at",
                        source.name, tenant_id, owner_user_id, source.dialect, source.version,
                        source.host, source.port, source.database, source.username, source.password,
                        source.description, json.dumps(source.extra_params, ensure_ascii=False),
                    )
                    source.updated_at = row["updated_at"]
```

对持久化 upsert 路径（`ON CONFLICT ... updated_at=NOW()` 的 INSERT）做同样处理：改为 `fetchrow` 并 `RETURNING updated_at`，把返回值赋给 `ds.updated_at`。

> **`execute` → `fetchrow` 会打破既有测试替身**：`tests/test_datasource/test_transfer.py` 中 Task 1 新增的 `test_duplicate_renames_instead_of_failing`，以及既有的 `test_batch_transaction_commit_or_rollback`（第 57 行起）都用 `SimpleNamespace(transaction=..., execute=...)` 伪造连接，且断言 `len(writes) == 4`。本步必须同步把这些替身改成同时提供 `fetchrow`（返回 `{"updated_at": None}` 或固定 `datetime`）并相应调整 `writes` 的记录方式；改完必须重跑 Task 1 的全部用例确认仍绿。

> `self._register(source)` 必须在事务提交之后执行（现有代码已如此），否则失败批次会污染内存。

- [ ] **Step 8: 摘要与 API schema 透出**

`src/datasource/registry.py` 的两处摘要字典各补一行：

```python
                    "updated_at": ds.updated_at.isoformat() if getattr(ds, "updated_at", None) else "",
```

```python
                    "updated_at": getattr(config, "updated_at", None).isoformat() if getattr(config, "updated_at", None) else "",
```

`src/api/schemas.py::DataSourceInfo` 补字段：

```python
    updated_at: str = ""
```

`src/api/routes/datasource.py::_datasource_info` 同步补映射：

```python
        updated_at=datasource.updated_at.isoformat() if getattr(datasource, "updated_at", None) else "",
```

- [ ] **Step 9: 运行全量后端回归**

Run: `.venv/Scripts/python.exe -m pytest tests/test_datasource tests/test_api -q`
Expected: PASS

- [ ] **Step 10: 提交**

```bash
git add src/datasource/config.py src/datasource/providers/external.py src/datasource/registry.py src/api/schemas.py src/api/routes/datasource.py tests/test_datasource/test_transfer.py
git commit -m "feat: 数据源列表按 updated_at 倒序并透出该字段"
```

---

### Task 4: 前端导出多选与更新时间列

**Files:**
- Modify: `frontend/src/types/index.ts:142-159`（`DatasourceConfig`）
- Modify: `frontend/src/pages/DatasourcePage.tsx`
- Modify: `frontend/src/i18n/en.ts`
- Test: `tests/test_frontend/test_runtime_browser.py`
- Test: `tests/test_frontend/test_datasource_lifecycle.py`（静态源码契约测试）

**Interfaces:**
- Consumes: Task 2 的 `GET /datasources/export?names=`；Task 3 的 `updated_at` 字段。
- Produces: 前端 `DatasourceConfig.updated_at?: string`。

- [ ] **Step 1: 扩展前端类型**

`frontend/src/types/index.ts` 的 `DatasourceConfig` 内补：

```ts
  updated_at?: string;
```

- [ ] **Step 2: 加选择状态、导出改造与导入改名提示**

`frontend/src/pages/DatasourcePage.tsx` 先把 `importSources` 的响应类型与提示改为展示改名映射（`spec/37` 要求「改名映射必须随导入响应回传**并在界面提示**」，只报条数等同静默丢失）：

```tsx
      const result = await uploadConfig<{ imported: string[]; renamed: { from: string; to: string }[] }>('/datasources/import', file);
      if (result.renamed?.length) {
        message.warning(t("已导入 {value0} 个数据源；其中 {value1} 个因重名已改名：{value2}", {
          value0: result.imported.length,
          value1: result.renamed.length,
          value2: result.renamed.map(item => `${item.from} → ${item.to}`).join('、'),
        }), 8);
      } else {
        message.success(t("已导入 {value0} 个数据源，请补充连接凭证后同步", { value0: result.imported.length }));
      }
```



`frontend/src/pages/DatasourcePage.tsx` 在 `pendingSync` 附近补状态：

```tsx
  const [selectedNames, setSelectedNames] = useState<string[]>([]);
```

把 `exportSources` 替换为：

```tsx
  // 方法作用：导出所选数据源；未勾选时导出全部。
  // Args: 无，读取当前勾选状态。
  // Returns: 无返回值。
  const exportSources = async (): Promise<void> => {
    setTransferring(true);
    try {
      const query = selectedNames.map(name => `names=${encodeURIComponent(name)}`).join('&');
      const blob = await downloadConfig(query ? `/datasources/export?${query}` : '/datasources/export');
      const url = URL.createObjectURL(blob);
      const anchor = document.createElement('a');
      anchor.href = url; anchor.download = 'datasources.json'; anchor.click();
      window.setTimeout(() => URL.revokeObjectURL(url), 1000);
      setSelectedNames([]);
    } catch (error) { message.error(error instanceof Error ? error.message : t("导出失败")); }
    finally { setTransferring(false); }
  };
```

- [ ] **Step 3: 工具栏按钮与表格选择**

工具栏按钮替换为（**未勾选时文案必须恰好是「导出数据源」**，`test_runtime_browser.py:64` 用 `exact=True` 点击）：

```tsx
              <Tooltip title={t("导出不含密码和敏感连接参数，导入后需重新填写")}>
                <Button loading={transferring} onClick={() => void exportSources()}>
                  {selectedNames.length ? t("导出所选 ({value0})", { value0: selectedNames.length }) : t("导出数据源")}
                </Button>
              </Tooltip>
```

`Table` 增加：

```tsx
          rowSelection={{
            selectedRowKeys: selectedNames,
            onChange: keys => setSelectedNames(keys as string[]),
          }}
```

表头列在「描述」之后加入：

```tsx
            { title: t("更新时间"), dataIndex: 'updated_at', key: 'updated_at', width: 170, render: (value: string) => value
              ? new Date(value).toLocaleString()
              : <Typography.Text type="secondary">-</Typography.Text> },
```

- [ ] **Step 4: 补英文文案**

`frontend/src/i18n/en.ts` 补充（**缺一条中文就会漏给英文用户**）：

```ts
  "导出所选 ({value0})": "Export selected ({value0})",
  "更新时间": "Updated at",
  "已导入 {value0} 个数据源；其中 {value1} 个因重名已改名：{value2}": "Imported {value0} data sources; {value1} were renamed due to name conflicts: {value2}",
```

- [ ] **Step 5: 类型检查与构建**

Run: `cd frontend && npm run build`
Expected: 构建成功，无 TypeScript 错误

- [ ] **Step 6: 扩展浏览器测试**

在 `tests/test_frontend/test_runtime_browser.py` 的 `TestRuntimeBrowser` 类中新增一个**自包含**测试方法。

**不要改既有的 `test_datasource_file_import_export`**：它的桩只提供一行数据（`imported` 列表），且用 `urlparse(...).path` 比较路径、丢掉了 query，无法断言 `names`。

```python
    # 方法作用：验证勾选一个或多个数据源时按 names 参数导出，未勾选时全量导出。
    # Args: self - 实例；browser_page/frontend_origin - 隔离浏览器和前端。
    # Returns: 无返回值。
    def test_datasource_export_selection(self, browser_page: Any, frontend_origin: str) -> None:
        """单选、多选与不选三种路径必须产生不同的导出请求。"""
        from playwright.sync_api import expect

        # Arrange
        page = browser_page
        export_urls: list[str] = []
        rows = [
            {"name": "sales", "dialect": "sqlite", "schema_sync": {"status": "idle"}},
            {"name": "orders", "dialect": "sqlite", "schema_sync": {"status": "idle"}},
        ]

        # 方法作用：提供两行数据源并记录导出请求的完整 URL。
        # Args: route - 拦截的 HTTP 请求。
        # Returns: 无返回值。
        def handle(route: Any) -> None:
            path = urlparse(route.request.url).path.removeprefix("/api/v1")
            if path == "/auth/me":
                data = {"authenticated": True, "auth_required": True, "role": "tenant_admin", "tenant_id": 2}
            elif path == "/datasources/export":
                export_urls.append(route.request.url)
                route.fulfill(body=json.dumps({"version": 1, "datasources": []}), content_type="application/json",
                              headers={"Content-Disposition": 'attachment; filename="datasources.json"'})
                return
            elif path == "/datasources":
                data = {"datasources": rows, "total": len(rows)}
            else:
                data = {}
            route.fulfill(json=data)

        page.route("**/api/v1/**", handle)
        page.goto(f"{frontend_origin}/datasource")
        expect(page.get_by_role("cell", name="sales", exact=True)).to_be_visible()

        # Act / Assert：不勾选时全量导出，URL 不得带 names
        with page.expect_download():
            page.get_by_role("button", name="导出数据源", exact=True).click()
        assert "names=" not in export_urls[-1]

        # Act / Assert：勾选 1 个即单选导出，导出后勾选被清空
        page.get_by_role("row", name=re.compile("sales")).get_by_role("checkbox").check()
        with page.expect_download():
            page.get_by_role("button", name=re.compile("导出所选")).click()
        assert export_urls[-1].count("names=") == 1
        expect(page.get_by_role("button", name="导出数据源", exact=True)).to_be_visible()

        # Act / Assert：勾选 2 个即多选导出
        page.get_by_role("row", name=re.compile("sales")).get_by_role("checkbox").check()
        page.get_by_role("row", name=re.compile("orders")).get_by_role("checkbox").check()
        with page.expect_download():
            page.get_by_role("button", name=re.compile("导出所选")).click()
        assert export_urls[-1].count("names=") == 2
```

> 该文件顶部已 import `json`（第 3 行）、`re`（第 7 行）、`urlparse`（第 8 行）与 `Any`（第 6 行），无需新增 import。第 14 行的 `pytestmark` 门禁要求 `RUN_BROWSER_TESTS=1` 才会执行。

同一 Step 中，另在 `tests/test_frontend/test_datasource_lifecycle.py`（静态源码文本断言文件）追加一条契约测试，保证改名映射确实被界面消费（该文件不依赖浏览器，永远会跑）：

```python
    # 方法作用：验证导入改名映射被界面消费而不是静默丢弃。
    # Args: self - pytest 测试类实例。
    # Returns: 无返回值，断言失败时由 pytest 报告。
    def test_import_rename_mapping_is_surfaced(self) -> None:
        """spec/37 要求改名必须在界面提示，只报导入条数等同静默丢失。"""
        # Arrange
        source = Path("frontend/src/pages/DatasourcePage.tsx").read_text(encoding="utf-8")

        # Act / Assert
        assert "renamed" in source, "导入响应中的 renamed 未被前端读取"
        assert "因重名已改名" in source, "改名映射未在界面提示"
```

- [ ] **Step 7: 运行浏览器测试**

Run: `.venv/Scripts/python.exe -m pytest tests/test_frontend/test_runtime_browser.py -q`
Expected: PASS

- [ ] **Step 8: 提交**

```bash
git add frontend/src/types/index.ts frontend/src/pages/DatasourcePage.tsx frontend/src/i18n/en.ts tests/test_frontend/test_runtime_browser.py
git commit -m "feat: 数据源页支持勾选导出并展示更新时间"
```

---

### Task 5: 修复两处空转轮询

**Files:**
- Modify: `frontend/src/pages/DatasourcePage.tsx:122-141`
- Modify: `frontend/src/pages/KnowledgePage.tsx:182-202`
- Test: `tests/test_frontend/test_datasource_lifecycle.py`（静态源码契约测试）
- Test: `tests/test_frontend/test_runtime_browser.py`（浏览器行为测试，需 `RUN_BROWSER_TESTS=1`）

**Interfaces:**
- Consumes: `DatasourceConfig.schema_sync.status`（取值 `idle|running|cancelling|succeeded|cancelled|failed|unknown`）。
- Produces: 无新接口。

- [ ] **Step 1: 修数据源页轮询**

`DatasourcePage.tsx` 的轮询 effect 替换为：

```tsx
  // 方法作用：仅在存在进行中的同步时轮询列表，空闲即停。
  // Args: 无，读取当前列表状态。
  // Returns: 卸载或进入空闲时停止轮询。
  useEffect(() => {
    const active = datasources.some(item =>
      ['running', 'cancelling'].includes(item.schema_sync?.status || ''));
    if (!active) return undefined;
    let alive = true;
    let timer: number;
    // 方法作用：刷新列表而不打断用户表单，并丢弃跨操作的旧结果。
    // Args: 无。
    // Returns: 安排下一次轮询。
    const poll = async (): Promise<void> => {
      const currentRevision = revision.current;
      try {
        const items = await fetchDatasourceCatalog();
        if (alive && currentRevision === revision.current) setDatasources(items);
      } catch {
        if (alive && currentRevision === revision.current) setDatasources(items => items.map(item => ({ ...item, schema_sync: { status: 'unknown' } })));
      } finally {
        if (alive) timer = window.setTimeout(() => void poll(), 1500);
      }
    };
    timer = window.setTimeout(() => void poll(), 1500);
    return () => { alive = false; window.clearTimeout(timer); };
  }, [datasources]);
```

> 依赖数组必须是 `[datasources]`：`changeSync` 提交启动后会 `setDatasources`，状态变为 `running` 时 effect 自动重跑并进入轮询；全部落入终态后 effect 重跑并立刻返回 `undefined`，轮询停止。

- [ ] **Step 2: 修知识页幽灵任务**

`KnowledgePage.tsx` 的轮询终止判断改为把「从响应中消失」视为终态：

```tsx
        const finished = taskIds.every(id => {
          const status = states[id]?.status;
          // 任务从响应中消失时按终止处理，否则 every 恒为假会导致永不停止的轮询。
          return status === undefined || doneStatuses.has(status);
        });
        if (finished) {
          const doneCount = taskIds.filter(id => states[id]?.status === 'done').length;
          notification[doneCount === taskIds.length ? 'success' : 'warning']({
            message: t("文档处理完成"), description: t("{value0} 成功, {value1} 失败", { value0: doneCount, value1: taskIds.length - doneCount }),
          });
          setTaskIds([]); reloadAll();
        }
```

- [ ] **Step 3: 写静态契约测试**

`tests/test_frontend/test_datasource_lifecycle.py` 是**静态源码文本断言**文件（`Path(...).read_text()`），不是浏览器测试，没有 `page` fixture。按该文件既有风格追加：

```python
    # 方法作用：验证数据源页轮询只在存在进行中同步时续期。
    # Args: self - pytest 测试类实例。
    # Returns: 无返回值，断言失败时由 pytest 报告。
    def test_polling_only_runs_while_sync_in_progress(self) -> None:
        """空闲页面持续轮询列表接口属于无谓负载，续期必须有可达的停止条件。"""
        # Arrange
        source = Path("frontend/src/pages/DatasourcePage.tsx").read_text(encoding="utf-8")

        # Act：轮询必须有进行中同步守卫，并以列表为依赖以便状态变化时启停
        guarded = "['running', 'cancelling'].includes(item.schema_sync?.status || '')" in source
        depends_on_list = "}, [datasources]);" in source

        # Assert
        assert guarded, "轮询缺少「存在 running/cancelling 同步」的守卫"
        assert depends_on_list, "轮询 effect 未以 datasources 为依赖，状态变化无法启停轮询"
```

- [ ] **Step 4: 写浏览器行为测试**

在 `tests/test_frontend/test_runtime_browser.py` 的 `TestRuntimeBrowser` 类中新增：

```python
    # 方法作用：验证数据源页在无进行中同步时不会持续轮询列表接口。
    # Args: self - 实例；browser_page/frontend_origin - 隔离浏览器和前端。
    # Returns: 无返回值。
    def test_idle_datasource_page_stops_polling(self, browser_page: Any, frontend_origin: str) -> None:
        """空闲页面每 1.5 秒请求一次列表属于无谓负载。"""
        from playwright.sync_api import expect

        # Arrange
        page = browser_page
        calls: list[str] = []
        rows = [{"name": "sales", "dialect": "sqlite", "schema_sync": {"status": "idle"}}]

        # 方法作用：返回空闲数据源并统计列表请求次数。
        # Args: route - 拦截的 HTTP 请求。
        # Returns: 无返回值。
        def handle(route: Any) -> None:
            path = urlparse(route.request.url).path.removeprefix("/api/v1")
            if path == "/auth/me":
                data = {"authenticated": True, "auth_required": True, "role": "tenant_admin", "tenant_id": 2}
            elif path == "/datasources":
                calls.append(route.request.url)
                data = {"datasources": rows, "total": len(rows)}
            else:
                data = {}
            route.fulfill(json=data)

        page.route("**/api/v1/**", handle)
        page.goto(f"{frontend_origin}/datasource")
        expect(page.get_by_role("cell", name="sales", exact=True)).to_be_visible()

        # Act：静置 5 秒，远超原来的 1.5 秒轮询间隔
        baseline = len(calls)
        page.wait_for_timeout(5000)

        # Assert
        assert len(calls) == baseline, f"空闲页面仍在轮询：{calls[baseline:]}"
```

- [ ] **Step 5: 运行浏览器回归**

Run: `.venv/Scripts/python.exe -m pytest tests/test_frontend/test_datasource_lifecycle.py -q && RUN_BROWSER_TESTS=1 .venv/Scripts/python.exe -m pytest tests/test_frontend/test_runtime_browser.py -q`
Expected: PASS

- [ ] **Step 6: 提交**

```bash
git add frontend/src/pages/DatasourcePage.tsx frontend/src/pages/KnowledgePage.tsx tests/test_frontend/test_datasource_lifecycle.py tests/test_frontend/test_runtime_browser.py
git commit -m "fix: 数据源页空闲轮询与知识页幽灵任务永不停止"
```

---

### Task 6: 同步被推翻的既有文档

Task 1 推翻了「重名整批拒绝」的既有描述，Task 3/4 新增了列表排序与更新时间列，Task 5 修正了轮询行为。这些文字若不同步，后续会话会照着错描述改代码。

**Files:**
- Modify: `features/25-runtime-platform-and-ui.md:13`
- Modify: `features/25-runtime-platform-and-ui.md:37`
- Modify: `CODE_GUIDE.md:152`

- [ ] **Step 1: 更新功能状态**

`features/25-runtime-platform-and-ui.md:13` 的 25.7 描述改为：

```
| 25.7 | 数据源 JSON 导入导出、无凭证导出、按名称单选/多选导出、整批事务与重名随机后缀改名 | 集成测试完成 |
```

`:37` 的验证说明改为：

```
- 25.7：`tests/test_datasource/test_transfer.py` 和 `tests/test_api/test_datasource_import_export.py` 验证动态方言、脱敏导出、按名称导出与去重、未知名称 404、multipart 上传、当前租户限定、403、格式/大小边界、重名随机后缀改名与改名映射回传、`updated_at` 倒序且不进导出；浏览器实际选择文件、勾选导出与下载。
```

- [ ] **Step 2: 更新代码指南**

`CODE_GUIDE.md:152` 中「重名整批拒绝」改为：

```
重名时按 6 位 base36 随机后缀改名并在响应回传映射，不做静默覆盖
```

- [ ] **Step 3: 提交**

```bash
git add features/25-runtime-platform-and-ui.md CODE_GUIDE.md
git commit -m "docs: 同步数据源导入导出与列表排序的功能描述"
```

---

## 完成标准

- [ ] `.venv/Scripts/python.exe -m pytest -m "not live_llm" -q` 全绿（基线 1956 passed，21 skipped）
- [ ] `.venv/Scripts/python.exe -m pytest tests/test_frontend -q` 全绿
- [ ] `cd frontend && npm run build` 通过
- [ ] `spec/37-runtime-platform-and-ui.md` 的约束与实现一致，无残留旧描述
- [ ] 导出文件仍不含任何凭证、`updated_at` 与同步状态

## 已知边界（不掩盖）

- 改名冲突检测依赖进程内 `_sources` 快照，数据库唯一约束是并发兜底。当前为单 Worker 部署；多 Worker 下极小概率的并发撞名仍会返回 409，与 `spec/37` 记录的「多 Worker 属独立部署验收项」一致，本轮不扩大范围。
- 本轮不做数据源列表的服务端排序参数化，排序固定为 `updated_at DESC`。
- 本轮不做按 dialect/host 等条件批量选择，也不改导出文件格式版本号。
