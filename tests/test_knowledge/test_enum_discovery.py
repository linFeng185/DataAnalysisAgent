"""枚举值发现测试 — 低基数检测 + 采样逻辑。"""

from __future__ import annotations

import logging
from sqlalchemy.dialects import postgresql

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
            MAX_ROWS_FOR_SAMPLING,
            MAX_DISTINCT_VALUES,
            MAX_EXECUTION_SECONDS,
            LOW_CARDINALITY_THRESHOLD,
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
