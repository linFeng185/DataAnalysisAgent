"""报表呈现层契约：hero、图文配对、图表基线与无框区段。"""

from pathlib import Path


class TestReportPresentation:
    """覆盖功能 23.23～23.25 的前端呈现契约。"""

    # 方法作用：验证报表块不再由 antd Card 逐块包裹。
    # Args: self - pytest 测试类实例。
    # Returns: 无返回值，断言失败时由 pytest 报告。
    def test_no_card_wrapper_in_report_blocks(self) -> None:
        """正文必须是无框区段，不能退回同质化卡片墙。"""
        renderer = Path("frontend/src/components/report/ReportDocumentRenderer.tsx").read_text(encoding="utf-8")

        assert "Card" not in renderer
        assert "report-block-content" in renderer

    # 方法作用：验证配对组件显式区分热力图与普通图表。
    # Args: self - pytest 测试类实例。
    # Returns: 无返回值，断言失败时由 pytest 报告。
    def test_pair_layout_branches_on_heatmap(self) -> None:
        """热力图必须满宽，普通图表按 62/38 横向配对。"""
        source = Path("frontend/src/components/report/ReportBlockPair.tsx").read_text(encoding="utf-8")

        assert "heatmap" in source
        assert "report-block-pair-stacked" in source
        assert "report-block-pair-split" in source

    # 方法作用：验证 hero 提升 KPI 且由具名常量限制数量。
    # Args: self - pytest 测试类实例。
    # Returns: 无返回值，断言失败时由 pytest 报告。
    def test_hero_promotes_kpi_with_limit_constant(self) -> None:
        """前四个 KPI 进入 hero，并从正文移除以避免重复。"""
        hero = Path("frontend/src/components/report/ReportHero.tsx").read_text(encoding="utf-8")
        renderer = Path("frontend/src/components/report/ReportDocumentRenderer.tsx").read_text(encoding="utf-8")

        assert "HERO_KPI_LIMIT" in hero
        assert "ReportHero" in renderer
        assert "heroKpis" in renderer

    # 方法作用：验证热力图色阶和 axisPointer 由图表基线注入。
    # Args: self - pytest 测试类实例。
    # Returns: 无返回值，断言失败时由 pytest 报告。
    def test_chart_base_injects_sequential_palette(self) -> None:
        """visualMap 支持对象和数组，且轴触发图提供十字准线。"""
        source = Path("frontend/src/components/report/chartBase.ts").read_text(encoding="utf-8")

        assert "sequentialPalette" in source
        assert "visualMap" in source
        assert "axisPointer" in source

    # 方法作用：验证十字准线只在按轴触发的提示上注入，柱状图不被整批系列淹没。
    # Args: self - pytest 测试类实例。
    # Returns: 无返回值，断言失败时由 pytest 报告。
    def test_chart_base_scopes_axis_pointer_to_axis_trigger(self) -> None:
        """实机取证：bar 的 trigger 是 axis、基线又补 cross，悬停单柱会列出同轴全部系列。"""
        # Arrange
        source = Path("frontend/src/components/report/chartBase.ts").read_text(encoding="utf-8")

        # Act：十字准线必须挂在按 trigger 判断的具名函数里，不能无条件合并
        # Assert
        assert "withTooltipBaseline" in source
        assert "trigger === 'axis'" in source
        assert "axisPointer: { type: 'cross' }" not in source

    # 方法作用：验证表格数字使用等宽字形，而 hero/KPI 大数字保持比例字形。
    # Args: self - pytest 测试类实例。
    # Returns: 无返回值，断言失败时由 pytest 报告。
    def test_tabular_nums_only_on_aligned_data(self) -> None:
        """大数字不应使用 tabular-nums，只有需要纵向对齐的数据区使用。"""
        styles = Path("frontend/src/index.css").read_text(encoding="utf-8")

        assert ".report-block-table" in styles and "tabular-nums" in styles
        assert ".report-hero-kpi-value { font-variant-numeric: normal;" in styles

    # 方法作用：验证模板注册表包含 hero 与连续色阶，并替换 ink 旧分类色板。
    # Args: self - pytest 测试类实例。
    # Returns: 无返回值，断言失败时由 pytest 报告。
    def test_templates_include_new_tokens_and_ink_palette(self) -> None:
        """四套模板经继承后都必须具备连续色阶与 hero 底色。"""
        source = Path("frontend/src/report/templates.ts").read_text(encoding="utf-8")

        assert "sequentialPalette" in source
        assert "heroBg" in source
        assert "'#3987e5', '#d95926', '#199e70', '#c98500'" in source

    # 方法作用：验证后端分组系列契约在前端纯函数模块中保留 type 字段，且渲染器已接入该契约。
    # Args: self - pytest 测试类实例。
    # Returns: 无返回值，断言失败时由 pytest 报告。
    def test_group_series_contract_requires_type(self) -> None:
        """前端替换 series 时必须携带 ECharts 子类型，避免整组被静默丢弃。

        分组归一逻辑已从渲染器提取到 chartGroups.ts（真实旧快照里分组 series 会缺 type
        或被硬编码成错误类型）。契约的**归属文件**随之迁移，因此这里同时断言
        「契约声明仍在」与「渲染器消费同一份归一结果」，覆盖范围不缩小。
        """
        # Arrange
        module = Path("frontend/src/components/report/chartGroups.ts").read_text(encoding="utf-8")
        renderer = Path("frontend/src/components/report/ReportDocumentRenderer.tsx").read_text(encoding="utf-8")

        # Act + Assert：契约声明 + 渲染器接线
        assert "type: string" in module
        assert "resolveChartGroups(" in renderer

    # 方法作用：验证图表基线存在**可执行**的行为测试入口，本文件只做入口存在性检查。
    # Args: self - pytest 测试类实例。
    # Returns: 无返回值，断言失败时由 pytest 报告。
    def test_chart_base_has_executable_behaviour_test(self) -> None:
        """旧快照柱图 tooltip 兼容必须由真正调用 withChartBaseline 的用例兜住，源码字符串断言不算数。

        运行方式：cd frontend && npm run test:chart（Node 24 原生 TS 类型擦除 + node:test，无新依赖）。
        """
        # Arrange
        package = Path("frontend/package.json").read_text(encoding="utf-8")
        behaviour = Path("frontend/tests/chartBase.test.mjs").read_text(encoding="utf-8")

        # Act + Assert：入口存在，且断言对象是返回值而不是源码文本
        assert '"test:chart": "node --test tests/chartBase.test.mjs"' in package
        assert "withChartBaseline" in behaviour
        assert "axisPointer" in behaviour
        assert "result.tooltip.trigger" in behaviour

    # 方法作用：验证报表内容文本归一存在**可执行**的行为测试入口，并已接入渲染器。
    # Args: self - pytest 测试类实例。
    # Returns: 无返回值，断言失败时由 pytest 报告。
    def test_report_content_has_executable_behaviour_test(self) -> None:
        """quality 与 relationship 的数组展示必须由真正调用纯函数的用例兜住。

        实机取证：分享页 22 个 quality 块的 content.limitations 是 string[]，经 displayText
        兜底后浏览器显示为 `[限制一,限制二]`。本用例只做「行为测试入口存在 + 渲染器已接线」
        的轻量检查，语义断言由 Node 用例承担，源码字符串断言不算数。

        运行方式：cd frontend && npm run test:report-content（Node 24 原生 TS 类型擦除 + node:test，无新依赖）。
        """
        # Arrange
        package = Path("frontend/package.json").read_text(encoding="utf-8")
        behaviour = Path("frontend/tests/reportContent.test.mjs").read_text(encoding="utf-8")
        renderer = Path("frontend/src/components/report/ReportDocumentRenderer.tsx").read_text(encoding="utf-8")

        # Act + Assert：入口存在，断言对象是返回值而不是源码文本
        assert '"test:report-content": "node --test tests/reportContent.test.mjs"' in package
        assert "toTextList(" in behaviour
        assert "joinTextList(" in behaviour
        assert "assert.deepEqual(toTextList(" in behaviour

        # Assert：渲染器不得再把这两个数组字段交给 JSON 兜底的 displayText
        assert "displayText(content.join_keys)" not in renderer
        assert "displayText(content.limitations)" not in renderer
        assert "joinTextList(content.join_keys, '、')" in renderer
        assert "toTextList(content.limitations)" in renderer

    # 方法作用：验证图表分组切换被包进自己的可滚动容器，不再把整页撑宽。
    # Args: self - pytest 测试类实例。
    # Returns: 无返回值，断言失败时由 pytest 报告。
    def test_chart_group_switch_scrolls_inside_its_container(self) -> None:
        """实机 390px：scrollWidth 646 > clientWidth 390，最大溢出源是宽 636 的 Segmented。

        修复要求滚动发生在控件容器内，且控件不得被隐藏或截断（否则切换项不可达）。
        """
        # Arrange
        renderer = Path("frontend/src/components/report/ReportDocumentRenderer.tsx").read_text(encoding="utf-8")
        styles = Path("frontend/src/index.css").read_text(encoding="utf-8")
        rule_start = styles.index(".report-chart-group-switch {")
        rule_body = styles[rule_start:styles.index("}", rule_start)]

        # Act + Assert：渲染器有明确 wrapper，CSS 让容器自己滚动
        assert '<div className="report-chart-group-switch">' in renderer
        assert "max-width: 100%" in rule_body
        assert "overflow-x: auto" in rule_body
        assert "overflow: hidden" not in rule_body
        # 控件本身必须还在，且保持内容宽度（被压缩到容器宽就无从滚动）
        assert ".report-chart-group-switch > .ant-segmented { width: max-content; }" in styles

    # 方法作用：验证分组切换的布局契约存在**可执行**测试入口，本用例只做轻量检查。
    # Args: self - pytest 测试类实例。
    # Returns: 无返回值，断言失败时由 pytest 报告。
    def test_group_switch_has_executable_layout_test(self) -> None:
        """源码字符串断言不算数：CSS 规则必须由真正解析声明块的 Node 用例兜住。

        运行方式：cd frontend && npm run test:report-layout（Node 24 + node:test，无新依赖）。
        """
        # Arrange
        package = Path("frontend/package.json").read_text(encoding="utf-8")
        behaviour = Path("frontend/tests/reportGroupSwitch.test.mjs").read_text(encoding="utf-8")

        # Act + Assert：入口存在，且断言对象是解析出的声明值而不是裸文本包含
        assert '"test:report-layout": "node --test tests/reportGroupSwitch.test.mjs"' in package
        assert "ruleOf('.report-chart-group-switch')" in behaviour
        assert "rule['overflow-x']" in behaviour
        assert "assert.equal(rule['max-width'], '100%'" in behaviour

    # 方法作用：验证图表分组选项归一存在**可执行**的行为测试入口，且渲染器只消费归一结果。
    # Args: self - pytest 测试类实例。
    # Returns: 无返回值，断言失败时由 pytest 报告。
    def test_chart_groups_has_executable_behaviour_test(self) -> None:
        """分组过滤/归一必须由真正调用 resolveChartGroups 的用例兜住，源码字符串断言不算数。

        实机取证：分享页持久化 document 是 version 3（60 张图，旧逻辑生成），页面版本按
        SPEC 不可变、后端不会回写。旧渲染器只检查「当前选项的 series 有没有 type」，于是
        分类轴上的「环比(%)」派生项、特殊图的历史分组、非法分组选项都会直接暴露给用户。
        本用例只做「行为测试入口存在 + 渲染器已接线」的轻量检查，语义断言由 Node 用例承担。

        运行方式：cd frontend && npm run test:report-groups（Node 24 原生 TS 类型擦除 + node:test，无新依赖）。
        """
        # Arrange
        package = Path("frontend/package.json").read_text(encoding="utf-8")
        behaviour = Path("frontend/tests/chartGroups.test.mjs").read_text(encoding="utf-8")
        renderer = Path("frontend/src/components/report/ReportDocumentRenderer.tsx").read_text(encoding="utf-8")

        # Act + Assert：入口存在，且断言对象是返回值而不是源码文本
        assert '"test:report-groups": "node --test tests/chartGroups.test.mjs"' in package
        assert "resolveChartGroups(" in behaviour
        assert "assert.deepEqual(result.groups" in behaviour
        assert "result.axisIssue" in behaviour
        assert "resolved.axisIssue" in renderer

        # Assert：渲染器的 groups / initialGroup / Segmented / 实际 series 都只来自归一结果
        assert "resolveChartGroups(block.content, block.echarts_option" in renderer
        assert "resolved.groups.length > 1" in renderer
        assert "value={resolved.activeGroup}" in renderer
        assert "groupOptions" not in renderer

    # 方法作用：验证空标签类目轴命中时显示可翻译的版本重建提示，而不是空标签 canvas。
    # Args: self - pytest 测试类实例。
    # Returns: 无返回值，断言失败时由 pytest 报告。
    def test_stale_category_axis_shows_rebuild_notice(self) -> None:
        """chart_price_promotion_1/2 与 chart_procurement_inventory_1/2 的类目轴只剩 ['']。

        旧快照已把数据聚合丢失，前端不重建这些类目，只如实告知必须重新生成报表版本。
        """
        # Arrange
        renderer = Path("frontend/src/components/report/ReportDocumentRenderer.tsx").read_text(encoding="utf-8")
        dictionary = Path("frontend/src/i18n/en.ts").read_text(encoding="utf-8")
        notice = "图表分类轴缺少可用标签，请重新生成报表版本"

        # Act + Assert：文案经 t(...) 出口，英文词典同步登记
        assert f't("{notice}")' in renderer
        assert f'"{notice}":' in dictionary

    # 方法作用：验证分组归一模块保持纯函数形态，可被 Node 直接 import 执行。
    # Args: self - pytest 测试类实例。
    # Returns: 无返回值，断言失败时由 pytest 报告。
    def test_chart_groups_module_stays_pure(self) -> None:
        """纯函数模块不得依赖 React/DOM，也不得打印 console 日志（Node 用例要能直接执行）。"""
        # Arrange
        module = Path("frontend/src/components/report/chartGroups.ts").read_text(encoding="utf-8")

        # Act + Assert
        assert "export function resolveChartGroups(" in module
        assert "from 'react'" not in module
        assert "window." not in module
        assert "console." not in module
