"""知识文件版本存储与精确原文读取真 PG 沙箱验收（36.5）。

边界与记账：
  * 真实 schema 与真实写入：真实 run_migrations 应用既有迁移 → 真实 FileStore.initialize
    补出版列与唯一索引 → 真实 FileStore.save(publication=自建契约字典, require_durable=True)
    落行；source_version/source_revision_hash 由原文 sha256 派生，publication 同时携带
    source_doc_id 供精确读取三方复核。自建契约字典不导入任何 trading_theory/screening 模块。
  * 唯一装配替身：settings 与 file_module.pg_connection 指向 127.0.0.1:55491 的随机独立库与
    角色（mainkv_ 前缀）；真实 asyncpg 连接与真实会话 GUC（与 pg_pool 同式），SQL 不改写、
    核心读取实现零替换。
  * 身份只用服务端公共 use_identity_context(RequestIdentity(...)) 合成受信身份，明确不是
    生产认证；AppContext 仅用替身 settings 绑定集中租户策略。
  * 覆盖：initialize/save(publication)/get_latest_versions/get_exact_version/mark_index_state、
    旧普通上传兼容、同版本拒绝、两次提交追加顺序、用户/租户越权、SHA 不符与篡改拒绝、
    拒绝后恢复、后端重建后旧版仍可读；至少五个独立有意义用例，共用一个函数级沙箱夹具。
  * 沙箱硬约束：仅在 RUN_MAIN_COMMON_PG_TESTS=1 且 MAIN_COMMON_PG_CONFIG 指向受控
    test-environment.json、主机/端口为 127.0.0.1:55491 时运行；任何不符失败关闭，
    绝不回退真实配置。清理只删本文件精确随机名（mainkv_ 前缀）的库/角色/会话并逐项验证收据；
    前缀内其它对象只报告不删除，不做其它 DROP/ALTER。
  * 已执行：2026-10-14 受控槽（holder_lane=main-common，仅本文件）9 passed、未过滤/未 skip；
    真实断连用例以 pg_terminate_backend 终止读取持有的真实连接并验证 FileStoreUnavailableError
    →同三元组恢复；逐例 drop 精确随机名并记录收据，末例以正负对照记录硬零。
"""

from __future__ import annotations

import asyncio
import builtins
import hashlib
import io
import json
import os
import socket
import uuid
from collections.abc import AsyncIterator, Coroutine, Iterator
from contextlib import asynccontextmanager, contextmanager
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import asyncpg
import pytest

import src.knowledge.file_store as file_module
from src.api.auth import use_identity_context
from src.app_context import AppContext, use_app_context
from src.knowledge.file_store import (
    FileStore,
    FileStoreDuplicateVersionError,
    FileStoreIntegrityError,
    FileStoreUnavailableError,
)
from src.security.tenant_policy import RequestIdentity

# 沙箱与清理白名单前缀：库/角色/会话核对均以此前缀为准。
_PREFIX = "mainkv_"

# 固定身份矩阵：属主普通用户、同租户他人、跨租户用户。
_TENANT_ID = 1
_OWNER_ID = 4
_OTHER_USER_ID = 5
_OTHER_TENANT_ID = 2
_OTHER_TENANT_USER_ID = 6
_USER_ROLE = "user"

# 受控临时 PG 环境硬约束：主机/端口与受控配置文件形状，任何不符即失败关闭。
_EXPECTED_HOST = "127.0.0.1"
_EXPECTED_PORT = 55491
_ENV_DIR_NAME = "dataAnalysisAgent-pg-20261011"
_ENV_FILE_NAME = "test-environment.json"

_TAG_IDS = [4242]

# 每次 drop 后的清理收据（进程内，仅计数），末例核对全部为零。
_RECEIPTS: list[dict[str, int]] = []


# 作用：拒绝非环回 socket.connect 并记录；环回交由原实现（沙箱 PG 与事件循环自管道所需）。
# Args: monkeypatch - pytest 补丁器。
# Returns: 无返回值（生成器夹具）。
@pytest.fixture(autouse=True)
def _deny_outbound_network(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """socket 层护栏：只拒非环回出站；不是 OS 级隔离，也不构成 API 外发授权。"""
    remote_attempts: list[Any] = []
    original_connect = socket.socket.connect

    # 作用：按目标地址区分环回与非环回。
    # Args: self - socket；address - 目标地址。
    # Returns: 环回返回原连接结果；非环回恒抛断言错误。
    def guarded(self: socket.socket, address: Any) -> None:
        """环回原样放行；非环回记录后抛断言错误。"""
        host = address[0] if isinstance(address, tuple) and address else ""
        if str(host) in {"127.0.0.1", "::1", "localhost"}:
            return original_connect(self, address)
        remote_attempts.append(address)
        raise AssertionError(f"测试禁止网络出站：{address!r}")

    monkeypatch.setattr(socket.socket, "connect", guarded)
    yield
    assert remote_attempts == [], f"出现远程出站尝试：{remote_attempts!r}"


# 运行期受保护文件：.env 按基名，config/app.yaml 与行情探针按仓库相对路径。
_REPO_ROOT = Path(__file__).resolve().parents[2]
_PROTECTED_CONFIG_PATHS = (
    (_REPO_ROOT / "config" / "app.yaml").resolve(),
    (_REPO_ROOT / "scripts" / "tushare_api_probe.py").resolve(),
)


# 作用：判定 open 目标是否为受保护配置/探针文件。
# Args: name - open 目标（str 或 PathLike）。
# Returns: 命中返回 True。
def _protected_target(name: object) -> bool:
    """.env 按基名拦截；config/app.yaml 与探针按 resolved 精确路径拦截。"""
    try:
        path = Path(str(name))
    except TypeError:
        return False
    if path.name == ".env":
        return True
    try:
        resolved = path.resolve()
    except OSError:
        return False
    return resolved in _PROTECTED_CONFIG_PATHS


# 作用：运行期文件闸门：打开受保护配置/探针文件立即失败并记账。
# Args: monkeypatch - pytest 补丁器。
# Returns: 无返回值（生成器夹具）。
@pytest.fixture(autouse=True)
def _deny_protected_config_reads(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """覆盖 builtins.open 与 io.open（Path.open 直接落 io.open）；其余路径原样放行。"""
    attempts: list[str] = []
    original_open = builtins.open
    original_io_open = io.open

    # 作用：守卫 builtins.open：命中即记账并失败，其余原样透传。
    # Args: file - 目标；*args/**kwargs - 透传参数。
    # Returns: 原 open 返回值（仅非受保护目标）。
    def guarded(file: Any, *args: Any, **kwargs: Any) -> Any:
        """只拦截精确受保护目标，不改变其他文件行为。"""
        if _protected_target(file):
            attempts.append(str(file))
            raise AssertionError(f"测试禁止读取受保护配置/探针文件：{file!r}")
        return original_open(file, *args, **kwargs)

    # 作用：守卫 io.open（Path.open 经由该入口）。
    # Args: file - 目标；*args/**kwargs - 透传参数。
    # Returns: 原 io.open 返回值（仅非受保护目标）。
    def guarded_io(file: Any, *args: Any, **kwargs: Any) -> Any:
        """与 guarded 同判定，落到底层 io.open。"""
        if _protected_target(file):
            attempts.append(str(file))
            raise AssertionError(f"测试禁止读取受保护配置/探针文件：{file!r}")
        return original_io_open(file, *args, **kwargs)

    monkeypatch.setattr(builtins, "open", guarded)
    monkeypatch.setattr(io, "open", guarded_io)
    yield
    assert attempts == [], f"出现受保护配置/探针读取尝试：{attempts!r}"


# 读取受控临时 PG 配置；未启用时跳过，配置形状/主机/端口不符即失败关闭，绝不回退用户配置。
# Args: 无。
# Returns: 配置字典。
def _pg_config() -> dict[str, Any]:
    """任何 admin connect 之前先核对受控路径与主机/端口；不回显口令或连接串。"""
    if os.getenv("RUN_MAIN_COMMON_PG_TESTS") != "1":
        pytest.skip("RUN_MAIN_COMMON_PG_TESTS 未开启")
    raw = os.getenv("MAIN_COMMON_PG_CONFIG", "")
    if not raw:
        raise RuntimeError("MAIN_COMMON_PG_CONFIG 未配置")
    path = Path(raw)
    if not path.is_file() or path.name != _ENV_FILE_NAME or path.parent.name != _ENV_DIR_NAME:
        raise RuntimeError("配置必须指向受控临时 PG 环境文件，拒绝其它路径")
    config = json.loads(path.read_text(encoding="utf-8"))
    try:
        port = int(config.get("port", 0))
    except (TypeError, ValueError):
        raise RuntimeError("临时 PG 端口配置非法，拒绝连接") from None
    if str(config.get("host")) != _EXPECTED_HOST or port != _EXPECTED_PORT:
        raise RuntimeError("临时 PG 主机或端口不符合沙箱约束，拒绝连接")
    return config


class _Sandbox:
    """独立空库 + 属主角色，名称固定 mainkv_ 前缀 + 随机后缀，清理严格校验前缀。"""

    # 作用：初始化沙箱名称与随机口令。
    # Args: config - 维护连接配置。
    # Returns: 无返回值。
    def __init__(self, config: dict[str, Any]) -> None:
        """口令只存进程内存；名称随机，避免与既有对象冲突。"""
        self.config = config
        self.suffix = uuid.uuid4().hex[:12]
        self.database = f"{_PREFIX}db_{self.suffix}"
        self.owner = f"{_PREFIX}o_{self.suffix}"
        self.password = uuid.uuid4().hex

    # 作用：以维护连接访问服务器默认库。
    # Args: self - 沙箱。
    # Returns: asyncpg 连接。
    async def admin(self) -> asyncpg.Connection:
        """使用受控配置的 database/port/username/password/host 字段，不回显。"""
        base = self.config
        return await asyncpg.connect(
            host=base["host"],
            port=int(base["port"]),
            user=base["username"],
            password=base["password"],
            database=base["database"],
        )

    # 作用：拼装本文件角色连接串（仅内部使用，不打印）。
    # Args: self - 沙箱；user - 角色。
    # Returns: 角色 URL。
    def role_url(self, user: str) -> str:
        """仅在进程内传给 asyncpg.connect，不落盘、不进日志与断言消息。"""
        base = self.config
        return (
            f"postgresql://{user}:{self.password}@"
            f"{base['host']}:{int(base['port'])}/{self.database}"
        )

    # 作用：创建本文件角色与空库。
    # Args: self - 沙箱。
    # Returns: 无返回值。
    async def create(self) -> None:
        """业务 DDL 一律发生在随后新建的随机库内；服务器层只增加本文件随机对象。"""
        connection = await self.admin()
        try:
            await connection.execute(
                f"CREATE ROLE \"{self.owner}\" LOGIN PASSWORD '{self.password}'"
            )
            await connection.execute(f'CREATE DATABASE "{self.database}" OWNER "{self.owner}"')
        finally:
            await connection.close()

    # 作用：按精确随机名清理本文件库/角色/会话，并用独立查询验证零残留后记录收据。
    # Args: self - 沙箱。
    # Returns: 清理收据（本次精确库/角色/会话计数）。
    async def drop(self) -> dict[str, int]:
        """只删本文件精确随机名对象；不做前缀扫描删除，不 DROP/ALTER 其它对象。"""
        connection = await self.admin()
        try:
            await connection.execute(
                "SELECT pg_terminate_backend(pid) FROM pg_stat_activity "
                "WHERE datname=$1 AND pid <> pg_backend_pid()",
                self.database,
            )
            await connection.execute(f'DROP DATABASE IF EXISTS "{self.database}"')
            await connection.execute(f'DROP ROLE IF EXISTS "{self.owner}"')
            receipt = await self._own_residue(connection)
        finally:
            await connection.close()
        for label, count in receipt.items():
            if count:
                raise AssertionError(f"沙箱清理未达硬零：{label}={count}（精确随机名对象仍有残留）")
        _RECEIPTS.append(dict(receipt))
        return receipt

    # 作用：独立查询本次精确随机库/角色/会话的残留计数。
    # Args: self - 沙箱；connection - 维护连接。
    # Returns: {database, role, session} 三项精确计数。
    async def _own_residue(self, connection: asyncpg.Connection) -> dict[str, int]:
        """只按精确名查询，不做前缀模糊匹配；任何非零由调用方报告。"""
        database = await connection.fetchval(
            "SELECT count(*) FROM pg_database WHERE datname = $1", self.database
        )
        role = await connection.fetchval(
            "SELECT count(*) FROM pg_roles WHERE rolname = $1", self.owner
        )
        session = await connection.fetchval(
            "SELECT count(*) FROM pg_stat_activity WHERE datname = $1 OR usename = $2",
            self.database,
            self.owner,
        )
        return {"database": int(database), "role": int(role), "session": int(session)}


# 作用：把真实 FileStore 的 DB 连接装配指向沙箱，并在事务内注入真实会话身份 GUC。
# Args: monkeypatch - 补丁夹具；sandbox - 沙箱。
# Returns: 装配账本（connects 计数、settings_url）。
def _install_sandbox_connection(
    monkeypatch: pytest.MonkeyPatch, sandbox: _Sandbox
) -> dict[str, Any]:
    """唯一 DB 连接装配替身：SQL/GUC 与 pg_pool 同式，不伪造任何查询结果。"""
    state: dict[str, Any] = {"connects": 0, "settings_url": sandbox.role_url(sandbox.owner)}

    # 作用：装配事务范围内的沙箱连接。
    # Args: tenant_id/user_id/role - 会话身份。
    # Returns: 已设 GUC 的 asyncpg 连接。
    @asynccontextmanager
    async def sandbox_connection(
        tenant_id: int, user_id: int, role: str
    ) -> AsyncIterator[asyncpg.Connection]:
        """每个保存/读取事务内设置真实会话 GUC，与生产 pg_connection 同式。"""
        state["connects"] += 1
        connection = await asyncpg.connect(sandbox.role_url(sandbox.owner))
        try:
            async with connection.transaction():
                await connection.execute(
                    "SELECT set_config('app.current_tenant_id', $1, true), "
                    "set_config('app.current_user_id', $2, true), "
                    "set_config('app.current_role', $3, true)",
                    str(tenant_id),
                    str(user_id),
                    role,
                )
                yield connection
        finally:
            await connection.close()

    monkeypatch.setattr(file_module, "pg_connection", sandbox_connection)
    monkeypatch.setattr(
        file_module,
        "get_settings",
        lambda: SimpleNamespace(database_url=state["settings_url"]),
    )
    return state


# 作用：合成受信 RequestIdentity 并用替身 settings 绑定集中租户策略。
# Args: tenant_id - 租户；user_id - 主体；role - 角色。
# Returns: 上下文管理器（退出时精确恢复）。
@contextmanager
def _trusted_identity(
    *, tenant_id: int = _TENANT_ID, user_id: int = _OWNER_ID, role: str = _USER_ROLE
) -> Iterator[None]:
    """身份仅经服务端公共 ContextVar 绑定，明确不是生产认证。"""
    context = AppContext(settings=SimpleNamespace(multi_tenant=False))
    identity = RequestIdentity(tenant_id=tenant_id, user_id=user_id, role=role)
    with use_app_context(context):
        with use_identity_context(identity):
            yield


# 作用：在合成受信身份下执行一个真实读取/写入协程。
# Args: coro - 待执行协程；tenant/user/role - 身份三元组。
# Returns: 协程返回值。
def _run(
    coro: Coroutine[Any, Any, Any],
    *,
    tenant: int = _TENANT_ID,
    user: int = _OWNER_ID,
    role: str = _USER_ROLE,
) -> Any:
    """统一经公共身份绑定入口执行，避免各用例自造身份通道。"""
    with _trusted_identity(tenant_id=tenant, user_id=user, role=role):
        return asyncio.run(coro)


# 作用：在真实沙箱上跑真实迁移并初始化真实 FileStore。
# Args: store - 真实 FileStore；sandbox - 沙箱。
# Returns: 已应用迁移文件名列表。
async def _bootstrap(store: FileStore, sandbox: _Sandbox) -> list[str]:
    """迁移与初始化都走真实实现；不预建表、不手写 DDL。"""
    from src.db.migrations import run_migrations

    applied = await run_migrations(sandbox.role_url(sandbox.owner))
    await store.initialize()
    return applied


# 作用：自建出版契约字典：版本/哈希由原文 sha256 派生，doc_id 供精确读取三方复核。
# Args: doc_id - 文档身份；content - 原文。
# Returns: 可直接传给 save(publication=...) 的映射。
def _publication(doc_id: str, content: bytes) -> dict[str, str]:
    """不导入 trading_theory/screening；字段即精确读取校验所需最小三元组。"""
    digest = hashlib.sha256(content).hexdigest()
    return {
        "source_doc_id": doc_id,
        "source_version": digest,
        "source_revision_hash": digest,
        "publication_basis": "main-common-acceptance-contract",
    }


# 作用：真实出版保存一行，返回播种账本。
# Args: store - 真实 FileStore；filename - 纯文件名；text - 原文；scope - 知识范围；
#       doc_id - 文档身份；tag_ids - 标签数组。
# Returns: 播种账本（file_id/doc_id/version/hash/content/filename/scope/publication）。
async def _seed(
    store: FileStore,
    *,
    filename: str,
    text: str,
    scope: str,
    doc_id: str,
    tag_ids: list[int] | None = None,
) -> dict[str, Any]:
    """版本推进与唯一索引都由真实 save 校验，测试不写 SQL。"""
    content = text.encode("utf-8")
    publication = _publication(doc_id, content)
    file_id = await store.save(
        filename,
        content,
        knowledge_scope=scope,
        tag_ids=list(tag_ids if tag_ids is not None else _TAG_IDS),
        publication=publication,
        require_durable=True,
    )
    return {
        "file_id": file_id,
        "doc_id": doc_id,
        "version": publication["source_version"],
        "hash": publication["source_revision_hash"],
        "content": content,
        "publication": publication,
        "filename": filename,
        "scope": scope,
    }


# 作用：按播种账本精确读取原文（读取实现零替换）。
# Args: store - 真实 FileStore；record - 播种账本；scope/tenant/user/role - 覆盖项。
# Returns: 读取结果或 None。
def _read_seeded(
    store: FileStore,
    record: dict[str, Any],
    *,
    scope: str = "",
    tenant: int = _TENANT_ID,
    user: int = _OWNER_ID,
    role: str = _USER_ROLE,
) -> Any:
    """三元组原样取自播种账本，不重算不猜测。"""
    return _run(
        store.get_exact_version(
            doc_id=record["doc_id"],
            source_version=record["version"],
            source_revision_hash=record["hash"],
            knowledge_scope=scope,
        ),
        tenant=tenant,
        user=user,
        role=role,
    )


# 作用：以受控身份执行裸 SQL（仅用于篡改与库层事实，不参与被测读取路径）。
# Args: sandbox - 沙箱；sql - 语句；*args - 参数；tenant_id/user_id/role - 会话身份。
# Returns: asyncpg 执行结果文本。
async def _raw_execute(
    sandbox: _Sandbox,
    sql: str,
    *args: Any,
    tenant_id: int = _TENANT_ID,
    user_id: int = _OWNER_ID,
    role: str = _USER_ROLE,
) -> str:
    """与 pg_pool 同式设置真实会话 GUC；只操作本沙箱库内数据。"""
    connection = await asyncpg.connect(sandbox.role_url(sandbox.owner))
    try:
        async with connection.transaction():
            await connection.execute(
                "SELECT set_config('app.current_tenant_id', $1, true), "
                "set_config('app.current_user_id', $2, true), "
                "set_config('app.current_role', $3, true)",
                str(tenant_id),
                str(user_id),
                role,
            )
            return await connection.execute(sql, *args)
    finally:
        await connection.close()


# 作用：建真沙箱、装配真连接、跑真迁移并初始化真 FileStore；读取实现零替换。
# Args: monkeypatch - 补丁器。
# Returns: 迭代器，产出环境账本。
@pytest.fixture()
def kg_env(monkeypatch: pytest.MonkeyPatch) -> Iterator[dict[str, Any]]:
    """函数级独立沙箱：每个用例互不共享库、角色与行；finally 只清理本文件对象。"""
    sandbox = _Sandbox(_pg_config())
    try:
        asyncio.run(sandbox.create())
        control = _install_sandbox_connection(monkeypatch, sandbox)
        store = FileStore()
        applied = asyncio.run(_bootstrap(store, sandbox))
        yield {"sandbox": sandbox, "store": store, "applied": applied, "control": control}
    finally:
        asyncio.run(sandbox.drop())


class TestKnowledgeVersionRealSchema:
    """36.5：真实 schema 上出版版本存储、索引状态回写与精确读取契约。"""

    # 作用：验证出版保存后精确读取返回原文与固定契约字段，且索引状态可精确回写。
    # Args: self - 测试实例；kg_env - 沙箱环境账本。
    # Returns: 无返回值；断言失败时由 pytest 报告。
    def test_exact_read_roundtrip_and_index_state(self, kg_env: dict[str, Any]) -> None:
        """publication 必须按存储原值返回；索引状态只按 id 精确回写。"""
        # Arrange
        store = kg_env["store"]
        name = "mainkv-版本往返.md"
        doc_id = f"document:private:{_TENANT_ID}:{_OWNER_ID}:{name}"
        record = _run(
            _seed(store, filename=name, text="mainkv 往返原文", scope="private", doc_id=doc_id)
        )
        # Act
        result = _read_seeded(store, record)
        marked = _run(store.mark_index_state(record["file_id"], "ready"))
        after_mark = _read_seeded(store, record)
        latest = _run(store.get_latest_versions(name, knowledge_scope="private", limit=2))
        # Assert
        assert result is not None
        assert result["id"] == record["file_id"]
        assert result["file_data"] == record["content"]
        assert result["size"] == len(record["content"])
        assert result["scope"] == "private"
        assert result["tenant_id"] == _TENANT_ID
        assert result["owner_user_id"] == _OWNER_ID
        assert result["tag_ids"] == _TAG_IDS
        assert result["source_version"] == record["version"]
        assert result["source_revision_hash"] == record["hash"]
        assert result["index_state"] == "pending"
        assert result["publication"] == dict(record["publication"])
        assert marked is True
        assert after_mark is not None and after_mark["index_state"] == "ready"
        assert [row["id"] for row in latest] == [record["file_id"]]

    # 作用：验证未知索引状态值失败关闭，且范围不符时不误伤其它范围行。
    # Args: self - 测试实例；kg_env - 沙箱环境账本。
    # Returns: 无返回值；断言失败时由 pytest 报告。
    def test_mark_index_state_rejects_unknown_and_scope_mismatch(
        self, kg_env: dict[str, Any]
    ) -> None:
        """未知状态必须 ValueError；scope 不匹配回写 0 行返回 False。"""
        # Arrange
        store = kg_env["store"]
        name = "mainkv-索引状态边界.md"
        doc_id = f"document:private:{_TENANT_ID}:{_OWNER_ID}:{name}"
        record = _run(
            _seed(
                store,
                filename=name,
                text="mainkv 索引状态边界原文",
                scope="private",
                doc_id=doc_id,
            )
        )
        # Act / Assert
        with pytest.raises(ValueError):
            _run(store.mark_index_state(record["file_id"], "unknown"))
        assert (
            _run(store.mark_index_state(record["file_id"], "ready", knowledge_scope="tenant"))
            is False
        )
        assert _run(store.mark_index_state(record["file_id"], "ready")) is True
        assert _run(store.mark_index_state(record["file_id"], "failed")) is True

    # 作用：验证旧普通上传兼容：可落行但不进入版本读取与精确读取通道。
    # Args: self - 测试实例；kg_env - 沙箱环境账本。
    # Returns: 无返回值；断言失败时由 pytest 报告。
    def test_plain_upload_legacy_compat(self, kg_env: dict[str, Any]) -> None:
        """source_version 为空的旧行不参与 get_latest_versions，也不能被精确读取。"""
        # Arrange
        store = kg_env["store"]
        name = "mainkv-旧上传兼容.md"
        legacy_id = _run(store.save(name, b"mainkv legacy bytes", knowledge_scope="private"))
        # Act
        latest = _run(store.get_latest_versions(name, knowledge_scope="private"))
        # Assert
        assert isinstance(legacy_id, int)
        assert latest == []
        with pytest.raises(ValueError):
            _run(
                store.get_exact_version(
                    doc_id="document:private:1:4:legacy.md",
                    source_version="",
                    source_revision_hash="0" * 64,
                )
            )

    # 作用：验证同版本重复提交被拒绝且携带既有行信息，拒绝后原版本仍可读。
    # Args: self - 测试实例；kg_env - 沙箱环境账本。
    # Returns: 无返回值；断言失败时由 pytest 报告。
    def test_duplicate_same_version_rejected_then_recovery(
        self, kg_env: dict[str, Any]
    ) -> None:
        """同内容派生同版本：不得静默复用旧行，也不得覆盖。"""
        # Arrange
        store = kg_env["store"]
        name = "mainkv-同版本拒绝.md"
        doc_id = f"document:private:{_TENANT_ID}:{_OWNER_ID}:{name}"
        record = _run(
            _seed(store, filename=name, text="mainkv 同版本原文", scope="private", doc_id=doc_id)
        )
        # Act / Assert
        with pytest.raises(FileStoreDuplicateVersionError) as excinfo:
            _run(
                _seed(
                    store,
                    filename=name,
                    text="mainkv 同版本原文",
                    scope="private",
                    doc_id=doc_id,
                )
            )
        assert excinfo.value.file_id == record["file_id"]
        assert excinfo.value.index_state == "pending"
        recovered = _read_seeded(store, record)
        assert recovered is not None and recovered["file_data"] == record["content"]
        latest = _run(store.get_latest_versions(name, knowledge_scope="private", limit=2))
        assert [row["id"] for row in latest] == [record["file_id"]]

    # 作用：验证两次出版提交按已提交追加序排列，并保留前版本指针。
    # Args: self - 测试实例；kg_env - 沙箱环境账本。
    # Returns: 无返回值；断言失败时由 pytest 报告。
    def test_two_commits_append_order_and_supersedes(self, kg_env: dict[str, Any]) -> None:
        """最新在前、旧版在后；新版 supersedes_file_id 指向旧版行主键。"""
        # Arrange
        store = kg_env["store"]
        name = "mainkv-追加顺序.md"
        doc_id = f"document:private:{_TENANT_ID}:{_OWNER_ID}:{name}"
        first = _run(
            _seed(store, filename=name, text="mainkv 第一版", scope="private", doc_id=doc_id)
        )
        second = _run(
            _seed(store, filename=name, text="mainkv 第二版", scope="private", doc_id=doc_id)
        )
        # Act
        latest = _run(store.get_latest_versions(name, knowledge_scope="private", limit=2))
        first_read = _read_seeded(store, first)
        second_read = _read_seeded(store, second)
        # Assert
        assert first["version"] != second["version"]
        assert [row["id"] for row in latest] == [second["file_id"], first["file_id"]]
        assert latest[0]["supersedes_file_id"] == first["file_id"]
        assert latest[1]["supersedes_file_id"] is None
        assert first_read is not None and first_read["file_data"] == first["content"]
        assert second_read is not None and second_read["file_data"] == second["content"]


class TestKnowledgeVersionVisibility:
    """36.5：精确读取与版本查询只认当前身份可见性，拒绝用户/租户越权。"""

    # 作用：验证同租户他人与跨租户身份读取 private 行必须拒绝，属主读取不受影响。
    # Args: self - 测试实例；kg_env - 沙箱环境账本。
    # Returns: 无返回值；断言失败时由 pytest 报告。
    def test_cross_user_and_cross_tenant_denied(self, kg_env: dict[str, Any]) -> None:
        """越权读取返回 None 而非数据；版本查询同样不泄露。"""
        # Arrange
        store = kg_env["store"]
        name = "mainkv-越权拒绝.md"
        doc_id = f"document:private:{_TENANT_ID}:{_OWNER_ID}:{name}"
        record = _run(
            _seed(store, filename=name, text="mainkv 越权原文", scope="private", doc_id=doc_id)
        )
        # Act
        owner_read = _read_seeded(store, record)
        same_tenant_other = _read_seeded(store, record, user=_OTHER_USER_ID)
        cross_tenant = _read_seeded(
            store, record, tenant=_OTHER_TENANT_ID, user=_OTHER_TENANT_USER_ID
        )
        other_latest = _run(
            store.get_latest_versions(name, knowledge_scope="private"),
            user=_OTHER_USER_ID,
        )
        # Assert
        assert owner_read is not None
        assert same_tenant_other is None
        assert cross_tenant is None
        assert other_latest == []
        assert (
            _run(store.mark_index_state(record["file_id"], "ready"), user=_OTHER_USER_ID)
            is False
        )

    # 作用：验证 SHA 不符与行内篡改都被结构化拒绝，恢复原字节后读取恢复。
    # Args: self - 测试实例；kg_env - 沙箱环境账本。
    # Returns: 无返回值；断言失败时由 pytest 报告。
    def test_sha_mismatch_and_tamper_rejected_then_recovery(
        self, kg_env: dict[str, Any]
    ) -> None:
        """请求哈希不符 0 行返回 None；行字节被改即 FileStoreIntegrityError。"""
        # Arrange
        store = kg_env["store"]
        sandbox = kg_env["sandbox"]
        name = "mainkv-哈希拒绝.md"
        doc_id = f"document:private:{_TENANT_ID}:{_OWNER_ID}:{name}"
        record = _run(
            _seed(store, filename=name, text="mainkv 哈希原文", scope="private", doc_id=doc_id)
        )
        absent_hash = hashlib.sha256(b"mainkv-absent-hash-probe").hexdigest()
        absent_version = hashlib.sha256(b"mainkv-absent-version-probe").hexdigest()
        # Act / Assert：请求侧不符
        mismatched = _run(
            store.get_exact_version(
                doc_id=record["doc_id"],
                source_version=record["version"],
                source_revision_hash=absent_hash,
            )
        )
        absent = _run(
            store.get_exact_version(
                doc_id=record["doc_id"],
                source_version=absent_version,
                source_revision_hash=record["hash"],
            )
        )
        assert mismatched is None and absent is None
        # Act / Assert：行内篡改必须真实落行，并被字节自证拒绝
        tampered = b"X" * len(record["content"])
        tamper_status = asyncio.run(
            _raw_execute(
                sandbox,
                "UPDATE knowledge_files SET file_data = $2 WHERE id = $1",
                record["file_id"],
                tampered,
            )
        )
        assert tamper_status == "UPDATE 1"
        with pytest.raises(FileStoreIntegrityError) as excinfo:
            _read_seeded(store, record)
        assert excinfo.value.reason == "bytes"
        # Act / Assert：恢复原字节后同一请求恢复成功
        restore_status = asyncio.run(
            _raw_execute(
                sandbox,
                "UPDATE knowledge_files SET file_data = $2 WHERE id = $1",
                record["file_id"],
                record["content"],
            )
        )
        assert restore_status == "UPDATE 1"
        recovered = _read_seeded(store, record)
        assert recovered is not None and recovered["file_data"] == record["content"]


class TestKnowledgeVersionConnectionRebuild:
    """36.5：连接被整体重建后，旧版本仍可按精确三元组读取。"""

    # 作用：以真实 pg_terminate_backend 终止读取持有的连接，验证真实故障与恢复链。
    # Args: self - 测试实例；kg_env - 沙箱环境账本；monkeypatch - 补丁夹具。
    # Returns: 无返回值；断言失败时由 pytest 报告。
    def test_old_version_readable_after_backend_rebuild(
        self, kg_env: dict[str, Any], monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """真实查询路径必须收到 FileStoreUnavailableError，再以同一三元组恢复成功。"""
        # Arrange
        store = kg_env["store"]
        sandbox = kg_env["sandbox"]
        name = "mainkv-连接重建.md"
        doc_id = f"document:private:{_TENANT_ID}:{_OWNER_ID}:{name}"
        record = _run(
            _seed(store, filename=name, text="mainkv 重建原文", scope="private", doc_id=doc_id)
        )

        # 作用：门控读取持有真实连接 → 记录后端 pid → 维护连接真实终止 → 放行并恢复。
        # Args: 无。
        # Returns: {"terminated": 终止结果, "failure": 真实异常, "recovered": 恢复读取}。
        async def _scenario() -> dict[str, Any]:
            release = asyncio.Event()
            ready = asyncio.Event()
            holder: dict[str, int] = {}
            gate = {"enabled": True}
            _install_gated_connection(
                monkeypatch,
                sandbox,
                gate=gate,
                release=release,
                holder=holder,
                ready=ready,
            )
            task = asyncio.create_task(
                store.get_exact_version(
                    doc_id=record["doc_id"],
                    source_version=record["version"],
                    source_revision_hash=record["hash"],
                )
            )
            try:
                await asyncio.wait_for(ready.wait(), timeout=15.0)
            except TimeoutError as exc:
                raise AssertionError("读取未在期限内持有真实连接，拒绝放行终止") from exc
            assert holder.get("pid", 0) > 0, "读取未记录真实后端 pid"
            admin = await sandbox.admin()
            try:
                terminated = await admin.fetchval(
                    "SELECT pg_terminate_backend($1)", holder["pid"]
                )
            finally:
                await admin.close()
            release.set()
            failure: BaseException | None = None
            try:
                await task
            except FileStoreUnavailableError as exc:
                failure = exc
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                failure = exc
            # 撤去仅连接边界的故障门控后，同一三元组必须恢复成功
            gate["enabled"] = False
            recovered = await store.get_exact_version(
                doc_id=record["doc_id"],
                source_version=record["version"],
                source_revision_hash=record["hash"],
            )
            return {
                "terminated": bool(terminated),
                "failure": failure,
                "recovered": recovered,
            }

        # Act
        outcome = _run(_scenario())
        # Assert：真实终止至少 1 个后端；失败来自真实查询路径而非 Mock 异常
        assert outcome["terminated"] is True
        failure = outcome["failure"]
        assert isinstance(failure, FileStoreUnavailableError)
        assert not isinstance(failure, FileStoreIntegrityError)
        assert failure.__cause__ is None
        assert "postgresql://" not in str(failure)
        recovered = outcome["recovered"]
        assert recovered is not None
        assert recovered["id"] == record["file_id"]
        assert recovered["file_data"] == record["content"]
        assert kg_env["control"]["connects"] >= 2


class TestKnowledgeVersionSandboxZeroResidue:
    """36.5：跑完后 mainkv_ 前缀库/角色/活动会话硬零，并以正对照证明计数通道有效。"""

    # 作用：核对逐例清理收据与 mainkv_ 前缀三列计数，并用受控库/角色做正对照。
    # Args: 无。
    # Returns: 无返回值；断言失败时由 pytest 报告。
    def test_mainkv_prefix_leaves_zero_database_role_session(self) -> None:
        """收据逐项为零；库/角色/会话按精确前缀计数为零；前缀对象只报告不删除。"""
        # Arrange
        config = _pg_config()

        # 作用：管理员连接上执行正对照与前缀负对照只读计数。
        # Args: 无。
        # Returns: 正对照与三列前缀计数。
        async def _counts() -> dict[str, int]:
            connection = await asyncpg.connect(
                host=config["host"],
                port=int(config["port"]),
                user=config["username"],
                password=config["password"],
                database=config["database"],
            )
            try:
                positive_db = await connection.fetchval(
                    "SELECT count(*) FROM pg_database WHERE datname = $1", config["database"]
                )
                positive_role = await connection.fetchval(
                    "SELECT count(*) FROM pg_roles WHERE rolname = $1", config["username"]
                )
                databases = await connection.fetchval(
                    "SELECT count(*) FROM pg_database WHERE starts_with(datname, $1)", _PREFIX
                )
                roles = await connection.fetchval(
                    "SELECT count(*) FROM pg_roles WHERE starts_with(rolname, $1)", _PREFIX
                )
                sessions = await connection.fetchval(
                    "SELECT count(*) FROM pg_stat_activity "
                    "WHERE starts_with(datname, $1) OR starts_with(usename, $1)",
                    _PREFIX,
                )
            finally:
                await connection.close()
            return {
                "positive_db": int(positive_db),
                "positive_role": int(positive_role),
                "databases": int(databases),
                "roles": int(roles),
                "sessions": int(sessions),
            }

        # Act
        counts = asyncio.run(_counts())
        receipts = [dict(item) for item in _RECEIPTS]
        # Assert：正对照证明计数通道与目标对象匹配；逐例收据与前缀负对照必须硬零
        assert counts["positive_db"] == 1
        assert counts["positive_role"] == 1
        assert receipts, "缺少逐用例清理收据（本次运行应至少包含一次沙箱清理）"
        assert all(
            (item["database"], item["role"], item["session"]) == (0, 0, 0)
            for item in receipts
        ), f"存在非零清理收据：{receipts!r}"
        assert (counts["databases"], counts["roles"], counts["sessions"]) == (0, 0, 0), (
            f"沙箱零残留被破坏：库={counts['databases']} 角色={counts['roles']} "
            f"会话={counts['sessions']}；前缀对象仅报告，本文件从不删除非本用例对象"
        )


# 作用：仅在连接边界装配门控：读取持有真实连接并记录后端 pid，放行后才继续真实查询。
# Args: monkeypatch - 补丁夹具；sandbox - 沙箱；gate - {"enabled": bool} 故障开关；
#       release - 放行事件；holder - 记录真实后端 pid；ready - 已持有连接并记录 pid 的就绪事件。
# Returns: 无返回值。
def _install_gated_connection(
    monkeypatch: pytest.MonkeyPatch,
    sandbox: _Sandbox,
    *,
    gate: dict[str, bool],
    release: asyncio.Event,
    holder: dict[str, int],
    ready: asyncio.Event,
) -> None:
    """异常必须来自真实被终止后端：不 Mock 异常、不替换 SQL 与读取实现。"""

    # 作用：装配带门控的事务范围内沙箱连接。
    # Args: tenant_id/user_id/role - 会话身份。
    # Returns: 真实 asyncpg 连接，门控开启时先记录 pid 并等待放行。
    @asynccontextmanager
    async def gated_connection(
        tenant_id: int, user_id: int, role: str
    ) -> AsyncIterator[asyncpg.Connection]:
        """与生产 pg_connection 同式设 GUC；故障由外部真实 pg_terminate_backend 注入。"""
        connection = await asyncpg.connect(sandbox.role_url(sandbox.owner))
        try:
            async with connection.transaction():
                await connection.execute(
                    "SELECT set_config('app.current_tenant_id', $1, true), "
                    "set_config('app.current_user_id', $2, true), "
                    "set_config('app.current_role', $3, true)",
                    str(tenant_id),
                    str(user_id),
                    role,
                )
                holder["pid"] = int(await connection.fetchval("SELECT pg_backend_pid()"))
                ready.set()
                if gate["enabled"]:
                    await release.wait()
                yield connection
        finally:
            await connection.close()

    monkeypatch.setattr(file_module, "pg_connection", gated_connection)
