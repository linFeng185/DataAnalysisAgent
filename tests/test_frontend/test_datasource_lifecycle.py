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

    # 方法作用：验证轮询请求失败只记行级提示，不覆盖上一轮读到的真实同步状态。
    # Args: self - pytest 测试类实例。
    # Returns: 无返回值，断言失败时由 pytest 报告。
    def test_poll_failure_keeps_last_known_sync_status(self) -> None:
        """一次列表请求失败不得把所有行的 schema_sync 覆盖成 unknown。"""
        # Arrange
        source = Path("frontend/src/pages/DatasourcePage.tsx").read_text(encoding="utf-8")

        # Act：取出 poll 的失败分支，确认它只写行级提示
        poll_body = source.split("const poll = async", 1)[1].split(
            "timer = window.setTimeout(() => void poll(), 1500);", 1)[0]
        failure_branch = poll_body.split("} catch {", 1)[1]

        # Assert
        assert "sync_read_error" in failure_branch, "轮询失败分支未记录行级读取失败提示"
        assert "schema_sync:" not in failure_branch, "轮询失败分支仍在改写 schema_sync，会覆盖真实状态"
        assert "schema_sync: { status: 'unknown' }" not in source, "unknown 仍在覆盖行状态"

    # 方法作用：验证轮询在未读到状态或读取失败时继续续期，全部终态才停止。
    # Args: self - pytest 测试类实例。
    # Returns: 无返回值，断言失败时由 pytest 报告。
    def test_polling_continues_until_every_row_is_terminal(self) -> None:
        """unknown 与读取失败都不是终态必须续期，全部终态必须停表。"""
        # Arrange
        source = Path("frontend/src/pages/DatasourcePage.tsx").read_text(encoding="utf-8")

        # Act：续期守卫必须同时覆盖进行中、未读到和读取失败三种情况
        guard = source.split("const active = datasources.some", 1)[1].split("if (!active)", 1)[0]

        # Assert
        assert "['running', 'cancelling'].includes(item.schema_sync?.status || '')" in guard
        assert "item.schema_sync?.status === 'unknown'" in guard, "unknown 行不会让轮询续期，页面无法自愈"
        assert "item.sync_read_error" in guard, "读取失败行不会让轮询续期，页面无法自愈"
        assert "if (!active) return undefined;" in source, "缺少全部终态即停的守卫，会变成永不停止的轮询"

    # 方法作用：验证 unknown 不再禁用「同步表结构」，未知状态允许重新发起同步。
    # Args: self - pytest 测试类实例。
    # Returns: 无返回值，断言失败时由 pytest 报告。
    def test_unknown_status_keeps_sync_action_clickable(self) -> None:
        """unknown 只代表没读到状态，不能让「同步表结构」永久变灰。"""
        # Arrange
        source = Path("frontend/src/pages/DatasourcePage.tsx").read_text(encoding="utf-8")

        # Act：定位启动同步按钮所在行
        start_button = [line for line in source.splitlines() if "void changeSync(datasource)}" in line]

        # Assert
        assert len(start_button) == 1, "未找到唯一的「同步表结构」按钮"
        assert "unknown" not in start_button[0], "unknown 仍在禁用「同步表结构」"
        assert "['running', 'cancelling'].includes(datasource.schema_sync?.status || '')" in start_button[0], \
            "确认进行中的同步必须继续禁用「同步表结构」"

    # 方法作用：验证发布阶段禁用终止时给出原因，且 running 但 phase 未知不会误禁。
    # Args: self - pytest 测试类实例。
    # Returns: 无返回值，断言失败时由 pytest 报告。
    def test_publishing_phase_stop_sync_disabled_with_reason(self) -> None:
        """发布阶段后端返回 409，界面保持禁用必须解释原因。"""
        # Arrange
        source = Path("frontend/src/pages/DatasourcePage.tsx").read_text(encoding="utf-8")
        dictionary = Path("frontend/src/i18n/en.ts").read_text(encoding="utf-8")

        # Act：取出终止按钮所在区块的禁用条件和 Tooltip 文案
        cancel_block = source.split("void changeSync(datasource, true)}", 1)[0].split(
            "{['running', 'cancelling'].includes(datasource.schema_sync?.status || '') && (", 1)[1]
        explains = "发布阶段不可终止，避免留下半发布快照" in cancel_block
        tooltip_guarded = "<Tooltip title={datasource.schema_sync?.phase === 'publishing' ? t(" in cancel_block

        # Assert
        assert explains, "发布阶段禁用终止没有说明原因"
        assert tooltip_guarded, "禁用原因未绑定到发布阶段 Tooltip"
        assert "datasource.schema_sync?.phase === 'publishing'}" in cancel_block, \
            "禁用条件必须精确等值判断，running 但 phase 未知不得误禁"
        assert "发布阶段不可终止，避免留下半发布快照" in dictionary, "新增界面文案缺少英文词条"
