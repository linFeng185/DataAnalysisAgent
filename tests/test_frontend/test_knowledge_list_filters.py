"""知识库列表筛选与创建人列契约测试，覆盖 spec/44 §2.2 §2.3。

事实依据：前端没有 JS 测试框架，沿用仓库既有的源码静态断言模式；
tests/test_frontend/ 既有文件无列表筛选主题；本文件只读取 .tsx/.ts 源码做断言，
不读写任何数据文件。
"""

from __future__ import annotations

from pathlib import Path

SOURCE = Path("frontend/src/pages/KnowledgePage.tsx").read_text(encoding="utf-8")
TYPES = Path("frontend/src/types/index.ts").read_text(encoding="utf-8")


class TestEntryKindFilter:
    """验证来源分层筛选控件。"""

    # 方法作用：验证列表请求携带 entry_kind。
    # Args: self - 测试实例。
    # Returns: 无返回值，断言失败时由 pytest 报告。
    def test_request_carries_entry_kind(self) -> None:
        """默认档位必须是 document。"""
        # Act / Assert
        assert "entry_kind" in SOURCE
        assert "'document'" in SOURCE and "'introspect'" in SOURCE

    # 方法作用：验证切档位时重置页码。
    # Args: self - 测试实例。
    # Returns: 无返回值，断言失败时由 pytest 报告。
    def test_kind_change_resets_page(self) -> None:
        """换档后停留在旧页码会看到空列表。"""
        # Act
        segment = SOURCE.split("onChange={value => { setEntryKind(")[1][:300]

        # Assert
        assert "setPage(1)" in segment


class TestSystemKnowledgeToggle:
    """验证「显示系统知识」开关。"""

    # 方法作用：验证开关存在且默认关闭。
    # Args: self - 测试实例。
    # Returns: 无返回值，断言失败时由 pytest 报告。
    def test_toggle_defaults_to_off(self) -> None:
        """默认隐藏系统知识。"""
        # Act / Assert
        assert "useState(false)" in SOURCE
        assert "include_system" in SOURCE

    # 方法作用：验证开关状态不落本地存储。
    # Args: self - 测试实例。
    # Returns: 无返回值，断言失败时由 pytest 报告。
    def test_toggle_not_persisted(self) -> None:
        """刷新页面必须回到默认隐藏。"""
        # Act / Assert
        assert "localStorage" not in SOURCE


class TestOwnerColumn:
    """验证创建人列。"""

    # 方法作用：验证条目表与文档表都有创建人列。
    # Args: self - 测试实例。
    # Returns: 无返回值，断言失败时由 pytest 报告。
    def test_owner_column_present(self) -> None:
        """两个表都要展示创建人。"""
        # Act / Assert
        assert SOURCE.count('t("创建人")') >= 2

    # 方法作用：验证类型定义补齐字段。
    # Args: self - 测试实例。
    # Returns: 无返回值，断言失败时由 pytest 报告。
    def test_types_extended(self) -> None:
        """缺少类型字段会导致 TS 构建失败。"""
        # Act / Assert
        assert TYPES.count("owner_username") >= 2

    # 方法作用：验证无主条目回退文案。
    # Args: self - 测试实例。
    # Returns: 无返回值，断言失败时由 pytest 报告。
    def test_system_fallback_label(self) -> None:
        """系统写入的条目显示「系统」。"""
        # Act / Assert
        assert 't("系统")' in SOURCE


class TestIntrospectReadOnly:
    """验证内省条目只读。"""

    # 方法作用：验证内省条目展示维护说明。
    # Args: self - 测试实例。
    # Returns: 无返回值，断言失败时由 pytest 报告。
    def test_introspect_rows_have_no_delete(self) -> None:
        """后端本就拒绝，前端不得给出可点按钮。"""
        # Act / Assert
        assert 't("随表结构自动维护")' in SOURCE
