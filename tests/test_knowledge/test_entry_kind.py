"""知识条目来源分层过滤测试，覆盖 spec/44 §2.2。"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from src.api.routes.knowledge import _entry_kind_filters  # noqa: E402


class TestEntryKindFilters:
    """验证 document / introspect / all 三档来源过滤与长期记忆排除。"""

    # 方法作用：验证默认档排除内省条目与长期记忆条目。
    # Args: self - 测试实例。
    # Returns: 无返回值，断言失败时由 pytest 报告。
    def test_document_kind_excludes_introspect_and_memory(self) -> None:
        """文档视图只保留有来源的知识。"""
        # Act
        filters = _entry_kind_filters("document")

        # Assert
        assert filters["not:source"] == "auto_introspect"
        assert filters["not:resource_kind"] == "long_term_memory"

    # 方法作用：验证内省档只取自动内省条目。
    # Args: self - 测试实例。
    # Returns: 无返回值，断言失败时由 pytest 报告。
    def test_introspect_kind_selects_only_introspect(self) -> None:
        """内省视图是正向过滤，不使用取反。"""
        # Act
        filters = _entry_kind_filters("introspect")

        # Assert
        assert filters["source"] == "auto_introspect"

    # 方法作用：验证 all 档仍排除长期记忆条目。
    # Args: self - 测试实例。
    # Returns: 无返回值，断言失败时由 pytest 报告。
    def test_all_kind_still_excludes_long_term_memory(self) -> None:
        """长期记忆属于记忆模块，任何档位都不得混入知识库。"""
        # Act
        filters = _entry_kind_filters("all")

        # Assert
        assert filters == {"not:resource_kind": "long_term_memory"}

    # 方法作用：验证内省档同样排除长期记忆条目。
    # Args: self - 测试实例。
    # Returns: 无返回值，断言失败时由 pytest 报告。
    def test_introspect_kind_excludes_long_term_memory(self) -> None:
        """内省档的正向条件也不能放行记忆条目。"""
        # Act
        filters = _entry_kind_filters("introspect")

        # Assert
        assert filters.get("not:resource_kind") == "long_term_memory"

    # 方法作用：验证非法取值被拒绝而不是静默放行。
    # Args: self - 测试实例。
    # Returns: 无返回值，断言失败时由 pytest 报告。
    def test_invalid_kind_rejected(self) -> None:
        """未知档位必须报错，避免退化成全量泄露。"""
        # Act / Assert
        with pytest.raises(ValueError):
            _entry_kind_filters("unknown")
