"""报表工作台与公开查看器前端契约测试。"""

from pathlib import Path


class TestReportWorkbenchFrontend:
    """覆盖功能 23.9：工作台、动态页面、权限守卫和公开查看器。"""

    # 方法作用：验证工作台提供自然语言计划预览、确认创建和运行轮询入口。
    # Args: self - pytest 测试类实例。
    # Returns: 无返回值，断言失败时由 pytest 报告。
    def test_workbench_has_plan_preview_and_job_polling(self) -> None:
        """工作台必须调用报表计划、任务和运行接口，不能退回固定 SQL 表单。"""
        # Arrange
        page = Path("frontend/src/pages/ReportWorkbenchPage.tsx").read_text(encoding="utf-8")
        api = Path("frontend/src/api/reportWorkbench.ts").read_text(encoding="utf-8")

        # Act / Assert
        assert "previewReportPlan" in page
        assert "createReportJob" in page
        assert "fetchReportJob" in page
        assert "fetchReportRuns" in page
        assert "fetchReportRun" in page
        assert "runSteps" in page
        assert "loadShares" in Path("frontend/src/pages/ReportViewerPage.tsx").read_text(encoding="utf-8")
        assert "revokeReportShare" in Path("frontend/src/pages/ReportViewerPage.tsx").read_text(encoding="utf-8")
        assert "setInterval" in page
        assert "REPORT_BASE = '/report-workbench'" in api
        assert "`${REPORT_BASE}/plans`" in api
        assert "`${REPORT_BASE}/jobs`" in api

    # 方法作用：验证动态文档只使用注册块和后端签发数据清单渲染。
    # Args: self - pytest 测试类实例。
    # Returns: 无返回值，断言失败时由 pytest 报告。
    def test_document_renderer_handles_chart_table_and_drilldown(self) -> None:
        """报表渲染器必须支持图表、表格和 Drilldown，且不执行任意 HTML。"""
        # Arrange
        renderer = Path("frontend/src/components/report/ReportDocumentRenderer.tsx").read_text(encoding="utf-8")

        # Act / Assert
        assert "ReportBlockType" in Path("frontend/src/types/report.ts").read_text(encoding="utf-8")
        assert "ReactECharts" in renderer
        assert "dataManifest" in renderer
        assert "onDrilldown" in renderer
        assert "dangerouslySetInnerHTML" not in renderer

    # 方法作用：验证公开查看器支持授权码解锁和服务端快照 Drilldown。
    # Args: self - pytest 测试类实例。
    # Returns: 无返回值，断言失败时由 pytest 报告。
    def test_shared_viewer_uses_short_lived_session(self) -> None:
        """公开页面不能把授权码写入持久化存储，明细请求必须携带短时会话。"""
        # Arrange
        page = Path("frontend/src/pages/SharedReportPage.tsx").read_text(encoding="utf-8")
        api = Path("frontend/src/api/reportWorkbench.ts").read_text(encoding="utf-8")

        # Act / Assert
        assert "unlockSharedReport" in page
        assert "sessionToken" in page
        assert "fetchSharedDrilldown" in page
        assert "X-Report-Share-Session" in api
        assert "localStorage" not in page

    # 方法作用：验证应用路由包含受保护报表入口和无需登录的公开入口。
    # Args: self - pytest 测试类实例。
    # Returns: 无返回值，断言失败时由 pytest 报告。
    def test_app_routes_report_permissions_and_public_share(self) -> None:
        """私有查看器需要能力守卫，公开分享路由不能被登录重定向拦截。"""
        # Arrange
        app = Path("frontend/src/App.tsx").read_text(encoding="utf-8")

        # Act / Assert
        assert "fetchReportPermissions" in app
        assert "ReportRoute" in app
        assert 'path="/report-workbench"' in app
        assert 'path="/reports/:reportId"' in app
        assert 'path="/shared/reports/:token"' in app

    # 方法作用：验证报表权限探针异常时前端按默认拒绝处理，避免故障期间暴露入口。
    # Args: self - pytest 测试类实例。
    # Returns: 无返回值，断言失败时由 pytest 报告。
    def test_report_permission_probe_fails_closed(self) -> None:
        """权限接口失败不能按用户角色放行菜单或路由。"""
        # Arrange
        app = Path("frontend/src/App.tsx").read_text(encoding="utf-8")

        # Act / Assert
        assert "setAllowed(false)" in app
        assert "setReportMenuAllowed(false)" in app
        assert "reportMenuAllowed === true" in app

    # 方法作用：验证报表布局在桌面、平板和移动端使用稳定 Grid 断点。
    # Args: self - pytest 测试类实例。
    # Returns: 无返回值，断言失败时由 pytest 报告。
    def test_report_layout_has_responsive_grid_contract(self) -> None:
        """动态块不能依赖固定单列布局或在窄屏发生横向溢出。"""
        # Arrange
        styles = Path("frontend/src/index.css").read_text(encoding="utf-8")

        # Act / Assert
        assert "report-section-grid" in styles
        assert "--report-desktop-span" in styles
        assert "--report-tablet-span" in styles
        assert "--report-mobile-span" in styles
        assert ".report-workbench-grid" in styles

    # 方法作用：验证运行日志展示真实计数而不是估算值。
    # Args: self - pytest 测试类实例。
    # Returns: 无返回值，断言失败时由 pytest 报告。
    def test_workbench_displays_real_run_counts(self) -> None:
        """页面必须展示 LLM 与 SQL 的真实成败计数，并把预估口径区分开。"""
        # Arrange
        page = Path("frontend/src/pages/ReportWorkbenchPage.tsx").read_text(encoding="utf-8")
        types = Path("frontend/src/types/report.ts").read_text(encoding="utf-8")
        styles = Path("frontend/src/index.css").read_text(encoding="utf-8")

        # Act / Assert
        for field in ("llm_call_count", "llm_failure_count", "sql_query_count", "sql_success_count", "sql_failure_count"):
            assert field in types
        assert "本次运行计数" in page
        assert "llm_failure_count" in page
        assert "sql_success_count" in page
        assert "query_count" in page
        assert ".report-run-stats" in styles

    # 方法作用：验证指标口径来源块渲染结构化条目而不是只显示一段文本。
    # Args: self - pytest 测试类实例。
    # Returns: 无返回值，断言失败时由 pytest 报告。
    def test_renderer_shows_metric_citations(self) -> None:
        """口径溯源需要展示指标名、版本与表达式，供用户核对计算口径。"""
        # Arrange
        renderer = Path("frontend/src/components/report/ReportDocumentRenderer.tsx").read_text(encoding="utf-8")

        # Act / Assert
        assert "case 'source_citation'" in renderer
        assert "content.items" in renderer
        assert "item.expression" in renderer
        # 复用既有的安全文本转换，不引入 HTML 注入面
        assert "dangerouslySetInnerHTML" not in renderer

    # 方法作用：验证深挖发现块渲染计算方法与样本披露。
    # Args: self - pytest 测试类实例。
    # Returns: 无返回值，断言失败时由 pytest 报告。
    def test_renderer_shows_findings_with_method_and_caveats(self) -> None:
        """深挖结论必须连同计算方法与样本局限一起展示。"""
        # Arrange
        renderer = Path("frontend/src/components/report/ReportDocumentRenderer.tsx").read_text(encoding="utf-8")
        types = Path("frontend/src/types/report.ts").read_text(encoding="utf-8")

        # Act / Assert
        assert "case 'findings'" in renderer
        assert "content.method" in renderer
        assert "item.caveats" in renderer
        assert "'findings'" in types
        assert "dangerouslySetInnerHTML" not in renderer

    # 方法作用：验证表格列标题使用后端下发的带备注标题。
    # Args: self - pytest 测试类实例。
    # Returns: 无返回值，断言失败时由 pytest 报告。
    def test_table_columns_use_backend_labels(self) -> None:
        """字段备注要显示在列标题上，同时数据键必须仍是原列名。"""
        # Arrange
        renderer = Path("frontend/src/components/report/ReportDocumentRenderer.tsx").read_text(encoding="utf-8")

        # Act / Assert
        assert "column_labels" in renderer
        # dataIndex 必须保持原列名，否则取不到行数据
        assert "dataIndex: name" in renderer

    # 方法作用：验证关系块展示影响方向与强度，并标注跨源证据。
    # Args: self - pytest 测试类实例。
    # Returns: 无返回值，断言失败时由 pytest 报告。
    def test_relationship_shows_direction_strength_and_cross_source(self) -> None:
        """影响判定要显示方向与强度；跨源结论需有独立标记。"""
        # Arrange
        renderer = Path("frontend/src/components/report/ReportDocumentRenderer.tsx").read_text(encoding="utf-8")

        # Act / Assert
        assert "content.direction" in renderer
        assert "content.strength" in renderer
        assert "content.cross_source" in renderer
        assert "跨源分布级" in renderer

    # 方法作用：验证生成面板含档位选择器、额度说明与建议单元分组。
    # Args: self - pytest 测试类实例。
    # Returns: 无返回值，断言失败时由 pytest 报告。
    def test_workbench_has_divergence_selector_and_budget_hint(self) -> None:
        """用户必须能在预览前选定档位，并看到额度的含义。"""
        # Arrange
        page = Path("frontend/src/pages/ReportWorkbenchPage.tsx").read_text(encoding="utf-8")
        api = Path("frontend/src/api/reportWorkbench.ts").read_text(encoding="utf-8")
        types = Path("frontend/src/types/report.ts").read_text(encoding="utf-8")

        # Act / Assert
        assert "Segmented" in page
        assert "发散档位" in page
        # 额度说明必须写在界面上，用户才知道档位意味着什么
        assert "计划期与运行期共享" in page
        # 用户单元与建议单元分组展示，建议单元带理由
        assert "你要求的分析" in page
        assert "建议补充的分析" in page
        assert "unit.rationale" in page
        assert "ReportDivergenceLevel" in types
        assert "divergence" in api

    # 方法作用：验证预览页可逐项取消建议单元并提交裁剪结果。
    # Args: self - pytest 测试类实例。
    # Returns: 无返回值，断言失败时由 pytest 报告。
    def test_workbench_sends_accepted_unit_ids(self) -> None:
        """取消勾选后创建任务必须提交 accepted_unit_ids；未取消时传 null 走全部接受。"""
        # Arrange
        page = Path("frontend/src/pages/ReportWorkbenchPage.tsx").read_text(encoding="utf-8")
        api = Path("frontend/src/api/reportWorkbench.ts").read_text(encoding="utf-8")

        # Act / Assert
        assert "Checkbox" in page
        assert "excludedUnitIds" in page
        assert "acceptedUnitIds" in page
        assert "纳入本次报表" in page
        # 接口层字段名必须与后端契约一致
        assert "accepted_unit_ids" in api
