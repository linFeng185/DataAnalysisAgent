"""901.6 精确原文读取（FileStore.get_exact_version）：三元组+字节自证、可见性复用、失败关闭。

对应 spec/901 §12.11 与 approved-exact-original-reader-design-20261014.json：
I/O 前严格校验三元组/scope；仅复用 _current_identity 与 _VISIBLE_FILES_SQL；
version+hash+scope 精确约束、LIMIT 2、多匹配拒绝；publication JSON 与列三方一致、
file_data/size/sha256 自证；后端不可达抛 FileStoreUnavailableError、取消原样；
正常重复读取不新增 INFO、重要异常一次脱敏。红→绿：先于 file_store.py。
"""

from __future__ import annotations

import asyncio
import hashlib
import json
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from types import SimpleNamespace
from typing import TYPE_CHECKING
from unittest.mock import AsyncMock

import pytest

if TYPE_CHECKING:
    from src.knowledge.file_store import FileStore

_CONTENT = b"# demo\noriginal-bytes-\x00\x01\xfe\xff"
_CONTENT_HASH = hashlib.sha256(_CONTENT).hexdigest()
_VERSION = "2026-10-01-v1"
_DOC_ID = "document:private:7:9:demo.md"
_SCOPE = "private"
_NOW = datetime(2026, 10, 12, 8, 0, tzinfo=timezone.utc)
_CONTRACT_KEYS = frozenset(
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


# 作用：构造带脚本化 fetch 的 FileStore 测试替身（离线假依赖，不连库）。
# Args: monkeypatch - 补丁夹具；rows - fetch 返回行；missing - 模拟无 PG 连接；
#     fetch_error - fetch 注入异常；ensure_error - 初始化注入异常。
# Returns: (store, connection, ensure) 三元组；connection 为 None 表示无库。
def _store(
    monkeypatch: pytest.MonkeyPatch,
    *,
    rows: list[dict[str, object]] | None = None,
    missing: bool = False,
    fetch_error: BaseException | None = None,
    ensure_error: BaseException | None = None,
) -> tuple[FileStore, object, AsyncMock]:
    """_ensure/_current_identity/角色/_connect 全部注入替身，产品源码走真实路径。"""
    import src.api.auth as auth_module
    import src.knowledge.file_store as file_module

    store = file_module.FileStore()
    ensure = (
        AsyncMock(side_effect=ensure_error)
        if ensure_error is not None
        else AsyncMock(return_value=True)
    )
    monkeypatch.setattr(store, "_ensure", ensure)
    monkeypatch.setattr(file_module, "_current_identity", lambda: (7, 9))
    monkeypatch.setattr(auth_module, "get_current_role", lambda: "analyst")
    if missing:
        connection = None
    else:
        fetch = (
            AsyncMock(side_effect=fetch_error)
            if fetch_error is not None
            else AsyncMock(return_value=list(rows or []))
        )
        connection = SimpleNamespace(fetch=fetch)

    # 作用：提供 FileStore 池化连接测试上下文。
    # Args: 无。
    # Returns: 测试事务范围内的数据库连接（或 None）。
    @asynccontextmanager
    async def scoped_connection():
        """只交出脚本化连接，不建立任何真实池事务。"""
        yield connection

    monkeypatch.setattr(store, "_connect", scoped_connection)
    return store, connection, ensure


# 作用：构造三元组/字节/长度/JSON 全自洽的行，覆盖项走真实映射。
# Args: overrides - 需要偏离默认的列。
# Returns: 行字典。
def _row(**overrides: object) -> dict[str, object]:
    """默认行与请求三元组一致，便于单点注入冲突。"""
    publication = {
        "source_doc_id": _DOC_ID,
        "source_version": _VERSION,
        "source_revision_hash": _CONTENT_HASH,
        "publication_basis": "claimed",
    }
    row: dict[str, object] = {
        "id": 41,
        "filename": "demo.md",
        "content_type": "text/markdown",
        "file_data": _CONTENT,
        "size": len(_CONTENT),
        "uploaded_at": _NOW,
        "knowledge_scope": _SCOPE,
        "datasource": "ds-1",
        "tag_ids": [3, 5],
        "tenant_id": 7,
        "user_id": 9,
        "source_version": _VERSION,
        "source_revision_hash": _CONTENT_HASH,
        "publication_json": json.dumps(publication, ensure_ascii=False),
        "index_state": "pending",
    }
    row.update(overrides)
    return row


# 作用：按默认三元组调用被测方法，覆盖项走真实关键字。
# Args: store - FileStore；overrides - 调用参数覆盖。
# Returns: 结果字典或 None。
async def _call(store: FileStore, **overrides: object) -> dict | None:
    """默认参数与服务端上传链路一致（revision_hash 即原文 sha256）。"""
    params: dict[str, object] = {
        "doc_id": _DOC_ID,
        "source_version": _VERSION,
        "source_revision_hash": _CONTENT_HASH,
        "knowledge_scope": _SCOPE,
    }
    params.update(overrides)
    return await store.get_exact_version(**params)


# 作用：记录 structlog 调用的最小替身，避免依赖全局日志配置与 caplog 路由。
# Args: 无。
# Returns: 无返回值（实例化）。
class _RecordingLogger:
    """按调用顺序记录 (级别, 事件, 字段)，供噪声与脱敏断言。"""

    # 作用：初始化空调用表。
    # Args: self - 记录器。
    # Returns: 无返回值。
    def __init__(self) -> None:
        self.calls: list[tuple[str, str, dict[str, object]]] = []

    # 作用：把任意日志级别调用记录为闭包。
    # Args: level - 级别名（debug/info/error 等）。
    # Returns: 记录函数。
    def __getattr__(self, level: str):
        """structlog 以方法名表达级别，这里按名捕获。"""

        # 作用：把一次日志调用追加到记录表。
        # Args: event - 事件名；fields - 结构化字段。
        # Returns: 无返回值。
        def _record(event: str, **fields: object) -> None:
            self.calls.append((level, event, dict(fields)))

        return _record

    # 作用：返回发生过的级别序列。
    # Args: self - 记录器。
    # Returns: 级别列表。
    def levels(self) -> list[str]:
        """供“正常读取不新增 INFO”一类断言使用。"""
        return [level for level, _, _ in self.calls]


class TestExactVersionRead:
    """901.6 精确原文：唯一命中返回完整 bytes 与合同 15 键；精确约束不落最新同名。"""

    # 作用：唯一命中返回完整原文与合同键集合。
    # Args: self - 测试；monkeypatch - 补丁夹具。
    # Returns: 无返回值。
    async def test_returns_contract_keys_and_full_bytes(self, monkeypatch) -> None:
        """file_data 为 bytes 原文、publication 为解析 dict 且不混入 raw TEXT。"""
        # Arrange
        store, _, _ = _store(monkeypatch, rows=[_row()])
        # Act
        result = await _call(store)
        # Assert
        assert result is not None
        assert set(result) == _CONTRACT_KEYS
        assert isinstance(result["file_data"], bytes) and result["file_data"] == _CONTENT
        assert result["size"] == len(_CONTENT)
        assert result["scope"] == "private" and result["owner_user_id"] == 9
        assert result["tenant_id"] == 7 and result["datasource"] == "ds-1"
        assert result["tag_ids"] == [3, 5]
        assert result["uploaded_at"] == _NOW.isoformat()
        assert result["publication"]["source_doc_id"] == _DOC_ID
        assert "publication_json" not in result

    # 作用：SQL 只按精确三元组+可见性+scope 过滤并 LIMIT 2。
    # Args: self - 测试；monkeypatch - 补丁夹具。
    # Returns: 无返回值。
    async def test_sql_binds_exact_triple_and_limit_two(self, monkeypatch) -> None:
        """绑定 version/hash/身份/scope；无 filename 或 uploaded_at 回退排序。"""
        # Arrange
        store, connection, _ = _store(monkeypatch, rows=[_row()])
        # Act
        await _call(store)
        # Assert
        sql = connection.fetch.await_args.args[0]
        params = connection.fetch.await_args.args[1:]
        assert "source_version = $1" in sql and "source_revision_hash = $2" in sql
        assert "LIMIT 2" in sql
        assert "filename =" not in sql and "ORDER BY uploaded_at" not in sql
        assert params == (_VERSION, _CONTENT_HASH, 7, 9, "private")

    # 作用：memoryview 字节必须转换为 bytes 返回。
    # Args: self - 测试；monkeypatch - 补丁夹具。
    # Returns: 无返回值。
    async def test_memoryview_converted_to_bytes(self, monkeypatch) -> None:
        """asyncpg 可能给 memoryview；返回值仍必须是 bytes。"""
        # Arrange
        store, _, _ = _store(monkeypatch, rows=[_row(file_data=memoryview(_CONTENT))])
        # Act
        result = await _call(store)
        # Assert
        assert result is not None
        assert isinstance(result["file_data"], bytes) and result["file_data"] == _CONTENT

    # 作用：index_state 照实返回，不冒充 ready。
    # Args: self - 测试；monkeypatch - 补丁夹具；state - 台账状态。
    # Returns: 无返回值。
    @pytest.mark.parametrize("state", ["pending", "ready", "failed"])
    async def test_index_state_returned_verbatim(self, monkeypatch, state: str) -> None:
        """原文读取成功与索引状态无关，failed 不因读取成功改口。"""
        # Arrange
        store, _, _ = _store(monkeypatch, rows=[_row(index_state=state)])
        # Act
        result = await _call(store)
        # Assert
        assert result is not None and result["index_state"] == state

    # 作用：0 可见行返回 None 且只查一次。
    # Args: self - 测试；monkeypatch - 补丁夹具。
    # Returns: 无返回值。
    async def test_zero_rows_returns_none(self, monkeypatch) -> None:
        """不存在/不可见由 None 表达，不落磁盘、不回退最新同名。"""
        # Arrange
        store, connection, _ = _store(monkeypatch, rows=[])
        # Act
        result = await _call(store)
        # Assert
        assert result is None
        assert connection.fetch.await_count == 1

    # 作用：缺失 uploaded_at 映射为空串（沿用既有读取口径）。
    # Args: self - 测试；monkeypatch - 补丁夹具。
    # Returns: 无返回值。
    async def test_missing_uploaded_at_maps_empty_string(self, monkeypatch) -> None:
        """时间列无值时给空串，不伪造时点。"""
        # Arrange
        store, _, _ = _store(monkeypatch, rows=[_row(uploaded_at=None)])
        # Act
        result = await _call(store)
        # Assert
        assert result is not None and result["uploaded_at"] == ""


class TestExactVersionVisibility:
    """901.6 可见性：只复用当前身份与 _VISIBLE_FILES_SQL，超管不扩张。"""

    # 作用：查询绑定当前身份并复用真实可见性片段。
    # Args: self - 测试；monkeypatch - 补丁夹具。
    # Returns: 无返回值。
    async def test_uses_current_identity_and_visible_sql(self, monkeypatch) -> None:
        """system 恒可见；tenant/private 按当前 tenant/user 且排除 super_admin 分支。"""
        # Arrange
        store, connection, _ = _store(monkeypatch, rows=[_row()])
        # Act
        await _call(store)
        # Assert
        sql = connection.fetch.await_args.args[0]
        assert "current_setting('app.current_role', true) <> 'super_admin'" in sql
        assert "knowledge_scope = 'system'" in sql
        assert "knowledge_scope = 'tenant' AND tenant_id = $3" in sql
        assert "knowledge_scope = 'private' AND tenant_id = $3 AND user_id = $4" in sql

    # 作用：super_admin 角色不得获得 private/tenant 放宽。
    # Args: self - 测试；monkeypatch - 补丁夹具。
    # Returns: 无返回值。
    async def test_super_admin_role_keeps_visibility_guard(self, monkeypatch) -> None:
        """角色为 super_admin 时 SQL 守卫与身份参数不变，0 行仍返回 None。"""
        # Arrange
        import src.api.auth as auth_module

        store, connection, _ = _store(monkeypatch, rows=[])
        monkeypatch.setattr(auth_module, "get_current_role", lambda: "super_admin")
        # Act
        result = await _call(store)
        # Assert
        sql = connection.fetch.await_args.args[0]
        params = connection.fetch.await_args.args[1:]
        assert result is None
        assert "current_setting('app.current_role', true) <> 'super_admin'" in sql
        assert params == (_VERSION, _CONTENT_HASH, 7, 9, "private")

    # 作用：空 scope 表示不追加范围过滤。
    # Args: self - 测试；monkeypatch - 补丁夹具。
    # Returns: 无返回值。
    async def test_empty_scope_means_any_scope(self, monkeypatch) -> None:
        """scope 空串走 ($5 = '') 分支，不缩小也不放宽租户可见性。"""
        # Arrange
        store, connection, _ = _store(monkeypatch, rows=[_row()])
        # Act
        await _call(store, knowledge_scope="")
        # Assert
        sql = connection.fetch.await_args.args[0]
        params = connection.fetch.await_args.args[1:]
        assert "$5 = '' OR knowledge_scope = $5" in sql
        assert params[4] == ""

    # 作用：他人 private 记录读不到（0 行 None）。
    # Args: self - 测试；monkeypatch - 补丁夹具。
    # Returns: 无返回值。
    async def test_other_owner_or_tenant_not_readable(self, monkeypatch) -> None:
        """可见性由 SQL 片段强制；真实 RLS 真库验证归独立真 PG 文件。"""
        # Arrange
        store, _, _ = _store(monkeypatch, rows=[])
        # Act
        result = await _call(store)
        # Assert
        assert result is None


class TestExactVersionInputRejection:
    """901.6 I/O 前拒绝：坏类型/空白/越界/非小写 hex/非法 scope 零外部调用。"""

    # 作用：非法输入在触库前抛出 ValueError。
    # Args: self - 测试；monkeypatch - 补丁夹具；field - 参数名；value - 非法值。
    # Returns: 无返回值。
    @pytest.mark.parametrize(
        ("field", "value"),
        [
            ("doc_id", ""),
            ("doc_id", "   "),
            ("doc_id", " demo.md"),
            ("doc_id", "demo.md "),
            ("doc_id", "x" * 129),
            ("doc_id", None),
            ("doc_id", 7),
            ("doc_id", True),
            ("source_version", ""),
            ("source_version", "  "),
            ("source_version", "v" * 65),
            ("source_version", None),
            ("source_version", 1.0),
            ("source_revision_hash", ""),
            ("source_revision_hash", "A" * 64),
            ("source_revision_hash", "a" * 63),
            ("source_revision_hash", "a" * 65),
            ("source_revision_hash", "g" * 64),
            ("source_revision_hash", None),
            ("source_revision_hash", b"a" * 64),
            ("knowledge_scope", "public"),
            ("knowledge_scope", " system"),
            ("knowledge_scope", "SYSTEM"),
            ("knowledge_scope", None),
            ("knowledge_scope", 3),
        ],
    )
    async def test_invalid_input_rejected_before_io(
        self,
        monkeypatch: pytest.MonkeyPatch,
        field: str,
        value: object,
    ) -> None:
        """失败关闭：不触发 _ensure/_connect/fetch，也不猜文件名或自动裁剪。"""
        # Arrange
        store, connection, ensure = _store(monkeypatch, rows=[_row()])
        # Act + Assert
        with pytest.raises(ValueError):
            await _call(store, **{field: value})
        assert ensure.await_count == 0
        assert connection.fetch.await_count == 0

    # 作用：边界长度恰好通过校验并正常读取。
    # Args: self - 测试；monkeypatch - 补丁夹具。
    # Returns: 无返回值。
    async def test_boundary_lengths_accepted(self, monkeypatch) -> None:
        """doc_id=128、source_version=64 属合法上界。"""
        # Arrange
        doc_id = "d" * 128
        version = "v" * 64
        publication = {
            "source_doc_id": doc_id,
            "source_version": version,
            "source_revision_hash": _CONTENT_HASH,
        }
        row = _row(source_version=version, publication_json=json.dumps(publication))
        store, _, _ = _store(monkeypatch, rows=[row])
        # Act
        result = await _call(store, doc_id=doc_id, source_version=version)
        # Assert
        assert result is not None and result["source_version"] == version


class TestExactVersionIntegrity:
    """901.6 完整性：多匹配/元数据/字节任一冲突即 FileStoreIntegrityError。"""

    # 作用：LIMIT 2 返回两行即多匹配拒绝。
    # Args: self - 测试；monkeypatch - 补丁夹具。
    # Returns: 无返回值。
    async def test_two_rows_rejected_as_multi_match(self, monkeypatch) -> None:
        """同三元组多行无法确证唯一原文，拒绝冒充。"""
        # Arrange
        import src.knowledge.file_store as file_module

        store, _, _ = _store(monkeypatch, rows=[_row(), _row(id=42)])
        # Act + Assert
        with pytest.raises(file_module.FileStoreIntegrityError) as caught:
            await _call(store)
        assert caught.value.reason == "multi_match"

    # 作用：多匹配优先于元数据解析。
    # Args: self - 测试；monkeypatch - 补丁夹具。
    # Returns: 无返回值。
    async def test_multi_match_precedes_metadata_parse(self, monkeypatch) -> None:
        """第二行 JSON 损坏也不能改变多匹配定性。"""
        # Arrange
        import src.knowledge.file_store as file_module

        store, _, _ = _store(monkeypatch, rows=[_row(), _row(id=42, publication_json="{")])
        # Act + Assert
        with pytest.raises(file_module.FileStoreIntegrityError) as caught:
            await _call(store)
        assert caught.value.reason == "multi_match"

    # 作用：publication_json 缺失/坏 JSON/非对象一律完整性冲突。
    # Args: self - 测试；monkeypatch - 补丁夹具；raw - 列原始值。
    # Returns: 无返回值。
    @pytest.mark.parametrize("raw", [None, "", "{", "[1]", '"text"', 7])
    async def test_bad_publication_payload_rejected(
        self,
        monkeypatch: pytest.MonkeyPatch,
        raw: object,
    ) -> None:
        """无法解析的安全 mapping 就不能自证三元组。"""
        # Arrange
        import src.knowledge.file_store as file_module

        store, _, _ = _store(monkeypatch, rows=[_row(publication_json=raw)])
        # Act + Assert
        with pytest.raises(file_module.FileStoreIntegrityError) as caught:
            await _call(store)
        assert caught.value.reason == "metadata"

    # 作用：publication 内 source_doc_id 与请求不符拒绝。
    # Args: self - 测试；monkeypatch - 补丁夹具。
    # Returns: 无返回值。
    async def test_doc_id_mismatch_rejected(self, monkeypatch) -> None:
        """同名不同文档绝不冒充；不按 title/文件名猜测。"""
        # Arrange
        import src.knowledge.file_store as file_module

        publication = {
            "source_doc_id": "document:private:7:9:other.md",
            "source_version": _VERSION,
            "source_revision_hash": _CONTENT_HASH,
        }
        store, _, _ = _store(monkeypatch, rows=[_row(publication_json=json.dumps(publication))])
        # Act + Assert
        with pytest.raises(file_module.FileStoreIntegrityError) as caught:
            await _call(store)
        assert caught.value.reason == "metadata"

    # 作用：行内 version/hash 列与请求不符拒绝。
    # Args: self - 测试；monkeypatch - 补丁夹具；column - 冲突列。
    # Returns: 无返回值。
    @pytest.mark.parametrize(
        ("column", "value"),
        [("source_version", "2026-10-02-v2"), ("source_revision_hash", "0" * 64)],
    )
    async def test_column_triple_mismatch_rejected(
        self,
        monkeypatch: pytest.MonkeyPatch,
        column: str,
        value: str,
    ) -> None:
        """列数据与请求不一致即拒绝，即使 SQL 本应过滤。"""
        # Arrange
        import src.knowledge.file_store as file_module

        store, _, _ = _store(monkeypatch, rows=[_row(**{column: value})])
        # Act + Assert
        with pytest.raises(file_module.FileStoreIntegrityError) as caught:
            await _call(store)
        assert caught.value.reason == "metadata"

    # 作用：publication 内 version/hash 与列/请求不符拒绝。
    # Args: self - 测试；monkeypatch - 补丁夹具；field - 冲突字段。
    # Returns: 无返回值。
    @pytest.mark.parametrize("field", ["source_version", "source_revision_hash"])
    async def test_publication_triple_mismatch_rejected(
        self,
        monkeypatch: pytest.MonkeyPatch,
        field: str,
    ) -> None:
        """JSON 与列必须相互印证，任一处被改写都拒绝。"""
        # Arrange
        import src.knowledge.file_store as file_module

        publication = {
            "source_doc_id": _DOC_ID,
            "source_version": _VERSION,
            "source_revision_hash": _CONTENT_HASH,
        }
        publication[field] = "0" * 64 if field == "source_revision_hash" else "2026-10-02-v2"
        store, _, _ = _store(monkeypatch, rows=[_row(publication_json=json.dumps(publication))])
        # Act + Assert
        with pytest.raises(file_module.FileStoreIntegrityError) as caught:
            await _call(store)
        assert caught.value.reason == "metadata"

    # 作用：file_data 类型必须是 bytes/memoryview。
    # Args: self - 测试；monkeypatch - 补丁夹具；raw - 非法字节值。
    # Returns: 无返回值。
    @pytest.mark.parametrize("raw", [None, "text", bytearray(b"x"), 7])
    async def test_file_data_type_rejected(
        self,
        monkeypatch: pytest.MonkeyPatch,
        raw: object,
    ) -> None:
        """字符串/数组/整型都不得冒充原文。"""
        # Arrange
        import src.knowledge.file_store as file_module

        store, _, _ = _store(monkeypatch, rows=[_row(file_data=raw)])
        # Act + Assert
        with pytest.raises(file_module.FileStoreIntegrityError) as caught:
            await _call(store)
        assert caught.value.reason == "bytes"

    # 作用：size 必须与真实长度一致且为真 int。
    # Args: self - 测试；monkeypatch - 补丁夹具；size - 非法长度。
    # Returns: 无返回值。
    @pytest.mark.parametrize("size", [len(_CONTENT) - 1, len(_CONTENT) + 1, None, True])
    async def test_size_mismatch_rejected(
        self,
        monkeypatch: pytest.MonkeyPatch,
        size: object,
    ) -> None:
        """长度台账与字节不符（含 bool 冒充）即拒绝。"""
        # Arrange
        import src.knowledge.file_store as file_module

        store, _, _ = _store(monkeypatch, rows=[_row(size=size)])
        # Act + Assert
        with pytest.raises(file_module.FileStoreIntegrityError) as caught:
            await _call(store)
        assert caught.value.reason == "bytes"

    # 作用：实际 sha256 必须等于请求的 revision hash。
    # Args: self - 测试；monkeypatch - 补丁夹具。
    # Returns: 无返回值。
    async def test_sha256_mismatch_rejected(self, monkeypatch) -> None:
        """同长度不同字节篡改必须被内容哈希抓住。"""
        # Arrange
        import src.knowledge.file_store as file_module

        tampered = _CONTENT[:-1] + b"\x00"
        store, _, _ = _store(monkeypatch, rows=[_row(file_data=tampered)])
        # Act + Assert
        with pytest.raises(file_module.FileStoreIntegrityError) as caught:
            await _call(store)
        assert caught.value.reason == "bytes"


class TestExactVersionTransport:
    """901.6 传输：后端不可达统一 Unavailable，取消原样，异常脱敏且保留定位。"""

    # 作用：无连接必须抛 Unavailable 而不是 None。
    # Args: self - 测试；monkeypatch - 补丁夹具。
    # Returns: 无返回值。
    async def test_missing_backend_raises_unavailable(self, monkeypatch) -> None:
        """后台不可达与零行不可混为一谈。"""
        # Arrange
        import src.knowledge.file_store as file_module

        store, _, _ = _store(monkeypatch, missing=True)
        # Act + Assert
        with pytest.raises(file_module.FileStoreUnavailableError):
            await _call(store)

    # 作用：底层异常包装为 Unavailable 且不泄露异常正文。
    # Args: self - 测试；monkeypatch - 补丁夹具。
    # Returns: 无返回值。
    async def test_backend_failure_wrapped_and_redacted(self, monkeypatch) -> None:
        """公开异常链无底层正文；日志一次安全栈（含定位帧、不含正文/标识）。"""
        # Arrange
        import src.knowledge.file_store as file_module

        recorder = _RecordingLogger()
        store, _, _ = _store(monkeypatch, fetch_error=RuntimeError("boom-secret"))
        monkeypatch.setattr(file_module, "logger", recorder)
        # Act + Assert
        with pytest.raises(file_module.FileStoreUnavailableError) as caught:
            await _call(store)
        assert "boom-secret" not in str(caught.value)
        assert caught.value.__cause__ is None and caught.value.__context__ is None
        errors = [call for call in recorder.calls if call[0] == "error"]
        assert len(errors) == 1
        event = errors[0][1]
        assert "Traceback (most recent call last)" in event
        assert "file_store.py" in event and "line " in event
        assert "exc_info" not in errors[0][2]
        rendered = repr(errors[0])
        assert "boom-secret" not in rendered and _DOC_ID not in rendered
        assert "original-bytes" not in rendered and _CONTENT_HASH not in rendered

    # 作用：取消不得被包装成 Unavailable。
    # Args: self - 测试；monkeypatch - 补丁夹具。
    # Returns: 无返回值。
    async def test_cancellation_propagates_unwrapped(self, monkeypatch) -> None:
        """asyncio.CancelledError 原样传播，不重试、不换类型。"""
        # Arrange
        store, _, _ = _store(monkeypatch, fetch_error=asyncio.CancelledError())
        # Act + Assert
        with pytest.raises(asyncio.CancelledError):
            await _call(store)

    # 作用：初始化异常统一映射为 Unavailable 且不进入查询。
    # Args: self - 测试；monkeypatch - 补丁夹具。
    # Returns: 无返回值。
    async def test_ensure_failure_maps_to_unavailable(self, monkeypatch) -> None:
        """_ensure 异常按后端不可用失败关闭，消息不含底层正文。"""
        import src.knowledge.file_store as file_module

        store, connection, _ = _store(
            monkeypatch, ensure_error=RuntimeError("init failed note=SEED-NOTE")
        )
        with pytest.raises(file_module.FileStoreUnavailableError) as caught:
            await _call(store)
        assert "SEED-NOTE" not in str(caught.value)
        assert connection.fetch.await_count == 0

    # 作用：初始化阶段取消原样传播。
    # Args: self - 测试；monkeypatch - 补丁夹具。
    # Returns: 无返回值。
    async def test_ensure_cancellation_propagates(self, monkeypatch) -> None:
        """初始化取消不得被包装成 Unavailable。"""
        store, _, _ = _store(monkeypatch, ensure_error=asyncio.CancelledError())
        with pytest.raises(asyncio.CancelledError):
            await _call(store)

    # 作用：_ensure 返回 False（未配置）即不可用且不进入读取。
    # Args: self - 测试；monkeypatch - 补丁夹具。
    # Returns: 无返回值。
    async def test_ensure_not_ready_stops_before_read(self, monkeypatch) -> None:
        """未就绪布尔必须失败关闭，不得继续查库。"""
        import src.knowledge.file_store as file_module

        store, connection, _ = _store(monkeypatch)
        monkeypatch.setattr(store, "_ensure", AsyncMock(return_value=False))
        with pytest.raises(file_module.FileStoreUnavailableError):
            await _call(store)
        assert connection.fetch.await_count == 0

    # 作用：瞬时初始化失败后下一读可成功，不被毒化状态卡死。
    # Args: self - 测试；monkeypatch - 补丁夹具。
    # Returns: 无返回值。
    async def test_read_recovers_after_transient_ensure_failure(self, monkeypatch) -> None:
        """首次初始化失败→Unavailable；恢复后同一实例可读到唯一行。"""
        import src.knowledge.file_store as file_module

        store, _, ensure = _store(monkeypatch, rows=[_row()])
        ensure.side_effect = [RuntimeError("init down"), True]
        with pytest.raises(file_module.FileStoreUnavailableError):
            await _call(store)
        result = await _call(store)
        assert result is not None and result["file_data"] == _CONTENT


class TestExactVersionLogging:
    """901.6 日志：正常重复读取不新增 INFO；完整性冲突一次脱敏且无正文。"""

    # 作用：正常重复读取不产生 INFO 及以上日志。
    # Args: self - 测试；monkeypatch - 补丁夹具。
    # Returns: 无返回值。
    async def test_repeated_reads_emit_no_info(self, monkeypatch) -> None:
        """两次成功读取均不得刷 INFO/WARNING/ERROR。"""
        # Arrange
        import src.knowledge.file_store as file_module

        recorder = _RecordingLogger()
        store, _, _ = _store(monkeypatch, rows=[_row()])
        monkeypatch.setattr(file_module, "logger", recorder)
        # Act
        await _call(store)
        await _call(store)
        # Assert
        noisy = [level for level in recorder.levels() if level in {"info", "warning", "error"}]
        assert noisy == []

    # 作用：完整性冲突只记录一次脱敏错误且不带私密标识。
    # Args: self - 测试；monkeypatch - 补丁夹具。
    # Returns: 无返回值。
    async def test_integrity_conflict_logged_once_without_secrets(self, monkeypatch) -> None:
        """reason 定位码可查，doc_id/文件名/原文/哈希不进日志。"""
        # Arrange
        import src.knowledge.file_store as file_module

        recorder = _RecordingLogger()
        publication = {
            "source_doc_id": "document:private:7:9:other.md",
            "source_version": _VERSION,
            "source_revision_hash": _CONTENT_HASH,
        }
        store, _, _ = _store(monkeypatch, rows=[_row(publication_json=json.dumps(publication))])
        monkeypatch.setattr(file_module, "logger", recorder)
        # Act + Assert
        with pytest.raises(file_module.FileStoreIntegrityError):
            await _call(store)
        errors = [call for call in recorder.calls if call[0] == "error"]
        assert len(errors) == 1
        rendered = repr(errors[0])
        assert _DOC_ID not in rendered and "demo.md" not in rendered
        assert "original-bytes" not in rendered and _CONTENT_HASH not in rendered
        assert "exc_info" not in errors[0][2]


class TestExactVersionStrictMetadata:
    """三元组原值严格一致：不用 str 强转掩盖 JSON/列坏类型。"""

    # 作用：JSON 内数值型三元组与字符串请求必须原值不等拒绝。
    # Args: self - 测试；monkeypatch - 补丁夹具。
    # Returns: 无返回值。
    async def test_numeric_publication_values_rejected(self, monkeypatch) -> None:
        """int 123/456 不得被强转成匹配字符串请求。"""
        import src.knowledge.file_store as file_module

        publication = {
            "source_doc_id": 123,
            "source_version": 456,
            "source_revision_hash": _CONTENT_HASH,
        }
        store, _, _ = _store(monkeypatch, rows=[_row(publication_json=json.dumps(publication))])
        with pytest.raises(file_module.FileStoreIntegrityError) as caught:
            await _call(store, doc_id="123", source_version="456")
        assert caught.value.reason == "metadata"

    # 作用：列值为数值型时不得被强转成匹配字符串请求。
    # Args: self - 测试；monkeypatch - 补丁夹具。
    # Returns: 无返回值。
    async def test_numeric_column_value_rejected(self, monkeypatch) -> None:
        """列 source_version=int 456 对请求 "456" 原值严格不等。"""
        import src.knowledge.file_store as file_module

        store, _, _ = _store(monkeypatch, rows=[_row(source_version=456)])
        with pytest.raises(file_module.FileStoreIntegrityError) as caught:
            await _call(store, source_version="456")
        assert caught.value.reason == "metadata"


class TestExactVersionQuietStrategy:
    """精确入口静默/严格策略：任务隔离、token 复位、旧接口默认语义不变、可重试恢复。"""

    # 作用：成功路径返回后静默开关必须复位。
    # Args: self - 测试；monkeypatch - 补丁夹具。
    # Returns: 无返回值。
    async def test_quiet_token_reset_on_success(self, monkeypatch) -> None:
        """精确读取正常返回不得把静默状态泄漏给后续调用。"""
        import src.knowledge.file_store as file_module

        store, _, _ = _store(monkeypatch, rows=[_row()])
        result = await _call(store)
        assert result is not None
        assert file_module._EXACT_READ_QUIET.get() is False

    # 作用：后端异常路径抛出后静默开关必须复位。
    # Args: self - 测试；monkeypatch - 补丁夹具。
    # Returns: 无返回值。
    async def test_quiet_token_reset_on_backend_failure(self, monkeypatch) -> None:
        """Unavailable 抛出后开关不得残留。"""
        import src.knowledge.file_store as file_module

        store, _, _ = _store(monkeypatch, fetch_error=RuntimeError("boom"))
        with pytest.raises(file_module.FileStoreUnavailableError):
            await _call(store)
        assert file_module._EXACT_READ_QUIET.get() is False

    # 作用：完整性冲突路径抛出后静默开关必须复位。
    # Args: self - 测试；monkeypatch - 补丁夹具。
    # Returns: 无返回值。
    async def test_quiet_token_reset_on_integrity_conflict(self, monkeypatch) -> None:
        """Integrity 抛出后开关不得残留。"""
        import src.knowledge.file_store as file_module

        store, _, _ = _store(monkeypatch, rows=[_row(), _row(id=42)])
        with pytest.raises(file_module.FileStoreIntegrityError):
            await _call(store)
        assert file_module._EXACT_READ_QUIET.get() is False

    # 作用：取消路径传播后静默开关必须复位。
    # Args: self - 测试；monkeypatch - 补丁夹具。
    # Returns: 无返回值。
    async def test_quiet_token_reset_on_cancellation(self, monkeypatch) -> None:
        """CancelledError 原样传播且开关复位。"""
        import src.knowledge.file_store as file_module

        store, _, _ = _store(monkeypatch, fetch_error=asyncio.CancelledError())
        with pytest.raises(asyncio.CancelledError):
            await _call(store)
        assert file_module._EXACT_READ_QUIET.get() is False

    # 作用：同实例并发旧调用不被静默（任务上下文隔离）。
    # Args: self - 测试；monkeypatch - 补丁夹具。
    # Returns: 无返回值。
    async def test_concurrent_legacy_helper_call_not_silenced(self, monkeypatch) -> None:
        """精确读取在途时，另一任务走真实 _ensure 缓存命中仍应记录 INFO。"""
        import src.knowledge.file_store as file_module

        recorder = _RecordingLogger()
        store, connection, _ = _store(monkeypatch)
        monkeypatch.setattr(
            store,
            "_ensure",
            file_module.FileStore._ensure.__get__(store, file_module.FileStore),
        )
        monkeypatch.setattr(file_module, "logger", recorder)
        store._ready = True
        store._schema_ready = True
        store._pg_configured = True
        release = asyncio.Event()

        # 作用：作用域外创建的并发旧调用任务（真实 _ensure 缓存命中仍应记 INFO）。
        # Args: 无。
        # Returns: 无返回值。
        async def legacy_call() -> None:
            await release.wait()
            await store._ensure()

        legacy_task = asyncio.create_task(legacy_call())

        # 作用：精确读取的连接替身：放行并发旧调用后再返回脚本行。
        # Args: sql - 语句；params - 绑定参数。
        # Returns: 脚本行列表。
        async def fetch(sql: str, *params: object) -> list[dict[str, object]]:
            release.set()
            await legacy_task
            return [_row()]

        connection.fetch = AsyncMock(side_effect=fetch)
        result = await _call(store)
        assert result is not None
        assert any(level == "info" for level, _, _ in recorder.calls)

    # 作用：默认旧接口的 helper 日志与回退行为保持不变。
    # Args: self - 测试；monkeypatch - 补丁夹具。
    # Returns: 无返回值。
    async def test_default_helper_logs_unchanged(self, monkeypatch) -> None:
        """非精确路径下 _ensure 缓存命中与 _connect 完成仍记录原有 INFO。"""
        import src.knowledge.file_store as file_module

        recorder = _RecordingLogger()
        store, connection, _ = _store(monkeypatch)
        monkeypatch.setattr(
            store,
            "_ensure",
            file_module.FileStore._ensure.__get__(store, file_module.FileStore),
        )
        monkeypatch.setattr(
            store,
            "_connect",
            file_module.FileStore._connect.__get__(store, file_module.FileStore),
        )
        monkeypatch.setattr(
            file_module,
            "get_settings",
            lambda: SimpleNamespace(database_url="postgresql://sandbox/knowledge"),
        )

        # 作用：出借脚本化连接的假池上下文。
        # Args: kwargs - 真实调用参数（忽略）。
        # Returns: 异步上下文迭代器。
        @asynccontextmanager
        async def fake_pg_connection(**kwargs: object):
            yield connection

        monkeypatch.setattr(file_module, "pg_connection", fake_pg_connection)
        monkeypatch.setattr(file_module, "logger", recorder)
        store._ready = True
        store._schema_ready = True
        store._pg_configured = True
        await store._ensure()
        async with store._connect() as conn:
            assert conn is connection
        events = [event for level, event, _ in recorder.calls if level == "info"]
        assert "知识文件 PG 初始化命中缓存" in events
        assert "知识文件 PG 连接完成" in events

    # 作用：真实 helper 下 strict 初始化失败后可重试成功。
    # Args: self - 测试；monkeypatch - 补丁夹具。
    # Returns: 无返回值。
    async def test_strict_ensure_failure_retries_on_next_read(self, monkeypatch) -> None:
        """首次 DDL 失败→Unavailable；下次读取重试就绪并返回原文。"""
        import src.api.auth as auth_module
        import src.knowledge.file_store as file_module

        conn = SimpleNamespace(
            execute=AsyncMock(return_value="OK"),
            fetch=AsyncMock(return_value=[_row()]),
        )
        calls = {"count": 0}

        # 作用：首次建立失败、其后出借连接的假池上下文。
        # Args: kwargs - 真实调用参数（忽略）。
        # Returns: 异步上下文迭代器。
        @asynccontextmanager
        async def flaky_pg_connection(**kwargs: object):
            calls["count"] += 1
            if calls["count"] == 1:
                raise RuntimeError("init down")
            yield conn

        store = file_module.FileStore()
        monkeypatch.setattr(
            file_module,
            "get_settings",
            lambda: SimpleNamespace(database_url="postgresql://sandbox/knowledge"),
        )
        monkeypatch.setattr(file_module, "pg_connection", flaky_pg_connection)
        monkeypatch.setattr(file_module, "_current_identity", lambda: (7, 9))
        monkeypatch.setattr(auth_module, "get_current_role", lambda: "analyst")
        with pytest.raises(file_module.FileStoreUnavailableError):
            await _call(store)
        result = await _call(store)
        assert result is not None and result["file_data"] == _CONTENT
