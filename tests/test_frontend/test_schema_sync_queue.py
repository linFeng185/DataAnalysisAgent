"""数据源同步排队的界面契约：非终态轮询、排队位置文案与排队中终止。"""

from pathlib import Path


class TestSchemaSyncQueueContract:
    """覆盖需求 8.3、8.4 的前端交互入口。"""

    # 方法作用：验证排队状态被当作非终态继续轮询。
    # Args: self - pytest 测试类实例。
    # Returns: 无返回值，断言失败时由 pytest 报告。
    def test_queued_status_keeps_polling(self) -> None:
        """排队任务不会自己刷新成运行中，轮询必须覆盖 queued。"""
        # Arrange
        source = Path("frontend/src/pages/DatasourcePage.tsx").read_text(encoding="utf-8")

        # Act：取出轮询续期守卫
        guard = source.split("const active = datasources.some", 1)[1].split("if (!active)", 1)[0]

        # Assert
        assert "['running', 'cancelling', 'queued'].includes(item.schema_sync?.status || '')" in guard, \
            "queued 未纳入轮询守卫，排队任务不会自动刷新成运行中"

    # 方法作用：验证状态列展示排队位置文案。
    # Args: self - pytest 测试类实例。
    # Returns: 无返回值，断言失败时由 pytest 报告。
    def test_queue_position_label(self) -> None:
        """排队状态要显示「前面还有 N 个」，下一个执行时显示即将开始。"""
        # Arrange
        source = Path("frontend/src/pages/DatasourcePage.tsx").read_text(encoding="utf-8")
        dictionary = Path("frontend/src/i18n/en.ts").read_text(encoding="utf-8")

        # Act：取出状态文案函数与状态列渲染
        label = source.split("const syncLabel =", 1)[1].split("};", 1)[0]

        # Assert：文案读取 queue_position，并在 0 时给出即将开始的说法
        assert "queue_position" in label, "状态文案未读取服务端返回的队列位置"
        assert "排队中（前面还有 {value0} 个）" in label
        assert "排队中（即将开始）" in label
        assert "syncLabel(state)" in source, "状态列未使用带队列位置的文案函数"
        assert '"排队中（前面还有 {value0} 个）"' in dictionary, "排队位置文案缺少英文词条"
        assert '"排队中（即将开始）"' in dictionary, "排队即将开始文案缺少英文词条"

    # 方法作用：验证排队中允许终止，排队中禁止重复启动。
    # Args: self - pytest 测试类实例。
    # Returns: 无返回值，断言失败时由 pytest 报告。
    def test_queued_row_can_be_cancelled(self) -> None:
        """排队任务必须能取消，否则用户只能干等；重复启动后端仍会 409。"""
        # Arrange
        source = Path("frontend/src/pages/DatasourcePage.tsx").read_text(encoding="utf-8")

        # Act：取出终止按钮的可见条件、禁用条件与启动按钮所在行
        visible = "['running', 'cancelling', 'queued'].includes(datasource.schema_sync?.status || '') && ("
        cancel_line = [line for line in source.splitlines() if "void changeSync(datasource, true)}" in line]
        start_button = [line for line in source.splitlines() if "void changeSync(datasource)}" in line]

        # Assert：排队时出现终止按钮、按钮默认可用，且排队中不能重复启动
        assert visible in source, "终止按钮可见条件未覆盖排队状态，排队任务无法取消"
        assert len(cancel_line) == 1 and "终止同步" in cancel_line[0], "排队中缺少终止按钮，用户无法取消排队"
        disabled_expr = cancel_line[0].split("disabled=", 1)[1].split(" onClick=", 1)[0]
        assert "'queued'" not in disabled_expr, "排队中终止按钮被禁用，用户无法取消排队"
        assert len(start_button) == 1
        assert "'queued'" in start_button[0], "排队中未禁用「同步表结构」，后端会返回 409"

    # 方法作用：验证排队状态有独立文案与颜色，不被当成失败。
    # Args: self - pytest 测试类实例。
    # Returns: 无返回值，断言失败时由 pytest 报告。
    def test_queued_label_registered(self) -> None:
        """排队是正常同步状态，必须出现在标签映射里。"""
        # Arrange
        source = Path("frontend/src/pages/DatasourcePage.tsx").read_text(encoding="utf-8")

        # Act / Assert
        assert "queued: t(\"排队中\")" in source, "排队状态缺少中文标签"
