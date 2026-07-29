"""P3 图表、Skill 面板和移动布局前端契约测试。"""

from pathlib import Path


class TestP3Experience:
    """覆盖功能 14.8、18.7.7、18.8.2 的前端入口。"""

    # 方法作用：验证图表面板调用重生成 API 并携带原始数据。
    # Args: self - pytest 测试类实例。
    # Returns: 无返回值，断言失败时由 pytest 报告。
    def test_chart_panel_exposes_adjustment_action(self) -> None:
        """结果图表应能在不重新执行 SQL 的情况下切换类型。"""
        # Arrange
        source = Path("frontend/src/components/ChartPanel.tsx").read_text(encoding="utf-8")

        # Act / Assert
        assert "post<ChartConfig>('/charts/adjust'" in source
        assert "rows" in source
        assert "调整图表" in source

    # 方法作用：验证聊天页加载可见 Skill 并把选中资源 ID 传入流请求。
    # Args: self - pytest 测试类实例。
    # Returns: 无返回值，断言失败时由 pytest 报告。
    def test_chat_page_has_explicit_skill_panel(self) -> None:
        """对话前应能选择一个或多个已启用 Skill。"""
        # Arrange
        page = Path("frontend/src/pages/ChatPage.tsx").read_text(encoding="utf-8")
        hook = Path("frontend/src/hooks/useChat.ts").read_text(encoding="utf-8")
        client = Path("frontend/src/api/client.ts").read_text(encoding="utf-8")

        # Act / Assert
        assert "selectedSkillIds" in page
        assert "enabled_skill_ids" in client
        assert "enabledSkillIds" in hook

    # 方法作用：验证窄屏改用抽屉导航并隐藏次要健康标签。
    # Args: self - pytest 测试类实例。
    # Returns: 无返回值，断言失败时由 pytest 报告。
    def test_mobile_layout_uses_drawer_navigation(self) -> None:
        """375px 视口不能保留占宽侧栏或让 Header 标签重叠。"""
        # Arrange
        app = Path("frontend/src/App.tsx").read_text(encoding="utf-8")
        styles = Path("frontend/src/index.css").read_text(encoding="utf-8")

        # Act / Assert
        assert "<Drawer" in app
        assert "mobile-health" in app
        assert "@media (max-width: 767px)" in styles

    # 方法作用：验证 Skills 管理页提供 Registry 目录和安装入口。
    # Args: self - pytest 测试类实例。
    # Returns: 无返回值，断言失败时由 pytest 报告。
    def test_skills_page_exposes_registry_installation(self) -> None:
        """已配置 Registry 时管理员应能浏览并安装审核版本。"""
        # Arrange
        source = Path("frontend/src/pages/SkillsPage.tsx").read_text(encoding="utf-8")

        # Act / Assert
        assert "'/skills/registry'" in source
        assert "/install" in source
        assert "Skill Registry" in source

    # 方法作用：验证自动化工作台提供任务管理、立即执行和站内通知视图。
    # Args: self - pytest 测试类实例。
    # Returns: 无返回值，断言失败时由 pytest 报告。
    def test_automation_page_exposes_schedules_and_notifications(self) -> None:
        """P3 主动洞察和定时报告必须有可操作的产品入口。"""
        # Arrange
        app = Path("frontend/src/App.tsx").read_text(encoding="utf-8")
        page = Path("frontend/src/pages/AutomationPage.tsx").read_text(encoding="utf-8")

        # Act / Assert
        assert 'to="/automation"' in app
        assert 'path="/automation"' in app
        assert "创建自动化任务" in page
        assert "/automation/schedules" in page
        assert "/automation/notifications" in page
        assert "/run" in page
