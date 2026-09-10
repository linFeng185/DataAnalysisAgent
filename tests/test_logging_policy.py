"""1.1.6 日志语义分级、频率和异常保留回归测试。"""

import json
import logging
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest
import structlog
from structlog.testing import capture_logs


class TestLoggingPolicy:
    """覆盖 1.1.6：高频纯转换静默，正常轮询不重复摘要，运维和异常不被屏蔽。"""

    # 方法作用：验证逐行读取和结果适配不因数据量放大日志条数。
    # Args: self - 测试实例；monkeypatch - 依赖替换工具。
    # Returns: 无返回值。
    def test_clickhouse_result_adapters_are_silent(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """读取一千行不产生一千条属性成功日志，返回值不变。"""
        import src.connectors.clickhouse as module

        # Arrange
        logger = MagicMock()
        monkeypatch.setattr(module, "logger", logger)
        # Act
        result = module.ClickHouseResult(["count"], [(number,) for number in range(1000)])
        rows = [row._mapping for row in result.fetchall()]
        result.close()
        # Assert
        assert len(rows) == 1000 and rows[-1] == {"count": 999}
        assert not logger.mock_calls

    # 方法作用：验证重复的数据源列表请求仅由访问中间件记录一次摘要。
    # Args: self - 实例；monkeypatch - 依赖替换工具。
    # Returns: 无返回值。
    async def test_polling_route_does_not_repeat_success_info(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """五轮请求不在列表路由额外记录五条成功日志。"""
        from src.api.routes.datasource import list_datasources
        from tests.test_api.test_datasource_import_export import TestDatasourceImportExport

        # Arrange
        TestDatasourceImportExport()._setup(monkeypatch)
        monkeypatch.setattr("src.app_context.get_app_context", lambda: SimpleNamespace(get_resource=lambda name: None))
        # Act
        with capture_logs() as events:
            for _attempt in range(5):
                result = await list_datasources(page=1, page_size=20)
        # Assert
        assert result["total"] == 1
        assert not [event for event in events if event["log_level"] == "info"]
        logging.getLogger(__name__).info("轮询降噪探针 requests=5 info_events=%s", sum(event["log_level"] == "info" for event in events))

    # 方法作用：验证正常 Registry 枚举及缓存复用不产生 INFO 成功明细。
    # Args: self - 实例；monkeypatch - 依赖替换。
    # Returns: 无返回值。
    async def test_registry_cached_reads_are_debug_only(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """枚举和解析缓存保留调试能力，不与外层访问日志重复。"""
        from src.connectors.plugins import require_plugin
        from src.datasource.config import DataSourceConfig
        from src.datasource.registry import DataSourceRegistry

        # Arrange
        registry = DataSourceRegistry()
        source = DataSourceConfig(name="sales", mode="external", dialect="sqlite", tenant_id=1)
        registry._cache[(1, "sales")] = source
        monkeypatch.setattr(registry, "_apply_published_schema", AsyncMock())
        monkeypatch.setattr(registry, "_effective_tenant_id", lambda tenant: 1)
        monkeypatch.setattr(registry, "_isolation_enabled", lambda: False)
        require_plugin("sqlite")
        # Act
        with capture_logs() as events:
            for _attempt in range(3):
                assert await registry.resolve("sales", tenant_id=1) is source
                assert (await registry.list_all())[0]["name"] == "sales"
        # Assert
        assert not [event for event in events if event["log_level"] == "info"]

    # 方法作用：验证没有过期数据时不产生重复清理成功日志。
    # Args: self - 测试实例。
    # Returns: 无返回值。
    async def test_empty_expiry_cycles_are_debug_only(self) -> None:
        """三次空清理保留返回值，但不重复记录 INFO。"""
        from src.knowledge.cache_refresher import CacheRefresher

        # Arrange
        store = SimpleNamespace(get_by_filter=AsyncMock(return_value=[]))
        refresher = CacheRefresher(schema_manager=SimpleNamespace(), vector_store=store)
        # Act
        with capture_logs() as events:
            for _attempt in range(3):
                assert await refresher.refresh_expired() == 0
        # Assert
        assert store.get_by_filter.await_count == 3
        assert not [event for event in events if event["log_level"] == "info"]

    # 方法作用：验证指纹不变或正常锁竞争只留下调试明细。
    # Args: self - 测试实例；monkeypatch - 依赖替换；lock_busy - 是否被其他 Worker 持锁。
    # Returns: 无返回值。
    @pytest.mark.parametrize("lock_busy", [False, True])
    async def test_unchanged_schema_polling_is_debug_only(
        self, monkeypatch: pytest.MonkeyPatch, lock_busy: bool,
    ) -> None:
        """已有基线后重复检查或跳过不产生成功摘要，也不改写缓存。"""
        from src.datasource.schema_snapshot import SchemaSnapshot
        from src.knowledge.cache_refresher import CacheRefresher

        # Arrange
        manager = SimpleNamespace(
            inspect_live_schema=AsyncMock(return_value=SchemaSnapshot(tables=[])),
            refresh_from_snapshot=AsyncMock(),
        )
        refresher = CacheRefresher(schema_manager=manager, vector_store=SimpleNamespace())
        await refresher.refresh_on_schema_change("sales", tenant_id=1)
        if lock_busy:
            monkeypatch.setattr(refresher, "_acquire_lock", AsyncMock(return_value=None))
        # Act
        with capture_logs() as events:
            for _attempt in range(3):
                assert not await refresher.refresh_on_schema_change("sales", tenant_id=1)
        # Assert
        manager.refresh_from_snapshot.assert_not_awaited()
        assert not [event for event in events if event["log_level"] == "info"]

    # 方法作用：验证周期服务只有实际清理或变更才记录一条聚合 INFO。
    # Args: self - 测试实例；monkeypatch - 依赖替换；deleted - 删除数量；changed - 是否发生变化。
    # Returns: 无返回值。
    @pytest.mark.parametrize(("deleted", "changed"), [(0, False), (2, False), (0, True)])
    async def test_refresh_cycle_logs_only_meaningful_summary(
        self, monkeypatch: pytest.MonkeyPatch, deleted: int, changed: bool,
    ) -> None:
        """正常租户枚举静默；有效工作保留聚合计数，避免吞掉状态变更。"""
        from src.knowledge.cache_refresher import CacheRefreshService

        # Arrange
        monkeypatch.setattr("src.app_context.get_app_context", lambda: SimpleNamespace(get_resource=lambda name: None))
        refresher = SimpleNamespace(
            refresh_expired=AsyncMock(return_value=deleted),
            refresh_on_schema_change=AsyncMock(return_value=changed),
        )
        registry = SimpleNamespace(
            list_tenant_ids=AsyncMock(return_value=[1]),
            list_all=AsyncMock(return_value=[{"name": "sales"}]),
        )
        service = CacheRefreshService(refresher, registry, interval_seconds=60)
        # Act
        with capture_logs() as events:
            result = await service.run_once()
        # Assert
        assert result == {"deleted": deleted, "checked": 1, "changed": int(changed)}
        summaries = [event for event in events if event["log_level"] == "info"]
        assert len(summaries) == int(bool(deleted or changed)), summaries
        if summaries:
            assert summaries[0]["event"] == "Schema 缓存周期刷新完成"
            assert summaries[0]["deleted"] == deleted
            assert summaries[0]["changed"] == int(changed)

    # 方法作用：以真实 structlog 配置验证过滤发生于渲染前且保留所有模块的重要日志。
    # Args: self - 实例；monkeypatch/tmp_path/capsys - 配置替换、临时日志和控制台捕获；level - 日志等级。
    # Returns: 无返回值。
    @pytest.mark.parametrize("level", ["INFO", "DEBUG"])
    def test_log_levels_and_operational_events(self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture, level: str) -> None:
        """DEBUG 按级别启用；图中的业务状态和 ClickHouse 关闭日志不能被整模块过滤。"""
        from src import logging_config

        # Arrange
        logfile = tmp_path / "app.log"
        root = logging.getLogger()
        old_level = root.level
        old_config = structlog.get_config()
        monkeypatch.setattr(root, "handlers", [])
        monkeypatch.setattr(logging_config, "get_settings", lambda: SimpleNamespace(log_file=str(logfile), log_level=level, log_format="json"))
        processor = MagicMock(wraps=logging_config._redact_sensitive_fields)
        monkeypatch.setattr(logging_config, "_redact_sensitive_fields", processor)
        logging_config.setup_logging()
        processor.reset_mock()
        # Act
        try:
            logger = logging_config.get_logger("src.graph.nodes.save_preference")
            logger.debug("调试明细", source_count=1)
            debug_processed = processor.call_count
            logger.info("用户偏好保存完成", changed=True)
            logging_config.get_logger("src.connectors.clickhouse").info("ClickHouse 客户端关闭完成")
            logger.warning("授权阻断", reason="denied")
            try:
                raise RuntimeError("token=top-secret")
            except RuntimeError:
                logger.error("操作失败", exc_info=True)
            for handler in root.handlers:
                handler.flush()
            content = logfile.read_text(encoding="utf-8")
            events = [json.loads(line)["event"] for line in content.splitlines()]
            console = capsys.readouterr().err
            # Assert
            assert debug_processed == (1 if level == "DEBUG" else 0)
            assert ("调试明细" in events) == (level == "DEBUG")
            assert {"用户偏好保存完成", "ClickHouse 客户端关闭完成", "授权阻断", "操作失败"} <= set(events)
            assert "RuntimeError" in content and "top-secret" not in content
            assert "授权阻断" in console and "操作失败" in console
            rotating = [handler for handler in root.handlers if hasattr(handler, "backupCount")]
            assert len(rotating) == 1 and rotating[0].backupCount == 7
        finally:
            for handler in root.handlers:
                handler.close()
            root.setLevel(old_level)
            structlog.configure(**old_config)
