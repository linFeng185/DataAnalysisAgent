"""枚举值发现测试 — 低基数检测 + 采样逻辑。"""

from __future__ import annotations

import asyncio
import logging
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from sqlalchemy.dialects import mssql, oracle, postgresql

from src.knowledge.enum_discovery import is_low_cardinality_candidate

logger = logging.getLogger(__name__)


class TestLowCardinalityDetection:
    def test_few_values_is_candidate(self):
        assert is_low_cardinality_candidate(["a", "b", "c"]) is True

    def test_exactly_20_is_candidate(self):
        assert is_low_cardinality_candidate([str(i) for i in range(20)]) is True

    def test_over_20_not_candidate(self):
        assert is_low_cardinality_candidate([str(i) for i in range(21)]) is False

    def test_empty_values_not_candidate(self):
        assert is_low_cardinality_candidate([]) is True  # empty is technically <= 20


class TestEnumDiscoveryIntegration:
    def test_module_imports(self):
        """确认模块可正常导入。"""
        from src.knowledge.enum_discovery import auto_discover_enum_values
        assert callable(auto_discover_enum_values)

    def test_constants(self):
        """确认安全阈值常量符合 SPEC 约束。"""
        from src.knowledge.enum_discovery import (
            LOW_CARDINALITY_THRESHOLD,
            MAX_DISTINCT_VALUES,
            MAX_EXECUTION_SECONDS,
            MAX_ROWS_FOR_SAMPLING,
        )
        assert MAX_ROWS_FOR_SAMPLING == 10_000_000
        assert MAX_DISTINCT_VALUES == 50
        assert MAX_EXECUTION_SECONDS == 5
        assert LOW_CARDINALITY_THRESHOLD == 20

    # 方法作用：验证枚举发现中的限定表名和列名按数据库方言安全引用。
    # Args: self - pytest 测试类实例。
    # Returns: 无返回值，断言失败时由 pytest 报告。
    def test_identifier_quoting_blocks_expression_injection(self):
        """用户控制的标识符不得成为 DISTINCT SQL 表达式。"""
        logger.debug("test_identifier_quoting_blocks_expression_injection 入口")
        from src.knowledge.enum_discovery import _quote_qualified_identifier

        dialect = postgresql.dialect()
        table = _quote_qualified_identifier(
            dialect, 'public.orders"; DROP TABLE users; --',
        )
        column = _quote_qualified_identifier(dialect, 'status" OR 1=1 --')

        assert table == '"public"."orders""; DROP TABLE users; --"'
        assert column == '"status"" OR 1=1 --"'
        logger.info("test_identifier_quoting_blocks_expression_injection 完成")

    # 方法作用：验证不同数据库方言使用各自支持的结果上限语法。
    # Args: self - pytest 测试类实例。
    # Returns: 无返回值，断言失败时由 pytest 报告。
    @pytest.mark.asyncio
    async def test_sample_distinct_uses_dialect_specific_limit(self):
        """MSSQL/Oracle 采样不得发送 PostgreSQL 风格 LIMIT。"""
        # Arrange
        from src.knowledge.enum_discovery import _sample_distinct

        class _Result:
            def fetchall(self):
                return [("paid",), ("refunded",)]

        class _Connection:
            def __init__(self, engine):
                self.engine = engine

            async def __aenter__(self):
                return self

            async def __aexit__(self, *_args):
                return False

            async def execute(self, statement, params=None):
                self.engine.sql = str(statement)
                self.engine.params = params
                return _Result()

        class _Engine:
            def __init__(self, dialect):
                self.dialect = dialect
                self.sql = ""
                self.params = None

            def connect(self):
                return _Connection(self)

        # Act
        captured = {}
        for name, dialect in (("mssql", mssql.dialect()), ("oracle", oracle.dialect()), ("postgres", postgresql.dialect())):
            engine = _Engine(dialect)
            values = await _sample_distinct(
                SimpleNamespace(engine=engine, connector=None),
                "sales.orders",
                "status",
                50,
            )
            captured[name] = (engine.sql, values)

        # Assert
        assert "TOP" in captured["mssql"][0].upper()
        assert " LIMIT " not in captured["mssql"][0].upper()
        assert "FETCH FIRST" in captured["oracle"][0].upper()
        assert " LIMIT " not in captured["oracle"][0].upper()
        assert " LIMIT " in captured["postgres"][0].upper()
        assert captured["mssql"][1] == ["paid", "refunded"]

    # 方法作用：验证同步数据库引擎在线程池中执行采样，避免阻塞事件循环。
    # Args: self - pytest 测试类实例。
    # Returns: 无返回值，断言失败时由 pytest 报告。
    @pytest.mark.asyncio
    async def test_sample_distinct_supports_sync_engine(self):
        """Oracle/MSSQL 同步 Engine 也应返回采样值。"""
        # Arrange
        from src.knowledge.enum_discovery import _sample_distinct

        class _Result:
            def fetchall(self):
                return [("active",), ("inactive",)]

        class _Connection:
            def __enter__(self):
                return self

            def __exit__(self, *_args):
                return False

            def execute(self, *_args, **_kwargs):
                return _Result()

        class _Engine:
            dialect = mssql.dialect()

            def connect(self):
                return _Connection()

        # Act
        values = await _sample_distinct(
            SimpleNamespace(engine=_Engine(), connector=None),
            "dbo.users",
            "status",
            20,
        )

        # Assert
        assert values == ["active", "inactive"]

    # 方法作用：验证枚举采样超出时间预算时安全回退为空结果。
    # Args: self - pytest 测试类实例；monkeypatch - pytest 补丁工具。
    # Returns: 无返回值，断言失败时由 pytest 报告。
    @pytest.mark.asyncio
    async def test_sample_distinct_honors_timeout(self, monkeypatch):
        """慢查询不得无限占用 Schema 后台任务。"""
        # Arrange
        from src.knowledge import enum_discovery as module

        class _Connection:
            async def __aenter__(self):
                return self

            async def __aexit__(self, *_args):
                return False

            async def execute(self, *_args, **_kwargs):
                await asyncio.sleep(0.05)
                return SimpleNamespace(fetchall=lambda: [("slow",)])

        class _Engine:
            dialect = postgresql.dialect()

            def connect(self):
                return _Connection()

        monkeypatch.setattr(module, "MAX_EXECUTION_SECONDS", 0.001)

        # Act
        values = await module._sample_distinct(
            SimpleNamespace(engine=_Engine(), connector=None),
            "orders",
            "status",
            20,
        )

        # Assert
        assert values is None

    # 方法作用：验证枚举发现把显式租户传递给数据源 Registry。
    # Args: self - pytest 测试类实例；monkeypatch - pytest 补丁工具。
    # Returns: 无返回值，断言失败时由 pytest 报告。
    @pytest.mark.asyncio
    async def test_auto_discover_passes_tenant_to_registry(self, monkeypatch):
        """同名数据源在不同租户下必须解析到各自连接。"""
        # Arrange
        from src.knowledge import enum_discovery as module

        registry = SimpleNamespace(resolve=AsyncMock(return_value=None))
        monkeypatch.setattr("src.datasource.registry.get_registry", lambda: registry)

        # Act
        result = await module.auto_discover_enum_values(
            "sales",
            "orders",
            "status",
            tenant_id=42,
        )

        # Assert
        assert result is None
        registry.resolve.assert_awaited_once_with("sales", tenant_id=42)

    # 方法作用：验证仅挂载 Connector 的数据源也能完成枚举采样。
    # Args: self - pytest 测试类实例。
    # Returns: 无返回值，断言失败时由 pytest 报告。
    @pytest.mark.asyncio
    async def test_sample_distinct_supports_connector_without_engine(self):
        """延迟创建 Engine 的 Connector-only 数据源不得被错误跳过。"""
        # Arrange
        from src.knowledge.enum_discovery import _sample_distinct

        class _Connector:
            config = SimpleNamespace(dialect="postgres")

            async def execute_bounded(self, _sql, _max_rows, _params=None):
                return ([{"value": "paid"}, {"value": "refunded"}], False)

        # Act
        values = await _sample_distinct(
            SimpleNamespace(engine=None, connector=_Connector()),
            "orders",
            "status",
            20,
        )

        # Assert
        assert values == ["paid", "refunded"]
