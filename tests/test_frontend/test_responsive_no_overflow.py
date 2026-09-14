"""页面自适应（窄屏无横向溢出）契约测试。

事实依据：页面容器从「限宽居中」改为「占满」后，窄屏（实测 900/420 视口、真实后端）
暴露出两类溢出——

1. 表格把容器撑破导致页面级横向滚动条：/history 页在 900 视口下
   documentElement.scrollWidth=931 > clientWidth=900，/knowledge 达 942；根因是
   表格列内容的最小宽度合计超过容器宽度，而表格容器没有任何横向滚动能力。
2. 卡片头部不换行撑宽整卡：/skills 在 420 视口下 .app-page 内容宽 632（视口 420），
   根因是 Card extra 里的 <Space> 不换行、按钮行宽度 595 撑破了卡片。

修复策略（与实现一一对应，防止回退）：

- 长文本表格（history/knowledge/memory/mcp/schema/links/报表列表）由 index.css 让
  .ant-table-wrapper 在页面容器内横向滚动，宽屏下表格仍按容器宽度渲染（1680 实测
  表格宽度=容器宽度 1382，无观感变化）。
- 需要固定列吸附或列宽固定的表格统一用 scroll={{ x: 'max-content' }}，替换原先写死的
  数字宽度（写死宽度与容器宽度不匹配时 fixed 列会错位）。
- 窄屏不得再出现写死 scroll 宽度的表格；767 断点不得再裁剪表格容器（overflow: hidden）。

前端没有 JS 测试框架，沿用 tests/test_frontend/ 既有的源码静态断言模式
（tests/test_frontend/test_page_width_unification.py 为范例）；本文件只读取源码文本。
"""

from __future__ import annotations

import logging
import re
from pathlib import Path

logger = logging.getLogger(__name__)

STYLES = Path("frontend/src/index.css").read_text(encoding="utf-8")

# 保留 scroll={{ x: 'max-content' }} 的页面及最少出现次数（防止被误删回写死数字）
MAX_CONTENT_SCROLL_PAGES = {
    "DatasourcePage.tsx": 1,
    "KnowledgePage.tsx": 3,
    "McpPage.tsx": 1,
    "SchemaPage.tsx": 3,
    "MetricsPage.tsx": 2,
    "SkillsPage.tsx": 1,
    "AdminPage.tsx": 8,
    "AutomationPage.tsx": 1,
    "ReportListPage.tsx": 1,
    "LinkManagementPage.tsx": 1,
}

# 长文本表格所在页面：小屏滚动依赖统一容器上的 CSS 规则，根节点必须是统一容器类
CSS_SCROLL_FALLBACK_PAGES = (
    "HistoryPage.tsx",
    "MemoryPage.tsx",
    "SkillsPage.tsx",
    "ReportListPage.tsx",
    "LinkManagementPage.tsx",
)

# 写死 scroll 宽度的写法：scroll={{ x: 1040 }} 之类，修复后必须绝迹
HARDCODED_SCROLL_X = re.compile(r"scroll=\{\{\s*x:\s*\d")
# 列宽自适应的写法，允许空格变体
MAX_CONTENT_SCROLL = re.compile(r"scroll=\{\{\s*x:\s*'max-content'\s*\}\}")


# 方法作用：读取指定菜单页源码文本。
# Args: filename - frontend/src/pages 下的文件名。
# Returns: 文件内容字符串。
def _page_source(filename: str) -> str:
    return Path(f"frontend/src/pages/{filename}").read_text(encoding="utf-8")


# 方法作用：截取指定断点的媒体查询块正文。
# Args: max_width - 断点像素值，如 767。
# Returns: 从标记到顶格右花括号之间的 CSS 文本。
def _media_block(max_width: int) -> str:
    marker = f"@media (max-width: {max_width}px) {{"
    start = STYLES.index(marker)
    end = STYLES.index("\n}", start)
    return STYLES[start:end]


class TestTableScrollStrategy:
    """验证表格的横向滚动策略：不写死宽度、长文本表格有容器滚动兜底。"""

    # 方法作用：验证所有页面都不再写死表格 scroll 的数字宽度。
    # Args: self - 测试实例。
    # Returns: 无返回值，断言失败时由 pytest 报告。
    def test_pages_do_not_hardcode_table_scroll_width(self) -> None:
        """写死宽度与容器宽度不匹配时 fixed 列会错位，必须一律用 max-content 或容器滚动。"""
        logger.debug("test_pages_do_not_hardcode_table_scroll_width 入口")
        # Arrange：收集全部页面源码。
        offenders = []

        # Act：逐个文件扫描写死 scroll 宽度的写法。
        for path in sorted(Path("frontend/src/pages").glob("*.tsx")):
            if HARDCODED_SCROLL_X.search(path.read_text(encoding="utf-8")):
                offenders.append(path.name)

        # Assert：不应有任何文件残留写死宽度。
        assert not offenders, f"仍存在写死 scroll 宽度的页面: {offenders}"
        logger.info("test_pages_do_not_hardcode_table_scroll_width 完成")

    # 方法作用：验证需要固定列吸附的表格保留 max-content 写法。
    # Args: self - 测试实例。
    # Returns: 无返回值，断言失败时由 pytest 报告。
    def test_scrollable_tables_use_max_content(self) -> None:
        """/datasource 等表格的固定列依赖 scroll.x，缺少后 fixed 列会退化为普通列。"""
        logger.debug("test_scrollable_tables_use_max_content 入口")
        # Arrange / Act / Assert：逐个页面核对最少出现次数。
        for filename, minimum in MAX_CONTENT_SCROLL_PAGES.items():
            source = _page_source(filename)
            found = len(MAX_CONTENT_SCROLL.findall(source))
            assert found >= minimum, f"{filename} 的 scroll={{ x: 'max-content' }} 少于 {minimum} 处（实际 {found}）"
        logger.info("test_scrollable_tables_use_max_content 完成")

    # 方法作用：验证 index.css 让页面容器内的表格可横向滚动。
    # Args: self - 测试实例。
    # Returns: 无返回值，断言失败时由 pytest 报告。
    def test_css_lets_table_wrapper_scroll_inside_page(self) -> None:
        """长文本表格不再靠页面级滚动条查看右侧列，滚动必须发生在表格容器内。"""
        logger.debug("test_css_lets_table_wrapper_scroll_inside_page 入口")
        # Arrange：抓取统一容器下的表格滚动规则。
        rule = re.search(r"\.app-page \.ant-table-wrapper,\s*\n\.report-workbench-page \.ant-table-wrapper \{([^}]*)\}", STYLES)

        # Act：取出规则正文。
        body = rule.group(1) if rule else ""

        # Assert：必须是横向自动滚动。
        assert rule, "index.css 缺少页面容器内表格的横向滚动规则"
        assert "overflow-x: auto" in body
        logger.info("test_css_lets_table_wrapper_scroll_inside_page 完成")


class TestNarrowScreenLayout:
    """验证窄屏布局：表格容器不被裁剪、卡片头部可换行、阅读宽度不被误改。"""

    # 方法作用：验证 767 断点不再裁剪表格容器。
    # Args: self - 测试实例。
    # Returns: 无返回值，断言失败时由 pytest 报告。
    def test_mobile_breakpoint_does_not_clip_table_wrapper(self) -> None:
        """断点里若对表格容器 overflow: hidden，窄屏表格将无法滚动、右侧列不可达。"""
        logger.debug("test_mobile_breakpoint_does_not_clip_table_wrapper 入口")
        # Arrange：取出 767 断点块。
        mobile = _media_block(767)

        # Act：定位断点里的表格容器规则。
        rule = re.search(r"\.ant-table-wrapper \{([^}]*)\}", mobile)

        # Assert：断点仍存在且不再裁剪（保留 max-width 约束即可）。
        assert mobile, "index.css 缺少 767 断点"
        assert rule, "767 断点缺少 .ant-table-wrapper 规则"
        assert "overflow: hidden" not in rule.group(1)
        assert "max-width: 100%" in rule.group(1)
        logger.info("test_mobile_breakpoint_does_not_clip_table_wrapper 完成")

    # 方法作用：验证长文本表格所在页面使用统一容器（CSS 滚动规则才能命中）。
    # Args: self - 测试实例。
    # Returns: 无返回值，断言失败时由 pytest 报告。
    def test_long_text_tables_live_in_shared_page_container(self) -> None:
        """CSS 选择器以 .app-page 为前缀，页面换掉容器类后兜底滚动会静默失效。"""
        logger.debug("test_long_text_tables_live_in_shared_page_container 入口")
        # Arrange / Act / Assert：逐个页面核对根容器。
        for filename in CSS_SCROLL_FALLBACK_PAGES:
            source = _page_source(filename)
            assert 'className="app-page"' in source, f"{filename} 不再使用 .app-page，表格容器滚动兜底会失效"
        logger.info("test_long_text_tables_live_in_shared_page_container 完成")

    # 方法作用：验证 Skills 页卡片头部工具条允许换行。
    # Args: self - 测试实例。
    # Returns: 无返回值，断言失败时由 pytest 报告。
    def test_skills_card_extra_wraps_on_narrow_screen(self) -> None:
        """420 视口下 extra 里的按钮行原本宽 595，不换行会把卡片和页面一起撑破。"""
        logger.debug("test_skills_card_extra_wraps_on_narrow_screen 入口")
        # Arrange / Act：读取技能页源码。
        source = _page_source("SkillsPage.tsx")

        # Assert：Card extra 的工具条 Space 必须允许换行。
        card_head = source[source.index('title={t("Skills 管理")}'):]
        assert "extra={" in card_head
        assert "<Space wrap>" in card_head.split("onClick")[0]
        logger.info("test_skills_card_extra_wraps_on_narrow_screen 完成")

    # 方法作用：验证对话分析页保留刻意设定的 960 阅读宽度。
    # Args: self - 测试实例。
    # Returns: 无返回值，断言失败时由 pytest 报告。
    def test_chat_page_reading_width_untouched(self) -> None:
        """对话列 960px 是刻意阅读宽度（用户确认该页无问题），响应式改造不得波及。"""
        logger.debug("test_chat_page_reading_width_untouched 入口")
        # Arrange / Act：读取对话页源码。
        source = _page_source("ChatPage.tsx")

        # Assert：阅读宽度保留、不套用统一容器。
        assert "maxWidth: 960" in source
        assert 'className="app-page"' not in source
        logger.info("test_chat_page_reading_width_untouched 完成")
