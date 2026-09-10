"""Milvus 过滤表达式和精确 metadata 校验回归测试。"""

from __future__ import annotations

import importlib.util
import json
import logging
import time
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

logger = logging.getLogger(__name__)


@pytest.mark.skipif(
    importlib.util.find_spec("pymilvus") is None,
    reason="未安装可选 pymilvus SDK",
)
class TestMilvusHealthCheck:
    """覆盖功能 19.17：Milvus 连接与服务端健康检查。"""

    # 方法作用：验证活跃连接会继续调用服务端探针并返回健康。
    # Args: self - pytest 测试类实例；monkeypatch - pytest 补丁工具。
    # Returns: 无返回值，断言失败时由 pytest 报告。
    @pytest.mark.asyncio
    async def test_health_check_probes_connected_server(self, monkeypatch) -> None:
        """连接存在且服务端 RPC 成功时必须返回 True。"""
        logger.debug("test_health_check_probes_connected_server 入口")
        try:
            # Arrange
            from unittest.mock import MagicMock

            from pymilvus import connections, utility

            from src.memory.vector_store_milvus import MilvusVectorStore

            has_connection = MagicMock(return_value=True)
            has_collection = MagicMock(return_value=True)
            monkeypatch.setattr(connections, "has_connection", has_connection)
            monkeypatch.setattr(utility, "has_collection", has_collection)

            # Act
            result = await MilvusVectorStore().health_check()

            # Assert
            assert result is True
            has_connection.assert_called_once_with("default")
            has_collection.assert_called_once_with("data_agent_knowledge", using="default", timeout=10.0)
            logger.info("test_health_check_probes_connected_server 完成")
        except Exception as exc:
            logger.error("test_health_check_probes_connected_server 异常: %s", exc, exc_info=True)
            raise

    # 方法作用：验证缺少默认连接时健康检查直接返回不健康。
    # Args: self - pytest 测试类实例；monkeypatch - pytest 补丁工具。
    # Returns: 无返回值，断言失败时由 pytest 报告。
    @pytest.mark.asyncio
    async def test_health_check_rejects_missing_connection(self, monkeypatch) -> None:
        """未建立默认连接时不得误报服务健康或发起 RPC。"""
        logger.debug("test_health_check_rejects_missing_connection 入口")
        try:
            # Arrange
            from unittest.mock import MagicMock

            from pymilvus import connections, utility

            from src.memory.vector_store_milvus import MilvusVectorStore

            has_connection = MagicMock(return_value=False)
            has_collection = MagicMock()
            monkeypatch.setattr(connections, "has_connection", has_connection)
            monkeypatch.setattr(utility, "has_collection", has_collection)

            # Act
            result = await MilvusVectorStore().health_check()

            # Assert
            assert result is False
            has_collection.assert_not_called()
            logger.info("test_health_check_rejects_missing_connection 完成")
        except Exception as exc:
            logger.error("test_health_check_rejects_missing_connection 异常: %s", exc, exc_info=True)
            raise

    # 方法作用：验证服务端 RPC 异常时健康检查按不可用处理。
    # Args: self - pytest 测试类实例；monkeypatch - pytest 补丁工具。
    # Returns: 无返回值，断言失败时由 pytest 报告。
    @pytest.mark.asyncio
    async def test_health_check_returns_false_when_probe_fails(self, monkeypatch) -> None:
        """连接对象存在但服务端不可达时必须返回 False。"""
        logger.debug("test_health_check_returns_false_when_probe_fails 入口")
        try:
            # Arrange
            from unittest.mock import MagicMock

            from pymilvus import connections, utility

            from src.memory.vector_store_milvus import MilvusVectorStore

            monkeypatch.setattr(connections, "has_connection", MagicMock(return_value=True))
            monkeypatch.setattr(
                utility,
                "has_collection",
                MagicMock(side_effect=RuntimeError("milvus unavailable")),
            )

            # Act
            result = await MilvusVectorStore().health_check()

            # Assert
            assert result is False
            logger.info("test_health_check_returns_false_when_probe_fails 完成")
        except Exception as exc:
            logger.error("test_health_check_returns_false_when_probe_fails 异常: %s", exc, exc_info=True)
            raise


class TestMilvusExpressionSafety:
    """覆盖功能 19.17：Milvus 表达式转义与精确过滤。"""

    # 方法作用：验证配置为 milvus 时工厂使用声明的 URI 创建 Milvus 后端。
    # Args: self - pytest 测试类实例；monkeypatch - pytest 补丁工具。
    # Returns: 无返回值，断言失败时由 pytest 报告。
    @pytest.mark.asyncio
    async def test_factory_selects_milvus_backend(self, monkeypatch) -> None:
        """Milvus 配置必须路由到 MilvusVectorStore，且完整传递连接地址。"""
        logger.debug("test_factory_selects_milvus_backend 入口")
        try:
            # Arrange
            from unittest.mock import AsyncMock

            from src.memory import vector_store as vector_module
            from src.memory.vector_store_milvus import MilvusVectorStore

            configured_uri = "http://192.168.195.133:19530"
            expected_store = MilvusVectorStore()
            create_mock = AsyncMock(return_value=expected_store)
            monkeypatch.setattr(MilvusVectorStore, "create", create_mock)
            settings = SimpleNamespace(
                vector_store_abstract_enabled=True,
                vector_store_type="milvus",
                milvus_uri=configured_uri,
            )

            # Act
            result = await vector_module._create_configured_vector_store(settings)  # noqa: SLF001

            # Assert
            assert result is expected_store
            create_mock.assert_awaited_once_with(configured_uri)
            logger.info("test_factory_selects_milvus_backend 完成")
        except Exception as exc:
            logger.error("test_factory_selects_milvus_backend 异常: %s", exc, exc_info=True)
            raise

    # 方法作用：验证 metadata 条件包含 JSON 实际空格格式并安全引用用户值。
    # Args: self - pytest 测试类实例。
    # Returns: 无返回值，断言失败时由 pytest 报告。
    def test_metadata_expression_uses_quoted_json_literal(self) -> None:
        """字符串中的引号和逻辑运算符不得逃逸出 Milvus 字面量。"""
        logger.debug("test_metadata_expression_uses_quoted_json_literal 入口")
        try:
            # Arrange
            from src.memory.vector_store_milvus import MilvusVectorStore

            store = MilvusVectorStore()
            value = 'sales" or id != "other'
            pattern = f'%{json.dumps("datasource")}: {json.dumps(value)}%'

            # Act
            expression = store._to_expr({"datasource": value})  # noqa: SLF001

            # Assert
            assert expression == f"metadata like {json.dumps(pattern)}"
            logger.info("test_metadata_expression_uses_quoted_json_literal 完成")
        except Exception as exc:
            logger.error(
                "test_metadata_expression_uses_quoted_json_literal 异常: %s",
                exc,
                exc_info=True,
            )
            raise

    # 方法作用：验证 ID 查询通过 JSON 字面量转义构造 Milvus 表达式。
    # Args: self - pytest 测试类实例；monkeypatch - pytest 补丁工具。
    # Returns: 无返回值，断言失败时由 pytest 报告。
    @pytest.mark.asyncio
    async def test_get_by_id_escapes_expression_value(self, monkeypatch) -> None:
        """恶意 entry_id 不得改变 ID 等值查询语义。"""
        logger.debug("test_get_by_id_escapes_expression_value 入口")
        try:
            # Arrange
            from unittest.mock import MagicMock

            from src.memory.vector_store_milvus import MilvusVectorStore

            collection = SimpleNamespace(query=MagicMock(return_value=[]))
            store = MilvusVectorStore()
            monkeypatch.setattr(store, "_get_collection", lambda: collection)
            entry_id = 'x" or id != "'

            # Act
            result = await store.get_by_id(entry_id)

            # Assert
            assert result is None
            assert collection.query.call_args.kwargs["expr"] == (
                f"id == {json.dumps(entry_id)}"
            )
            logger.info("test_get_by_id_escapes_expression_value 完成")
        except Exception as exc:
            logger.error("test_get_by_id_escapes_expression_value 异常: %s", exc, exc_info=True)
            raise

    # 方法作用：验证 Milvus 返回候选后仍按解析后的 metadata 做精确条件判断。
    # Args: self - pytest 测试类实例。
    # Returns: 无返回值，断言失败时由 pytest 报告。
    def test_metadata_matching_is_exact(self) -> None:
        """LIKE 命中的相似文本不得绕过租户和数据源精确过滤。"""
        logger.debug("test_metadata_matching_is_exact 入口")
        try:
            # Arrange
            from src.memory.vector_store_milvus import MilvusVectorStore

            metadata = {"tenant_id": 12, "datasource": "sales-archive"}

            # Act / Assert
            assert MilvusVectorStore._metadata_matches(  # noqa: SLF001
                metadata, {"tenant_id": 12, "datasource": "sales-archive"}
            )
            assert not MilvusVectorStore._metadata_matches(  # noqa: SLF001
                metadata, {"tenant_id": 1, "datasource": "sales"}
            )
            logger.info("test_metadata_matching_is_exact 完成")
        except Exception as exc:
            logger.error("test_metadata_matching_is_exact 异常: %s", exc, exc_info=True)
            raise

    # 方法作用：验证 Milvus 精确后过滤支持统一的不等值语法。
    # Args: self - pytest 测试类实例。
    # Returns: 无返回值，断言失败时由 pytest 报告。
    def test_metadata_matching_supports_not_equal_dsl(self) -> None:
        """not: 前缀和 $ne 运算符必须与 Chroma、pgvector 语义一致。"""
        logger.debug("test_metadata_matching_supports_not_equal_dsl 入口")
        from src.memory.vector_store_milvus import MilvusVectorStore

        metadata = {"tenant_id": 12, "status": "active", "source": "manual"}

        assert MilvusVectorStore._metadata_matches(  # noqa: SLF001
            metadata,
            {"tenant_id": 12, "not:status": "deleted", "source": {"$ne": "legacy"}},
        )
        assert not MilvusVectorStore._metadata_matches(  # noqa: SLF001
            metadata, {"status": {"$ne": "active"}},
        )
        logger.info("test_metadata_matching_supports_not_equal_dsl 完成")

    # 方法作用：验证 metadata 查询使用分页迭代器且严格遵守调用方返回上限。
    # Args: self - pytest 测试类实例；monkeypatch - pytest 补丁工具。
    # Returns: 无返回值，断言失败时由 pytest 报告。
    @pytest.mark.asyncio
    async def test_get_by_filter_uses_iterator_and_honors_limit(self, monkeypatch) -> None:
        """Milvus 查询不得把较小 limit 硬编码放大到超过服务端窗口。"""
        logger.debug("test_get_by_filter_uses_iterator_and_honors_limit 入口")
        try:
            # Arrange
            from src.memory.vector_store_milvus import MilvusVectorStore

            rows = [
                {
                    "id": f"entry-{index}",
                    "content": f"content-{index}",
                    "metadata": json.dumps({"tenant_id": 1}),
                }
                for index in range(3)
            ]
            iterator = SimpleNamespace(
                next=MagicMock(side_effect=[rows, []]),
                close=MagicMock(),
            )
            collection = SimpleNamespace(query_iterator=MagicMock(return_value=iterator))
            store = MilvusVectorStore()
            monkeypatch.setattr(store, "_get_collection", lambda: collection)

            # Act
            result = await store.get_by_filter({"tenant_id": 1}, limit=2)

            # Assert
            assert [entry.id for entry in result] == ["entry-0", "entry-1"]
            query_kwargs = collection.query_iterator.call_args.kwargs
            assert query_kwargs["batch_size"] <= 1_000
            assert query_kwargs["limit"] == -1
            iterator.close.assert_called_once_with()
            logger.info("test_get_by_filter_uses_iterator_and_honors_limit 完成")
        except Exception as exc:
            logger.error(
                "test_get_by_filter_uses_iterator_and_honors_limit 异常: %s",
                exc,
                exc_info=True,
            )
            raise

    # 方法作用：验证嵌入编码在线程池运行并关闭第三方进度条。
    # Args: self - pytest 测试类实例；monkeypatch - pytest 补丁工具。
    # Returns: 无返回值，断言失败时由 pytest 报告。
    @pytest.mark.asyncio
    async def test_embed_disables_progress_bar(self, monkeypatch) -> None:
        """单条或批量编码均不得向服务控制台写入 tqdm 进度条。"""
        logger.debug("test_embed_disables_progress_bar 入口")
        try:
            # Arrange
            from src.memory.vector_store_milvus import MilvusVectorStore

            encoded = SimpleNamespace(tolist=lambda: [0.1, 0.2])
            encode = MagicMock(return_value=encoded)
            store = MilvusVectorStore()
            monkeypatch.setattr(store, "_get_embed_fn", AsyncMock(return_value=encode))

            # Act
            result = await store._embed("hello")  # noqa: SLF001

            # Assert
            assert result == [0.1, 0.2]
            encode.assert_called_once_with("hello", show_progress_bar=False)
            logger.info("test_embed_disables_progress_bar 完成")
        except Exception as exc:
            logger.error("test_embed_disables_progress_bar 异常: %s", exc, exc_info=True)
            raise


@pytest.mark.skipif(
    importlib.util.find_spec("pymilvus") is None,
    reason="未安装可选 pymilvus SDK",
)
class TestMilvusCollectionLoadResilience:
    """覆盖 19.17 修复：集合加载只做一次、失败熔断、外部释放后自愈。

    背景：Milvus 2.4.0 的 load 任务会永久卡在 0%，原实现每次操作都无条件
    调用 Collection.load()，导致每个请求都白等一次完整 RPC 超时。
    """

    # 装配可直接替换 pymilvus.Collection 的假集合，避免连接真实 Milvus。
    # Args: monkeypatch - pytest 补丁工具；load_error - 非空时让 load 抛出该异常。
    # Returns: 假集合 MagicMock 实例。
    @staticmethod
    def _fake_collection(
        monkeypatch: pytest.MonkeyPatch,
        load_error: Exception | None = None,
    ) -> MagicMock:
        import pymilvus

        collection = MagicMock()
        if load_error is not None:
            collection.load.side_effect = load_error
        monkeypatch.setattr(pymilvus, "Collection", MagicMock(return_value=collection))
        return collection

    # 方法作用：验证连续操作复用加载标记，只发起一次 LoadCollection。
    # Args: self - pytest 测试类实例；monkeypatch - pytest 补丁工具。
    # Returns: 无返回值，断言失败时由 pytest 报告。
    @pytest.mark.asyncio
    async def test_collection_loads_once_across_operations(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """集合已加载后不得在每次读操作重发重量级 LoadCollection RPC。"""
        logger.debug("test_collection_loads_once_across_operations 入口")
        try:
            # Arrange
            from src.memory.vector_store_milvus import MilvusVectorStore

            collection = self._fake_collection(monkeypatch)
            collection.query.return_value = [
                {"id": "entry", "content": "body", "metadata": json.dumps({"tenant_id": 1})},
            ]
            store = MilvusVectorStore()

            # Act
            first = await store.get_by_id("entry")
            second = await store.get_by_id("entry")

            # Assert
            assert first is not None and second is not None
            assert collection.load.call_count == 1
            logger.info("test_collection_loads_once_across_operations 完成")
        except Exception as exc:
            logger.error(
                "test_collection_loads_once_across_operations 异常: %s", exc, exc_info=True,
            )
            raise

    # 方法作用：验证 load 失败后立即熔断，后续请求快速失败而非逐个超时。
    # Args: self - pytest 测试类实例；monkeypatch - pytest 补丁工具。
    # Returns: 无返回值，断言失败时由 pytest 报告。
    @pytest.mark.asyncio
    async def test_load_failure_opens_circuit_and_short_circuits(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """集合不可用时，第二个请求必须不再触碰 SDK，避免重复付出超时代价。"""
        logger.debug("test_load_failure_opens_circuit_and_short_circuits 入口")
        try:
            # Arrange
            from src.memory.vector_store_milvus import (
                MilvusCollectionUnavailable,
                MilvusVectorStore,
            )

            collection = self._fake_collection(
                monkeypatch, TimeoutError("wait for loading collection timeout"),
            )
            store = MilvusVectorStore()

            # Act
            with pytest.raises(TimeoutError):
                await store.get_by_id("entry")

            # Assert
            assert store._load_failure_until > time.monotonic()  # noqa: SLF001
            with pytest.raises(MilvusCollectionUnavailable):
                await store.get_by_id("entry")
            assert collection.load.call_count == 1
            logger.info("test_load_failure_opens_circuit_and_short_circuits 完成")
        except Exception as exc:
            logger.error(
                "test_load_failure_opens_circuit_and_short_circuits 异常: %s", exc, exc_info=True,
            )
            raise

    # 方法作用：验证熔断窗口结束后恢复尝试加载，不会永久锁死向量检索。
    # Args: self - pytest 测试类实例；monkeypatch - pytest 补丁工具。
    # Returns: 无返回值，断言失败时由 pytest 报告。
    @pytest.mark.asyncio
    async def test_circuit_expiry_retries_load(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """熔断只是限流窗口，服务端恢复后必须能自愈。"""
        logger.debug("test_circuit_expiry_retries_load 入口")
        try:
            # Arrange
            from src.memory.vector_store_milvus import MilvusVectorStore

            collection = self._fake_collection(monkeypatch, TimeoutError("load timeout"))
            collection.query.return_value = []
            store = MilvusVectorStore()
            with pytest.raises(TimeoutError):
                await store.get_by_id("entry")

            # Act
            store._load_failure_until = time.monotonic() - 1  # noqa: SLF001
            collection.load.side_effect = None
            result = await store.get_by_id("entry")

            # Assert
            assert result is None
            assert collection.load.call_count == 2
            logger.info("test_circuit_expiry_retries_load 完成")
        except Exception as exc:
            logger.error("test_circuit_expiry_retries_load 异常: %s", exc, exc_info=True)
            raise

    # 方法作用：验证服务端驱逐或人工 release 后本地标记失效并重建集合。
    # Args: self - pytest 测试类实例；monkeypatch - pytest 补丁工具。
    # Returns: 无返回值，断言失败时由 pytest 报告。
    @pytest.mark.asyncio
    async def test_not_loaded_error_invalidates_flag(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """集合被外部释放时不得永久停留在「已加载」假象上。"""
        logger.debug("test_not_loaded_error_invalidates_flag 入口")
        try:
            # Arrange
            from src.memory.vector_store_milvus import MilvusVectorStore

            collection = self._fake_collection(monkeypatch)
            collection.query.side_effect = RuntimeError("collection not loaded")
            store = MilvusVectorStore()

            # Act
            with pytest.raises(RuntimeError):
                await store.get_by_id("entry")

            # Assert
            assert store._collection_loaded is False  # noqa: SLF001
            collection.query.side_effect = None
            collection.query.return_value = []
            await store.get_by_id("entry")
            assert collection.load.call_count == 2
            logger.info("test_not_loaded_error_invalidates_flag 完成")
        except Exception as exc:
            logger.error("test_not_loaded_error_invalidates_flag 异常: %s", exc, exc_info=True)
            raise

    # 方法作用：验证错误分类只把「未加载」类错误视为标记失效原因。
    # Args: self - pytest 测试类实例；message - 异常文案；expected - 期望分类。
    # Returns: 无返回值，断言失败时由 pytest 报告。
    @pytest.mark.parametrize(
        ("message", "expected"),
        [
            ("collection not loaded", True),
            ("Collection not found", True),
            ("no available query node", True),
            ("wait for loading collection timeout, collection: data_agent_knowledge", False),
        ],
    )
    def test_not_loaded_error_marker_classification(
        self, message: str, expected: bool,
    ) -> None:
        """真实日志中的加载超时不得被误判为未加载，否则会绕开熔断反复重载。"""
        logger.debug("test_not_loaded_error_marker_classification 入口")
        from src.memory.vector_store_milvus import MilvusVectorStore

        assert MilvusVectorStore._is_not_loaded_error(RuntimeError(message)) is expected  # noqa: SLF001
        logger.info("test_not_loaded_error_marker_classification 完成")

    # 方法作用：验证熔断期内健康检查直接判不健康且不发起探测 RPC。
    # Args: self - pytest 测试类实例；monkeypatch - pytest 补丁工具。
    # Returns: 无返回值，断言失败时由 pytest 报告。
    @pytest.mark.asyncio
    async def test_health_check_reports_unhealthy_during_circuit(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """集合必然不可服务时，健康检查不应再产生无意义的远程调用。"""
        logger.debug("test_health_check_reports_unhealthy_during_circuit 入口")
        try:
            # Arrange
            from pymilvus import connections, utility

            from src.memory.vector_store_milvus import MilvusVectorStore

            has_connection = MagicMock(return_value=True)
            monkeypatch.setattr(connections, "has_connection", has_connection)
            monkeypatch.setattr(utility, "has_collection", MagicMock(return_value=True))
            store = MilvusVectorStore()
            store._load_failure_until = time.monotonic() + 60  # noqa: SLF001
            store._load_error_detail = "wait for loading collection timeout"  # noqa: SLF001

            # Act
            result = await store.health_check()

            # Assert
            assert result is False
            has_connection.assert_not_called()
            logger.info("test_health_check_reports_unhealthy_during_circuit 完成")
        except Exception as exc:
            logger.error(
                "test_health_check_reports_unhealthy_during_circuit 异常: %s", exc, exc_info=True,
            )
            raise

    # 方法作用：验证空批次写入不触碰集合，边界输入保持零 RPC。
    # Args: self - pytest 测试类实例；monkeypatch - pytest 补丁工具。
    # Returns: 无返回值，断言失败时由 pytest 报告。
    @pytest.mark.asyncio
    async def test_empty_upsert_skips_collection(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """空批次属于边界输入，不得触发集合加载。"""
        logger.debug("test_empty_upsert_skips_collection 入口")
        try:
            # Arrange
            import pymilvus

            from src.memory.vector_store_milvus import MilvusVectorStore

            factory = MagicMock()
            monkeypatch.setattr(pymilvus, "Collection", factory)

            # Act
            result = await MilvusVectorStore().upsert([])

            # Assert
            assert result == 0
            factory.assert_not_called()
            logger.info("test_empty_upsert_skips_collection 完成")
        except Exception as exc:
            logger.error("test_empty_upsert_skips_collection 异常: %s", exc, exc_info=True)
            raise
