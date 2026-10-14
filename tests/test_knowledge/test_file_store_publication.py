"""901.4-b FileStore 出版路径：版本台账、失败关闭与前版本查询。"""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import datetime
from types import ModuleType, SimpleNamespace
from typing import TYPE_CHECKING
from unittest.mock import AsyncMock

import pytest

if TYPE_CHECKING:
    from src.knowledge.file_store import FileStore

_PUBLICATION = {
    "source_version": "ab" * 32,
    "source_revision_hash": "cd" * 32,
    "publication_basis": "claimed",
}


# 作用：构造带脚本化响应的 FileStore 测试替身。
# Args: monkeypatch - 补丁夹具；fetchrow_results - fetchrow 依序返回行（可含 None）；
#     fetch_result - fetch 返回行；missing - 是否模拟 PostgreSQL 未配置；
#     stub_ensure - 是否以替身表示初始化就绪。
# Returns: (store, connection) 二元组；未配置数据库时 connection 为 None。
def _store(
    monkeypatch: pytest.MonkeyPatch,
    *,
    fetchrow_results: list[dict[str, object] | None] | None = None,
    fetch_result: list[dict[str, object]] | None = None,
    missing: bool = False,
    stub_ensure: bool = True,
) -> tuple[FileStore, object]:
    """离线构造 FileStore：_ensure、_current_identity、角色与 _connect 全部注入替身。"""
    import src.api.auth as auth_module
    import src.knowledge.file_store as file_module

    store = file_module.FileStore()
    if stub_ensure:
        # 初始化就绪语义由就绪重试组用真实 _ensure 单独验证。
        monkeypatch.setattr(store, "_ensure", AsyncMock(return_value=True))
    monkeypatch.setattr(file_module, "_current_identity", lambda: (7, 9))
    monkeypatch.setattr(auth_module, "get_current_role", lambda: "analyst")
    if missing:
        connection = None
    else:
        connection = SimpleNamespace(
            execute=AsyncMock(return_value="UPDATE 1"),
            fetchrow=AsyncMock(side_effect=list(fetchrow_results or [])),
            fetch=AsyncMock(return_value=list(fetch_result or [])),
        )

    # 作用：提供 FileStore 池化连接测试上下文。
    # Args: 无。
    # Returns: 测试事务范围内的数据库连接。
    @asynccontextmanager
    async def scoped_connection() -> AsyncIterator[object]:
        """只交出脚本化连接，不建立任何真实池事务。"""
        yield connection

    monkeypatch.setattr(store, "_connect", scoped_connection)
    return store, connection


# 作用：构造按 SQL 排序键模拟返回顺序的连接替身（离线反序数据）。
# Args: monkeypatch - 补丁夹具；rows - 物理提交序行（id 升序）；insert_id - INSERT 主键。
# Returns: (store, connection) 二元组。
def _committed_order_store(
    monkeypatch: pytest.MonkeyPatch,
    rows: list[dict[str, object]] | None = None,
    *,
    insert_id: int = 900,
) -> tuple[FileStore, object]:
    """模拟数据库排序语义：id 倒序代表提交追加序，uploaded_at 反序仅作对照。"""
    import src.api.auth as auth_module
    import src.knowledge.file_store as file_module

    fixture = list(rows or [])
    store = file_module.FileStore()
    monkeypatch.setattr(store, "_ensure", AsyncMock(return_value=True))
    monkeypatch.setattr(file_module, "_current_identity", lambda: (7, 9))
    monkeypatch.setattr(auth_module, "get_current_role", lambda: "analyst")

    # 作用：按 SQL 明示排序键模拟数据库返回顺序。
    # Args: sql - 查询文本。
    # Returns: 排序后的行副本列表。
    def ordered(sql: str) -> list[dict[str, object]]:
        """id DESC 走提交追加序；其余按 uploaded_at 反序以证明两条语义会分叉。"""
        if "id DESC" in sql:
            return sorted(fixture, key=lambda row: row["id"], reverse=True)
        return sorted(fixture, key=lambda row: row["uploaded_at"], reverse=True)

    # 作用：前驱查询按排序取首行；INSERT 返回主键；其余返回空。
    # Args: sql - 查询文本；params - 绑定参数。
    # Returns: 行字典或 None。
    async def fetchrow(sql: str, *params: object) -> dict[str, object] | None:
        """按语句分流：前驱查询给排序首行，INSERT 给固定主键，其余 None。"""
        if "FROM knowledge_files AS k" in sql:
            ordered_rows = ordered(sql)
            return dict(ordered_rows[0]) if ordered_rows else None
        if "RETURNING id" in sql:
            return {"id": insert_id}
        return None

    # 作用：版本列表查询按排序截断返回。
    # Args: sql - 查询文本；params - 绑定参数（末位为 limit）。
    # Returns: 行字典列表。
    async def fetch(sql: str, *params: object) -> list[dict[str, object]]:
        """按 SQL 排序截取前 limit 行，供提交序断言。"""
        return [dict(row) for row in ordered(sql)[: int(params[-1])]]

    connection = SimpleNamespace(
        execute=AsyncMock(return_value="SELECT 1"),
        fetchrow=AsyncMock(side_effect=fetchrow),
        fetch=AsyncMock(side_effect=fetch),
    )

    # 作用：提供 FileStore 池化连接测试上下文。
    # Args: 无。
    # Returns: 测试事务范围内的数据库连接。
    @asynccontextmanager
    async def scoped_connection() -> AsyncIterator[object]:
        """只交出脚本化连接，不建立任何真实池事务。"""
        yield connection

    monkeypatch.setattr(store, "_connect", scoped_connection)
    return store, connection


# 作用：为真实 _connect 链路注入可信身份（租户/用户/角色）。
# Args: monkeypatch - 补丁夹具；file_module - 知识文件存储模块。
# Returns: 无返回值。
def _patch_identity(monkeypatch: pytest.MonkeyPatch, file_module: ModuleType) -> None:
    """只替换身份解析入口；_connect 与 pg_connection 仍走真实源码路径（假依赖）。"""
    import src.api.auth as auth_module

    monkeypatch.setattr(file_module, "_current_identity", lambda: (7, 9))
    monkeypatch.setattr(auth_module, "get_current_role", lambda: "analyst")


# 作用：提供空事务上下文，模拟 asyncpg 连接的事务入口。
# Args: 无。
# Returns: 异步上下文迭代器。
@asynccontextmanager
async def _noop_transaction() -> AsyncIterator[None]:
    """只放行事务体，不开启真实事务、不模拟提交。"""
    yield


class TestKnowledgeFileStorePublication:
    """901.4-b 知识文件出版边界：保存/前版本/就绪重试/提交序/连接边界，全部离线假依赖。"""

    # 出版保存必须原子校验前版本并沉淀不可变版本行。

    # 作用：出版保存写入版本列、pending 状态并挂接前版本。
    # Args: monkeypatch - 补丁夹具。
    # Returns: 无返回值。
    async def test_publication_save_writes_version_and_supersedes(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """命名锁先行、前版本按 id 序查询、INSERT 绑定版本列与直接前驱 id。"""
        # Arrange
        store, connection = _store(
            monkeypatch,
            fetchrow_results=[{"id": 99}, None, {"id": 812}],
        )
        # Act
        file_id = await store.save(
            "demo.md",
            b"content",
            knowledge_scope="private",
            publication=_PUBLICATION,
            require_durable=True,
        )
        # Assert
        assert file_id == 812
        # 命名锁以独立语句先行执行，不依赖查询计划是否触达 CTE。
        lock_sql = connection.execute.await_args_list[0].args[0]
        assert "pg_advisory_xact_lock" in lock_sql
        assert connection.execute.await_args_list[0].args[1] == "private|7|9|demo.md"
        prev_sql = connection.fetchrow.await_args_list[0].args[0]
        assert "pg_advisory_xact_lock" not in prev_sql
        insert_sql = connection.fetchrow.await_args_list[2].args[0]
        insert_params = connection.fetchrow.await_args_list[2].args[1:]
        assert "source_version" in insert_sql and "publication_json" in insert_sql
        assert "index_state" in insert_sql
        assert _PUBLICATION["source_version"] in insert_params
        assert 99 in insert_params

    # 作用：同文档同版本已存在时结构化拒绝并携带恢复信息。
    # Args: monkeypatch - 补丁夹具。
    # Returns: 无返回值。
    async def test_duplicate_version_raises_structured(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """重复出版必须抛 FileStoreDuplicateVersionError，并携带既有 file_id 与索引状态。"""
        # Arrange
        import src.knowledge.file_store as file_module

        store, _ = _store(
            monkeypatch,
            fetchrow_results=[{"id": 99}, {"id": 99, "index_state": "failed"}],
        )
        # Act + Assert：重复版本在写入前结构化拒绝，恢复信息原样透出。
        with pytest.raises(file_module.FileStoreDuplicateVersionError) as caught:
            await store.save(
                "demo.md",
                b"content",
                knowledge_scope="private",
                publication=_PUBLICATION,
                require_durable=True,
            )
        assert caught.value.file_id == 99
        assert caught.value.index_state == "failed"

    # 作用：旧保存路径无 publication 时保持返回 None 兼容。
    # Args: monkeypatch - 补丁夹具。
    # Returns: 无返回值。
    async def test_legacy_save_returns_none_without_database(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """无 publication 且无库时旧路径不抛错，仍以 None 表达未持久化。"""
        # Arrange
        store, _ = _store(monkeypatch, missing=True)
        # Act
        result = await store.save("demo.md", b"content", knowledge_scope="private")
        # Assert
        assert result is None

    # 作用：出版路径要求持久化时无数据库必须失败关闭。
    # Args: monkeypatch - 补丁夹具。
    # Returns: 无返回值。
    async def test_publication_save_fails_closed_without_database(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """require_durable 且无库时必须 FileStoreUnavailableError，不得静默落盘。"""
        # Arrange
        import src.knowledge.file_store as file_module

        store, _ = _store(monkeypatch, missing=True)
        # Act + Assert：存储缺失时出版保存失败关闭。
        with pytest.raises(file_module.FileStoreUnavailableError):
            await store.save(
                "demo.md",
                b"content",
                knowledge_scope="private",
                publication=_PUBLICATION,
                require_durable=True,
            )

    # 作用：写入范围权限被拒时出版保存失败关闭且不触数据库。
    # Args: monkeypatch - 补丁夹具。
    # Returns: 无返回值。
    async def test_publication_save_denied_scope_fails_closed(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """租户策略拒绝写范围时抛 PermissionError，锁与前版本查询都不得发生。"""
        # Arrange
        import src.app_context as app_context_module

        store, connection = _store(monkeypatch, fetchrow_results=[{"id": 1}])
        monkeypatch.setattr(
            app_context_module,
            "get_tenant_policy",
            lambda: SimpleNamespace(can_write_scope=lambda *args, **kwargs: False),
        )
        # Act + Assert：权限拒绝必须在任何 SQL 之前失败关闭。
        with pytest.raises(PermissionError):
            await store.save(
                "demo.md",
                b"content",
                knowledge_scope="private",
                publication=_PUBLICATION,
                require_durable=True,
            )
        assert connection.execute.await_count == 0
        assert connection.fetchrow.await_count == 0

    # 作用：出版补丁缺 source_version 时在写库前失败关闭。
    # Args: monkeypatch - 补丁夹具。
    # Returns: 无返回值。
    async def test_publication_save_requires_source_version(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """空 source_version 属调用方错误：写入方法直接抛 ValueError 且不取命名锁。"""
        # Arrange
        store, connection = _store(monkeypatch)
        publication = {"source_revision_hash": "cd" * 32, "publication_basis": "claimed"}
        # Act + Assert：缺版本号的出版补丁在命名锁之前被拒绝。
        with pytest.raises(ValueError):
            await store._save_publication_row(
                connection,
                "demo.md",
                b"content",
                ext=".md",
                knowledge_scope="private",
                datasource="",
                tag_ids=None,
                tenant_id=7,
                user_id=9,
                publication=publication,
            )
        assert connection.execute.await_count == 0

    # 前版本查询失败不能当成首次版本。

    # 作用：无数据库时前版本查询失败关闭，不返回空列表。
    # Args: monkeypatch - 补丁夹具。
    # Returns: 无返回值。
    async def test_latest_versions_fails_closed_without_database(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """无库必须抛 FileStoreUnavailableError，不得让未知前驱被当成首版。"""
        # Arrange
        import src.knowledge.file_store as file_module

        store, _ = _store(monkeypatch, missing=True)
        # Act + Assert：存储缺失时不得回传空列表冒充首版。
        with pytest.raises(file_module.FileStoreUnavailableError):
            await store.get_latest_versions("demo.md", knowledge_scope="private")

    # 作用：前版本按提交追加序（id 倒序）返回且带版本列。
    # Args: monkeypatch - 补丁夹具。
    # Returns: 无返回值。
    async def test_latest_versions_returns_rows(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """前版本按 id 倒序（提交追加序）返回，并携带版本与索引状态列。"""
        # Arrange
        rows = [
            {
                "id": 8,
                "source_version": "bb",
                "source_revision_hash": "dd",
                "publication_json": "{}",
                "index_state": "ready",
            },
            {
                "id": 7,
                "source_version": "aa",
                "source_revision_hash": "cc",
                "publication_json": "{}",
                "index_state": "failed",
            },
        ]
        store, connection = _store(monkeypatch, fetch_result=rows)
        # Act
        result = await store.get_latest_versions("demo.md", knowledge_scope="private", limit=2)
        # Assert
        assert [row["id"] for row in result] == [8, 7]
        assert "ORDER BY id DESC" in connection.fetch.await_args.args[0]

    # 作用：按精确文件 ID 回写索引状态，不误伤同名历史。
    # Args: monkeypatch - 补丁夹具。
    # Returns: 无返回值。
    async def test_mark_index_state_uses_exact_identity(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """回写条件锚定精确 id，不得误伤同名历史版本。"""
        # Arrange
        store, connection = _store(monkeypatch)
        # Act
        result = await store.mark_index_state(17, "ready", knowledge_scope="private")
        # Assert
        assert result is True
        sql = connection.execute.await_args.args[0]
        params = connection.execute.await_args.args[1:]
        assert "WHERE id = $1" in sql
        assert params[0] == 17 and params[1] == "ready"

    # 作用：空 sidecar 状态值失败关闭，防止写脏台账。
    # Args: monkeypatch - 补丁夹具。
    # Returns: 无返回值。
    async def test_mark_index_state_rejects_unknown_state(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """未知索引状态在触库前以 ValueError 失败关闭。"""
        # Arrange
        store, _ = _store(monkeypatch)
        # Act + Assert：非法状态不得写入台账。
        with pytest.raises(ValueError):
            await store.mark_index_state(17, "done", knowledge_scope="private")

    # 作用：前版本查询遇未预期数据库异常时失败关闭。
    # Args: monkeypatch - 补丁夹具。
    # Returns: 无返回值。
    async def test_latest_versions_wraps_unexpected_failure(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """查询异常必须包装为 FileStoreUnavailableError，不得回传空列表。"""
        # Arrange
        import src.knowledge.file_store as file_module

        store, connection = _store(monkeypatch)
        connection.fetch = AsyncMock(side_effect=RuntimeError("查询中断"))
        # Act + Assert：底层异常统一按存储不可用失败关闭。
        with pytest.raises(file_module.FileStoreUnavailableError):
            await store.get_latest_versions("demo.md", knowledge_scope="private")

    # 作用：无数据库时索引状态回写返回 False 而不是抛错。
    # Args: monkeypatch - 补丁夹具。
    # Returns: 无返回值。
    async def test_mark_index_state_returns_false_without_database(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """索引状态属出版后置步骤：存储缺失时以 False 交由上传层决策。"""
        # Arrange
        store, _ = _store(monkeypatch, missing=True)
        # Act
        result = await store.mark_index_state(17, "ready", knowledge_scope="private")
        # Assert
        assert result is False

    # 作用：索引状态回写遇数据库异常时吞错返回 False。
    # Args: monkeypatch - 补丁夹具。
    # Returns: 无返回值。
    async def test_mark_index_state_swallows_execute_failure(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """回写失败不得上抛给上传主流程，由 False 表达未更新。"""
        # Arrange
        store, connection = _store(monkeypatch)
        connection.execute = AsyncMock(side_effect=RuntimeError("回写中断"))
        # Act
        result = await store.mark_index_state(17, "ready", knowledge_scope="private")
        # Assert
        assert result is False

    # 出版路径要求真实持久化：初始化失败不得冒充就绪，重试可恢复。

    # 作用：DDL 失败后出版保存失败关闭，旧路径在同样状态下仍返回 None 兼容。
    # Args: monkeypatch - 补丁夹具。
    # Returns: 无返回值。
    async def test_publication_save_fails_closed_when_schema_not_ready(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """初始化不就绪时出版保存失败关闭，同态 legacy 路径仍返回 None 兼容。"""
        # Arrange
        import src.knowledge.file_store as file_module

        store, connection = _store(monkeypatch, fetchrow_results=[{"id": 812}], stub_ensure=False)
        legacy_store, _ = _store(monkeypatch, missing=True, stub_ensure=False)
        monkeypatch.setattr(
            file_module,
            "get_settings",
            lambda: SimpleNamespace(database_url="postgresql://sandbox/knowledge"),
        )

        # 作用：模拟数据库初始化失败。
        # Args: *args - pg_connection 位置参数；**kwargs - pg_connection 关键字参数。
        # Returns: 异步上下文迭代器；进入时恒抛 RuntimeError。
        @asynccontextmanager
        async def failing_connection(*args: object, **kwargs: object) -> AsyncIterator[object]:
            """首次取连接即失败，模拟 DDL 未建成。"""
            raise RuntimeError("ddl failed")
            yield  # pragma: no cover

        monkeypatch.setattr(file_module, "pg_connection", failing_connection)
        # Act + Assert：未就绪必须按存储不可用失败关闭，且不触达版本查询。
        with pytest.raises(file_module.FileStoreUnavailableError):
            await store.save(
                "demo.md",
                b"content",
                knowledge_scope="private",
                publication=_PUBLICATION,
                require_durable=True,
            )
        assert connection.fetchrow.await_count == 0
        # 旧路径无 publication 时不抛错，保持返回 None 兼容。
        assert await legacy_store.save("demo.md", b"content", knowledge_scope="private") is None

    # 作用：初始化失败后重试成功即恢复，出版保存继续写入。
    # Args: monkeypatch - 补丁夹具。
    # Returns: 无返回值。
    async def test_publication_save_recovers_after_schema_retry(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """首次 DDL 失败失败关闭，重试就绪后同一出版请求成功写入。"""
        # Arrange
        import src.knowledge.file_store as file_module

        store, _ = _store(
            monkeypatch, fetchrow_results=[None, None, {"id": 903}], stub_ensure=False
        )
        monkeypatch.setattr(
            file_module,
            "get_settings",
            lambda: SimpleNamespace(database_url="postgresql://sandbox/knowledge"),
        )
        attempts = {"count": 0}
        ddl_connection = SimpleNamespace(execute=AsyncMock(return_value="CREATE TABLE"))

        # 作用：首次初始化失败、之后成功。
        # Args: *args - pg_connection 位置参数；**kwargs - pg_connection 关键字参数。
        # Returns: 命中成功分支时的数据库连接。
        @asynccontextmanager
        async def flaky_connection(*args: object, **kwargs: object) -> AsyncIterator[object]:
            """首次抛错、之后出借 DDL 连接，用计数模拟恢复。"""
            attempts["count"] += 1
            if attempts["count"] == 1:
                raise RuntimeError("ddl failed")
            yield ddl_connection

        monkeypatch.setattr(file_module, "pg_connection", flaky_connection)
        # Act + Assert：首次初始化失败必须按存储不可用失败关闭。
        with pytest.raises(file_module.FileStoreUnavailableError):
            await store.save(
                "demo.md",
                b"content",
                knowledge_scope="private",
                publication=_PUBLICATION,
                require_durable=True,
            )
        # Act：重试时 DDL 成功，出版保存继续。
        file_id = await store.save(
            "demo.md",
            b"content",
            knowledge_scope="private",
            publication=_PUBLICATION,
            require_durable=True,
        )
        # Assert
        assert file_id == 903
        assert attempts["count"] >= 2

    # 作用：未配置 PostgreSQL 时出版保存失败关闭而旧路径仍兼容。
    # Args: monkeypatch - 补丁夹具。
    # Returns: 无返回值。
    async def test_publication_save_fails_closed_without_pg_url(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """磁盘回退不得冒充持久化：出版路径必须拒绝，legacy 路径返回 None。"""
        # Arrange
        import src.knowledge.file_store as file_module

        store, _ = _store(monkeypatch, missing=True, stub_ensure=False)
        legacy_store, _ = _store(monkeypatch, missing=True, stub_ensure=False)
        monkeypatch.setattr(file_module, "get_settings", lambda: SimpleNamespace(database_url=""))
        # Act + Assert：无 PG 配置时出版路径失败关闭。
        with pytest.raises(file_module.FileStoreUnavailableError):
            await store.save(
                "demo.md",
                b"content",
                knowledge_scope="private",
                publication=_PUBLICATION,
                require_durable=True,
            )
        # 旧路径无 publication 时保持返回 None 兼容。
        assert await legacy_store.save("demo.md", b"content", knowledge_scope="private") is None

    # 作用：公开初始化失败必须上抛，恢复后重试命中就绪缓存。
    # Args: monkeypatch - 补丁夹具。
    # Returns: 无返回值。
    async def test_initialize_propagates_failure_then_recovers(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """初始化失败不得静默；恢复后再次初始化走就绪缓存短路且不再读配置。"""
        # Arrange
        import src.knowledge.file_store as file_module

        store = file_module.FileStore()
        calls = {"count": 0}

        # 作用：首次调用抛配置错误，之后返回无 PG 的磁盘回退配置。
        # Args: 无。
        # Returns: 含 database_url 的设置替身。
        def flaky_settings() -> SimpleNamespace:
            """用调用计数模拟首次读配置失败、之后回退磁盘配置。"""
            calls["count"] += 1
            if calls["count"] == 1:
                raise RuntimeError("配置读取失败")
            return SimpleNamespace(database_url="")

        monkeypatch.setattr(file_module, "get_settings", flaky_settings)
        # Act + Assert：首次初始化失败上抛且状态不就绪。
        with pytest.raises(RuntimeError):
            await store.initialize()
        # Act：恢复后初始化走磁盘回退并标记就绪。
        await store.initialize()
        # Act：再次初始化命中就绪缓存，不再读取设置。
        await store.initialize()
        # Assert
        assert calls["count"] == 2

    # 前驱与 latest 取已提交追加序（id 倒序），不取事务开始观察时间 uploaded_at。

    _ROWS = [
        {
            "id": 1,
            "source_version": "aa" * 32,
            "source_revision_hash": "cc" * 32,
            "publication_json": "{}",
            "index_state": "ready",
            # B 事务开始晚（uploaded_at 即事务开始 NOW），却先持标识锁提交。
            "uploaded_at": datetime(2026, 10, 12, 8, 0, 5),
        },
        {
            "id": 2,
            "source_version": "bb" * 32,
            "source_revision_hash": "dd" * 32,
            "publication_json": "{}",
            "index_state": "ready",
            # A 事务开始早、锁后写入晚：真实追加序在前，uploaded_at 反而更早。
            "uploaded_at": datetime(2026, 10, 12, 8, 0, 0),
        },
    ]

    # 作用：新版本 C 的直接前驱须为最近提交的 A(id=2)，不得回指 B(id=1)。
    # Args: monkeypatch - 补丁夹具。
    # Returns: 无返回值。
    async def test_prev_version_follows_committed_order(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """前驱取提交追加序首行 A(id=2)，与 uploaded_at 反序结果分叉。"""
        # Arrange
        store, connection = _committed_order_store(monkeypatch, self._ROWS)
        # Act
        file_id = await store.save(
            "demo.md",
            b"content",
            knowledge_scope="private",
            publication=_PUBLICATION,
            require_durable=True,
        )
        # Assert：INSERT 绑定的前驱必须是 id=2（A），不是 uploaded_at 更晚的 id=1（B）。
        assert file_id == 900
        insert_params = connection.fetchrow.await_args_list[2].args[1:]
        assert 2 in insert_params and 1 not in insert_params

    # 作用：latest 必须按追加序返回 [A(id=2), B(id=1)]，不得按 uploaded_at 反排。
    # Args: monkeypatch - 补丁夹具。
    # Returns: 无返回值。
    async def test_latest_versions_follows_committed_order(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """latest 返回 [A(id=2), B(id=1)]，以提交追加序而非事务观察时间排序。"""
        # Arrange
        store, _ = _committed_order_store(monkeypatch, self._ROWS)
        # Act
        result = await store.get_latest_versions("demo.md", knowledge_scope="private", limit=2)
        # Assert
        assert [row["id"] for row in result] == [2, 1]

    # 持久化连接边界：失败重抛、未配置回退、断连恢复与已连接复用，全部离线假依赖（不连真库）。

    # 作用：连接池借出失败时 _connect 必须原样重抛。
    # Args: monkeypatch - 补丁夹具。
    # Returns: 无返回值。
    async def test_connect_reraises_pool_failure(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """获取连接失败不得被吞成 None 或空结果，必须让出版路径失败关闭。"""
        # Arrange
        import src.knowledge.file_store as file_module
        import src.memory.pg_pool as pg_pool_module

        store = file_module.FileStore()
        _patch_identity(monkeypatch, file_module)
        monkeypatch.setattr(
            file_module,
            "get_settings",
            lambda: SimpleNamespace(database_url="postgresql://sandbox/knowledge"),
        )

        # 作用：模拟 asyncpg 池借出连接即失败。
        # Args: 无。
        # Returns: 异步上下文迭代器；进入时恒抛 RuntimeError。
        @asynccontextmanager
        async def failing_acquire() -> AsyncIterator[object]:
            """假池 acquire：进入即抛错，从不真正建连。"""
            raise RuntimeError("连接获取失败")
            yield  # pragma: no cover

        monkeypatch.setattr(
            pg_pool_module,
            "get_pg_pool",
            AsyncMock(return_value=SimpleNamespace(acquire=failing_acquire)),
        )
        # Act + Assert：真实 pg_connection 与 _connect 都不得把失败降级为无库。
        with pytest.raises(RuntimeError):
            async with store._connect():
                pass

    # 作用：未配置 PostgreSQL 时 _connect 交出 None 且不尝试建连。
    # Args: monkeypatch - 补丁夹具。
    # Returns: 无返回值。
    async def test_connect_falls_back_without_database_url(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """磁盘回退契约：无 PG 配置只交出 None，绝不调用连接池。"""
        # Arrange
        import src.knowledge.file_store as file_module
        import src.memory.pg_pool as pg_pool_module

        store = file_module.FileStore()
        _patch_identity(monkeypatch, file_module)
        monkeypatch.setattr(file_module, "get_settings", lambda: SimpleNamespace(database_url=""))
        pool_factory = AsyncMock()
        monkeypatch.setattr(pg_pool_module, "get_pg_pool", pool_factory)
        # Act
        async with store._connect() as conn:
            # Assert
            assert conn is None
        # Assert
        pool_factory.assert_not_awaited()

    # 作用：首次断连失败后重试恢复，并复用同一已连接对象。
    # Args: monkeypatch - 补丁夹具。
    # Returns: 无返回值。
    async def test_connect_recovers_and_reuses_pooled_connection(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """失败重抛后可重试成功；池内同一已连接对象必须原样交出而非重建。"""
        # Arrange
        import src.knowledge.file_store as file_module
        import src.memory.pg_pool as pg_pool_module

        store = file_module.FileStore()
        _patch_identity(monkeypatch, file_module)
        monkeypatch.setattr(
            file_module,
            "get_settings",
            lambda: SimpleNamespace(database_url="postgresql://sandbox/knowledge"),
        )
        conn = SimpleNamespace(
            execute=AsyncMock(return_value="SELECT 1"),
            transaction=_noop_transaction,
        )
        attempts = {"count": 0}

        # 作用：首次借出失败、之后复用同一已连接对象。
        # Args: 无。
        # Returns: 连接借用上下文。
        @asynccontextmanager
        async def flaky_acquire() -> AsyncIterator[object]:
            """假池 acquire：首次抛错、之后每次出借同一内存连接。"""
            attempts["count"] += 1
            if attempts["count"] == 1:
                raise RuntimeError("连接获取失败")
            yield conn

        monkeypatch.setattr(
            pg_pool_module,
            "get_pg_pool",
            AsyncMock(return_value=SimpleNamespace(acquire=flaky_acquire)),
        )
        # Act + Assert：首次断连失败原样上抛。
        with pytest.raises(RuntimeError):
            async with store._connect():
                pass
        # Act + Assert：恢复后连续两次借用都是同一连接对象。
        async with store._connect() as first:
            assert first is conn
        async with store._connect() as second:
            assert second is conn
        # Assert
        assert attempts["count"] == 3

    # 作用：出版保存经真实源码 _connect 上下文路径与假池依赖完成写入。
    # Args: monkeypatch - 补丁夹具。
    # Returns: 无返回值。
    async def test_publication_save_uses_source_connect_path_with_fake_pool(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """仅真实源码 context 路径 + Fake 依赖（非真实 PG）：先注入 RLS 身份再取锁写入。"""
        # Arrange
        import src.knowledge.file_store as file_module
        import src.memory.pg_pool as pg_pool_module

        store = file_module.FileStore()
        _patch_identity(monkeypatch, file_module)
        monkeypatch.setattr(store, "_ensure", AsyncMock(return_value=True))
        monkeypatch.setattr(
            file_module,
            "get_settings",
            lambda: SimpleNamespace(database_url="postgresql://sandbox/knowledge"),
        )

        # 作用：按语句分派出版保存所需的连接响应。
        # Args: sql - 语句；params - 绑定参数。
        # Returns: 前驱行、插入主键或 None。
        async def fetchrow(sql: str, *params: object) -> dict[str, object] | None:
            """按语句分流：前驱查询给既有行，INSERT 给固定主键，其余 None。"""
            if "FROM knowledge_files AS k" in sql:
                return {"id": 99}
            if "RETURNING id" in sql:
                return {"id": 812}
            return None

        conn = SimpleNamespace(
            execute=AsyncMock(return_value="SELECT 1"),
            fetchrow=AsyncMock(side_effect=fetchrow),
            transaction=_noop_transaction,
        )

        # 作用：出借已连接的假池连接替身。
        # Args: 无。
        # Returns: 连接借用上下文。
        @asynccontextmanager
        async def acquire() -> AsyncIterator[object]:
            """假池 acquire：只出借脚本化内存连接对象。"""
            yield conn

        monkeypatch.setattr(
            pg_pool_module,
            "get_pg_pool",
            AsyncMock(return_value=SimpleNamespace(acquire=acquire)),
        )
        # Act
        file_id = await store.save(
            "demo.md",
            b"content",
            knowledge_scope="private",
            publication=_PUBLICATION,
            require_durable=True,
        )
        # Assert
        assert file_id == 812
        statements = [call.args[0] for call in conn.execute.await_args_list]
        assert "app.current_tenant_id" in statements[0]
        assert conn.execute.await_args_list[0].args[1:] == ("7", "9", "analyst")
        assert "pg_advisory_xact_lock" in statements[1]
