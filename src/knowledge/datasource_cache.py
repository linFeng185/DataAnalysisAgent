"""连接级数据库内容缓存，支持本地文件和 Redis 后端。"""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import re
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from src.datasource.config import DataSourceConfig
from src.knowledge.models import KnowledgeEntry
from src.logging_config import get_logger

logger = get_logger(__name__)

_CACHE_VERSION = 1
_FINGERPRINT_PATTERN = re.compile(r"^[0-9a-f]{64}$")
_DIALECT_ALIASES = {
    "postgresql": "postgres",
    "postgresql+asyncpg": "postgres",
    "mysql+aiomysql": "mysql",
    "sqlite+aiosqlite": "sqlite",
}
_DEFAULT_PORTS = {
    "clickhouse": 8123,
    "mysql": 3306,
    "postgres": 5432,
    "oracle": 1521,
    "mssql": 1433,
}
_NAMESPACE_PARAMS = (
    "catalog",
    "http_port",
    "schema",
    "search_path",
    "service_name",
    "warehouse",
)


class CacheExpiredError(ValueError):
    """缓存载荷已超过记录级 TTL。"""


# 生成不依赖用户、会话和数据源显示名称的稳定连接指纹。
# Args: datasource - 已归一化的数据源连接配置。
# Returns: 64 位十六进制 SHA-256 指纹。
def build_connection_fingerprint(datasource: DataSourceConfig) -> str:
    logger.debug(
        "生成连接指纹入口",
        datasource=datasource.name,
        dialect=datasource.dialect,
    )
    try:
        dialect = _DIALECT_ALIASES.get(
            datasource.dialect.strip().lower(), datasource.dialect.strip().lower(),
        )
        port = int(datasource.port or _DEFAULT_PORTS.get(dialect, 0))
        database = datasource.database.strip()
        if dialect == "sqlite" and database not in {"", ":memory:"}:
            database = os.path.normcase(os.path.abspath(os.path.expanduser(database)))
        namespace = {
            key: datasource.extra_params.get(key)
            for key in _NAMESPACE_PARAMS
            if datasource.extra_params.get(key) not in (None, "")
        }
        if dialect == "clickhouse" and namespace.get("http_port"):
            port = int(namespace["http_port"])
        identity = {
            "dialect": dialect,
            "host": datasource.host.strip().lower(),
            "port": port,
            "database": database,
            "username": datasource.username.strip(),
            "namespace": namespace,
        }
        canonical = json.dumps(
            identity, ensure_ascii=True, sort_keys=True, separators=(",", ":"),
        )
        fingerprint = hashlib.sha256(canonical.encode("utf-8")).hexdigest()
        logger.info(
            "生成连接指纹完成",
            datasource=datasource.name,
            fingerprint=fingerprint[:12],
        )
        return fingerprint
    except Exception as exc:
        logger.error(
            "生成连接指纹失败",
            datasource=datasource.name,
            error=str(exc),
            exc_info=True,
        )
        raise


# 校验缓存指纹格式，阻止路径穿越和不可追踪的 Redis key。
# Args: fingerprint - 待校验的连接指纹。
# Returns: 校验通过的原始指纹。
def _validate_fingerprint(fingerprint: str) -> str:
    logger.debug("校验缓存指纹入口", fingerprint=fingerprint[:12] if fingerprint else "")
    if not _FINGERPRINT_PATTERN.fullmatch(fingerprint):
        logger.error("缓存指纹校验失败", fingerprint=fingerprint[:12] if fingerprint else "")
        raise ValueError("缓存指纹必须是 64 位十六进制 SHA-256")
    logger.info("校验缓存指纹完成", fingerprint=fingerprint[:12])
    return fingerprint


# 将知识条目封装为带版本和记录级 TTL 的 JSON 载荷。
# Args: fingerprint - 连接指纹；entries - Schema 知识条目；ttl_seconds - 有效期秒数。
# Returns: 可持久化的紧凑 JSON 字符串。
def _serialize_record(
    fingerprint: str, entries: list[KnowledgeEntry], ttl_seconds: int,
) -> str:
    logger.debug(
        "序列化数据库缓存入口",
        fingerprint=fingerprint[:12],
        entry_count=len(entries),
        ttl_seconds=ttl_seconds,
    )
    try:
        _validate_fingerprint(fingerprint)
        if ttl_seconds <= 0:
            raise ValueError("缓存 TTL 必须大于 0")
        created_at = datetime.now(timezone.utc)
        payload = {
            "version": _CACHE_VERSION,
            "fingerprint": fingerprint,
            "created_at": created_at.isoformat(),
            "expires_at": (created_at + timedelta(seconds=ttl_seconds)).isoformat(),
            "entries": [entry.to_dict() for entry in entries],
        }
        result = json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
        logger.info(
            "序列化数据库缓存完成",
            fingerprint=fingerprint[:12],
            entry_count=len(entries),
        )
        return result
    except Exception as exc:
        logger.error(
            "序列化数据库缓存失败",
            fingerprint=fingerprint[:12],
            error=str(exc),
            exc_info=True,
        )
        raise


# 校验并恢复缓存载荷中的知识条目。
# Args: raw - JSON 文本或 Redis 字节；fingerprint - 期望的连接指纹。
# Returns: 反序列化后的知识条目列表。
def _deserialize_record(raw: str | bytes, fingerprint: str) -> list[KnowledgeEntry]:
    logger.debug("反序列化数据库缓存入口", fingerprint=fingerprint[:12])
    try:
        _validate_fingerprint(fingerprint)
        text = raw.decode("utf-8") if isinstance(raw, bytes) else raw
        payload = json.loads(text)
        if not isinstance(payload, dict):
            raise ValueError("缓存载荷必须是 JSON object")
        if payload.get("version") != _CACHE_VERSION:
            raise ValueError("缓存版本不兼容")
        if payload.get("fingerprint") != fingerprint:
            raise ValueError("缓存连接指纹不匹配")
        expires_at = datetime.fromisoformat(str(payload["expires_at"]))
        if expires_at.tzinfo is None:
            expires_at = expires_at.replace(tzinfo=timezone.utc)
        if datetime.now(timezone.utc) >= expires_at:
            logger.warning("数据库缓存已过期", fingerprint=fingerprint[:12])
            raise CacheExpiredError("缓存已过期")
        raw_entries = payload.get("entries")
        if not isinstance(raw_entries, list):
            raise ValueError("缓存 entries 必须是数组")
        entries = [KnowledgeEntry.from_dict(item) for item in raw_entries]
        logger.info(
            "反序列化数据库缓存完成",
            fingerprint=fingerprint[:12],
            entry_count=len(entries),
        )
        return entries
    except CacheExpiredError:
        raise
    except Exception as exc:
        logger.error(
            "反序列化数据库缓存失败",
            fingerprint=fingerprint[:12],
            error=str(exc),
            exc_info=True,
        )
        raise


class LocalDatasourceCache:
    """以连接指纹为文件名的本地 JSON 持久化缓存。"""

    # 初始化本地缓存目录和默认 TTL。
    # Args: cache_dir - 缓存目录；ttl_seconds - 记录有效期秒数。
    # Returns: 无返回值。
    def __init__(self, cache_dir: str | Path, ttl_seconds: int) -> None:
        logger.debug(
            "初始化本地数据库缓存入口",
            cache_dir=str(cache_dir),
            ttl_seconds=ttl_seconds,
        )
        if ttl_seconds <= 0:
            logger.error("初始化本地数据库缓存失败", error="TTL 必须大于 0")
            raise ValueError("缓存 TTL 必须大于 0")
        self._cache_dir = Path(cache_dir)
        self._ttl_seconds = ttl_seconds
        self._cache_dir.mkdir(parents=True, exist_ok=True)
        logger.info("初始化本地数据库缓存完成", cache_dir=str(self._cache_dir))

    # 读取指定连接的缓存条目，过期或不存在时返回 None。
    # Args: fingerprint - 连接指纹。
    # Returns: 命中的知识条目列表；未命中时返回 None。
    async def get(self, fingerprint: str) -> list[KnowledgeEntry] | None:
        logger.debug("读取本地数据库缓存入口", fingerprint=fingerprint[:12] if fingerprint else "")
        path = self._path_for(fingerprint)
        if not path.exists():
            logger.info("本地数据库缓存未命中", fingerprint=fingerprint[:12])
            return None
        try:
            raw = await asyncio.to_thread(path.read_text, encoding="utf-8")
            entries = _deserialize_record(raw, fingerprint)
            logger.info(
                "读取本地数据库缓存完成",
                fingerprint=fingerprint[:12],
                entry_count=len(entries),
            )
            return entries
        except CacheExpiredError:
            await self.delete(fingerprint)
            logger.info("本地数据库过期缓存已删除", fingerprint=fingerprint[:12])
            return None
        except Exception as exc:
            await asyncio.to_thread(self._quarantine, path)
            logger.error(
                "读取本地数据库缓存失败，已隔离损坏文件",
                fingerprint=fingerprint[:12],
                error=str(exc),
                exc_info=True,
            )
            return None

    # 原子写入指定连接的缓存条目。
    # Args: fingerprint - 连接指纹；entries - 待缓存的 Schema 知识条目。
    # Returns: 无返回值。
    async def set(self, fingerprint: str, entries: list[KnowledgeEntry]) -> None:
        logger.debug(
            "写入本地数据库缓存入口",
            fingerprint=fingerprint[:12] if fingerprint else "",
            entry_count=len(entries),
        )
        try:
            path = self._path_for(fingerprint)
            raw = _serialize_record(fingerprint, entries, self._ttl_seconds)
            await asyncio.to_thread(self._atomic_write, path, raw)
            logger.info(
                "写入本地数据库缓存完成",
                fingerprint=fingerprint[:12],
                entry_count=len(entries),
            )
        except Exception as exc:
            logger.error(
                "写入本地数据库缓存失败",
                fingerprint=fingerprint[:12] if fingerprint else "",
                error=str(exc),
                exc_info=True,
            )
            raise

    # 删除指定连接的本地缓存文件。
    # Args: fingerprint - 连接指纹。
    # Returns: 实际删除文件时返回 True，否则返回 False。
    async def delete(self, fingerprint: str) -> bool:
        logger.debug("删除本地数据库缓存入口", fingerprint=fingerprint[:12] if fingerprint else "")
        try:
            path = self._path_for(fingerprint)
            existed = path.exists()
            if existed:
                await asyncio.to_thread(path.unlink)
            logger.info("删除本地数据库缓存完成", fingerprint=fingerprint[:12], deleted=existed)
            return existed
        except Exception as exc:
            logger.error(
                "删除本地数据库缓存失败",
                fingerprint=fingerprint[:12] if fingerprint else "",
                error=str(exc),
                exc_info=True,
            )
            raise

    # 将连接指纹映射为受控缓存文件路径。
    # Args: fingerprint - 连接指纹。
    # Returns: 缓存 JSON 文件路径。
    def _path_for(self, fingerprint: str) -> Path:
        logger.debug("生成本地缓存路径入口", fingerprint=fingerprint[:12] if fingerprint else "")
        validated = _validate_fingerprint(fingerprint)
        path = self._cache_dir / f"{validated}.json"
        logger.info("生成本地缓存路径完成", fingerprint=validated[:12])
        return path

    # 通过同目录临时文件和 os.replace 保证写入原子性。
    # Args: path - 最终文件路径；raw - 完整 JSON 文本。
    # Returns: 无返回值。
    def _atomic_write(self, path: Path, raw: str) -> None:
        logger.debug("本地缓存原子写入口", path=str(path), chars=len(raw))
        temp_path = path.with_suffix(f".{os.getpid()}.{id(raw)}.tmp")
        try:
            temp_path.write_text(raw, encoding="utf-8")
            os.replace(temp_path, path)
            logger.info("本地缓存原子写完成", path=str(path), chars=len(raw))
        except Exception as exc:
            if temp_path.exists():
                temp_path.unlink()
            logger.error("本地缓存原子写失败", path=str(path), error=str(exc), exc_info=True)
            raise

    # 将损坏文件改名保留，避免后续请求重复解析同一坏载荷。
    # Args: path - 损坏缓存文件路径。
    # Returns: 无返回值。
    def _quarantine(self, path: Path) -> None:
        logger.debug("隔离损坏缓存入口", path=str(path))
        if not path.exists():
            logger.info("隔离损坏缓存跳过", path=str(path), reason="文件不存在")
            return
        stamp = datetime.now(timezone.utc).strftime("%Y%m%d%H%M%S%f")
        target = path.with_name(f"{path.stem}.corrupt-{stamp}.json")
        try:
            os.replace(path, target)
            logger.info("隔离损坏缓存完成", source=str(path), target=str(target))
        except Exception as exc:
            logger.error("隔离损坏缓存失败", path=str(path), error=str(exc), exc_info=True)
            raise


class RedisDatasourceCache:
    """基于 redis.asyncio 的多实例共享数据库内容缓存。"""

    # 初始化 Redis 缓存客户端、键前缀和 TTL。
    # Args: redis_url - Redis URL；prefix - 键前缀；ttl_seconds - 有效期；client - 测试注入客户端。
    # Returns: 无返回值。
    def __init__(
        self,
        redis_url: str,
        prefix: str,
        ttl_seconds: int,
        client: Any | None = None,
    ) -> None:
        logger.debug(
            "初始化 Redis 数据库缓存入口",
            prefix=prefix,
            ttl_seconds=ttl_seconds,
            injected_client=client is not None,
        )
        if ttl_seconds <= 0:
            logger.error("初始化 Redis 数据库缓存失败", error="TTL 必须大于 0")
            raise ValueError("缓存 TTL 必须大于 0")
        if client is None:
            from redis.asyncio import Redis

            client = Redis.from_url(redis_url)
        self._client = client
        self._prefix = prefix.rstrip(":")
        self._ttl_seconds = ttl_seconds
        logger.info("初始化 Redis 数据库缓存完成", prefix=self._prefix)

    # 从 Redis 读取并恢复指定连接的缓存条目。
    # Args: fingerprint - 连接指纹。
    # Returns: 命中的知识条目列表；未命中时返回 None。
    async def get(self, fingerprint: str) -> list[KnowledgeEntry] | None:
        logger.debug("读取 Redis 数据库缓存入口", fingerprint=fingerprint[:12] if fingerprint else "")
        key = self._key_for(fingerprint)
        try:
            raw = await self._client.get(key)
            if raw is None:
                logger.info("Redis 数据库缓存未命中", fingerprint=fingerprint[:12])
                return None
            entries = _deserialize_record(raw, fingerprint)
            logger.info(
                "读取 Redis 数据库缓存完成",
                fingerprint=fingerprint[:12],
                entry_count=len(entries),
            )
            return entries
        except CacheExpiredError:
            await self.delete(fingerprint)
            logger.info("Redis 数据库过期缓存已删除", fingerprint=fingerprint[:12])
            return None
        except Exception as exc:
            logger.error(
                "读取 Redis 数据库缓存失败",
                fingerprint=fingerprint[:12],
                error=str(exc),
                exc_info=True,
            )
            raise

    # 使用 Redis SET EX 写入指定连接的缓存条目。
    # Args: fingerprint - 连接指纹；entries - 待缓存的 Schema 知识条目。
    # Returns: 无返回值。
    async def set(self, fingerprint: str, entries: list[KnowledgeEntry]) -> None:
        logger.debug(
            "写入 Redis 数据库缓存入口",
            fingerprint=fingerprint[:12] if fingerprint else "",
            entry_count=len(entries),
        )
        try:
            key = self._key_for(fingerprint)
            raw = _serialize_record(fingerprint, entries, self._ttl_seconds)
            await self._client.set(key, raw, ex=self._ttl_seconds)
            logger.info(
                "写入 Redis 数据库缓存完成",
                fingerprint=fingerprint[:12],
                entry_count=len(entries),
            )
        except Exception as exc:
            logger.error(
                "写入 Redis 数据库缓存失败",
                fingerprint=fingerprint[:12] if fingerprint else "",
                error=str(exc),
                exc_info=True,
            )
            raise

    # 删除指定连接的 Redis 缓存键。
    # Args: fingerprint - 连接指纹。
    # Returns: 实际删除键时返回 True，否则返回 False。
    async def delete(self, fingerprint: str) -> bool:
        logger.debug("删除 Redis 数据库缓存入口", fingerprint=fingerprint[:12] if fingerprint else "")
        try:
            key = self._key_for(fingerprint)
            deleted = bool(await self._client.delete(key))
            logger.info("删除 Redis 数据库缓存完成", fingerprint=fingerprint[:12], deleted=deleted)
            return deleted
        except Exception as exc:
            logger.error(
                "删除 Redis 数据库缓存失败",
                fingerprint=fingerprint[:12] if fingerprint else "",
                error=str(exc),
                exc_info=True,
            )
            raise

    # 关闭 Redis 客户端连接。
    # Args: 无。
    # Returns: 无返回值。
    async def close(self) -> None:
        logger.debug("关闭 Redis 数据库缓存入口")
        try:
            close = getattr(self._client, "aclose", None)
            if close is not None:
                await close()
            logger.info("关闭 Redis 数据库缓存完成")
        except Exception as exc:
            logger.error("关闭 Redis 数据库缓存失败", error=str(exc), exc_info=True)
            raise

    # 生成包含版本号的 Redis 缓存键。
    # Args: fingerprint - 连接指纹。
    # Returns: 完整 Redis key。
    def _key_for(self, fingerprint: str) -> str:
        logger.debug("生成 Redis 缓存键入口", fingerprint=fingerprint[:12] if fingerprint else "")
        validated = _validate_fingerprint(fingerprint)
        key = f"{self._prefix}:v{_CACHE_VERSION}:{validated}"
        logger.info("生成 Redis 缓存键完成", fingerprint=validated[:12])
        return key


# 根据 Settings 创建配置指定的数据库内容缓存后端。
# Args: settings - 项目 Settings 或具有同名属性的配置对象。
# Returns: LocalDatasourceCache 或 RedisDatasourceCache 实例。
def create_datasource_cache(settings: Any):
    logger.debug(
        "创建数据库内容缓存入口",
        backend=getattr(settings, "datasource_cache_backend", "local"),
    )
    try:
        backend = str(settings.datasource_cache_backend).strip().lower()
        if backend == "local":
            result = LocalDatasourceCache(
                settings.datasource_cache_dir,
                settings.datasource_cache_ttl_seconds,
            )
        elif backend == "redis":
            result = RedisDatasourceCache(
                settings.redis_url,
                settings.datasource_cache_redis_prefix,
                settings.datasource_cache_ttl_seconds,
            )
        else:
            raise ValueError(f"不支持的数据库内容缓存后端: {backend}")
        logger.info("创建数据库内容缓存完成", backend=backend)
        return result
    except Exception as exc:
        logger.error("创建数据库内容缓存失败", error=str(exc), exc_info=True)
        raise


_cache_singleton: LocalDatasourceCache | RedisDatasourceCache | None = None


# 获取进程内共享的数据库内容缓存后端实例。
# Args: 无。
# Returns: 配置选定的数据库内容缓存后端。
def get_datasource_cache() -> LocalDatasourceCache | RedisDatasourceCache:
    global _cache_singleton
    logger.debug("获取数据库内容缓存单例入口", initialized=_cache_singleton is not None)
    if _cache_singleton is None:
        from src.config import get_settings

        _cache_singleton = create_datasource_cache(get_settings())
    logger.info("获取数据库内容缓存单例完成", backend=type(_cache_singleton).__name__)
    return _cache_singleton
