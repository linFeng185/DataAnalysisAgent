"""连接级数据库内容缓存测试，覆盖功能 6.1.12-6.1.15。"""

from __future__ import annotations

import asyncio
import json
import logging
import threading
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from src.datasource.config import DataSourceConfig
from src.knowledge.models import KnowledgeEntry, KnowledgeSource

logger = logging.getLogger(__name__)


# 创建测试用 Schema 条目，保持各场景的缓存载荷一致。
# Args: datasource - 条目来源数据源名称。
# Returns: 包含表和字段的知识条目列表。
def _make_entries(datasource: str = "primary") -> list[KnowledgeEntry]:
    logger.debug("_make_entries 入口", extra={"datasource": datasource})
    entries = [
        KnowledgeEntry(
            id=f"table:{datasource}.orders",
            content="orders - 订单表",
            source=KnowledgeSource.AUTO_INTROSPECT,
            category="table",
            table_name="orders",
            ttl=604800,
            metadata={"datasource": datasource, "row_count_estimate": 12},
        ),
        KnowledgeEntry(
            id=f"column:{datasource}.orders.id",
            content="orders.id: BIGINT",
            source=KnowledgeSource.AUTO_INTROSPECT,
            category="column",
            table_name="orders",
            column_name="id",
            ttl=604800,
            metadata={"datasource": datasource, "type": "BIGINT"},
        ),
    ]
    logger.info("_make_entries 完成", extra={"count": len(entries)})
    return entries


class TestConnectionFingerprint:
    """覆盖功能 6.1.12：连接身份稳定性与隔离性。"""

    # 验证显示名称、密码和用户会话不会分裂同一连接的缓存。
    # Args: self - pytest 测试类实例。
    # Returns: 无返回值，断言失败时由 pytest 报告。
    def test_same_connection_uses_same_fingerprint(self):
        """同一连接的不同别名和密码轮换应生成相同指纹。"""
        logger.debug("test_same_connection_uses_same_fingerprint 入口")
        from src.knowledge.datasource_cache import build_connection_fingerprint

        first = DataSourceConfig(
            name="alice-view",
            dialect="postgresql",
            mode="external",
            host="DB.EXAMPLE.COM ",
            port=0,
            database="analytics",
            username="reader",
            password="old-secret",
            extra_params={"schema": "public"},
        )
        second = DataSourceConfig(
            name="bob-view",
            dialect="postgres",
            mode="external",
            host="db.example.com",
            port=5432,
            database="analytics",
            username="reader",
            password="new-secret",
            extra_params={"schema": "public"},
        )

        first_key = build_connection_fingerprint(first)
        second_key = build_connection_fingerprint(second)

        assert first_key == second_key
        assert "secret" not in first_key
        assert "alice" not in first_key
        assert len(first_key) == 64
        logger.info("test_same_connection_uses_same_fingerprint 完成")

    # 验证数据库、账号或命名空间变化会隔离缓存。
    # Args: self - pytest 测试类实例。
    # Returns: 无返回值，断言失败时由 pytest 报告。
    def test_different_connection_uses_different_fingerprint(self):
        """连接访问边界变化时必须生成不同指纹。"""
        logger.debug("test_different_connection_uses_different_fingerprint 入口")
        from src.knowledge.datasource_cache import build_connection_fingerprint

        base = DataSourceConfig(
            name="base", dialect="mysql", mode="external", host="localhost",
            port=3306, database="sales", username="reader",
        )
        other_database = DataSourceConfig(
            name="other-db", dialect="mysql", mode="external", host="localhost",
            port=3306, database="finance", username="reader",
        )
        other_user = DataSourceConfig(
            name="other-user", dialect="mysql", mode="external", host="localhost",
            port=3306, database="sales", username="admin",
        )

        fingerprints = {
            build_connection_fingerprint(base),
            build_connection_fingerprint(other_database),
            build_connection_fingerprint(other_user),
        }

        assert len(fingerprints) == 3
        logger.info("test_different_connection_uses_different_fingerprint 完成")

    # 方法作用：验证 SQLite 实际 db_path 变化时连接级缓存不会发生碰撞。
    # Args: self - pytest 测试类实例。
    # Returns: 无返回值，断言失败时由 pytest 报告。
    def test_sqlite_extra_db_path_is_part_of_fingerprint(self) -> None:
        """database 为空而由 db_path 指定的两个 SQLite 文件必须隔离。"""
        # Arrange
        from src.knowledge.datasource_cache import build_connection_fingerprint

        first = DataSourceConfig(
            name="one",
            dialect="sqlite",
            mode="external",
            extra_params={"db_path": "C:/data/one.db"},
        )
        second = DataSourceConfig(
            name="two",
            dialect="sqlite",
            mode="external",
            extra_params={"db_path": "C:/data/two.db"},
        )

        # Act
        first_key = build_connection_fingerprint(first)
        second_key = build_connection_fingerprint(second)

        # Assert
        assert first_key != second_key


class TestLocalDatasourceCache:
    """覆盖功能 6.1.13：本地持久化、TTL 和损坏隔离。"""

    # 验证不同缓存实例可通过连接指纹共享磁盘条目。
    # Args: self - pytest 测试类实例；tmp_path - pytest 临时目录。
    # Returns: 无返回值，断言失败时由 pytest 报告。
    async def test_round_trip_persists_across_instances(self, tmp_path):
        """本地缓存写入后应可被新实例读取。"""
        logger.debug("test_round_trip_persists_across_instances 入口")
        from src.knowledge.datasource_cache import LocalDatasourceCache

        entries = _make_entries()
        first = LocalDatasourceCache(tmp_path, ttl_seconds=3600)
        second = LocalDatasourceCache(tmp_path, ttl_seconds=3600)

        await first.set("a" * 64, entries)
        restored = await second.get("a" * 64)

        assert restored is not None
        assert [entry.id for entry in restored] == [entry.id for entry in entries]
        assert restored[0].metadata["row_count_estimate"] == 12
        logger.info("test_round_trip_persists_across_instances 完成")

    # 验证过期记录不会继续作为命中返回。
    # Args: self - pytest 测试类实例；tmp_path - pytest 临时目录。
    # Returns: 无返回值，断言失败时由 pytest 报告。
    async def test_expired_record_returns_cache_miss(self, tmp_path):
        """记录级 TTL 过期后应返回 None 并移除旧文件。"""
        logger.debug("test_expired_record_returns_cache_miss 入口")
        from src.knowledge.datasource_cache import LocalDatasourceCache

        fingerprint = "b" * 64
        cache = LocalDatasourceCache(tmp_path, ttl_seconds=3600)
        await cache.set(fingerprint, _make_entries())
        cache_file = tmp_path / f"{fingerprint}.json"
        payload = json.loads(cache_file.read_text(encoding="utf-8"))
        payload["expires_at"] = (
            datetime.now(timezone.utc) - timedelta(seconds=1)
        ).isoformat()
        cache_file.write_text(json.dumps(payload), encoding="utf-8")

        restored = await cache.get(fingerprint)

        assert restored is None
        assert not cache_file.exists()
        logger.info("test_expired_record_returns_cache_miss 完成")

    # 验证非法 JSON 不会中断 Schema 获取链路。
    # Args: self - pytest 测试类实例；tmp_path - pytest 临时目录。
    # Returns: 无返回值，断言失败时由 pytest 报告。
    async def test_corrupt_record_is_quarantined(self, tmp_path):
        """损坏缓存应返回 miss，并改名隔离用于排查。"""
        logger.debug("test_corrupt_record_is_quarantined 入口")
        from src.knowledge.datasource_cache import LocalDatasourceCache

        fingerprint = "c" * 64
        cache_file = tmp_path / f"{fingerprint}.json"
        cache_file.write_text("{not-json", encoding="utf-8")
        cache = LocalDatasourceCache(tmp_path, ttl_seconds=3600)

        restored = await cache.get(fingerprint)

        assert restored is None
        assert not cache_file.exists()
        assert list(tmp_path.glob(f"{fingerprint}.corrupt-*.json"))
        logger.info("test_corrupt_record_is_quarantined 完成")

    # 验证并发读取同一损坏文件时，只有一个请求执行隔离，其他请求仍返回缓存未命中。
    # Args: self - pytest 测试类实例；tmp_path - pytest 临时目录；monkeypatch - pytest 补丁工具。
    # Returns: 无返回值，断言失败时由 pytest 报告。
    async def test_concurrent_corrupt_reads_do_not_raise(self, tmp_path, monkeypatch):
        """隔离竞态不应把损坏缓存升级为请求异常。"""
        logger.debug("test_concurrent_corrupt_reads_do_not_raise 入口")
        import src.knowledge.datasource_cache as cache_module
        from src.knowledge.datasource_cache import LocalDatasourceCache

        fingerprint = "e" * 64
        cache_file = tmp_path / f"{fingerprint}.json"
        cache_file.write_text("{not-json", encoding="utf-8")
        cache = LocalDatasourceCache(tmp_path, ttl_seconds=3600)
        exists_barrier = threading.Barrier(2)
        exists_lock = threading.Lock()
        original_exists = Path.exists
        original_replace = cache_module.os.replace
        exists_calls = 0
        replace_calls = 0

        def synchronized_exists(path):
            nonlocal exists_calls
            if path == cache_file:
                with exists_lock:
                    exists_calls += 1
                    should_wait = exists_calls <= 2
                if should_wait:
                    exists_barrier.wait(timeout=5)
            return original_exists(path)

        def flaky_replace(source, target):
            nonlocal replace_calls
            with exists_lock:
                replace_calls += 1
                call_number = replace_calls
            if call_number == 2:
                raise FileNotFoundError("simulated concurrent quarantine race")
            return original_replace(source, target)

        monkeypatch.setattr(Path, "exists", synchronized_exists)
        monkeypatch.setattr(cache_module.os, "replace", flaky_replace)

        results = await asyncio.gather(
            asyncio.to_thread(cache._quarantine, cache_file),
            asyncio.to_thread(cache._quarantine, cache_file),
            return_exceptions=True,
        )

        assert results == [None, None]
        assert not cache_file.exists()
        assert list(tmp_path.glob(f"{fingerprint}.corrupt-*.json"))
        logger.info("test_concurrent_corrupt_reads_do_not_raise 完成")

    # 方法作用：验证缺失 created_at 的缓存载荷按结构损坏处理。
    # Args: self - pytest 测试类实例；tmp_path - pytest 临时目录。
    # Returns: 无返回值，断言失败时由 pytest 报告。
    async def test_missing_created_at_is_quarantined(self, tmp_path) -> None:
        """缓存契约缺少创建时间时必须返回 miss 并隔离文件。"""
        # Arrange
        from src.knowledge.datasource_cache import LocalDatasourceCache

        fingerprint = "d" * 64
        cache_file = tmp_path / f"{fingerprint}.json"
        cache_file.write_text(json.dumps({
            "version": 1,
            "fingerprint": fingerprint,
            "expires_at": (datetime.now(timezone.utc) + timedelta(hours=1)).isoformat(),
            "entries": [],
        }), encoding="utf-8")
        cache = LocalDatasourceCache(tmp_path, ttl_seconds=3600)

        # Act
        restored = await cache.get(fingerprint)

        # Assert
        assert restored is None
        assert not cache_file.exists()
        assert list(tmp_path.glob(f"{fingerprint}.corrupt-*.json"))

    # 验证空指纹被拒绝，避免写入不可追踪的共享文件。
    # Args: self - pytest 测试类实例；tmp_path - pytest 临时目录。
    # Returns: 无返回值，断言失败时由 pytest 报告。
    async def test_empty_fingerprint_is_rejected(self, tmp_path):
        """非法缓存键应抛出 ValueError。"""
        logger.debug("test_empty_fingerprint_is_rejected 入口")
        from src.knowledge.datasource_cache import LocalDatasourceCache

        cache = LocalDatasourceCache(tmp_path, ttl_seconds=3600)

        with pytest.raises(ValueError):
            await cache.set("", _make_entries())
        logger.info("test_empty_fingerprint_is_rejected 完成")


class TestRedisDatasourceCache:
    """覆盖功能 6.1.14：Redis 键、TTL、读写和删除。"""

    # 验证 Redis 中无法解析的载荷按缓存未命中处理，并删除坏键。
    # Args: self - pytest 测试类实例。
    # Returns: 无返回值，断言失败时由 pytest 报告。
    async def test_corrupt_record_returns_miss_and_deletes_key(self):
        """损坏 JSON 不应把缓存故障升级为 Redis 服务故障。"""
        logger.debug("test_corrupt_record_returns_miss_and_deletes_key 入口")
        from src.knowledge.datasource_cache import RedisDatasourceCache

        fingerprint = "e" * 64
        client = SimpleNamespace(
            get=AsyncMock(return_value=b"{bad-json"),
            delete=AsyncMock(return_value=1),
        )
        cache = RedisDatasourceCache(
            "redis://unused", prefix="tests:datasource", ttl_seconds=60, client=client,
        )

        restored = await cache.get(fingerprint)

        key = f"tests:datasource:v1:{fingerprint}"
        assert restored is None
        client.get.assert_awaited_once_with(key)
        client.delete.assert_awaited_once_with(key)
        logger.info("test_corrupt_record_returns_miss_and_deletes_key 完成")

    # 验证清理损坏载荷失败时仍保持缓存未命中语义，不阻断实时降级路径。
    # Args: self - pytest 测试类实例。
    # Returns: 无返回值，断言失败时由 pytest 报告。
    async def test_corrupt_record_delete_failure_still_returns_miss(self):
        """Redis 删除坏键失败不应重新抛出清理异常。"""
        logger.debug("test_corrupt_record_delete_failure_still_returns_miss 入口")
        from src.knowledge.datasource_cache import RedisDatasourceCache

        client = SimpleNamespace(
            get=AsyncMock(return_value=b"not-json"),
            delete=AsyncMock(side_effect=ConnectionError("delete unavailable")),
        )
        cache = RedisDatasourceCache(
            "redis://unused", prefix="tests:datasource", ttl_seconds=60, client=client,
        )

        restored = await cache.get("f" * 64)

        assert restored is None
        client.delete.assert_awaited_once()
        logger.info("test_corrupt_record_delete_failure_still_returns_miss 完成")

    # 验证 Redis 读取本身失败时不被误判为载荷损坏，调用方仍能感知服务故障。
    # Args: self - pytest 测试类实例。
    # Returns: 无返回值，断言失败时由 pytest 报告。
    async def test_redis_get_failure_is_propagated(self):
        """Redis 连接异常必须继续向上传播，且不执行坏键清理。"""
        logger.debug("test_redis_get_failure_is_propagated 入口")
        from src.knowledge.datasource_cache import RedisDatasourceCache

        failure = ConnectionError("redis unavailable")
        client = SimpleNamespace(
            get=AsyncMock(side_effect=failure),
            delete=AsyncMock(return_value=1),
        )
        cache = RedisDatasourceCache(
            "redis://unused", prefix="tests:datasource", ttl_seconds=60, client=client,
        )

        with pytest.raises(ConnectionError, match="redis unavailable"):
            await cache.get("1" * 64)

        client.delete.assert_not_awaited()
        logger.info("test_redis_get_failure_is_propagated 完成")

    # 验证 Redis 写入携带命名空间和过期时间，并可恢复条目。
    # Args: self - pytest 测试类实例。
    # Returns: 无返回值，断言失败时由 pytest 报告。
    async def test_round_trip_uses_namespaced_key_and_expiry(self):
        """Redis 后端应使用 SET EX 写入版本化 JSON。"""
        logger.debug("test_round_trip_uses_namespaced_key_and_expiry 入口")
        from src.knowledge.datasource_cache import RedisDatasourceCache

        client = SimpleNamespace(
            set=AsyncMock(return_value=True),
            get=AsyncMock(return_value=None),
            delete=AsyncMock(return_value=1),
        )
        cache = RedisDatasourceCache(
            "redis://unused", prefix="tests:datasource", ttl_seconds=900, client=client,
        )
        fingerprint = "d" * 64

        await cache.set(fingerprint, _make_entries())
        key, raw = client.set.await_args.args[:2]
        client.set.assert_awaited_once_with(key, raw, ex=900)
        client.get.return_value = raw.encode("utf-8")
        restored = await cache.get(fingerprint)
        deleted = await cache.delete(fingerprint)

        assert key == f"tests:datasource:v1:{fingerprint}"
        assert restored is not None and restored[0].table_name == "orders"
        assert deleted is True
        client.delete.assert_awaited_once_with(key)
        logger.info("test_round_trip_uses_namespaced_key_and_expiry 完成")

    # 验证关闭接口会释放异步 Redis 客户端。
    # Args: self - pytest 测试类实例。
    # Returns: 无返回值，断言失败时由 pytest 报告。
    async def test_close_releases_redis_client(self):
        """RedisDatasourceCache.close 应调用客户端 aclose。"""
        logger.debug("test_close_releases_redis_client 入口")
        from src.knowledge.datasource_cache import RedisDatasourceCache

        client = SimpleNamespace(aclose=AsyncMock())
        cache = RedisDatasourceCache(
            "redis://unused", prefix="tests:datasource", ttl_seconds=60, client=client,
        )

        await cache.close()

        client.aclose.assert_awaited_once_with()
        logger.info("test_close_releases_redis_client 完成")


class TestDatasourceCacheFactory:
    """覆盖缓存后端工厂和进程内单例公开接口。"""

    # 验证配置可在本地与 Redis 后端之间切换。
    # Args: self - pytest 测试类实例；tmp_path - pytest 临时目录。
    # Returns: 无返回值，断言失败时由 pytest 报告。
    def test_factory_switches_backend_from_settings(self, tmp_path):
        """create_datasource_cache 应严格服从 backend 配置。"""
        logger.debug("test_factory_switches_backend_from_settings 入口")
        from src.knowledge.datasource_cache import (
            LocalDatasourceCache,
            RedisDatasourceCache,
            create_datasource_cache,
        )

        base = {
            "datasource_cache_dir": str(tmp_path),
            "datasource_cache_ttl_seconds": 60,
            "datasource_cache_redis_prefix": "tests:datasource",
            "redis_url": "redis://localhost:6379/15",
        }

        local = create_datasource_cache(SimpleNamespace(
            datasource_cache_backend="local", **base,
        ))
        redis = create_datasource_cache(SimpleNamespace(
            datasource_cache_backend="redis", **base,
        ))

        assert isinstance(local, LocalDatasourceCache)
        assert isinstance(redis, RedisDatasourceCache)
        logger.info("test_factory_switches_backend_from_settings 完成")

    # 验证单个 AppContext 只创建一次后端，避免重复连接 Redis。
    # Args: self - pytest 测试类实例；monkeypatch - pytest 补丁工具。
    # Returns: 无返回值，断言失败时由 pytest 报告。
    def test_get_datasource_cache_reuses_context_resource(self, monkeypatch):
        """get_datasource_cache 连续调用应返回同一实例。"""
        logger.debug("test_get_datasource_cache_reuses_context_resource 入口")
        import src.knowledge.datasource_cache as cache_module
        from src.app_context import AppContext, use_app_context

        expected = object()
        sync_create = MagicMock(return_value=expected)
        monkeypatch.setattr(cache_module, "create_datasource_cache", sync_create)

        context = AppContext(SimpleNamespace(multi_tenant=False))
        with use_app_context(context):
            first = cache_module.get_datasource_cache()
            second = cache_module.get_datasource_cache()

        assert first is expected and second is expected
        sync_create.assert_called_once()
        logger.info("test_get_datasource_cache_reuses_context_resource 完成")


class TestSchemaManagerSharedCache:
    """覆盖功能 6.1.15：SchemaManager 优先复用连接级共享缓存。"""

    # 验证共享缓存命中时不访问或重写向量缓存，也不重复内省。
    # Args: self - pytest 测试类实例；monkeypatch - pytest 补丁工具。
    # Returns: 无返回值，断言失败时由 pytest 报告。
    async def test_shared_cache_hit_skips_database_introspection(self, monkeypatch):
        """同连接缓存命中应直接构建 SchemaSnapshot。"""
        logger.debug("test_shared_cache_hit_skips_database_introspection 入口")
        from src.knowledge.schema_manager import SchemaManager

        cache = SimpleNamespace(
            get=AsyncMock(return_value=_make_entries("first-alias")),
            set=AsyncMock(),
            delete=AsyncMock(return_value=True),
        )
        manager = SchemaManager(datasource_cache=cache)
        datasource = DataSourceConfig(
            name="second-alias", dialect="postgres", mode="external",
            host="db.example.com", port=5432, database="analytics", username="reader",
        )
        monkeypatch.setattr(manager, "_resolve_datasource", AsyncMock(return_value=datasource))
        monkeypatch.setattr(manager, "_query_cache", AsyncMock(side_effect=AssertionError("不应查询别名缓存")))
        monkeypatch.setattr(manager, "_introspect_from_db", AsyncMock(side_effect=AssertionError("不应重复内省")))
        monkeypatch.setattr(manager, "_load_from_docs", lambda _: [])
        monkeypatch.setattr(manager, "_upsert_to_cache", AsyncMock())

        snapshot = await manager.get_or_fetch_schema("second-alias")

        assert [table.name for table in snapshot.tables] == ["orders"]
        manager._introspect_from_db.assert_not_awaited()
        manager._upsert_to_cache.assert_not_awaited()
        logger.info("test_shared_cache_hit_skips_database_introspection 完成")
