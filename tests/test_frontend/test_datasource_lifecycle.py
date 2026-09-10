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

    # 方法作用：验证数据源页轮询只在存在进行中同步时续期。
    # Args: self - pytest 测试类实例。
    # Returns: 无返回值，断言失败时由 pytest 报告。
    def test_polling_only_runs_while_sync_in_progress(self) -> None:
        """空闲页面持续轮询列表接口属于无谓负载，续期必须有可达的停止条件。"""
        # Arrange
        source = Path("frontend/src/pages/DatasourcePage.tsx").read_text(encoding="utf-8")

        # Act：轮询必须有进行中同步守卫，并以列表为依赖以便状态变化时启停
        guarded = "['running', 'cancelling'].includes(item.schema_sync?.status || '')" in source
        depends_on_list = "}, [datasources]);" in source

        # Assert
        assert guarded, "轮询缺少「存在 running/cancelling 同步」的守卫"
        assert depends_on_list, "轮询 effect 未以 datasources 为依赖，状态变化无法启停轮询"
