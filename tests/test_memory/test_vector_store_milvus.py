"""Milvus 过滤表达式和精确 metadata 校验回归测试。"""

from __future__ import annotations

import json
import logging
from types import SimpleNamespace

import pytest


logger = logging.getLogger(__name__)


class TestMilvusExpressionSafety:
    """覆盖功能 19.17：Milvus 表达式转义与精确过滤。"""

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
