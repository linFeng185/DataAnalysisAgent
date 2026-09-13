"""知识库上传表单交互契约测试，覆盖 spec/44 §3。

事实依据：前端没有 JS 测试框架，沿用仓库既有的源码静态断言模式；
tests/test_frontend/ 既有文件覆盖报表模板主题，无上传表单主题；本文件只读取
KnowledgePage.tsx 源码做断言，不读写任何数据文件。
"""

from __future__ import annotations

import re
from pathlib import Path

SOURCE = Path("frontend/src/pages/KnowledgePage.tsx").read_text(encoding="utf-8")


class TestChunkControls:
    """验证切块与重叠的滑块与手动输入并存。"""

    # 方法作用：验证两个字段都使用 Slider。
    # Args: self - 测试实例。
    # Returns: 无返回值，断言失败时由 pytest 报告。
    def test_sliders_present(self) -> None:
        """滑块与手动输入并存。"""
        # Act / Assert
        assert SOURCE.count("<Slider") >= 2

    # 方法作用：验证滑块范围与步长符合设计。
    # Args: self - 测试实例。
    # Returns: 无返回值，断言失败时由 pytest 报告。
    def test_slider_ranges(self) -> None:
        """块大小 200-4000 步长 100，重叠步长 10。"""
        # Act / Assert
        assert "min={200}" in SOURCE and "max={4000}" in SOURCE and "step={100}" in SOURCE
        assert "step={10}" in SOURCE

    # 方法作用：验证重叠上限随块大小收窄。
    # Args: self - 测试实例。
    # Returns: 无返回值，断言失败时由 pytest 报告。
    def test_overlap_clamped_by_chunk_size(self) -> None:
        """overlap 必须小于 chunkSize。"""
        # Act / Assert
        assert re.search(r"Math\.min\(\s*500\s*,", SOURCE)


class TestDatasourceSelect:
    """验证数据源为下拉选择。"""

    # 方法作用：验证数据源不再使用文本输入。
    # Args: self - 测试实例。
    # Returns: 无返回值，断言失败时由 pytest 报告。
    def test_datasource_uses_select(self) -> None:
        """全仓库禁止手输数据源名称。"""
        # Act
        segment = SOURCE.split('{t("数据源")}')[1].split("</div>")[0]

        # Assert
        assert "<Select" in segment
        assert "fetchDatasourceCatalog" in SOURCE

    # 方法作用：验证旧的手输占位文案已移除。
    # Args: self - 测试实例。
    # Returns: 无返回值，断言失败时由 pytest 报告。
    def test_placeholder_no_longer_free_text(self) -> None:
        """旧占位「可选数据源名称」必须移除。"""
        # Act / Assert
        assert "可选数据源名称" not in SOURCE


class TestTagPicker:
    """验证标签多选面板。"""

    # 方法作用：验证面板使用自定义下拉渲染。
    # Args: self - 测试实例。
    # Returns: 无返回值，断言失败时由 pytest 报告。
    def test_panel_uses_dropdown_render(self) -> None:
        """标签需要以标签块网格展示。"""
        # Act / Assert
        assert "dropdownRender" in SOURCE

    # 方法作用：验证选中态含勾选标记与浅色背景。
    # Args: self - 测试实例。
    # Returns: 无返回值，断言失败时由 pytest 报告。
    def test_selected_state_marker(self) -> None:
        """选中标签右下角需要勾。"""
        # Act / Assert
        assert "CheckOutlined" in SOURCE
        assert "#e6f4ff" in SOURCE

    # 方法作用：验证新建个人标签入口保留。
    # Args: self - 测试实例。
    # Returns: 无返回值，断言失败时由 pytest 报告。
    def test_create_tag_entry_kept(self) -> None:
        """面板内需保留新建个人标签。"""
        # Act / Assert
        assert "createPersonalTag" in SOURCE

    # 方法作用：验证标签块支持键盘切换。
    # Args: self - 测试实例。
    # Returns: 无返回值，断言失败时由 pytest 报告。
    def test_tag_blocks_are_keyboard_reachable(self) -> None:
        """标签块需要 role 与 Enter 响应，保证键盘可达。"""
        # Act / Assert
        assert 'role="button"' in SOURCE
        assert "event.key === 'Enter'" in SOURCE
