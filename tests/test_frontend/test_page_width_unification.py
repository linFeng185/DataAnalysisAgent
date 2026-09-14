"""菜单页面宽度统一契约测试：所有数据展示页占满可用宽度。

事实依据：用户反馈「除对话分析和指标目录外，其他菜单页面只有中间有内容，
两边都是空白」。根因是各页根容器写死内联 maxWidth 并 margin auto 居中，
宽屏（实测 1680 视口、可用内容区 1480px）下两侧留出 100~240px 空白。
本轮统一改为 index.css 的 .app-page 容器，并去掉
.report-workbench-page / .report-page-body 的 1440 上限。

前端没有 JS 测试框架，沿用 tests/test_frontend/ 既有的源码静态断言模式
（tests/test_frontend/test_report_templates.py 为范例）；本文件只读取源码文本，
不读写任何数据文件。
"""

from __future__ import annotations

import re
from pathlib import Path

STYLES = Path("frontend/src/index.css").read_text(encoding="utf-8")

# 需要统一占满宽度的菜单页（基准页 MetricsPage 也在内，保证所有页面同源）
MENU_PAGES = (
    "SchemaPage.tsx",
    "DatasourcePage.tsx",
    "HistoryPage.tsx",
    "KnowledgePage.tsx",
    "MemoryPage.tsx",
    "SkillsPage.tsx",
    "McpPage.tsx",
    "AdminPage.tsx",
    "NotificationsPage.tsx",
    "MetricsPage.tsx",
)

# 逐文件列出旧根容器写死的宽度上限，改后必须消失（避免只测一个页面漏掉其余）
LEGACY_ROOT_WIDTH = {
    "SchemaPage.tsx": "maxWidth: 1000",
    "DatasourcePage.tsx": "maxWidth: 1120",
    "HistoryPage.tsx": "maxWidth: 1000",
    "KnowledgePage.tsx": "maxWidth: 1000",
    "MemoryPage.tsx": "maxWidth: 1000",
    "SkillsPage.tsx": "maxWidth: 1000",
    "McpPage.tsx": "maxWidth",
    "AdminPage.tsx": "maxWidth: 1280",
    "NotificationsPage.tsx": "maxWidth: 1080",
    "MetricsPage.tsx": "style={{ padding: 24 }}",
}

# 根容器「内联 maxWidth + margin auto 居中」的写法特征，覆盖带不带空格两种风格
ROOT_CENTER_STYLE = re.compile(r"maxWidth\s*:\s*\d+[^\n]*margin\s*:\s*[\"']0 auto[\"']")


# 方法作用：读取指定菜单页源码文本。
# Args: filename - frontend/src/pages 下的文件名。
# Returns: 文件内容字符串。
def _page_source(filename: str) -> str:
    return Path(f"frontend/src/pages/{filename}").read_text(encoding="utf-8")


# 方法作用：截取指定断点的媒体查询块正文。
# Args: max_width - 断点像素值，如 1100。
# Returns: 从标记到顶格右花括号之间的 CSS 文本。
def _media_block(max_width: int) -> str:
    marker = f"@media (max-width: {max_width}px) {{"
    start = STYLES.index(marker)
    end = STYLES.index("\n}", start)
    return STYLES[start:end]


class TestAppPageContainer:
    """验证 index.css 中的统一页面容器定义。"""

    # 方法作用：验证 .app-page 具备占满宽度的三要素。
    # Args: self - 测试实例。
    # Returns: 无返回值，断言失败时由 pytest 报告。
    def test_css_defines_app_page_full_width(self) -> None:
        """.app-page 必须 width:100% + min-width:0 + padding:24px，对齐指标目录观感。"""
        # Arrange
        rule = re.search(r"\.app-page \{([^}]*)\}", STYLES)

        # Act
        body = rule.group(1) if rule else ""

        # Assert
        assert rule, "index.css 缺少 .app-page 规则"
        assert "width: 100%" in body
        assert "min-width: 0" in body
        assert "padding: 24px" in body
        assert "box-sizing: border-box" in body

    # 方法作用：验证两个既有断点都收窄了 .app-page 内边距，且沿用既有断点数值。
    # Args: self - 测试实例。
    # Returns: 无返回值，断言失败时由 pytest 报告。
    def test_css_app_page_padding_shrinks_on_narrow_screens(self) -> None:
        """1100px 收 16px、767px 收 12px，窄屏不因内边距产生横向滚动。"""
        # Arrange
        tablet = _media_block(1100)
        mobile = _media_block(767)

        # Act
        tablet_padding = re.search(r"\.app-page \{([^}]*)\}", tablet)
        mobile_padding = re.search(r"\.app-page \{([^}]*)\}", mobile)

        # Assert
        assert tablet_padding and "padding: 16px" in tablet_padding.group(1)
        assert mobile_padding and "padding: 12px" in mobile_padding.group(1)
        # 断点嵌套下 767 在 1100 之后出现，后者生效，确保越窄内边距越小
        assert STYLES.index("@media (max-width: 1100px)") < STYLES.index("@media (max-width: 767px)")

    # 方法作用：验证报表页容器与菜单页同源，不再写死 1440 上限。
    # Args: self - 测试实例。
    # Returns: 无返回值，断言失败时由 pytest 报告。
    def test_css_report_containers_drop_1440_cap(self) -> None:
        """.report-workbench-page 与 .report-page-body 占满宽度，保留各自动态内边距。"""
        # Arrange
        workbench = re.search(r"\.report-workbench-page \{([^}]*)\}", STYLES)
        body = re.search(r"\.report-page-body \{([^}]*)\}", STYLES)

        # Act / Assert
        assert workbench and "width: 100%" in workbench.group(1)
        assert "padding: 24px" in workbench.group(1)
        assert body and "width: 100%" in body.group(1)
        # 正文内边距仍走模板空间变量，不被统一容器改写
        assert "var(--report-space-unit" in body.group(1)
        assert "1440" not in STYLES


class TestMenuPagesUseSharedContainer:
    """验证每个菜单页根容器都换成了 .app-page。"""

    # 方法作用：逐文件验证根容器挂上 .app-page 且旧写死宽度已清除。
    # Args: self - 测试实例。
    # Returns: 无返回值，断言失败时由 pytest 报告。
    def test_each_menu_page_uses_app_page_container(self) -> None:
        """任一页面漏改就会继续在宽屏留白，必须逐文件断言。"""
        # Arrange / Act / Assert
        for filename in MENU_PAGES:
            source = _page_source(filename)
            assert 'className="app-page"' in source, f"{filename} 未使用 .app-page 根容器"
            assert not ROOT_CENTER_STYLE.search(source), f"{filename} 仍存在 maxWidth 居中根容器"
            legacy = LEGACY_ROOT_WIDTH.get(filename)
            if legacy:
                assert legacy not in source, f"{filename} 仍残留旧宽度上限 {legacy}"

    # 方法作用：验证指标目录（基准页）也归一到同一个容器类，不再自带内联内边距。
    # Args: self - 测试实例。
    # Returns: 无返回值，断言失败时由 pytest 报告。
    def test_metrics_page_baseline_uses_app_page(self) -> None:
        """基准页是观感来源，改成同源容器后各页内边距才一致。"""
        # Arrange
        source = _page_source("MetricsPage.tsx")

        # Act / Assert
        assert 'return <div className="app-page">' in source
        assert "style={{ padding: 24 }}" not in source

    # 方法作用：验证对话分析页保持刻意设定的阅读宽度，不被误改。
    # Args: self - 测试实例。
    # Returns: 无返回值，断言失败时由 pytest 报告。
    def test_chat_page_keeps_reading_width(self) -> None:
        """对话列 960px 是刻意阅读宽度，用户明确说该页没有问题。"""
        # Arrange
        source = _page_source("ChatPage.tsx")

        # Act / Assert
        assert "maxWidth: 960" in source
        assert 'className="app-page"' not in source

    # 方法作用：验证通知中心保留自身外框容器，仅内层改用统一容器。
    # Args: self - 测试实例。
    # Returns: 无返回值，断言失败时由 pytest 报告。
    def test_notifications_page_keeps_page_frame(self) -> None:
        """notification-page 外框是既有结构，只替换内层限宽容器。"""
        # Arrange
        source = _page_source("NotificationsPage.tsx")

        # Act / Assert
        assert 'className="notification-page"' in source
        assert 'className="app-page"' in source

    # 方法作用：验证报表工作台其余两个子页也挂上统一容器。
    # Args: self - 测试实例。
    # Returns: 无返回值，断言失败时由 pytest 报告。
    def test_report_workbench_subpages_use_app_page(self) -> None:
        """作业列表与链接管理根节点是裸 Space，原先零内边距贴边，需与创建页同容器。"""
        # Arrange
        pages = ("ReportListPage.tsx", "LinkManagementPage.tsx")

        # Act / Assert
        for filename in pages:
            source = _page_source(filename)
            assert 'className="app-page"' in source, f"{filename} 缺少统一页面容器"
