"""数据源管理页生命周期能力静态契约测试。"""

from pathlib import Path


class TestDatasourcePageLifecycle:
    """覆盖功能 18.3.4、18.3.5 的前端交互入口。"""

    # 方法作用：验证页面接入编辑接口和临时连接测试接口。
    # Args: self - pytest 测试类实例。
    # Returns: 无返回值，断言失败时由 pytest 报告。
    def test_page_exposes_edit_and_connection_test_actions(self) -> None:
        """管理员应能在同一表单内测试、创建和编辑数据源。"""
        # Arrange
        source = Path("frontend/src/pages/DatasourcePage.tsx").read_text(encoding="utf-8")

        # Act
        has_update = "put(`/datasources/${encodeURIComponent(editing.name)}`" in source
        has_probe = "post<DatasourceConnectionResult>('/datasources/test'" in source

        # Assert
        assert has_update
        assert has_probe
        assert "EditOutlined" in source
        assert "测试连接" in source

    # 方法作用：验证移动窄屏时数据源表格可以横向滚动而不挤压文本。
    # Args: self - pytest 测试类实例。
    # Returns: 无返回值，断言失败时由 pytest 报告。
    def test_table_has_stable_responsive_width(self) -> None:
        """固定格式表格应声明横向滚动宽度。"""
        # Arrange
        source = Path("frontend/src/pages/DatasourcePage.tsx").read_text(encoding="utf-8")

        # Act / Assert
        assert "scroll={{ x:" in source

    # 方法作用：验证导入改名映射被界面消费而不是静默丢弃。
    # Args: self - pytest 测试类实例。
    # Returns: 无返回值，断言失败时由 pytest 报告。
    def test_import_rename_mapping_is_surfaced(self) -> None:
        """spec/37 要求改名必须在界面提示，只报导入条数等同静默丢失。"""
        # Arrange
        source = Path("frontend/src/pages/DatasourcePage.tsx").read_text(encoding="utf-8")

        # Act / Assert
        assert "renamed" in source, "导入响应中的 renamed 未被前端读取"
        assert "因重名已改名" in source, "改名映射未在界面提示"
