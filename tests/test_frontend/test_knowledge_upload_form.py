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


class TestUploadModalStateReset:
    """验证重新打开上传弹窗时表单状态被完整重置，不残留上一次的输入。"""

    # 方法作用：验证默认配置工厂清空标签、数据源与分类。
    # Args: self - 测试实例。
    # Returns: 无返回值，断言失败时由 pytest 报告。
    def test_default_cfg_clears_tag_datasource_and_category(self) -> None:
        """默认配置必须清空标签多选、数据源与知识类别。"""
        # Act
        segment = SOURCE.split("const createDefaultUploadCfg")[1].split("});")[0]

        # Assert
        assert "tagIds: []" in segment
        assert "datasource: ''" in segment
        assert "category: ''" in segment
        assert "strategy: 'auto'" in segment and "chunkSize: 800" in segment

    # 方法作用：验证重置助手覆盖弹窗内的全部状态。
    # Args: self - 测试实例。
    # Returns: 无返回值，断言失败时由 pytest 报告。
    def test_reset_helper_clears_all_modal_state(self) -> None:
        """重置助手必须清空配置、待上传文件、个人标签输入与下拉展开态。"""
        # Act
        segment = SOURCE.split("const resetUploadForm")[1].split("};")[0]

        # Assert
        assert "setUploadCfg(createDefaultUploadCfg(role))" in segment
        assert "setPendingFiles([])" in segment
        assert "setPersonalTagName('')" in segment
        assert "setTagPanelOpen(false)" in segment

    # 方法作用：验证打开弹窗前先重置，而非关闭时重置。
    # Args: self - 测试实例。
    # Returns: 无返回值，断言失败时由 pytest 报告。
    def test_reset_runs_before_dialog_open(self) -> None:
        """handleFileSelect 必须先调用重置再打开弹窗。"""
        # Act
        segment = SOURCE.split("const handleFileSelect")[1].split("};")[0]

        # Assert
        assert "resetUploadForm();" in segment
        assert segment.index("resetUploadForm();") < segment.index("setCfgOpen(true)")

    # 方法作用：验证上传配置 state 由工厂函数初始化而非字面量。
    # Args: self - 测试实例。
    # Returns: 无返回值，断言失败时由 pytest 报告。
    def test_state_initialized_from_factory(self) -> None:
        """上传配置 state 复用同一工厂，避免默认值两处漂移。"""
        # Act / Assert
        assert "useState<UploadCfg>(() => createDefaultUploadCfg(role))" in SOURCE


class TestSliderWidth:
    """验证切块滑块有显式宽度约束，不会在 flex 布局中被压成零宽。"""

    # 方法作用：验证两个滑块都放在 flex 子项包裹层中。
    # Args: self - 测试实例。
    # Returns: 无返回值，断言失败时由 pytest 报告。
    def test_sliders_wrapped_by_flex_item(self) -> None:
        """滑块外层需要 flex:1 与 minWidth:0，轴条才有可见宽度。"""
        # Act / Assert
        assert SOURCE.count("flex: '1 1 0'") >= 2
        assert SOURCE.count("minWidth: 0") >= 2
        assert SOURCE.count("<Slider style={{ width: '100%' }}") >= 2

    # 方法作用：验证滑块不再使用 antd Space 包裹。
    # Args: self - 测试实例。
    # Returns: 无返回值，断言失败时由 pytest 报告。
    def test_space_no_longer_wraps_sliders(self) -> None:
        """Space 会额外包一层 div，加在 Slider 上的 flex 对布局无效，必须移除。"""
        # Act / Assert
        assert "Space style={{ width: '100%' }}" not in SOURCE

    # 方法作用：验证数字输入框不会被滑块挤压。
    # Args: self - 测试实例。
    # Returns: 无返回值，断言失败时由 pytest 报告。
    def test_input_number_keeps_fixed_width(self) -> None:
        """手动输入框保持 96px 且不收缩。"""
        # Act / Assert
        assert SOURCE.count("style={{ width: 96, flexShrink: 0 }}") >= 2


class TestRepeatedFilePick:
    """验证取消后重选同一个文件仍能打开上传弹窗。"""

    # 方法作用：验证选择文件后清空了隐藏 input 的 value。
    # Args: self - 测试实例。
    # Returns: 无返回值，断言失败时由 pytest 报告。
    def test_file_input_value_reset_after_pick(self) -> None:
        """浏览器只在 value 变化时派发 change，不清空就选不了同一个文件。"""
        # Arrange
        handler = SOURCE.split("const handleFileSelect", 1)[1].split("};", 1)[0]
        # Act / Assert
        assert "fileRef.current.value = ''" in handler
        # 清空必须发生在提前返回之前，否则取消选择时不会复位。
        assert handler.index("fileRef.current.value = ''") < handler.index("return;")

    # 方法作用：验证选中文件在清空 input 之前先快照成数组。
    # Args: self - 测试实例。
    # Returns: 无返回值，断言失败时由 pytest 报告。
    def test_file_snapshot_precedes_value_reset(self) -> None:
        """e.target.files 与 input 绑定，清空 value 会把它一并清空成空列表。

        真实浏览器实测：清空 value 后同一 FileList 引用长度由 1 变 0，
        因此必须先 Array.from 快照，否则后续判空恒真、弹窗永远打不开。
        """
        # Arrange
        handler = SOURCE.split("const handleFileSelect", 1)[1].split("};", 1)[0]
        # Act
        snapshot = handler.index("Array.from(files)")
        reset = handler.index("fileRef.current.value = ''")
        # Assert
        assert snapshot < reset, "必须先快照 FileList，再清空 input.value"
        # 判空与写回都必须基于快照，不能再读已失效的活引用。
        assert "setPendingFiles(selected)" in handler
        assert "files.length === 0" not in handler
