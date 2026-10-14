"""901.6 精确原文读取独立验收：FileStore.get_exact_version（spec §12.11）。

边界：
  * 独立文件：不导入 E 的 owner 测试或任何既有测试模块，自建假连接替身；真 PG 可见性
    矩阵由单独排队文件覆盖（本文件只断言 SQL 形状/身份参数，不冒充真实库行为）。
  * 新异常类 FileStoreIntegrityError 一律经模块属性惰性获取：E 未落地时按“方法/属性缺失”
    红，不因顶层 import 失败造成 collection 空文件。
  * 仅离线：不连接数据库/网络/配置；全部边界以 monkeypatch 注入。
"""

from __future__ import annotations

import asyncio
import hashlib
import inspect
import json
import logging
import re
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock

import pytest
from structlog.testing import capture_logs

import src.knowledge.file_store as file_module
from src.api import auth as api_auth
from src.knowledge.file_store import FileStore, FileStoreUnavailableError

# 合同固定返回键（spec §12.11 / approved return_contract）。
_EXPECTED_KEYS = frozenset(
    {
        "id",
        "filename",
        "content_type",
        "file_data",
        "size",
        "uploaded_at",
        "scope",
        "datasource",
        "tag_ids",
        "tenant_id",
        "owner_user_id",
        "source_version",
        "source_revision_hash",
        "index_state",
        "publication",
    }
)
# 合成哨兵：全部非真实凭据，仅用于日志探针与内容定位。
_SECRET_DOC = "SYNTHETIC-DOCID-NOT-REAL-2f7c"
_SECRET_BODY = b"SYNTHETIC-BODY-NOT-REAL-9d31"
_SECRET_DSN = "postgres://demo:SYNTHETIC-NOT-REAL@127.0.0.1:1/demo"
_SECRET_PLAIN = "SYNTHETIC-CONNECTION-NOTE-NOT-REAL-4a81"
_ROW_VERSION = "a" * 64
_ROW_HASH = "b" * 64
_MOMENT = datetime(2026, 10, 1, 12, 0, tzinfo=timezone.utc)
_BASE_DOC = "document:private:7:9:acceptance.txt"


# 作用：计算内容 sha256 十六进制。
# Args: content - 原始字节。
# Returns: 64 位小写十六进制摘要。
def _sha(content: bytes) -> str:
    """计算内容 sha256 十六进制。"""
    return hashlib.sha256(content).hexdigest()


# 作用：惰性取新异常类，未落地时抛出 AttributeError 形成可定位红证。
# Args: 无。
# Returns: FileStoreIntegrityError 类型。
def _integrity_error() -> type[BaseException]:
    """惰性取新异常类：未落地时抛出 AttributeError，形成可定位红证。"""
    return file_module.FileStoreIntegrityError


# 作用：构造与上传出版补丁同形的合成 publication 映射。
# Args: doc_id - 合成文档标识；version - 服务端版本；revision_hash - 原文摘要。
# Returns: 合成 publication 字典。
def _publication(doc_id: str, version: str, revision_hash: str) -> dict[str, Any]:
    """构造与上传出版补丁同形的合成 publication 映射。"""
    return {
        "source_doc_id": doc_id,
        "source_version": version,
        "source_revision_hash": revision_hash,
        "publication_basis": "unknown",
        "publication_precision": "unknown",
        "acceptance_note": "synthetic",
    }


# 作用：构造 knowledge_files 行形制字典（字段名与既有读回一致）。
# Args: 关键字见签名（doc_id/version/revision_hash/content/file_id/scope/
#     tenant_id/owner_user_id/size/file_data/index_state/publication_json）。
# Returns: 行形制字典。
def _row(
    *,
    doc_id: str = _BASE_DOC,
    revision_hash: str = _ROW_HASH,
    content: bytes = _SECRET_BODY,
    version: str = _ROW_VERSION,
    file_id: int = 41,
    scope: str = "private",
    tenant_id: int = 7,
    owner_user_id: int = 9,
    size: int | None = None,
    file_data: Any = None,
    index_state: str = "ready",
    publication_json: str | None = None,
) -> dict[str, Any]:
    """构造 knowledge_files 行形制字典（字段名与既有读回一致）。"""
    return {
        "id": file_id,
        "filename": "acceptance-original.txt",
        "content_type": "text/plain",
        "file_data": content if file_data is None else file_data,
        "size": len(content) if size is None else size,
        "uploaded_at": _MOMENT,
        "knowledge_scope": scope,
        "datasource": "acceptance-ds",
        "tag_ids": [11, 12],
        "tenant_id": tenant_id,
        "user_id": owner_user_id,
        "source_version": version,
        "source_revision_hash": revision_hash,
        "publication_json": (
            publication_json
            if publication_json is not None
            else json.dumps(
                _publication(doc_id, version, revision_hash), ensure_ascii=False, sort_keys=True
            )
        ),
        "index_state": index_state,
    }


class _ScriptedConnection:
    """脚本化假连接：记录 SQL/参数，按序返回行或抛出注入异常。"""

    # 作用：注入行集合与可选异常。
    # Args: self - 假连接；rows - 脚本行；error - 注入异常。
    # Returns: 无返回值。
    def __init__(
        self, *, rows: list[dict[str, Any]] | None = None, error: BaseException | None = None
    ) -> None:
        """注入行集合与可选异常。"""
        self.rows = list(rows or [])
        self.error = error
        self.queries: list[tuple[str, tuple[Any, ...]]] = []

    # 作用：返回全部脚本行（模拟 LIMIT 2 结果集）。
    # Args: self - 假连接；sql/params - 记录用查询与参数。
    # Returns: 行列表；注入异常时抛出。
    async def fetch(self, sql: str, *params: Any) -> list[dict[str, Any]]:
        """返回全部脚本行（模拟 LIMIT 2 结果集）。"""
        self.queries.append((sql, params))
        if self.error is not None:
            raise self.error
        return list(self.rows)

    # 作用：返回首行或 None（模拟单行读取）。
    # Args: self - 假连接；sql/params - 记录用查询与参数。
    # Returns: 首行或 None；注入异常时抛出。
    async def fetchrow(self, sql: str, *params: Any) -> dict[str, Any] | None:
        """返回首行或 None（模拟单行读取）。"""
        self.queries.append((sql, params))
        if self.error is not None:
            raise self.error
        return self.rows[0] if self.rows else None

    # 作用：返回脚本行数（模拟计数式多匹配探测）。
    # Args: self - 假连接；sql/params - 记录用查询与参数。
    # Returns: 行数；注入异常时抛出。
    async def fetchval(self, sql: str, *params: Any) -> Any:
        """返回脚本行数（模拟计数式多匹配探测）。"""
        self.queries.append((sql, params))
        if self.error is not None:
            raise self.error
        return len(self.rows)


# 作用：离线构造 FileStore，注入 _ensure/_current_identity/_connect 并计数。
# Args: monkeypatch - 补丁器；connection - 假连接；connect_error - 连接注入异常；
#     ensure_error - 初始化注入异常；identity - 可信租户/用户。
# Returns: (FileStore, 调用计数状态)。
def _install(
    monkeypatch: pytest.MonkeyPatch,
    *,
    connection: Any,
    connect_error: BaseException | None = None,
    ensure_error: BaseException | None = None,
    identity: tuple[int, int] = (7, 9),
) -> tuple[FileStore, dict[str, int]]:
    """离线构造 FileStore：注入 _ensure/_current_identity/_connect 三个边界并计数。"""
    store = FileStore()
    state = {"ensure": 0, "connect": 0}

    # 作用：计数后返回就绪或按注入异常失败。
    # Args: 无。
    # Returns: 是否就绪。
    async def _ensure() -> bool:
        state["ensure"] += 1
        if ensure_error is not None:
            raise ensure_error
        return True

    # 作用：计数后 yield 假连接或按注入异常失败。
    # Args: 无。
    # Returns: 无返回值（异步上下文生成器）。
    @asynccontextmanager
    async def _connect() -> AsyncIterator[Any]:
        state["connect"] += 1
        if connect_error is not None:
            raise connect_error
        yield connection

    monkeypatch.setattr(store, "_ensure", _ensure)
    monkeypatch.setattr(store, "_connect", _connect)
    monkeypatch.setattr(file_module, "_current_identity", lambda: identity)
    return store, state


# 作用：以关键字调用 get_exact_version（惰性属性：方法缺失即红）。
# Args: store - 被调用 FileStore；doc_id/version/revision/scope - 请求四元组。
# Returns: 方法返回值。
async def _call(
    store: FileStore,
    *,
    doc_id: str = _BASE_DOC,
    version: str = _ROW_VERSION,
    revision: str = _ROW_HASH,
    scope: str = "",
) -> Any:
    """以关键字调用 get_exact_version（惰性属性：方法缺失即红）。"""
    return await store.get_exact_version(
        doc_id=doc_id,
        source_version=version,
        source_revision_hash=revision,
        knowledge_scope=scope,
    )


# 作用：拼接 structlog 捕获与 caplog 文本，供敏感探针断言。
# Args: logs - capture_logs 事件；caplog - pytest 日志夹具。
# Returns: 合并文本。
def _logs_text(logs: list[dict[str, Any]], caplog: pytest.LogCaptureFixture) -> str:
    """拼接 structlog 捕获与 caplog 文本，供敏感探针断言。"""
    return repr(logs) + "\n" + caplog.text


# 作用：兼容 datetime 与 isoformat 字符串两种既有返回样式。
# Args: value - 返回的 uploaded_at；expected - 期望时刻。
# Returns: 是否一致。
def _uploaded_at_matches(value: Any, expected: datetime) -> bool:
    """兼容 datetime 与 isoformat 字符串两种既有返回样式。"""
    if isinstance(value, datetime):
        return value == expected
    return str(value) == expected.isoformat()


# 作用：装配真实日志管线到临时文件（不改源码、不读真实配置），返回可恢复快照。
# Args: log_file - 临时日志路径；monkeypatch - 补丁器。
# Returns: (根 handler 快照, 根级别快照, structlog 处理链快照)。
def _configure_real_logging(
    log_file: Path, monkeypatch: pytest.MonkeyPatch
) -> tuple[list[Any], int, list[Any]]:
    """仅注入日志字段的 Settings；全局日志状态由用例在 finally 恢复。"""
    import structlog

    from src import logging_config
    from src.config import Settings

    root = logging.getLogger()
    snapshot = (list(root.handlers), root.level, list(structlog.get_config()["processors"]))
    settings = Settings(log_file=str(log_file), log_format="console", log_level="INFO")
    monkeypatch.setattr(logging_config, "get_settings", lambda: settings)
    logging_config.setup_logging()
    return snapshot


# 作用：恢复根 handler/级别与 structlog 处理链，避免污染联合回归。
# Args: snapshot - _configure_real_logging 返回的快照。
# Returns: 无返回值。
def _restore_logging(snapshot: tuple[list[Any], int, list[Any]]) -> None:
    """setup_logging 会清空根 handler 并原地改处理链，用例结束必须还原。"""
    import structlog

    handlers, level, processors = snapshot
    root = logging.getLogger()
    for handler in root.handlers:
        handler.close()
    root.handlers.clear()
    root.handlers.extend(handlers)
    root.setLevel(level)
    current = structlog.get_config()["processors"]
    if isinstance(current, list):
        current.clear()
        current.extend(processors)


# 作用：拼接公开 cause/context 全程文本，供安全断言。
# Args: root - 顶层异常。
# Returns: 链上各类异常的类型与文本拼接。
def _exception_chain_text(root: BaseException) -> str:
    """只走公开 __cause__/__context__，不采信任何日志字段。"""
    parts: list[str] = []
    current: BaseException | None = root
    while current is not None:
        parts.append(f"{type(current).__name__}: {current}")
        doc = getattr(current, "doc", "")
        if isinstance(doc, str) and doc:
            parts.append(f"{type(current).__name__}.doc: {doc}")
        current = current.__cause__ or current.__context__
    return "\n".join(parts)


# 作用：保留真实 _ensure 缓存命中与真实 _connect，仅 Fake 设置/连接/身份与底层 conn。
# Args: monkeypatch - 补丁器；connection - 真实 _connect 将 yield 的底层连接替身。
# Returns: 装配完成的 FileStore。
def _real_transport_store(monkeypatch: pytest.MonkeyPatch, *, connection: Any) -> FileStore:
    """真实 helper 的日志边界必须被覆盖，不能只在替换 _ensure/_connect 时取证。"""
    store = FileStore()
    store._ready = True
    store._pg_configured = True
    store._schema_ready = True
    stub = SimpleNamespace(database_url="postgresql://sandbox-NOT-REAL@127.0.0.1:1/demo")

    # 作用：yield 脚本连接（最低获取替身）。
    # Args: kwargs - 真实调用方传入的租户/用户/角色。
    # Returns: 无返回值（异步上下文生成器）。
    @asynccontextmanager
    async def _pg_connection(**kwargs: Any) -> AsyncIterator[Any]:
        yield connection

    monkeypatch.setattr(file_module, "get_settings", lambda: stub)
    monkeypatch.setattr(file_module, "pg_connection", _pg_connection)
    monkeypatch.setattr(file_module, "_current_identity", lambda: (7, 9))
    monkeypatch.setattr(api_auth, "get_current_role", lambda: "analyst")
    return store


# 作用：刷新真实 handler 后读取日志文本。
# Args: log_file - 临时日志路径。
# Returns: 日志文件文本。
def _flush_and_read(log_file: Path) -> str:
    """写盘后再断言，避免缓冲造成假绿。"""
    for handler in logging.getLogger().handlers:
        handler.flush()
    return log_file.read_text(encoding="utf-8")


# 作用：支持池化调用（acquire/事务/execute/close）的脚本连接。
class _PoolConnection(_ScriptedConnection):
    """同一条脚本连接同时充当池与连接，供真实 pg_pool 链使用。"""

    # 作用：初始化并记录关闭次数与执行语句。
    # Args: kwargs - 同 _ScriptedConnection（rows/error）。
    # Returns: 无返回值。
    def __init__(self, **kwargs: Any) -> None:
        super().__init__(**kwargs)
        self.closed = 0
        self.executed: list[str] = []

    # 作用：记录并忽略 DDL/身份设置语句。
    # Args: self - 连接；sql - 语句；params - 参数。
    # Returns: None。
    async def execute(self, sql: str, *params: Any) -> None:
        self.executed.append(sql)

    # 作用：最小获取协议：返回 yield 自身的异步上下文。
    # Args: self - 连接。
    # Returns: 异步上下文管理器。
    @asynccontextmanager
    async def acquire(self) -> AsyncIterator[Any]:
        yield self

    # 作用：最小事务协议：空事务。
    # Args: self - 连接。
    # Returns: 异步上下文管理器。
    @asynccontextmanager
    async def transaction(self) -> AsyncIterator[None]:
        yield None

    # 作用：记录关闭次数。
    # Args: self - 连接。
    # Returns: None。
    async def close(self) -> None:
        self.closed += 1


# 作用：健康检查即失败的池替身，可注入清理故障。
class _BrokenPool:
    """acquire 进入即抛合成故障；close 可成功或按注入故障失败。"""

    # 作用：绑定健康检查与清理故障。
    # Args: error - 健康检查注入异常；close_error - 清理注入异常。
    # Returns: 无返回值。
    def __init__(self, error: BaseException, *, close_error: BaseException | None = None) -> None:
        self._error = error
        self._close_error = close_error
        self.closed = 0

    # 作用：进入获取即抛合成故障（不 yield）。
    # Args: self - 池替身。
    # Returns: 异步上下文管理器。
    @asynccontextmanager
    async def acquire(self) -> AsyncIterator[Any]:
        raise self._error
        yield None  # pragma: no cover

    # 作用：记录关闭次数并按注入故障失败。
    # Args: self - 池替身。
    # Returns: None；注入清理故障时抛出。
    async def close(self) -> None:
        self.closed += 1
        if self._close_error is not None:
            raise self._close_error


# 作用：在捕获日志之外构造真实 AppContext 并绑定 pg_pool 入口。
# Args: monkeypatch - 补丁器；database_url - 合成 DSN（默认沙箱非真实）。
# Returns: 真实 AppContext（settings 为合成非真实桩）。
def _bind_real_app_context(
    monkeypatch: pytest.MonkeyPatch,
    *,
    database_url: str = "postgresql://sandbox-NOT-REAL@127.0.0.1:1/demo",
) -> Any:
    """仅 get_app_context 一处替换；资源字典/锁/close_resource 全走真实实现。"""
    from src.app_context import AppContext
    from src.memory import pg_pool as pg_pool_module

    context = AppContext(settings=SimpleNamespace(database_url=database_url, multi_tenant=False))
    monkeypatch.setattr(pg_pool_module, "get_app_context", lambda: context)
    return context


# 作用：把 FileStore 指向合成设置/身份，保留真实 _connect/_ensure 与 pg_pool 链。
# Args: monkeypatch - 补丁器；warmed - 预置缓存标志短路 DDL；database_url - 合成 DSN。
# Returns: 装配好的 FileStore。
def _real_chain_store(
    monkeypatch: pytest.MonkeyPatch,
    *,
    warmed: bool = True,
    database_url: str = "postgresql://sandbox-NOT-REAL@127.0.0.1:1/demo",
) -> FileStore:
    """warmed=False 时走真实初始化路径（用于取消/恢复）。"""
    store = FileStore()
    if warmed:
        store._ready = True
        store._pg_configured = True
        store._schema_ready = True
    stub = SimpleNamespace(database_url=database_url)
    monkeypatch.setattr(file_module, "get_settings", lambda: stub)
    monkeypatch.setattr(file_module, "_current_identity", lambda: (7, 9))
    monkeypatch.setattr(api_auth, "get_current_role", lambda: "analyst")
    return store


class TestExactVersionNotReplacedByLatest:
    """A 组：精确旧版本读取，最新同名/其它通道均不得替代。"""

    # 作用：唯一命中时返回精确版本完整 bytes 与固定 15 键、publication 为解析 dict。
    # Args: self - 用例；monkeypatch - 补丁夹具。
    # Returns: 无返回值；断言失败即红。
    async def test_returns_exact_version_bytes_with_fixed_keys(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        content = _SECRET_BODY
        conn = _ScriptedConnection(rows=[_row(revision_hash=_sha(content), content=content)])
        store, _ = _install(monkeypatch, connection=conn)
        result = await _call(store, revision=_sha(content))
        assert result is not None
        assert set(result.keys()) == _EXPECTED_KEYS
        assert result["id"] == 41
        assert result["filename"] == "acceptance-original.txt"
        assert result["content_type"] == "text/plain"
        assert result["file_data"] == content
        assert isinstance(result["file_data"], bytes)
        assert result["size"] == len(content)
        assert result["scope"] == "private"
        assert result["datasource"] == "acceptance-ds"
        assert result["tag_ids"] == [11, 12]
        assert result["tenant_id"] == 7
        assert result["owner_user_id"] == 9
        assert result["source_version"] == _ROW_VERSION
        assert result["source_revision_hash"] == _sha(content)
        assert result["index_state"] == "ready"
        assert result["publication"] == _publication(_BASE_DOC, _ROW_VERSION, _sha(content))
        assert "publication_json" not in result
        assert _uploaded_at_matches(result["uploaded_at"], _MOMENT)

    # 作用：SQL 必须精确等值 version+hash、LIMIT 2，且无“最新同名”降序回退语义。
    # Args: self - 用例；monkeypatch - 补丁夹具。
    # Returns: 无返回值；形状不符即红。
    async def test_query_is_exact_without_latest_fallback(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        content = _SECRET_BODY
        conn = _ScriptedConnection(rows=[_row(revision_hash=_sha(content), content=content)])
        store, _ = _install(monkeypatch, connection=conn)
        fallback = AsyncMock(side_effect=AssertionError("不得回退最新同名或其它读取通道"))
        monkeypatch.setattr(store, "get_by_name", fallback)
        monkeypatch.setattr(store, "get", fallback)
        await _call(store, revision=_sha(content))
        assert conn.queries, "必须发生一次精确查询"
        sql, params = conn.queries[0]
        assert re.search(r"source_version\s*=\s*\$", sql)
        assert re.search(r"source_revision_hash\s*=\s*\$", sql)
        assert re.search(r"limit\s+2\b", sql, re.IGNORECASE)
        assert re.search(r"order\s+by[^;]*desc", sql, re.IGNORECASE) is None
        assert _ROW_VERSION in params and _sha(content) in params
        assert 7 in params and 9 in params

    # 作用：index_state 照实返回语义由多匹配拒绝优先于任何 ready 冒充共同保证。
    # Args: self - 用例；monkeypatch - 补丁夹具。
    # Returns: 无返回值。
    async def test_index_state_not_promoted_to_ready(self, monkeypatch: pytest.MonkeyPatch) -> None:
        content = _SECRET_BODY
        conn = _ScriptedConnection(
            rows=[
                _row(revision_hash=_sha(content), content=content, index_state="failed"),
                _row(revision_hash=_sha(content), content=content, index_state="failed"),
            ]
        )
        store, _ = _install(monkeypatch, connection=conn)
        with pytest.raises(_integrity_error()):
            await _call(store, revision=_sha(content))

    # 作用：正常路径重复读取不得新增 INFO 及以上日志。
    # Args: self - 用例；monkeypatch/caplog - 夹具。
    # Returns: 无返回值。
    async def test_repeated_reads_emit_no_info(
        self, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
    ) -> None:
        content = _SECRET_BODY
        conn = _ScriptedConnection(rows=[_row(revision_hash=_sha(content), content=content)])
        store, _ = _install(monkeypatch, connection=conn)
        with caplog.at_level(logging.DEBUG), capture_logs() as logs:
            for _ in range(6):
                result = await _call(store, revision=_sha(content))
                assert result is not None
        noisy = [
            event
            for event in logs
            if str(event.get("log_level", "")).lower() in {"info", "warning", "error", "critical"}
        ]
        assert noisy == [], f"正常重复读取出现高阶日志：{noisy}"
        assert not [record for record in caplog.records if record.levelno >= logging.INFO]


class TestVisibilityAndIdentityContract:
    """B 组：可见性谓词与身份契约复用，不扩权。"""

    # 作用：方法签名固定四参数，无 tenant/owner/role 入口。
    # Args: self - 用例。
    # Returns: 无返回值。
    def test_signature_has_no_extra_identity_parameters(self) -> None:
        signature = inspect.signature(FileStore.get_exact_version)
        assert set(signature.parameters) == {
            "self",
            "doc_id",
            "source_version",
            "source_revision_hash",
            "knowledge_scope",
        }
        assert signature.parameters["knowledge_scope"].default == ""

    # 作用：0 行返回 None；SQL 复用可见性谓词（含 super_admin 不扩权）并绑定当前身份。
    # Args: self - 用例；monkeypatch - 补丁夹具。
    # Returns: 无返回值。
    async def test_visible_predicate_reused_and_scope_bound(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        conn = _ScriptedConnection(rows=[])
        store, _ = _install(monkeypatch, connection=conn)
        fallback = AsyncMock(side_effect=AssertionError("0 行不得回退其它读取通道"))
        monkeypatch.setattr(store, "get_by_name", fallback)
        monkeypatch.setattr(store, "get", fallback)
        assert await _call(store, scope="tenant") is None
        sql, params = conn.queries[0]
        assert "current_setting('app.current_role', true) <> 'super_admin'" in sql
        assert "knowledge_scope = 'system'" in sql
        assert "tenant" in params
        assert 7 in params and 9 in params


class TestIntegrityConflicts:
    """C 组：TEXT 元数据三元组、bytes、长度、hash 完整性拒闭。"""

    # 作用：publication source_doc_id 与请求不符必须完整性拒绝。
    # Args: self - 用例；monkeypatch - 补丁夹具。
    # Returns: 无返回值。
    async def test_doc_id_mismatch_rejected(self, monkeypatch: pytest.MonkeyPatch) -> None:
        content = _SECRET_BODY
        revision = _sha(content)
        bad = json.dumps(_publication("document:private:7:9:other.txt", _ROW_VERSION, revision))
        conn = _ScriptedConnection(
            rows=[_row(revision_hash=revision, content=content, publication_json=bad)]
        )
        store, _ = _install(monkeypatch, connection=conn)
        with pytest.raises(_integrity_error()):
            await _call(store, revision=revision)

    # 作用：publication source_version 与列/请求不符必须拒绝。
    # Args: self - 用例；monkeypatch - 补丁夹具。
    # Returns: 无返回值。
    async def test_publication_version_mismatch_rejected(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        content = _SECRET_BODY
        revision = _sha(content)
        bad = json.dumps(_publication(_BASE_DOC, "c" * 64, revision))
        conn = _ScriptedConnection(
            rows=[_row(revision_hash=revision, content=content, publication_json=bad)]
        )
        store, _ = _install(monkeypatch, connection=conn)
        with pytest.raises(_integrity_error()):
            await _call(store, revision=revision)

    # 作用：publication source_revision_hash 与列/请求不符必须拒绝。
    # Args: self - 用例；monkeypatch - 补丁夹具。
    # Returns: 无返回值。
    async def test_publication_hash_mismatch_rejected(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        content = _SECRET_BODY
        revision = _sha(content)
        bad = json.dumps(_publication(_BASE_DOC, _ROW_VERSION, "d" * 64))
        conn = _ScriptedConnection(
            rows=[_row(revision_hash=revision, content=content, publication_json=bad)]
        )
        store, _ = _install(monkeypatch, connection=conn)
        with pytest.raises(_integrity_error()):
            await _call(store, revision=revision)

    # 作用：损坏 JSON 与合法但非 mapping 的 JSON 都必须拒绝而不是崩溃/None。
    # Args: self - 用例；monkeypatch - 补丁夹具；raw - 注入的 publication_json。
    # Returns: 无返回值。
    @pytest.mark.parametrize("raw", ["{not-json", "[1, 2, 3]", '"text"', ""])
    async def test_broken_or_non_mapping_publication_rejected(
        self, monkeypatch: pytest.MonkeyPatch, raw: str
    ) -> None:
        content = _SECRET_BODY
        revision = _sha(content)
        conn = _ScriptedConnection(
            rows=[_row(revision_hash=revision, content=content, publication_json=raw)]
        )
        store, _ = _install(monkeypatch, connection=conn)
        with pytest.raises(_integrity_error()):
            await _call(store, revision=revision)

    # 作用：实际字节 sha256 与请求不符必须拒绝。
    # Args: self - 用例；monkeypatch - 补丁夹具。
    # Returns: 无返回值。
    async def test_file_bytes_hash_mismatch_rejected(self, monkeypatch: pytest.MonkeyPatch) -> None:
        revision = _sha(b"expected-content")
        conn = _ScriptedConnection(rows=[_row(revision_hash=revision, content=b"tampered-content")])
        store, _ = _install(monkeypatch, connection=conn)
        with pytest.raises(_integrity_error()):
            await _call(store, revision=revision)

    # 作用：size 与 bytes 长度不一致必须拒绝。
    # Args: self - 用例；monkeypatch - 补丁夹具。
    # Returns: 无返回值。
    async def test_size_mismatch_rejected(self, monkeypatch: pytest.MonkeyPatch) -> None:
        content = _SECRET_BODY
        revision = _sha(content)
        conn = _ScriptedConnection(
            rows=[_row(revision_hash=revision, content=content, size=len(content) + 1)]
        )
        store, _ = _install(monkeypatch, connection=conn)
        with pytest.raises(_integrity_error()):
            await _call(store, revision=revision)

    # 作用：file_data 非 bytes/memoryview 必须拒绝。
    # Args: self - 用例；monkeypatch - 补丁夹具。
    # Returns: 无返回值。
    async def test_non_bytes_file_data_rejected(self, monkeypatch: pytest.MonkeyPatch) -> None:
        content = _SECRET_BODY
        revision = _sha(content)
        conn = _ScriptedConnection(
            rows=[
                _row(
                    revision_hash=revision,
                    content=content,
                    file_data="text-not-bytes",
                    size=len(content),
                )
            ]
        )
        store, _ = _install(monkeypatch, connection=conn)
        with pytest.raises(_integrity_error()):
            await _call(store, revision=revision)

    # 作用：memoryview 输入必须归一为 bytes 后返回，sha256 与 size 照常校验。
    # Args: self - 用例；monkeypatch - 补丁夹具。
    # Returns: 无返回值。
    async def test_memoryview_normalized_to_bytes(self, monkeypatch: pytest.MonkeyPatch) -> None:
        content = _SECRET_BODY
        revision = _sha(content)
        conn = _ScriptedConnection(
            rows=[
                _row(
                    revision_hash=revision,
                    content=content,
                    file_data=memoryview(content),
                    size=len(content),
                )
            ]
        )
        store, _ = _install(monkeypatch, connection=conn)
        result = await _call(store, revision=revision)
        assert result is not None
        assert isinstance(result["file_data"], bytes)
        assert result["file_data"] == content


class TestBackendFailureAndCancellation:
    """E 组：后端不可用/取消/输入前置校验。"""

    # 作用：连接不可用必须抛 Unavailable，不得返回 None。
    # Args: self - 用例；monkeypatch - 补丁夹具。
    # Returns: 无返回值。
    async def test_unavailable_connection_raises_unavailable(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        store, _ = _install(monkeypatch, connection=None)
        with pytest.raises(FileStoreUnavailableError):
            await _call(store)

    # 作用：建立连接抛错必须收敛为 Unavailable。
    # Args: self - 用例；monkeypatch - 补丁夹具。
    # Returns: 无返回值。
    async def test_connect_failure_raises_unavailable(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        store, _ = _install(
            monkeypatch, connection=None, connect_error=RuntimeError(f"down {_SECRET_DSN}")
        )
        with pytest.raises(FileStoreUnavailableError):
            await _call(store)

    # 作用：查询失败必须收敛为 Unavailable，且异常正文不得携带底层 DSN 哨兵。
    # Args: self - 用例；monkeypatch - 补丁夹具。
    # Returns: 无返回值。
    async def test_query_failure_masks_underlying_text(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        conn = _ScriptedConnection(error=RuntimeError(f"backend {_SECRET_DSN}"))
        store, _ = _install(monkeypatch, connection=conn)
        with pytest.raises(FileStoreUnavailableError) as error:
            await _call(store)
        assert _SECRET_DSN not in str(error.value)

    # 作用：取消必须原样传播，不得包装为 Unavailable 或其它异常。
    # Args: self - 用例；monkeypatch - 补丁夹具。
    # Returns: 无返回值。
    async def test_cancelled_error_propagates(self, monkeypatch: pytest.MonkeyPatch) -> None:
        conn = _ScriptedConnection(error=asyncio.CancelledError())
        store, _ = _install(monkeypatch, connection=conn)
        with pytest.raises(asyncio.CancelledError):
            await _call(store)

    # 作用：非法输入必须在任何 I/O（_ensure/_connect 与查询）之前拒绝。
    # Args: self - 用例；monkeypatch - 补丁夹具；case - 单键覆盖。
    # Returns: 无返回值。
    @pytest.mark.parametrize(
        "case",
        [
            {"doc_id": ""},
            {"doc_id": "   "},
            {"doc_id": 123},
            {"doc_id": True},
            {"doc_id": "x" * 129},
            {"source_version": ""},
            {"source_version": "y" * 65},
            {"source_version": None},
            {"source_revision_hash": "A" * 64},
            {"source_revision_hash": "a" * 63},
            {"source_revision_hash": "a" * 65},
            {"source_revision_hash": "z" * 64},
            {"source_revision_hash": "  "},
            {"source_revision_hash": 123},
            {"knowledge_scope": "public"},
            {"knowledge_scope": "SYSTEM"},
        ],
    )
    async def test_invalid_inputs_rejected_before_any_io(
        self, monkeypatch: pytest.MonkeyPatch, case: dict[str, Any]
    ) -> None:
        conn = _ScriptedConnection(rows=[_row()])
        store, state = _install(monkeypatch, connection=conn)
        kwargs = {
            "doc_id": _BASE_DOC,
            "version": _ROW_VERSION,
            "revision": _ROW_HASH,
            "scope": "",
        }
        kwargs.update(case)
        with pytest.raises((ValueError, TypeError)):
            await _call(store, **kwargs)
        assert state == {"ensure": 0, "connect": 0}
        assert conn.queries == []


class TestLoggingProbes:
    """F 组：敏感探针与异常日志脱敏。"""

    # 作用：成功路径日志不得出现合成 doc_id 与正文哨兵。
    # Args: self - 用例；monkeypatch/caplog - 夹具。
    # Returns: 无返回值。
    async def test_sensitive_values_absent_on_success(
        self, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
    ) -> None:
        content = _SECRET_BODY
        revision = _sha(content)
        doc = f"document:private:7:9:{_SECRET_DOC}.txt"
        conn = _ScriptedConnection(rows=[_row(doc_id=doc, revision_hash=revision, content=content)])
        store, _ = _install(monkeypatch, connection=conn)
        with caplog.at_level(logging.DEBUG), capture_logs() as logs:
            result = await _call(store, doc_id=doc, revision=revision)
        assert result is not None
        text = _logs_text(logs, caplog)
        assert _SECRET_DOC not in text
        assert _SECRET_BODY.decode() not in text

    # 作用：后端异常路径只记录一次 error 且不含底层 DSN 正文。
    # Args: self - 用例；monkeypatch/caplog - 夹具。
    # Returns: 无返回值。
    async def test_error_path_logs_once_redacted(
        self, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
    ) -> None:
        conn = _ScriptedConnection(error=RuntimeError(f"backend {_SECRET_DSN}"))
        store, _ = _install(monkeypatch, connection=conn)
        with caplog.at_level(logging.DEBUG), capture_logs() as logs:
            with pytest.raises(FileStoreUnavailableError):
                await _call(store)
        errors = [event for event in logs if str(event.get("log_level", "")).lower() == "error"]
        assert len(errors) == 1, f"错误路径必须只记录一次：{errors}"
        assert _SECRET_DSN not in _logs_text(logs, caplog)

    # 作用：完整性冲突路径不得把原文/元数据哨兵写进日志。
    # Args: self - 用例；monkeypatch/caplog - 夹具。
    # Returns: 无返回值。
    async def test_integrity_conflict_logs_no_content(
        self, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
    ) -> None:
        content = _SECRET_BODY
        conn = _ScriptedConnection(rows=[_row(revision_hash=_sha(b"other"), content=content)])
        store, _ = _install(monkeypatch, connection=conn)
        with caplog.at_level(logging.DEBUG), capture_logs() as logs:
            with pytest.raises(_integrity_error()):
                await _call(store, revision=_sha(b"other"))
        text = _logs_text(logs, caplog)
        assert _SECRET_BODY.decode() not in text
        assert _SECRET_DOC not in text


class TestEnsureBoundaryAndSafeStacks:
    """G 组：_ensure 初始化边界、公开异常链与可定位安全栈。"""

    # 作用：_ensure 初始化失败必须统一 Unavailable，且不得进入查询。
    # Args: self - 用例；monkeypatch - 补丁夹具。
    # Returns: 无返回值。
    async def test_ensure_failure_maps_to_unavailable(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        conn = _ScriptedConnection(rows=[])
        store, _ = _install(
            monkeypatch,
            connection=conn,
            ensure_error=RuntimeError(f"init failed note={_SECRET_PLAIN}"),
        )
        with pytest.raises(FileStoreUnavailableError) as error:
            await _call(store)
        assert _SECRET_PLAIN not in str(error.value)
        assert conn.queries == []

    # 作用：_ensure 中的取消必须原样传播，不得被包装。
    # Args: self - 用例；monkeypatch - 补丁夹具。
    # Returns: 无返回值。
    async def test_ensure_cancelled_propagates(self, monkeypatch: pytest.MonkeyPatch) -> None:
        store, _ = _install(monkeypatch, connection=None, ensure_error=asyncio.CancelledError())
        with pytest.raises(asyncio.CancelledError):
            await _call(store)

    # 作用：公开异常链（cause/context 全程）不得含底层正文。
    # Args: self - 用例；monkeypatch - 补丁夹具。
    # Returns: 无返回值。
    async def test_public_exception_chain_free_of_underlying_text(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        note = f"backend read failed body={_SECRET_BODY.decode()}"
        conn = _ScriptedConnection(error=RuntimeError(note))
        store, _ = _install(monkeypatch, connection=conn)
        with pytest.raises(FileStoreUnavailableError) as error:
            await _call(store)
        chain: list[str] = []
        current: BaseException | None = error.value
        while current is not None:
            chain.append(f"{type(current).__name__}: {current}")
            current = current.__cause__ or current.__context__
        joined = "\n".join(chain)
        assert note not in joined, f"公开异常链泄漏底层正文：{joined}"
        assert _SECRET_BODY.decode() not in joined
        assert _SECRET_PLAIN not in joined

    # 作用：真实日志管线格式化后不得含底层正文，且保留函数/行号可定位栈。
    # Args: self - 用例；tmp_path - 临时目录；monkeypatch - 补丁夹具。
    # Returns: 无返回值。
    async def test_formatted_log_keeps_frames_without_underlying_text(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        log_file = tmp_path / "logs" / "app.log"
        snapshot = _configure_real_logging(log_file, monkeypatch)
        try:
            note = f"backend read failed body={_SECRET_BODY.decode()}"
            conn = _ScriptedConnection(error=RuntimeError(note))
            store, _ = _install(monkeypatch, connection=conn)
            with pytest.raises(FileStoreUnavailableError):
                await _call(store)
            content = _flush_and_read(log_file)
            assert note not in content, "格式化日志回显底层异常正文"
            assert _SECRET_BODY.decode() not in content
            assert _SECRET_PLAIN not in content
            assert "file_store.py" in content, "安全栈必须保留可定位帧"
            assert re.search(r"line \d+", content), "安全栈必须保留行号"
            assert content.count("Traceback (most recent call last)") == 1
        finally:
            _restore_logging(snapshot)

    # 作用：完整性冲突也必须有一次含函数/行号的安全栈，且不含正文哨兵。
    # Args: self - 用例；tmp_path - 临时目录；monkeypatch - 补丁夹具。
    # Returns: 无返回值。
    async def test_integrity_conflict_logs_one_safe_stack(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        log_file = tmp_path / "logs" / "app.log"
        snapshot = _configure_real_logging(log_file, monkeypatch)
        try:
            content_bytes = _SECRET_BODY
            conn = _ScriptedConnection(
                rows=[_row(revision_hash=_sha(b"other"), content=content_bytes)]
            )
            store, _ = _install(monkeypatch, connection=conn)
            with pytest.raises(_integrity_error()):
                await _call(store, revision=_sha(b"other"))
            content = _flush_and_read(log_file)
            assert _SECRET_BODY.decode() not in content
            assert _SECRET_DOC not in content
            assert "完整性冲突" in content
            assert "file_store.py" in content, "完整性冲突缺少可定位安全栈"
            assert re.search(r"line \d+", content), "安全栈必须保留行号"
            assert content.count("Traceback (most recent call last)") == 1
        finally:
            _restore_logging(snapshot)


class TestNumericStringMetadataStrictness:
    """H 组：三元组原值严格一致，不得用 str 强转掩盖 JSON/列坏类型。"""

    # 作用：请求为合法数字字符串而 publication 放整数，必须按原值严格不符拒绝。
    # Args: self - 用例；monkeypatch - 补丁夹具。
    # Returns: 无返回值。
    async def test_integer_metadata_vs_numeric_string_request_rejected(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        content = _SECRET_BODY
        revision = _sha(content)
        doc = "123"
        version = "456"
        metadata = {
            "source_doc_id": 123,
            "source_version": 456,
            "source_revision_hash": revision,
            "publication_basis": "unknown",
            "publication_precision": "unknown",
        }
        conn = _ScriptedConnection(
            rows=[
                _row(
                    doc_id=doc,
                    revision_hash=revision,
                    content=content,
                    version=version,
                    publication_json=json.dumps(metadata),
                )
            ]
        )
        store, _ = _install(monkeypatch, connection=conn)
        with pytest.raises(_integrity_error()):
            await _call(store, doc_id=doc, version=version, revision=revision)


class TestRealTransportLoggingBoundaries:
    """I 组：真实 _connect/_ensure 全链日志，不得回显正文且只一条异常栈。"""

    # 作用：真实 _connect 下 fetch 异常：全链日志无正文、只一条可定位异常栈。
    # Args: self - 用例；tmp_path - 临时目录；monkeypatch - 补丁夹具。
    # Returns: 无返回值。
    async def test_real_connect_fetch_failure_single_safe_stack(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        log_file = tmp_path / "logs" / "app.log"
        snapshot = _configure_real_logging(log_file, monkeypatch)
        try:
            note = f"fetch failed body={_SECRET_BODY.decode()}"
            conn = _ScriptedConnection(error=RuntimeError(note))
            store = _real_transport_store(monkeypatch, connection=conn)
            with pytest.raises(FileStoreUnavailableError) as error:
                await _call(store)
            assert note not in _exception_chain_text(error.value)
            assert _SECRET_BODY.decode() not in _exception_chain_text(error.value)
            content = _flush_and_read(log_file)
            assert _SECRET_BODY.decode() not in content, "全链日志回显底层正文"
            assert _SECRET_PLAIN not in content
            assert "file_store.py" in content, "安全栈必须保留可定位帧"
            assert re.search(r"line \d+", content), "安全栈必须保留行号"
            assert content.count("Traceback (most recent call last)") == 1
        finally:
            _restore_logging(snapshot)

    # 作用：真实 _connect 下完整性冲突：同样只一条安全栈且不回显正文。
    # Args: self - 用例；tmp_path - 临时目录；monkeypatch - 补丁夹具。
    # Returns: 无返回值。
    async def test_real_connect_integrity_conflict_single_safe_stack(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        log_file = tmp_path / "logs" / "app.log"
        snapshot = _configure_real_logging(log_file, monkeypatch)
        try:
            conn = _ScriptedConnection(
                rows=[_row(revision_hash=_sha(b"other"), content=_SECRET_BODY)]
            )
            store = _real_transport_store(monkeypatch, connection=conn)
            with pytest.raises(_integrity_error()):
                await _call(store, revision=_sha(b"other"))
            content = _flush_and_read(log_file)
            assert _SECRET_BODY.decode() not in content, "全链日志回显底层正文"
            assert _SECRET_DOC not in content
            assert "完整性冲突" in content
            assert "file_store.py" in content, "安全栈必须保留可定位帧"
            assert re.search(r"line \d+", content), "安全栈必须保留行号"
            assert content.count("Traceback (most recent call last)") == 1
        finally:
            _restore_logging(snapshot)

    # 作用：真实 _ensure 初始化失败：不得继续读取，公开安全 Unavailable，日志无正文。
    # Args: self - 用例；tmp_path - 临时目录；monkeypatch - 补丁夹具。
    # Returns: 无返回值。
    async def test_real_ensure_failure_stops_read_and_is_safe(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        log_file = tmp_path / "logs" / "app.log"
        snapshot = _configure_real_logging(log_file, monkeypatch)
        try:
            store = FileStore()
            conn = _ScriptedConnection(rows=[])
            stub = SimpleNamespace(database_url="postgresql://sandbox-NOT-REAL@127.0.0.1:1/demo")
            enters = {"count": 0}

            # 作用：首次进入抛初始化故障，其后 yield 脚本连接。
            # Args: kwargs - 真实调用方传入身份。
            # Returns: 无返回值（异步上下文生成器）。
            @asynccontextmanager
            async def _pg_connection(**kwargs: Any) -> AsyncIterator[Any]:
                enters["count"] += 1
                if enters["count"] == 1:
                    raise RuntimeError(f"init ddl failed note={_SECRET_PLAIN}")
                yield conn

            monkeypatch.setattr(file_module, "get_settings", lambda: stub)
            monkeypatch.setattr(file_module, "pg_connection", _pg_connection)
            monkeypatch.setattr(file_module, "_current_identity", lambda: (7, 9))
            monkeypatch.setattr(api_auth, "get_current_role", lambda: "analyst")
            with pytest.raises(FileStoreUnavailableError) as error:
                await _call(store)
            assert _SECRET_PLAIN not in str(error.value)
            assert conn.queries == [], "初始化失败后不得继续读取"
            content = _flush_and_read(log_file)
            assert _SECRET_PLAIN not in content, "初始化失败日志回显底层正文"
            assert "file_store.py" in content, "安全栈必须保留可定位帧"
        finally:
            _restore_logging(snapshot)

    # 作用：真实 _ensure 缓存命中路径下，正常重复读取不得新增 INFO。
    # Args: self - 用例；monkeypatch/caplog - 夹具。
    # Returns: 无返回值。
    async def test_real_ensure_repeated_reads_emit_no_info(
        self, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
    ) -> None:
        content = _SECRET_BODY
        conn = _ScriptedConnection(rows=[_row(revision_hash=_sha(content), content=content)])
        store = _real_transport_store(monkeypatch, connection=conn)
        with caplog.at_level(logging.DEBUG), capture_logs() as logs:
            for _ in range(6):
                result = await _call(store, revision=_sha(content))
                assert result is not None
        noisy = [
            event
            for event in logs
            if str(event.get("log_level", "")).lower() in {"info", "warning", "error", "critical"}
        ]
        assert noisy == [], f"真实 helper 正常重复读取出现高阶日志：{noisy}"
        assert not [record for record in caplog.records if record.levelno >= logging.INFO]

    # 作用：损坏 publication_json 的 JSONDecodeError.doc 不得经公开异常链携带原文。
    # Args: self - 用例；tmp_path - 临时目录；monkeypatch - 补丁夹具。
    # Returns: 无返回值。
    async def test_integrity_broken_json_chain_excludes_raw_doc(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        log_file = tmp_path / "logs" / "app.log"
        snapshot = _configure_real_logging(log_file, monkeypatch)
        try:
            raw = '{"note": "' + _SECRET_DOC + '"'
            conn = _ScriptedConnection(
                rows=[
                    _row(
                        revision_hash=_sha(_SECRET_BODY),
                        content=_SECRET_BODY,
                        publication_json=raw,
                    )
                ]
            )
            store = _real_transport_store(monkeypatch, connection=conn)
            with pytest.raises(_integrity_error()) as error:
                await _call(store, revision=_sha(_SECRET_BODY))
            chain_text = _exception_chain_text(error.value)
            assert _SECRET_DOC not in chain_text, "公开异常链携带损坏原文"
            assert _SECRET_BODY.decode() not in chain_text
            content = _flush_and_read(log_file)
            assert _SECRET_DOC not in content
            assert _SECRET_BODY.decode() not in content
            assert "完整性冲突" in content
            assert "file_store.py" in content, "安全栈必须保留可定位帧"
            assert content.count("Traceback (most recent call last)") == 1
        finally:
            _restore_logging(snapshot)

    # 作用：共享层两种失败形态（yield 体内 fetch／建立前 get_pg_pool）一次参数化取证。
    # Args: self - 用例；tmp_path - 临时目录；monkeypatch - 补丁夹具；mode - 失败形态。
    # Returns: 无返回值。
    @pytest.mark.parametrize("mode", ["body_fetch", "pool_acquire"])
    async def test_inherited_pg_pool_connection_single_safe_stack(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, mode: str
    ) -> None:
        """仅 Fake 最低获取边界；未来若做 pg_pool 静默须另回归旧日志与并发隔离，禁 logger.disabled。"""
        log_file = tmp_path / "logs" / "app.log"
        snapshot = _configure_real_logging(log_file, monkeypatch)
        try:
            from src.memory import pg_pool as pg_pool_module

            body_note = f"fetch failed body={_SECRET_BODY.decode()}"
            note = body_note if mode == "body_fetch" else f"pool acquire {_SECRET_DOC}"
            conn = _ScriptedConnection(error=RuntimeError(body_note))
            store = FileStore()
            store._ready = True
            store._pg_configured = True
            store._schema_ready = True
            stub = SimpleNamespace(database_url="postgresql://sandbox-NOT-REAL@127.0.0.1:1/demo")
            entered = {"pool_connection": 0}

            # 作用：返回假池；pool_acquire 形态下抛合成获取故障。
            # Args: 无。
            # Returns: 假池对象。
            async def _fake_pool() -> Any:
                if mode == "pool_acquire":
                    raise RuntimeError(note)
                return object()

            # 作用：计数后 yield 脚本连接。
            # Args: pool/tenant_id/user_id/role - 真实调用方参数。
            # Returns: 无返回值（异步上下文生成器）。
            @asynccontextmanager
            async def _fake_pool_connection(
                pool: Any, tenant_id: int, user_id: int, role: str
            ) -> AsyncIterator[Any]:
                entered["pool_connection"] += 1
                yield conn

            monkeypatch.setattr(file_module, "get_settings", lambda: stub)
            monkeypatch.setattr(file_module, "_current_identity", lambda: (7, 9))
            monkeypatch.setattr(api_auth, "get_current_role", lambda: "analyst")
            monkeypatch.setattr(pg_pool_module, "get_pg_pool", _fake_pool)
            monkeypatch.setattr(pg_pool_module, "pg_pool_connection", _fake_pool_connection)
            with pytest.raises(FileStoreUnavailableError) as error:
                await _call(store)
            assert note not in _exception_chain_text(error.value)
            if mode == "pool_acquire":
                assert entered["pool_connection"] == 0, "建立前失败不得进入身份连接"
                assert conn.queries == [], "建立前失败不得继续读取"
            else:
                assert entered["pool_connection"] == 1
                assert len(conn.queries) == 1
            content = _flush_and_read(log_file)
            assert _SECRET_BODY.decode() not in content, "共享层继承链回显底层正文"
            assert _SECRET_DOC not in content
            assert "pg_connection 失败" not in content, "共享层仍单独记录失败事件（需一并授权）"
            assert content.count("Traceback (most recent call last)") == 1
        finally:
            _restore_logging(snapshot)


class TestRealAppContextBoundaries:
    """J 组：真实 AppContext 工厂失败/取消/并发/解析失败/close_resource 清理链。"""

    # 作用：真实 AppContext 工厂失败：整链一条安全栈、无原文/locals、无独立 error 事件。
    # Args: self - 用例；tmp_path - 临时目录；monkeypatch - 补丁夹具。
    # Returns: 无返回值。
    async def test_real_app_context_factory_failure_single_safe_stack(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _bind_real_app_context(monkeypatch)  # 捕获日志之外构造
        log_file = tmp_path / "logs" / "app.log"
        snapshot = _configure_real_logging(log_file, monkeypatch)
        try:
            from src.memory import pg_pool as pg_pool_module

            note = f"create pool failed note={_SECRET_BODY.decode()}"

            # 作用：最低层 asyncpg.create_pool 替身，仅按注入故障失败。
            # Args: args/kwargs - 真实调用参数。
            # Returns: 永不返回。
            async def _fake_create_pool(*args: Any, **kwargs: Any) -> Any:
                raise RuntimeError(note)

            monkeypatch.setattr(pg_pool_module.asyncpg, "create_pool", _fake_create_pool)
            store = _real_chain_store(monkeypatch)
            with pytest.raises(FileStoreUnavailableError) as error:
                await _call(store)
            assert note not in _exception_chain_text(error.value)
            content = _flush_and_read(log_file)
            assert _SECRET_BODY.decode() not in content, "AppContext 层回显底层正文"
            assert "locals" not in content, "安全栈不得携带 locals"
            assert "异步获取 AppContext 资源失败" not in content, "AppContext 独立 error 事件"
            assert "创建 PG 池失败" not in content
            assert "get_pg_pool 失败" not in content
            assert "pg_connection 失败" not in content
            assert content.count("Traceback (most recent call last)") == 1
        finally:
            _restore_logging(snapshot)

    # 作用：真实创建链上的取消原样传播，且不缓存半成品、重试可恢复。
    # Args: self - 用例；monkeypatch - 补丁夹具。
    # Returns: 无返回值。
    async def test_real_app_context_cancel_propagates_and_recovers(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        context = _bind_real_app_context(monkeypatch)
        from src.memory import pg_pool as pg_pool_module

        quality = _PoolConnection(
            rows=[_row(revision_hash=_sha(_SECRET_BODY), content=_SECRET_BODY)]
        )
        calls = {"count": 0}

        # 作用：首次创建抛取消、其后返回可复用池连接。
        # Args: args/kwargs - 真实调用参数。
        # Returns: 池连接；首次取消。
        async def _fake_create_pool(*args: Any, **kwargs: Any) -> Any:
            calls["count"] += 1
            if calls["count"] == 1:
                raise asyncio.CancelledError()
            return quality

        monkeypatch.setattr(pg_pool_module.asyncpg, "create_pool", _fake_create_pool)
        store = _real_chain_store(monkeypatch, warmed=False)
        with pytest.raises(asyncio.CancelledError):
            await _call(store, revision=_sha(_SECRET_BODY))
        assert context.get_resource("pg_pool") is None, "取消不得缓存半成品资源"
        result = await _call(store, revision=_sha(_SECRET_BODY))
        assert result is not None
        assert result["file_data"] == _SECRET_BODY
        assert calls["count"] == 2

    # 作用：并发作用域：一次创建失败不得污染并发读，失败不缓存半成品。
    # Args: self - 用例；tmp_path - 临时目录；monkeypatch - 补丁夹具。
    # Returns: 无返回值。
    async def test_real_app_context_concurrent_scope_unaffected(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        context = _bind_real_app_context(monkeypatch)
        log_file = tmp_path / "logs" / "app.log"
        snapshot = _configure_real_logging(log_file, monkeypatch)
        try:
            from src.memory import pg_pool as pg_pool_module

            note = f"create pool failed note={_SECRET_BODY.decode()}"
            quality = _PoolConnection(
                rows=[_row(revision_hash=_sha(_SECRET_BODY), content=_SECRET_BODY)]
            )
            calls = {"count": 0}

            # 作用：首次创建失败、其后返回可复用池连接。
            # Args: args/kwargs - 真实调用参数。
            # Returns: 池连接；首次失败。
            async def _fake_create_pool(*args: Any, **kwargs: Any) -> Any:
                calls["count"] += 1
                if calls["count"] == 1:
                    raise RuntimeError(note)
                return quality

            monkeypatch.setattr(pg_pool_module.asyncpg, "create_pool", _fake_create_pool)
            store = _real_chain_store(monkeypatch)
            outcomes = await asyncio.gather(
                _call(store, revision=_sha(_SECRET_BODY)),
                _call(store, revision=_sha(_SECRET_BODY)),
                return_exceptions=True,
            )
            failures = [item for item in outcomes if isinstance(item, BaseException)]
            successes = [item for item in outcomes if isinstance(item, dict)]
            assert len(failures) == 1 and len(successes) == 1
            assert isinstance(failures[0], FileStoreUnavailableError)
            assert successes[0]["file_data"] == _SECRET_BODY
            assert context.get_resource("pg_pool") is not None
            content = _flush_and_read(log_file)
            assert _SECRET_BODY.decode() not in content, "并发失败链回显底层正文"
            assert "异步获取 AppContext 资源失败" not in content, "AppContext 独立 error 事件"
            assert content.count("Traceback (most recent call last)") == 1
        finally:
            _restore_logging(snapshot)

    # 作用：非法 DSN 在真实 parser 失败：无独立 error 事件、无重复栈、无原文。
    # Args: self - 用例；tmp_path - 临时目录；monkeypatch - 补丁夹具。
    # Returns: 无返回值。
    async def test_real_parser_failure_single_safe_stack(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        illegal = f"postgres {_SECRET_PLAIN}"  # 含 postgres 子串但不含合法 scheme
        _bind_real_app_context(monkeypatch, database_url=illegal)  # 捕获日志之外构造
        log_file = tmp_path / "logs" / "app.log"
        snapshot = _configure_real_logging(log_file, monkeypatch)
        try:
            from src.memory import pg_pool as pg_pool_module

            # 作用：最低层替身：非法 DSN 不得触达真实建池。
            # Args: args/kwargs - 真实调用参数。
            # Returns: 永不返回。
            async def _fake_create_pool(*args: Any, **kwargs: Any) -> Any:
                raise AssertionError("非法 DSN 不得触达 asyncpg.create_pool")

            monkeypatch.setattr(pg_pool_module.asyncpg, "create_pool", _fake_create_pool)
            store = _real_chain_store(monkeypatch, database_url=illegal)
            with pytest.raises(FileStoreUnavailableError) as error:
                await _call(store)
            assert _SECRET_PLAIN not in _exception_chain_text(error.value)
            content = _flush_and_read(log_file)
            assert _SECRET_PLAIN not in content
            assert "to_asyncpg_url 失败" not in content, "db.utils 独立 error 事件"
            assert "异步获取 AppContext 资源失败" not in content, "AppContext 独立 error 事件"
            assert "创建 PG 池失败" not in content
            assert content.count("Traceback (most recent call last)") == 1
        finally:
            _restore_logging(snapshot)

    # 作用：健康检查失败必须触达真实 close_resource 清理链且不泄漏正文。
    # Args: self - 用例；tmp_path - 临时目录；monkeypatch - 补丁夹具。
    # Returns: 无返回值。
    async def test_real_close_resource_reachable_on_health_failure(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        context = _bind_real_app_context(monkeypatch)
        log_file = tmp_path / "logs" / "app.log"
        snapshot = _configure_real_logging(log_file, monkeypatch)
        try:
            from src.memory import pg_pool as pg_pool_module

            broken = _BrokenPool(RuntimeError(f"health check {_SECRET_PLAIN}"))
            context.set_resource("pg_pool", broken, closer=pg_pool_module._close_pg_pool_resource)
            quality = _PoolConnection(
                rows=[_row(revision_hash=_sha(_SECRET_BODY), content=_SECRET_BODY)]
            )

            # 作用：重建路径返回可复用池连接。
            # Args: args/kwargs - 真实调用参数。
            # Returns: 池连接。
            async def _fake_create_pool(*args: Any, **kwargs: Any) -> Any:
                return quality

            monkeypatch.setattr(pg_pool_module.asyncpg, "create_pool", _fake_create_pool)
            store = _real_chain_store(monkeypatch)
            result = await _call(store, revision=_sha(_SECRET_BODY))
            assert result is not None
            assert result["file_data"] == _SECRET_BODY
            assert broken.closed == 1, "健康检查失败必须触达 close_resource 清理链"
            content = _flush_and_read(log_file)
            assert _SECRET_PLAIN not in content
            assert "PG 池失效，重建" not in content
        finally:
            _restore_logging(snapshot)

    # 作用：close_resource 清理器失败链：一次安全栈、无原文、无独立 error 事件。
    # Args: self - 用例；tmp_path - 临时目录；monkeypatch - 补丁夹具。
    # Returns: 无返回值。
    async def test_real_close_resource_closer_failure_single_safe_stack(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        context = _bind_real_app_context(monkeypatch)
        log_file = tmp_path / "logs" / "app.log"
        snapshot = _configure_real_logging(log_file, monkeypatch)
        try:
            from src.memory import pg_pool as pg_pool_module

            close_note = f"closer failed {_SECRET_DOC}"
            broken = _BrokenPool(
                RuntimeError(f"health check {_SECRET_PLAIN}"),
                close_error=RuntimeError(close_note),
            )
            context.set_resource("pg_pool", broken, closer=pg_pool_module._close_pg_pool_resource)

            # 作用：重建路径替身（清理失败即中止，不会被调用）。
            # Args: args/kwargs - 真实调用参数。
            # Returns: 永不返回。
            async def _fake_create_pool(*args: Any, **kwargs: Any) -> Any:
                raise AssertionError("清理失败后不得继续重建")

            monkeypatch.setattr(pg_pool_module.asyncpg, "create_pool", _fake_create_pool)
            store = _real_chain_store(monkeypatch)
            with pytest.raises(FileStoreUnavailableError) as error:
                await _call(store, revision=_sha(_SECRET_BODY))
            assert close_note not in _exception_chain_text(error.value)
            content = _flush_and_read(log_file)
            assert _SECRET_DOC not in content, "close_resource 失败链回显底层正文"
            assert _SECRET_PLAIN not in content
            assert "关闭 AppContext 资源失败" not in content, "AppContext 独立 error 事件"
            assert content.count("Traceback (most recent call last)") == 1
        finally:
            _restore_logging(snapshot)
