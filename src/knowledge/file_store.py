"""知识库文件存储：PostgreSQL bytea 与受控磁盘回退。"""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import traceback
from collections.abc import AsyncIterator, Mapping
from contextlib import asynccontextmanager
from contextvars import ContextVar
from typing import Any

from src.config import get_settings
from src.logging_config import get_logger
from src.memory.pg_pool import defer_pg_error_logging, pg_connection

logger = get_logger(__name__)

_VISIBLE_FILES_SQL = """
    knowledge_scope = 'system'
    OR (current_setting('app.current_role', true) <> 'super_admin' AND (
        (knowledge_scope = 'tenant' AND tenant_id = {tenant_param})
        OR (knowledge_scope = 'private' AND tenant_id = {tenant_param} AND user_id = {user_param})
    ))
"""

_ALLOWED_INDEX_STATES: frozenset[str] = frozenset({"pending", "ready", "failed"})

# 精确原文三元组的输入边界与 scope 白名单（I/O 前严格校验，不猜文件名）。
_MAX_DOC_ID_LENGTH = 128
_MAX_SOURCE_VERSION_LENGTH = 64
_EXACT_READ_SCOPES: frozenset[str] = frozenset({"system", "tenant", "private"})
_LOWER_HEX_DIGITS: frozenset[str] = frozenset("0123456789abcdef")

# 精确原文读取专用：helper 静默/严格开关（任务隔离，旧调用默认语义不变）。
_EXACT_READ_QUIET: ContextVar[bool] = ContextVar("knowledge_exact_read_quiet", default=False)

# 同名身份命名锁：独立语句先行执行，事务提交自动释放，不依赖查询计划是否触达 CTE。
_IDENTITY_LOCK_SQL = "SELECT pg_advisory_xact_lock(hashtext($1)::bigint)"

# 前驱取已提交追加序：同名写入经身份锁串行，SERIAL id 即提交序；uploaded_at=NOW() 仅事务开始观察时间。
_PREV_VERSION_SQL = """
    SELECT k.id, k.source_version, k.source_revision_hash, k.publication_json, k.index_state
    FROM knowledge_files AS k
    WHERE k.filename = $1 AND k.knowledge_scope = $2
      AND k.tenant_id = $3 AND k.user_id = $4 AND k.source_version <> ''
    ORDER BY k.id DESC LIMIT 1
"""

_DUPLICATE_VERSION_SQL = """
    SELECT id, index_state FROM knowledge_files
    WHERE filename = $1 AND knowledge_scope = $2 AND tenant_id = $3
      AND user_id = $4 AND source_version = $5
"""

_INSERT_PUBLICATION_SQL = """
    INSERT INTO knowledge_files
    (filename, content_type, file_data, size, tenant_id, user_id, knowledge_scope,
     datasource, tag_ids, source_version, source_revision_hash, publication_json,
     index_state, supersedes_file_id)
    VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11, $12, $13, $14)
    RETURNING id
"""

# latest 同上取已提交追加序（id 倒序），不得按事务开始观察时间排序。
_LATEST_VERSIONS_SQL = (
    """
    SELECT id, filename, source_version, source_revision_hash, publication_json,
           index_state, supersedes_file_id, uploaded_at
    FROM knowledge_files WHERE filename = $1 AND source_version <> '' AND (
"""
    + _VISIBLE_FILES_SQL.format(tenant_param="$2", user_param="$3")
    + """
    ) AND ($4 = '' OR knowledge_scope = $4)
    ORDER BY id DESC LIMIT $5
"""
)

_MARK_INDEX_STATE_SQL = """
    UPDATE knowledge_files SET index_state = $2
    WHERE id = $1 AND tenant_id = $3 AND user_id = $4
      AND ($5 = '' OR knowledge_scope = $5)
"""

# 精确三元组＋当前身份可见性；LIMIT 2 供多匹配拒绝，不按 filename/uploaded_at 回退。
_EXACT_VERSION_SQL = (
    """
    SELECT id, filename, content_type, file_data, size, uploaded_at, knowledge_scope,
           datasource, tag_ids, tenant_id, user_id, source_version, source_revision_hash,
           publication_json, index_state
    FROM knowledge_files WHERE source_version = $1 AND source_revision_hash = $2 AND (
"""
    + _VISIBLE_FILES_SQL.format(tenant_param="$3", user_param="$4")
    + """
    ) AND ($5 = '' OR knowledge_scope = $5)
    LIMIT 2
"""
)


class FileStoreUnavailableError(RuntimeError):
    """知识文件持久化不可用：出版路径必须失败关闭，不得降级冒充完成。"""


class FileStoreDuplicateVersionError(RuntimeError):
    """同名同版本已存在：携带既有 file_id 与索引状态供恢复或 409 分流。"""

    # 作用：构造结构化重复版本错误。
    # Args: file_id - 既有版本行主键；index_state - 既有索引状态。
    # Returns: 无返回值（实例化）。
    def __init__(self, *, file_id: int, index_state: str = "") -> None:
        """记录既有行主键与索引状态，绝不静默复用旧版。"""
        self.file_id = file_id
        self.index_state = index_state or "pending"
        super().__init__(f"同名同版本已存在: file_id={file_id} index_state={self.index_state}")


class FileStoreIntegrityError(RuntimeError):
    """精确原文完整性冲突：多匹配/元数据/字节自证不一致，拒绝冒充原文。"""

    # 作用：构造带非敏感定位码的完整性错误。
    # Args: reason - 定位码（multi_match/metadata/bytes）。
    # Returns: 无返回值（实例化）。
    def __init__(self, reason: str = "integrity") -> None:
        """只携带定位码，不带原文、私密标识或底层异常正文。"""
        self.reason = reason
        super().__init__(f"知识原文完整性冲突: {reason}")


def _current_identity() -> tuple[int, int]:
    """读取当前请求的租户和用户身份。

    Returns:
        `(tenant_id, user_id)` 二元组。
    """
    from src.api.auth import get_current_tenant_id, get_current_user_id

    tenant_id = get_current_tenant_id()
    user_id = get_current_user_id()
    logger.debug("读取知识文件身份", tenant_id=tenant_id, user_id=user_id)
    return tenant_id, user_id


# 作用：精确原文读取的 I/O 前输入校验：严格 str、无首尾空白、长度有界、scope 白名单。
# Args: doc_id - 文档身份；source_version - 版本；source_revision_hash - 64 位小写 hex；
#     knowledge_scope - 空或 system/tenant/private。
# Returns: 无返回值；非法输入抛出 ValueError，不裁剪不猜测。
def _validate_exact_version_args(
    doc_id: object,
    source_version: object,
    source_revision_hash: object,
    knowledge_scope: object,
) -> None:
    """服务端上传链路保证 revision_hash 即原文 sha256，这里只做格式与类型拒入。"""
    _require_exact_text(doc_id, field="doc_id", max_length=_MAX_DOC_ID_LENGTH)
    _require_exact_text(
        source_version, field="source_version", max_length=_MAX_SOURCE_VERSION_LENGTH
    )
    if (
        not isinstance(source_revision_hash, str)
        or len(source_revision_hash) != 64
        or any(char not in _LOWER_HEX_DIGITS for char in source_revision_hash)
    ):
        raise ValueError("source_revision_hash 必须是 64 位小写十六进制")
    if not isinstance(knowledge_scope, str) or (
        knowledge_scope != "" and knowledge_scope not in _EXACT_READ_SCOPES
    ):
        raise ValueError("knowledge_scope 仅支持空/system/tenant/private")


# 作用：严格文本字段：非空、无首尾空白、长度有界。
# Args: value - 待校验值；field - 错误定位字段名；max_length - 长度上界。
# Returns: 无返回值；非法输入抛出 ValueError。
def _require_exact_text(value: object, *, field: str, max_length: int) -> None:
    """空白或坏类型直接拒绝，不做 trim 后放行。"""
    if not isinstance(value, str) or not value or value != value.strip():
        raise ValueError(f"{field} 必须是去空白后的非空字符串")
    if len(value) > max_length:
        raise ValueError(f"{field} 长度不得超过 {max_length}")


# 作用：安全解析 publication_json 为 mapping，坏 JSON/非对象即完整性冲突。
# Args: raw - TEXT 列原始值。
# Returns: 解析后的映射；失败抛出 FileStoreIntegrityError。
def _parse_exact_publication(raw: object) -> Mapping[str, Any]:
    """先 loads 再验类型；失败不在 except 内抛出，避免 __context__ 携带 JSONDecodeError.doc。"""
    if not isinstance(raw, str) or not raw:
        raise FileStoreIntegrityError("metadata")
    parsed: object = None
    parse_failed = False
    try:
        parsed = json.loads(raw)
    except TypeError, ValueError:
        parse_failed = True
    if parse_failed or not isinstance(parsed, Mapping):
        raise FileStoreIntegrityError("metadata")
    return parsed


# 作用：复核行与 publication 三元组等于请求与列，任一处不符即完整性冲突。
# Args: row - 数据库行；publication - 已解析的出版映射；doc_id/source_version/
#     source_revision_hash - 请求三元组。
# Returns: 无返回值；冲突抛出 FileStoreIntegrityError。
def _verify_exact_metadata(
    row: Mapping[str, Any],
    publication: Mapping[str, Any],
    *,
    doc_id: str,
    source_version: str,
    source_revision_hash: str,
) -> None:
    """列、JSON 与请求三方原值严格相等，禁 str 强转掩盖坏类型。"""
    if (
        row["source_version"] != source_version
        or row["source_revision_hash"] != source_revision_hash
    ):
        raise FileStoreIntegrityError("metadata")
    if (
        publication.get("source_doc_id") != doc_id
        or publication.get("source_version") != source_version
        or publication.get("source_revision_hash") != source_revision_hash
    ):
        raise FileStoreIntegrityError("metadata")


# 作用：把 file_data 收敛为 bytes 并自证长度与 sha256 等于请求。
# Args: raw - 字节列；size - 长度台账；expected_hash - 请求的 revision hash。
# Returns: bytes 原文；类型/长度/哈希冲突抛出 FileStoreIntegrityError。
def _coerce_exact_file_data(raw: object, size: object, expected_hash: str) -> bytes:
    """只接受 bytes/memoryview，bool 冒充长度也拒绝。"""
    if isinstance(raw, memoryview):
        data = raw.tobytes()
    elif isinstance(raw, bytes):
        data = raw
    else:
        raise FileStoreIntegrityError("bytes")
    if isinstance(size, bool) or not isinstance(size, int) or size != len(data):
        raise FileStoreIntegrityError("bytes")
    if hashlib.sha256(data).hexdigest() != expected_hash:
        raise FileStoreIntegrityError("bytes")
    return data


# 作用：生成只含定位帧的安全栈：文件/行号/函数与源码行，不含异常正文与局部变量。
# Args: error - 捕获到的异常。
# Returns: 单次 Traceback 文本；无帧时仅保留头行。
def _safe_stack(error: BaseException) -> str:
    """traceback.extract_tb 不携带异常消息与 locals，供日志安全回放定位。"""
    lines = ["Traceback (most recent call last):"]
    for frame in traceback.extract_tb(error.__traceback__):
        lines.append(f'  File "{frame.filename}", line {frame.lineno}, in {frame.name}')
        if frame.line:
            lines.append(f"    {frame.line.strip()}")
    return "\n".join(lines)


class FileStore:
    """将上传的原始文件按租户和用户存入 PostgreSQL。"""

    def __init__(self) -> None:
        self._ready = False
        self._schema_ready = False
        self._pg_configured = False

    # 方法作用：通过公开生命周期接口预热知识文件存储。
    # Args: self - 当前 FileStore。
    # Returns: 无返回值。
    async def initialize(self) -> None:
        logger.debug("知识文件存储公开初始化入口", ready=self._ready)
        try:
            await self._ensure()
        except Exception as exc:
            logger.error("知识文件存储公开初始化失败", error=str(exc), exc_info=True)
            raise
        logger.info("知识文件存储公开初始化完成", ready=self._ready)

    # 作用：确保知识文件表与身份字段就绪。
    # Args: self - 当前 FileStore。
    # Returns: 就绪 True；未配置或默认路径失败 False；精确读取静默/严格模式下初始化异常原样上抛。
    async def _ensure(self) -> bool:
        """确保知识文件表和身份字段存在。

        Returns:
            出版元数据表是否确证就绪；未配置或初始化失败返回 False。
        """
        logger.debug("知识文件 PG 初始化入口", ready=self._ready)
        if self._ready and (self._schema_ready or not self._pg_configured):
            if not _EXACT_READ_QUIET.get():
                logger.info("知识文件 PG 初始化命中缓存", ready=True)
            return self._schema_ready
        settings = get_settings()
        url = settings.database_url
        self._pg_configured = bool(url and "postgres" in url)
        if not self._pg_configured:
            self._ready = True
            if not _EXACT_READ_QUIET.get():
                logger.info("知识文件启用磁盘回退", reason="PostgreSQL 未配置")
            return False

        try:
            async with pg_connection(tenant_id=1, user_id=0, role="super_admin") as conn:
                await conn.execute("""
                    CREATE TABLE IF NOT EXISTS knowledge_files (
                        id SERIAL PRIMARY KEY,
                        filename TEXT NOT NULL,
                        content_type TEXT DEFAULT 'application/octet-stream',
                        file_data BYTEA NOT NULL,
                        size BIGINT DEFAULT 0,
                        user_id INT NOT NULL DEFAULT 0,
                        tenant_id INT NOT NULL DEFAULT 1,
                        knowledge_scope VARCHAR(16) NOT NULL DEFAULT 'private',
                        datasource VARCHAR(128) NOT NULL DEFAULT '',
                        tag_ids BIGINT[] NOT NULL DEFAULT '{}',
                        source_version VARCHAR(64) NOT NULL DEFAULT '',
                        source_revision_hash VARCHAR(64) NOT NULL DEFAULT '',
                        publication_json TEXT NOT NULL DEFAULT '',
                        index_state VARCHAR(16) NOT NULL DEFAULT '',
                        supersedes_file_id INT,
                        uploaded_at TIMESTAMPTZ DEFAULT NOW()
                    )
                """)
                await conn.execute(
                    "ALTER TABLE knowledge_files ADD COLUMN IF NOT EXISTS user_id INT NOT NULL DEFAULT 0"
                )
                await conn.execute(
                    "ALTER TABLE knowledge_files ADD COLUMN IF NOT EXISTS tenant_id INT NOT NULL DEFAULT 1"
                )
                await conn.execute(
                    "ALTER TABLE knowledge_files ADD COLUMN IF NOT EXISTS "
                    "knowledge_scope VARCHAR(16) NOT NULL DEFAULT 'private'"
                )
                await conn.execute(
                    "ALTER TABLE knowledge_files ADD COLUMN IF NOT EXISTS "
                    "datasource VARCHAR(128) NOT NULL DEFAULT ''"
                )
                await conn.execute(
                    "ALTER TABLE knowledge_files ADD COLUMN IF NOT EXISTS "
                    "tag_ids BIGINT[] NOT NULL DEFAULT '{}'"
                )
                await conn.execute(
                    "ALTER TABLE knowledge_files ADD COLUMN IF NOT EXISTS "
                    "source_version VARCHAR(64) NOT NULL DEFAULT ''"
                )
                await conn.execute(
                    "ALTER TABLE knowledge_files ADD COLUMN IF NOT EXISTS "
                    "source_revision_hash VARCHAR(64) NOT NULL DEFAULT ''"
                )
                await conn.execute(
                    "ALTER TABLE knowledge_files ADD COLUMN IF NOT EXISTS "
                    "publication_json TEXT NOT NULL DEFAULT ''"
                )
                await conn.execute(
                    "ALTER TABLE knowledge_files ADD COLUMN IF NOT EXISTS "
                    "index_state VARCHAR(16) NOT NULL DEFAULT ''"
                )
                await conn.execute(
                    "ALTER TABLE knowledge_files ADD COLUMN IF NOT EXISTS supersedes_file_id INT"
                )
                await conn.execute(
                    "DO $$ BEGIN IF NOT EXISTS (SELECT 1 FROM pg_constraint "
                    "WHERE conname = 'ck_knowledge_files_scope') THEN "
                    "ALTER TABLE knowledge_files ADD CONSTRAINT ck_knowledge_files_scope "
                    "CHECK (knowledge_scope IN ('system', 'tenant', 'private')); "
                    "END IF; END $$"
                )
                await conn.execute(
                    "CREATE INDEX IF NOT EXISTS idx_knowledge_files_scope_identity_uploaded "
                    "ON knowledge_files (knowledge_scope, tenant_id, user_id, uploaded_at DESC)"
                )
                await conn.execute(
                    "CREATE UNIQUE INDEX IF NOT EXISTS uq_knowledge_files_publication_version "
                    "ON knowledge_files (knowledge_scope, tenant_id, user_id, filename, "
                    "source_version) WHERE source_version <> ''"
                )
            self._ready = True
            self._schema_ready = True
            if not _EXACT_READ_QUIET.get():
                logger.info("knowledge_files 表已就绪")
            return True
        except Exception as exc:
            self._ready = True
            self._schema_ready = False
            if _EXACT_READ_QUIET.get():
                # 精确读取由外层统一记录一次安全栈；这里保持状态供下次重试。
                raise
            logger.error("knowledge_files 创建失败，降级磁盘", error=str(exc), exc_info=True)
            return False

    # 作用：创建带当前 RLS 身份参数的池化连接。
    # Args: self - 当前 FileStore。
    # Returns: 正常 yield 连接；未配置 PostgreSQL 时 yield None；异常原样重抛（精确读取作用域内不记栈）。
    @asynccontextmanager
    async def _connect(self) -> AsyncIterator[Any | None]:
        """创建带当前 RLS 参数的 PostgreSQL 连接。

        Returns:
            已设置身份上下文的连接；连接失败返回 None。
        """
        from src.api.auth import get_current_role
        from src.knowledge.governance import normalize_role

        tenant_id, user_id = _current_identity()
        role = normalize_role(get_current_role())
        settings = get_settings()
        url = settings.database_url
        logger.debug("知识文件 PG 连接入口", tenant_id=tenant_id, user_id=user_id)
        if not url or "postgres" not in url:
            if not _EXACT_READ_QUIET.get():
                logger.info("知识文件 PG 连接回退", reason="PostgreSQL 未配置")
            yield None
            return
        try:
            async with pg_connection(
                tenant_id=tenant_id,
                user_id=user_id,
                role=role,
            ) as conn:
                yield conn
            if not _EXACT_READ_QUIET.get():
                logger.info("知识文件 PG 连接完成", tenant_id=tenant_id, user_id=user_id, role=role)
        except Exception as exc:
            if _EXACT_READ_QUIET.get():
                # 业务异常已在调用方连接 context 内收敛，这里不再提前记录未脱敏异常。
                raise
            logger.error("知识文件 PG 连接失败", error=str(exc), exc_info=True)
            raise

    async def save(
        self,
        filename: str,
        content: bytes,
        *,
        knowledge_scope: str = "private",
        datasource: str = "",
        tag_ids: list[int] | None = None,
        publication: Mapping[str, object] | None = None,
        require_durable: bool = False,
    ) -> int | None:
        """按当前身份、知识范围和标签保存原始文件。

        Args:
            filename: 原始文件名。
            content: 文件二进制内容。
            knowledge_scope: system/tenant/private 知识范围。
            datasource: 可选绑定的数据源名称。
            tag_ids: 已校验的标签 ID。
            publication: 服务端发布补丁；None 保持旧路径。
            require_durable: 出版路径要求真实持久化，不可用必须失败关闭。

        Returns:
            文件 ID；旧路径数据库不可用返回 None，出版路径失败关闭。
        """
        from src.api.auth import get_current_role
        from src.app_context import get_tenant_policy
        from src.knowledge.governance import normalize_knowledge_scope, normalize_role
        from src.security.tenant_policy import RequestIdentity

        tenant_id, user_id = _current_identity()
        role = normalize_role(get_current_role())
        normalized_scope = normalize_knowledge_scope(knowledge_scope).value
        logger.debug(
            "保存知识文件入口",
            filename=filename,
            tenant_id=tenant_id,
            user_id=user_id,
            role=role,
            knowledge_scope=normalized_scope,
        )
        if not get_tenant_policy().can_write_scope(
            normalized_scope,
            RequestIdentity(tenant_id=tenant_id, user_id=user_id, role=role),
        ):
            logger.warning(
                "保存知识文件权限拒绝",
                filename=filename,
                role=role,
                knowledge_scope=normalized_scope,
            )
            raise PermissionError(f"当前角色无权写入 {normalized_scope} 知识")
        schema_ready = await self._ensure()
        if publication and not schema_ready:
            logger.error("保存知识文件失败", filename=filename, reason="出版表未就绪")
            raise FileStoreUnavailableError("知识文件出版存储未就绪，拒绝降级")
        try:
            async with self._connect() as conn:
                if conn is None:
                    if require_durable or publication:
                        logger.error(
                            "保存知识文件失败",
                            filename=filename,
                            reason="PostgreSQL 不可用",
                        )
                        raise FileStoreUnavailableError("知识文件存储不可用，出版路径拒绝降级")
                    logger.info("保存知识文件回退", filename=filename, reason="PG 不可用")
                    return None
                ext = os.path.splitext(filename)[1].lower()
                if publication:
                    return await self._save_publication_row(
                        conn,
                        filename,
                        content,
                        ext=ext,
                        knowledge_scope=normalized_scope,
                        datasource=datasource,
                        tag_ids=tag_ids,
                        tenant_id=tenant_id,
                        user_id=user_id,
                        publication=publication,
                    )
                row = await conn.fetchrow(
                    "INSERT INTO knowledge_files "
                    "(filename, content_type, file_data, size, tenant_id, user_id, "
                    "knowledge_scope, datasource, tag_ids) "
                    "VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9) RETURNING id",
                    filename,
                    _content_type(ext),
                    content,
                    len(content),
                    tenant_id,
                    user_id,
                    normalized_scope,
                    datasource.strip(),
                    list(tag_ids or []),
                )
                result = row["id"] if row else None
                logger.info(
                    "保存知识文件完成", file_id=result, tenant_id=tenant_id, user_id=user_id
                )
                return result
        except FileStoreUnavailableError, FileStoreDuplicateVersionError:
            raise
        except Exception as exc:
            logger.error("PG 保存失败", error=str(exc), exc_info=True)
            return None

    async def get(self, file_id: int) -> dict | None:
        """按当前租户和用户读取文件。

        Args:
            file_id: 文件 ID。

        Returns:
            可见文件字典；不存在或无权访问返回 None。
        """
        tenant_id, user_id = _current_identity()
        logger.debug("读取知识文件入口", file_id=file_id, tenant_id=tenant_id, user_id=user_id)
        await self._ensure()
        try:
            async with self._connect() as conn:
                if conn is None:
                    return None
                row = await conn.fetchrow(
                    "SELECT id, filename, content_type, file_data, size, uploaded_at, "
                    "knowledge_scope, datasource, tag_ids, tenant_id, user_id "
                    "FROM knowledge_files WHERE id = $1 AND ("
                    + _VISIBLE_FILES_SQL.format(tenant_param="$2", user_param="$3")
                    + ")",
                    file_id,
                    tenant_id,
                    user_id,
                )
                if not row:
                    logger.info("读取知识文件完成", found=False, file_id=file_id, user_id=user_id)
                    return None
                result = {
                    "id": row["id"],
                    "filename": row["filename"],
                    "content_type": row["content_type"],
                    "file_data": row["file_data"],
                    "size": row["size"],
                    "uploaded_at": row["uploaded_at"].isoformat() if row["uploaded_at"] else "",
                    "scope": row["knowledge_scope"],
                    "datasource": row["datasource"],
                    "tag_ids": list(row["tag_ids"] or []),
                    "tenant_id": row["tenant_id"],
                    "owner_user_id": row["user_id"],
                }
                logger.info("读取知识文件完成", found=True, file_id=file_id, user_id=user_id)
                return result
        except Exception as exc:
            logger.error("读取知识文件失败", error=str(exc), exc_info=True)
            return None

    async def get_by_name(
        self,
        filename: str,
        *,
        knowledge_scope: str = "",
    ) -> dict | None:
        """按当前租户和用户读取最新同名文件。

        Args:
            filename: 原始文件名。
            knowledge_scope: 可选知识范围，用于区分同名文档。

        Returns:
            可见文件字典；不存在返回 None。
        """
        tenant_id, user_id = _current_identity()
        logger.debug(
            "按名称读取知识文件入口",
            filename=filename,
            tenant_id=tenant_id,
            user_id=user_id,
            knowledge_scope=knowledge_scope,
        )
        await self._ensure()
        try:
            async with self._connect() as conn:
                if conn is None:
                    return None
                row = await conn.fetchrow(
                    "SELECT id, filename, content_type, file_data, size, uploaded_at, "
                    "knowledge_scope, datasource, tag_ids, tenant_id, user_id "
                    "FROM knowledge_files WHERE filename = $1 AND ("
                    + _VISIBLE_FILES_SQL.format(tenant_param="$2", user_param="$3")
                    + ") AND ($4 = '' OR knowledge_scope = $4) "
                    + "ORDER BY uploaded_at DESC LIMIT 1",
                    filename,
                    tenant_id,
                    user_id,
                    knowledge_scope,
                )
                result = None
                if row:
                    result = {
                        "id": row["id"],
                        "filename": row["filename"],
                        "content_type": row["content_type"],
                        "file_data": row["file_data"],
                        "size": row["size"],
                        "scope": row["knowledge_scope"],
                        "datasource": row["datasource"],
                        "tag_ids": list(row["tag_ids"] or []),
                        "tenant_id": row["tenant_id"],
                        "owner_user_id": row["user_id"],
                    }
                logger.info("按名称读取知识文件完成", found=result is not None, filename=filename)
                return result
        except Exception as exc:
            logger.error("按名称读取知识文件失败", error=str(exc), exc_info=True)
            return None

    # 作用：按精确三元组读取当前身份可见的完整原文，元数据与字节双重自证。
    # Args: doc_id - 文档身份；source_version - 版本；source_revision_hash - 原文 sha256；
    #     knowledge_scope - 可选范围（空表示不限）。
    # Returns: 唯一可见行的完整原文与合同键；0 行 None；冲突/不可用抛出结构化异常。
    async def get_exact_version(
        self,
        *,
        doc_id: str,
        source_version: str,
        source_revision_hash: str,
        knowledge_scope: str = "",
    ) -> dict[str, Any] | None:
        """I/O 前拒非法输入；只按当前身份可见性，不回退最新同名、不落磁盘。"""
        _validate_exact_version_args(doc_id, source_version, source_revision_hash, knowledge_scope)
        tenant_id, user_id = _current_identity()
        logger.debug("精确读取知识原文入口", tenant_id=tenant_id, user_id=user_id)
        failure: FileStoreUnavailableError | None = None
        with defer_pg_error_logging():
            token = _EXACT_READ_QUIET.set(True)
            try:
                try:
                    ready = await self._ensure()
                    if not ready:
                        logger.error("精确读取知识原文失败", reason="store_not_ready")
                        raise FileStoreUnavailableError("知识文件存储未就绪，精确原文读取失败关闭")
                    pending: BaseException | None = None
                    result: dict[str, Any] | None = None
                    async with self._connect() as conn:
                        if conn is None:
                            logger.error("精确读取知识原文失败", reason="store_unavailable")
                            raise FileStoreUnavailableError(
                                "知识文件存储不可用，精确原文读取失败关闭"
                            )
                        try:
                            rows = await conn.fetch(
                                _EXACT_VERSION_SQL,
                                source_version,
                                source_revision_hash,
                                tenant_id,
                                user_id,
                                knowledge_scope,
                            )
                            if rows:
                                if len(rows) > 1:
                                    raise FileStoreIntegrityError("multi_match")
                                row = rows[0]
                                publication = _parse_exact_publication(row["publication_json"])
                                _verify_exact_metadata(
                                    row,
                                    publication,
                                    doc_id=doc_id,
                                    source_version=source_version,
                                    source_revision_hash=source_revision_hash,
                                )
                                file_data = _coerce_exact_file_data(
                                    row["file_data"], row["size"], source_revision_hash
                                )
                                result = {
                                    "id": row["id"],
                                    "filename": row["filename"],
                                    "content_type": row["content_type"],
                                    "file_data": file_data,
                                    "size": row["size"],
                                    "uploaded_at": (
                                        row["uploaded_at"].isoformat() if row["uploaded_at"] else ""
                                    ),
                                    "scope": row["knowledge_scope"],
                                    "datasource": row["datasource"],
                                    "tag_ids": list(row["tag_ids"] or []),
                                    "tenant_id": row["tenant_id"],
                                    "owner_user_id": row["user_id"],
                                    "source_version": row["source_version"],
                                    "source_revision_hash": row["source_revision_hash"],
                                    "index_state": row["index_state"],
                                    "publication": dict(publication),
                                }
                        except asyncio.CancelledError:
                            raise
                        except Exception as exc:
                            # 查询/校验异常在连接 context 内收敛，正常退出后再统一处理，
                            # 避免穿越共享连接层的旧异常日志边界。
                            pending = exc
                    if pending is not None:
                        raise pending
                    if result is None:
                        logger.debug("精确读取知识原文完成", found=False)
                        return None
                    logger.debug("精确读取知识原文完成", found=True)
                    return result
                except asyncio.CancelledError:
                    raise
                except FileStoreIntegrityError as exc:
                    logger.error(
                        f"精确读取知识原文完整性冲突\n{_safe_stack(exc)}", reason=exc.reason
                    )
                    raise
                except FileStoreUnavailableError:
                    raise
                except Exception as exc:
                    failure = FileStoreUnavailableError("读取知识原文失败")
                    logger.error(
                        f"精确读取知识原文失败\n{_safe_stack(exc)}",
                        error_type=type(exc).__name__,
                    )
                if failure is not None:
                    # 在 except 之外抛出：公开异常链不含底层 cause/context 正文。
                    raise failure from None
            finally:
                _EXACT_READ_QUIET.reset(token)
        raise AssertionError("精确原文读取必须 return 或结构化失败")  # pragma: no cover

    # 方法作用：在受控事务内串行校验前版本并写入不可变出版版本行。
    # Args: conn - 当前身份连接；filename/content - 原文；ext - 扩展名；
    #     knowledge_scope - 范围；datasource/tag_ids - 绑定；tenant_id/user_id - 主体；
    #     publication - 服务端发布补丁。
    # Returns: 新版本行主键；重复版本抛出 FileStoreDuplicateVersionError。
    async def _save_publication_row(
        self,
        conn: Any,
        filename: str,
        content: bytes,
        *,
        ext: str,
        knowledge_scope: str,
        datasource: str,
        tag_ids: list[int] | None,
        tenant_id: int,
        user_id: int,
        publication: Mapping[str, object],
    ) -> int | None:
        """同名并发由事务内命名锁串行化，前版本校验与插入原子完成。"""
        source_version = str(publication.get("source_version") or "").strip()
        if not source_version:
            raise ValueError("出版保存缺少 source_version")
        source_revision_hash = str(publication.get("source_revision_hash") or "").strip()
        identity_key = f"{knowledge_scope}|{tenant_id}|{user_id}|{filename}"
        # 先取命名锁再校验前版本，避免空表首版本并发下锁语句未被计划执行。
        await conn.execute(_IDENTITY_LOCK_SQL, identity_key)
        previous = await conn.fetchrow(
            _PREV_VERSION_SQL, filename, knowledge_scope, tenant_id, user_id
        )
        duplicate = await conn.fetchrow(
            _DUPLICATE_VERSION_SQL,
            filename,
            knowledge_scope,
            tenant_id,
            user_id,
            source_version,
        )
        if duplicate:
            logger.info("知识文件重复版本拒绝", file_id=duplicate["id"], filename=filename)
            raise FileStoreDuplicateVersionError(
                file_id=duplicate["id"], index_state=duplicate["index_state"] or "pending"
            )
        row = await conn.fetchrow(
            _INSERT_PUBLICATION_SQL,
            filename,
            _content_type(ext),
            content,
            len(content),
            tenant_id,
            user_id,
            knowledge_scope,
            datasource.strip(),
            list(tag_ids or []),
            source_version,
            source_revision_hash,
            json.dumps(dict(publication), ensure_ascii=False, sort_keys=True),
            "pending",
            previous["id"] if previous else None,
        )
        result = row["id"] if row else None
        logger.info("保存知识出版文件完成", file_id=result, filename=filename)
        return result

    # 方法作用：读取同名文档最近的服务端版本行，供写入前前版本校验。
    # Args: filename - 文件名；knowledge_scope - 可选范围；limit - 最大条数。
    # Returns: 版本行字典列表；存储不可用时抛出 FileStoreUnavailableError。
    async def get_latest_versions(
        self,
        filename: str,
        *,
        knowledge_scope: str = "",
        limit: int = 2,
    ) -> list[dict]:
        """查询失败必须失败关闭，绝不返回空列表冒充首次版本。"""
        tenant_id, user_id = _current_identity()
        logger.debug(
            "读取知识文件版本入口",
            filename=filename,
            knowledge_scope=knowledge_scope,
            limit=limit,
        )
        await self._ensure()
        try:
            async with self._connect() as conn:
                if conn is None:
                    raise FileStoreUnavailableError("知识文件存储不可用，版本查询失败关闭")
                rows = await conn.fetch(
                    _LATEST_VERSIONS_SQL,
                    filename,
                    tenant_id,
                    user_id,
                    knowledge_scope,
                    max(1, int(limit)),
                )
                result = [dict(row) for row in rows]
                logger.info("读取知识文件版本完成", count=len(result), filename=filename)
                return result
        except FileStoreUnavailableError:
            raise
        except Exception as exc:
            logger.error("读取知识文件版本失败", filename=filename, error=str(exc), exc_info=True)
            raise FileStoreUnavailableError("读取知识文件版本失败") from exc

    # 方法作用：按精确版本行主键回写索引状态，不误伤同名历史版本。
    # Args: file_id - 版本行主键；state - pending/ready/failed；knowledge_scope - 可选范围。
    # Returns: 更新到行时 True；未知状态抛出 ValueError，其余失败返回 False。
    async def mark_index_state(
        self,
        file_id: int,
        state: str,
        *,
        knowledge_scope: str = "",
    ) -> bool:
        """索引状态只按 id 精确回写；未知状态值失败关闭，防止写脏台账。"""
        normalized_state = str(state or "")
        if normalized_state not in _ALLOWED_INDEX_STATES:
            logger.warning("回写知识文件索引状态拒绝", file_id=file_id, reason="未知状态值")
            raise ValueError(f"未知索引状态: {state}")
        tenant_id, user_id = _current_identity()
        try:
            async with self._connect() as conn:
                if conn is None:
                    return False
                status = await conn.execute(
                    _MARK_INDEX_STATE_SQL,
                    int(file_id),
                    normalized_state,
                    tenant_id,
                    user_id,
                    knowledge_scope,
                )
                updated = not status.endswith(" 0")
                logger.info("回写知识文件索引状态完成", file_id=file_id, updated=updated)
                return updated
        except Exception as exc:
            logger.error(
                "回写知识文件索引状态失败",
                file_id=file_id,
                error_type=type(exc).__name__,
                exc_info=True,
            )
            return False

    async def list_files(self) -> list[dict]:
        """列出当前租户和用户可见的知识文件。

        Returns:
            文件摘要列表。
        """
        tenant_id, user_id = _current_identity()
        logger.debug("列出知识文件入口", tenant_id=tenant_id, user_id=user_id)
        await self._ensure()
        try:
            async with self._connect() as conn:
                if conn is None:
                    result = _disk_list()
                    logger.info(
                        "列出知识文件完成", source="disk", count=len(result), user_id=user_id
                    )
                    return result
                rows = await conn.fetch(
                    "SELECT id, filename, size, uploaded_at, knowledge_scope, datasource, "
                    "tag_ids, tenant_id, user_id FROM knowledge_files "
                    "WHERE ("
                    + _VISIBLE_FILES_SQL.format(tenant_param="$1", user_param="$2")
                    + ") ORDER BY uploaded_at DESC",
                    tenant_id,
                    user_id,
                )
                result = [
                    {
                        "id": row["id"],
                        "name": row["filename"],
                        "size": row["size"],
                        "modified": row["uploaded_at"].timestamp() if row["uploaded_at"] else 0,
                        "scope": row["knowledge_scope"],
                        "datasource": row["datasource"],
                        "tag_ids": list(row["tag_ids"] or []),
                        "tenant_id": row["tenant_id"],
                        "owner_user_id": row["user_id"],
                        "is_builtin": False,
                    }
                    for row in rows
                ]
                logger.info(
                    "列出知识文件完成", source="postgres", count=len(result), user_id=user_id
                )
                return result
        except Exception as exc:
            logger.error("列出知识文件失败", error=str(exc), exc_info=True)
            return _disk_list()

    async def delete(self, filename: str, *, knowledge_scope: str = "") -> bool:
        """删除当前租户和用户拥有的同名文件。

        Args:
            filename: 原始文件名。
            knowledge_scope: 可选知识范围，用于区分同名文档。

        Returns:
            至少删除一条可见记录时返回 True。
        """
        from src.api.auth import get_current_role
        from src.knowledge.governance import normalize_role

        tenant_id, user_id = _current_identity()
        role = normalize_role(get_current_role())
        logger.debug(
            "删除知识文件入口",
            filename=filename,
            tenant_id=tenant_id,
            user_id=user_id,
            role=role,
            knowledge_scope=knowledge_scope,
        )
        await self._ensure()
        try:
            async with self._connect() as conn:
                if conn is None:
                    logger.info("删除知识文件回退", filename=filename, reason="PG 不可用")
                    return False
                status = await conn.execute(
                    "DELETE FROM knowledge_files WHERE filename = $1 AND ("
                    "($4 = 'super_admin' AND knowledge_scope = 'system') OR "
                    "(($4 = 'tenant_admin') AND knowledge_scope = 'tenant' AND tenant_id = $2) OR "
                    "($4 <> 'super_admin' AND knowledge_scope = 'private' AND tenant_id = $2 AND user_id = $3)"
                    ") AND ($5 = '' OR knowledge_scope = $5)",
                    filename,
                    tenant_id,
                    user_id,
                    role,
                    knowledge_scope,
                )
                deleted = not status.endswith(" 0")
                logger.info("删除知识文件完成", filename=filename, deleted=deleted, user_id=user_id)
                return deleted
        except Exception as exc:
            logger.error("删除知识文件失败", error=str(exc), exc_info=True)
            return False

    # 方法作用：按精确文件 ID 删除当前身份有权管理的记录。
    # Args: file_id - 文件主键；knowledge_scope - 可选范围过滤。
    # Returns: 精确记录被删除时返回 True，不存在、无连接或失败返回 False。
    async def delete_by_id(self, file_id: int, *, knowledge_scope: str = "") -> bool:
        """为批量上传补偿提供不会误删同名历史文件的精确边界。"""
        from src.api.auth import get_current_role
        from src.knowledge.governance import normalize_role

        tenant_id, user_id = _current_identity()
        role = normalize_role(get_current_role())
        logger.debug(
            "按 ID 删除知识文件入口",
            file_id=file_id,
            tenant_id=tenant_id,
            user_id=user_id,
            role=role,
            knowledge_scope=knowledge_scope,
        )
        if int(file_id) <= 0:
            logger.warning("按 ID 删除知识文件拒绝", file_id=file_id, reason="ID 非正数")
            return False
        await self._ensure()
        try:
            async with self._connect() as conn:
                if conn is None:
                    logger.info("按 ID 删除知识文件回退", file_id=file_id, reason="PG 不可用")
                    return False
                status = await conn.execute(
                    "DELETE FROM knowledge_files WHERE id = $1 AND ("
                    "($4 = 'super_admin' AND knowledge_scope = 'system') OR "
                    "(($4 = 'tenant_admin') AND "
                    "knowledge_scope = 'tenant' AND tenant_id = $2) OR "
                    "($4 <> 'super_admin' AND knowledge_scope = 'private' AND tenant_id = $2 AND user_id = $3)"
                    ") AND ($5 = '' OR knowledge_scope = $5)",
                    int(file_id),
                    tenant_id,
                    user_id,
                    role,
                    knowledge_scope,
                )
                deleted = not status.endswith(" 0")
                logger.info(
                    "按 ID 删除知识文件完成",
                    file_id=file_id,
                    deleted=deleted,
                    user_id=user_id,
                )
                return deleted
        except Exception as exc:
            logger.error(
                "按 ID 删除知识文件失败",
                file_id=file_id,
                error_type=type(exc).__name__,
                exc_info=True,
            )
            return False


def _content_type(ext: str) -> str:
    """根据扩展名获取文件 MIME 类型。

    Args:
        ext: 小写扩展名。

    Returns:
        MIME 类型字符串。
    """
    return {
        ".pdf": "application/pdf",
        ".docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
        ".doc": "application/msword",
        ".txt": "text/plain",
        ".md": "text/markdown",
    }.get(ext, "application/octet-stream")


def _disk_list() -> list[dict]:
    """列出内置指标目录中的文档。

    Returns:
        内置文档摘要列表。
    """
    docs_dir = os.path.join(os.path.dirname(__file__), "..", "..", "docs", "metrics")
    docs = []
    if os.path.isdir(docs_dir):
        for filename in sorted(os.listdir(docs_dir)):
            if any(filename.endswith(ext) for ext in (".md", ".txt", ".pdf", ".docx", ".doc")):
                path = os.path.join(docs_dir, filename)
                docs.append(
                    {
                        "name": filename,
                        "size": os.path.getsize(path),
                        "modified": os.path.getmtime(path),
                        "scope": "system",
                        "datasource": "",
                        "tag_ids": [],
                        "is_builtin": True,
                    }
                )
    return docs


# 方法作用：从当前 AppContext 获取知识文件存储。
# Args: 无。
# Returns: 当前应用独享的 FileStore 实例。
def get_file_store() -> FileStore:
    """获取当前应用的 FileStore。

    Returns:
        当前 AppContext 的 FileStore 实例。
    """
    from src.app_context import get_app_context

    logger.debug("获取 FileStore 入口")
    result = get_app_context().get_or_create("file_store", FileStore)
    logger.debug("获取 FileStore 完成")
    return result
