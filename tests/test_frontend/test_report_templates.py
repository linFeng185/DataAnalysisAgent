"""报表视觉模板体系前端契约测试，覆盖 23.15。

前端没有 JS 测试框架，沿用仓库既有的源码静态断言模式。
"""

import re
from pathlib import Path


class TestTemplateRegistry:
    """验证 23.15：模板注册表与解析回退。"""

    # 方法作用：验证四套模板 id 唯一且包含默认模板。
    # Args: self - 测试实例。
    # Returns: 无返回值，断言失败时由 pytest 报告。
    def test_registry_has_four_unique_templates(self) -> None:
        """默认模板必须存在，否则回退没有落点。"""
        # Arrange
        source = Path("frontend/src/report/templates.ts").read_text(encoding="utf-8")

        # Act
        ids = re.findall(r"^\s{4}id: '([a-z0-9_-]+)'", source, re.MULTILINE)

        # Assert
        assert len(ids) == 4
        assert len(set(ids)) == 4
        assert set(ids) == {"insight", "ink", "paper", "business"}

    # 方法作用：验证解析函数对未知 id 有回退分支而不是抛错。
    # Args: self - 测试实例。
    # Returns: 无返回值，断言失败时由 pytest 报告。
    def test_resolve_template_falls_back(self) -> None:
        """分享页面对外部用户，未知模板必须静默回退。"""
        # Arrange
        source = Path("frontend/src/report/templates.ts").read_text(encoding="utf-8")

        # Act / Assert
        assert "fallback: found === undefined" in source
        # token 用继承补齐，保证任何模板都不可能缺键
        assert "...insightTokens, ...template.tokens" in source

    # 方法作用：验证模板 token 覆盖设计要求的全部字段。
    # Args: self - 测试实例。
    # Returns: 无返回值，断言失败时由 pytest 报告。
    def test_tokens_cover_required_fields(self) -> None:
        """缺字段会让样式静默失效。"""
        # Arrange
        source = Path("frontend/src/report/templates.ts").read_text(encoding="utf-8")

        # Act / Assert
        for field in (
            "accent", "accentSoft", "bg", "surface", "border", "textPrimary", "textSecondary",
            "fontFamily", "headingFontFamily", "radius", "shadow", "spaceUnit", "density", "chartPalette",
        ):
            assert f"{field}:" in source


class TestStyleVariables:
    """验证 23.15：报表样式改由 CSS 变量驱动，不再硬编码色值。"""

    # 方法作用：验证报表相关规则使用变量且不再残留硬编码主色。
    # Args: self - 测试实例。
    # Returns: 无返回值，断言失败时由 pytest 报告。
    def test_report_rules_use_variables(self) -> None:
        """按选择器分块切片后检查，避免误伤同文件其他模块的硬编码色值。"""
        # Arrange
        styles = Path("frontend/src/index.css").read_text(encoding="utf-8")

        # Act：抽出以 .report- 开头的规则块
        blocks = re.findall(r"\.report-[^{]*\{[^}]*\}", styles)

        # Assert
        assert blocks
        assert any("var(--report-" in block for block in blocks)
        # 报表块内不得再出现旧版硬编码主色
        assert not any("#1677ff" in block for block in blocks)

    # 方法作用：验证模板作用域不写入全局根变量。
    # Args: self - 测试实例。
    # Returns: 无返回值，断言失败时由 pytest 报告。
    def test_template_scope_does_not_touch_root(self) -> None:
        """模板不得污染报表之外的界面。"""
        # Arrange
        scope = Path("frontend/src/components/report/TemplateScope.tsx").read_text(encoding="utf-8")

        # Act / Assert
        assert ":root" not in scope
        assert "documentElement" not in scope
        assert "components:" in scope


class TestChartBaseline:
    """验证 23.15：图表基线注入色板且不覆盖后端意图。"""

    # 方法作用：验证注入色板并保留后端已设的键。
    # Args: self - 测试实例。
    # Returns: 无返回值，断言失败时由 pytest 报告。
    def test_injects_palette_and_merges_missing(self) -> None:
        """后端白名单不允许下发 color，配色只能在这里统一注入。"""
        # Arrange
        source = Path("frontend/src/components/report/chartBase.ts").read_text(encoding="utf-8")

        # Act / Assert
        assert "chartPalette" in source
        assert "mergeMissing" in source
        # 深色模板必须强制覆盖浅色假设下生成的颜色字段
        assert "force" in source
        assert "isDarkSurface" in source


class TestPageWiring:
    """验证 23.15：三个页面的模板接入。"""

    # 方法作用：验证阅读端与分享页都有切换控件。
    # Args: self - 测试实例。
    # Returns: 无返回值，断言失败时由 pytest 报告。
    def test_viewer_and_shared_page_have_switcher(self) -> None:
        """两个页面共用切换逻辑与控件。"""
        # Arrange
        viewer = Path("frontend/src/pages/ReportViewerPage.tsx").read_text(encoding="utf-8")
        shared = Path("frontend/src/pages/SharedReportPage.tsx").read_text(encoding="utf-8")

        # Act / Assert
        for page in (viewer, shared):
            assert "TemplateSwitcher" in page
            assert "useReportTemplate" in page
            assert "templateId={templateId}" in page

    # 方法作用：验证渲染器消费文档记录的模板，模板作用域统一由页面外壳持有。
    # Args: self - 测试实例。
    # Returns: 无返回值，断言失败时由 pytest 报告。
    def test_renderer_consumes_document_theme(self) -> None:
        """渲染器必须读 document.theme，而不是写死一套外观。"""
        # Arrange
        renderer = Path("frontend/src/components/report/ReportDocumentRenderer.tsx").read_text(encoding="utf-8")
        shell = Path("frontend/src/components/report/ReportPageShell.tsx").read_text(encoding="utf-8")

        # Act / Assert
        assert "useReportTemplate" in renderer
        assert "resolveTemplate" in renderer
        # 渲染器不再自带作用域，避免与页面外壳嵌套成两层主题
        assert "<TemplateScope" not in renderer
        assert "<TemplateScope" in shell
        assert "withChartBaseline" in renderer
        # 图表高度由模板密度决定，不再硬编码 320
        assert "densityMetrics" in renderer
        assert "height: 320" not in renderer

    # 方法作用：验证生成面板可选模板并随创建任务提交。
    # Args: self - 测试实例。
    # Returns: 无返回值，断言失败时由 pytest 报告。
    def test_workbench_passes_template_id(self) -> None:
        """模板不进计划哈希，因此切换它不触发重新预览。"""
        # Arrange
        page = Path("frontend/src/pages/ReportWorkbenchPage.tsx").read_text(encoding="utf-8")
        api = Path("frontend/src/api/reportWorkbench.ts").read_text(encoding="utf-8")

        # Act / Assert
        assert "reportTemplates" in page
        assert "templateId" in page
        assert "template_id" in api


class TestChartLegendLayout:
    """验证图表基线不会造出压住数据的图例，并为已声明的图例预留网格空间。

    事实依据（2026-09-14 浏览器实测）：后端下发的报表 option 只有
    `{xAxis, yAxis, series, tooltip}`，**没有 legend**；基线原先用
    `isPlainObject(legend) ? legend : {}` 补样式，等于给每个图表凭空加了一个 legend，
    ECharts 把它渲染在 `top:null / bottom:15`，而 `grid.bottom` 只有 8，
    于是图例压在柱状图与横轴标签上（用户报的「柱状图的按钮和数据重叠」）。
    """

    # 方法作用：验证基线不再把缺失或 false 的 legend 变成空对象。
    # Args: self - 测试实例。
    # Returns: 无返回值，断言失败时由 pytest 报告。
    def test_baseline_never_invents_a_legend(self) -> None:
        """凭空补出 legend 键就等于强行让 ECharts 画图例。"""
        # Arrange
        source = Path("frontend/src/components/report/chartBase.ts").read_text(encoding="utf-8")

        # Act / Assert
        assert "isPlainObject(merged.legend) ? merged.legend : {}" not in source
        assert "isPlainObject(merged.legend) && merged.legend.show !== false" in source

    # 方法作用：验证为图例预留网格空间的能力存在。
    # Args: self - 测试实例。
    # Returns: 无返回值，断言失败时由 pytest 报告。
    def test_legend_space_is_reserved_in_grid(self) -> None:
        """ECharts 不会自动为图例让位，必须自己算边距。"""
        # Arrange
        source = Path("frontend/src/components/report/chartBase.ts").read_text(encoding="utf-8")

        # Act / Assert
        assert "reserveLegendSpace" in source
        assert "LEGEND_BAND_PX" in source
        # 横向图例按锚定方向预留：贴底改 grid.bottom、贴顶改 grid.top；纵向占横向空间
        assert "grid.bottom = Math.max(" in source
        assert "grid.top = Math.max(" in source
        assert "grid.right = Math.max(" in source

    # 方法作用：验证预留值足以容纳一行图例。
    # Args: self - 测试实例。
    # Returns: 无返回值，断言失败时由 pytest 报告。
    def test_legend_band_covers_one_line(self) -> None:
        """一行图例约 20px，加外边距与留白不应少于 40px。"""
        # Arrange
        source = Path("frontend/src/components/report/chartBase.ts").read_text(encoding="utf-8")
        match = re.search(r"LEGEND_BAND_PX\s*=\s*(\d+)", source)

        # Act / Assert
        assert match, "未找到 LEGEND_BAND_PX 定义"
        assert int(match.group(1)) >= 40

    # 方法作用：验证没有 grid 的图表不会被塞进一个 grid。
    # Args: self - 测试实例。
    # Returns: 无返回值，断言失败时由 pytest 报告。
    def test_reserve_skips_when_grid_absent(self) -> None:
        """饼图等无坐标轴图表不该被强加网格。"""
        # Arrange
        source = Path("frontend/src/components/report/chartBase.ts").read_text(encoding="utf-8")
        start = source.index("function reserveLegendSpace")
        body = source[start:source.index("\n}", start)]

        # Act / Assert
        assert "isPlainObject(grid)" in body
        assert "return;" in body
