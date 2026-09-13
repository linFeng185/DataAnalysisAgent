"""报表工作台三个子页（创建报表 / 报表信息 / 链接管理）的前端契约测试。"""

from pathlib import Path

APP = "frontend/src/App.tsx"
CREATE_PAGE = "frontend/src/pages/ReportCreatePage.tsx"
LIST_PAGE = "frontend/src/pages/ReportListPage.tsx"
STATUS_MODULE = "frontend/src/report/status.ts"
EXAMPLES = "frontend/src/report/promptExamples.ts"
API = "frontend/src/api/reportWorkbench.ts"
TYPES = "frontend/src/types/report.ts"


class TestReportSubmenu:
    """覆盖需求 1.1、1.8：子菜单能力键控制与旧「自动化」入口移除。"""

    # 方法作用：验证报表工作台是含三个子项的子菜单，且每个子项按自己的能力键控制。
    # Args: self - pytest 测试类实例。
    # Returns: 无返回值，断言失败时由 pytest 报告。
    def test_menu_is_submenu_with_three_capability_keys(self) -> None:
        """三个子页必须各配一个能力键；父菜单仍由 report.menu 控制。"""
        # Arrange
        app = Path(APP).read_text(encoding="utf-8")

        # Act / Assert：父菜单可见性沿用 reportMenuAllowed（report.menu）
        assert "reportMenuAllowed === true" in app
        assert "{t(\"报表工作台\")}" in app
        # 子菜单结构：children 三项，各按自己的键控制
        assert "children: [" in app
        assert app.count("reportCapabilities.includes('") == 3, "三个子项必须各有一个能力键判断"
        assert "reportCapabilities.includes('report.create_page')" in app
        assert "reportCapabilities.includes('report.list')" in app
        assert "reportCapabilities.includes('report.link_manage')" in app
        # 能力探针失败必须按无能力处理，不能把子项全量放行
        assert "setReportCapabilities([])" in app
        assert "setReportCapabilities(result.capabilities || [])" in app

    # 方法作用：验证三个子页路径都注册在报表能力守卫之下。
    # Args: self - pytest 测试类实例。
    # Returns: 无返回值，断言失败时由 pytest 报告。
    def test_submenu_routes_are_under_report_guard(self) -> None:
        """子页路由必须逐个包在 ReportRoute 里，不能绕过服务端能力校验。"""
        # Arrange
        app = Path(APP).read_text(encoding="utf-8")

        # Act / Assert
        assert '<Route path="/report-workbench/create" element={<ReportRoute><ReportCreatePage /></ReportRoute>} />' in app
        assert '<Route path="/report-workbench/jobs" element={<ReportRoute><ReportListPage /></ReportRoute>} />' in app
        assert '<Route path="/report-workbench/links" element={<ReportRoute><LinkManagementPage /></ReportRoute>} />' in app
        assert "const ReportCreatePage = lazy(() => import('./pages/ReportCreatePage'));" in app
        assert "const ReportListPage = lazy(() => import('./pages/ReportListPage'));" in app

    # 方法作用：验证旧路径重定向到创建报表页，且旧「自动化」菜单与路由已移除。
    # Args: self - pytest 测试类实例。
    # Returns: 无返回值，断言失败时由 pytest 报告。
    def test_legacy_path_redirects_and_automation_removed(self) -> None:
        """旧路径保留兼容重定向；需求 1.8 要求删除自动化菜单项与路由。"""
        # Arrange
        app = Path(APP).read_text(encoding="utf-8")

        # Act / Assert
        assert '<Route path="/report-workbench" element={<Navigate to="/report-workbench/create" replace />} />' in app
        assert 'to="/automation"' not in app, "自动化菜单项必须已移除"
        assert 'path="/automation"' not in app, "自动化路由必须已移除"
        assert "AutomationPage" not in app, "自动化页面不应再被路由引用（页面文件暂留待通知模块统一处理）"


class TestReportCreatePage:
    """覆盖需求 1.7、2.x：创建页左右两栏与纯静态案例引导。"""

    # 方法作用：验证创建页保留完整创建能力，并把提交改为后台计划生成接口。
    # Args: self - pytest 测试类实例。
    # Returns: 无返回值，断言失败时由 pytest 报告。
    def test_create_page_keeps_form_and_posts_report(self) -> None:
        """创建页要保留提示词、数据源、模型、档位与模板，提交走 POST /reports。"""
        # Arrange
        page = Path(CREATE_PAGE).read_text(encoding="utf-8")
        api = Path(API).read_text(encoding="utf-8")

        # Act / Assert：表单能力齐全
        assert "Input.TextArea" in page
        assert 'mode="multiple"' in page
        assert "选择规划模型" in page
        assert "Segmented" in page and "发散档位" in page
        assert "报表模板" in page
        assert "生成计划" in page
        # 档位与模板的说明文案必须保留
        assert "计划期与运行期共享" in page
        assert "模板只影响呈现样式" in page
        # 提交走新报表资源接口，且不再在本页请求计划预览
        assert "await createReport({" in page
        assert "previewReportPlan" not in page, "创建页不应再调用旧预览接口"
        assert "createReport" in api
        assert "`${REPORT_BASE}/reports`" in api
        # 成功后提示并跳转报表信息页
        assert "报表已创建，正在后台生成计划，完成后会通知你" in page
        assert "navigate('/report-workbench/jobs')" in page

    # 方法作用：验证左右两栏结构与窄屏堆叠断点。
    # Args: self - pytest 测试类实例。
    # Returns: 无返回值，断言失败时由 pytest 报告。
    def test_create_page_has_two_column_layout(self) -> None:
        """左侧生成计划、右侧案例；窄屏（<992px）由 lg 断点把案例栏落回下方。"""
        # Arrange
        page = Path(CREATE_PAGE).read_text(encoding="utf-8")

        # Act / Assert
        assert "<Row gutter={[20, 20]} align=\"top\">" in page
        assert "lg={16}" in page and "lg={8}" in page
        assert page.index("lg={16}") < page.index("lg={8}"), "表单在左、案例在右"
        assert "t(\"报表需求\")" in page
        assert "t(\"案例\")" in page
        assert "xs={24}" in page, "窄屏必须整列堆叠，避免挤压表单"

    # 方法作用：验证案例数据来自独立文件，覆盖八类写法且完全不含后端调用。
    # Args: self - pytest 测试类实例。
    # Returns: 无返回值，断言失败时由 pytest 报告。
    def test_examples_are_static_and_cover_required_dimensions(self) -> None:
        """案例必须纯静态：文件里不允许出现任何 fetch/post/get 调用。"""
        # Arrange
        page = Path(CREATE_PAGE).read_text(encoding="utf-8")
        examples = Path(EXAMPLES).read_text(encoding="utf-8")

        # Act / Assert：数据独立存放，组件不写死案例
        assert "from '../report/promptExamples'" in page
        assert "reportPromptExamples" in page
        # 纯静态：不出现任何请求调用
        assert "fetch(" not in examples
        assert "post(" not in examples
        assert "get(" not in examples
        # 至少 8 条示例
        assert examples.count("prompt: '") >= 8
        # 八类维度齐全
        for dimension in ("单源经营分析", "多源关联分析", "自然年", "定时周报", "发散", "永久保留", "指定数据源", "@metric("):
            assert dimension in examples, f"案例缺少维度：{dimension}"
        # 点击案例只填入提示词，是纯前端行为
        assert "onClick={() => applyExample(example.prompt)}" in page


class TestReportListPage:
    """覆盖需求 1.2～1.6、2.2～2.4：报表信息列表、详情、调度与页面版本。"""

    # 方法作用：验证表格七列齐全且顺序与用户要求一致。
    # Args: self - pytest 测试类实例。
    # Returns: 无返回值，断言失败时由 pytest 报告。
    def test_list_page_has_seven_columns_in_order(self) -> None:
        """七列顺序：摘要标题、数据源、发散档位、任务状态、运行次数、下次运行、创建时间。"""
        # Arrange
        page = Path(LIST_PAGE).read_text(encoding="utf-8")
        ordered_labels = [
            "报表摘要标题", "选择的数据源", "发散档位", "任务状态", "运行次数", "下次运行时间", "创建时间",
        ]

        # Act：取每列标题在文件中的位置
        positions = [page.index(f'title: t("{label}")') for label in ordered_labels]

        # Assert
        assert positions == sorted(positions), "七列顺序必须与需求一致"
        assert "dataIndex: 'datasources'" in page
        assert "dataIndex: 'runs_started'" in page
        assert "dataIndex: 'next_run_at'" in page
        assert "dataIndex: 'created_at'" in page

    # 方法作用：验证默认倒序、分页与「空/加载/失败/403」四类状态都有展示。
    # Args: self - pytest 测试类实例。
    # Returns: 无返回值，断言失败时由 pytest 报告。
    def test_list_page_default_order_paging_and_states(self) -> None:
        """后端已按创建时间倒序返回；页面必须提示该顺序并给出分页与明确状态。"""
        # Arrange
        page = Path(LIST_PAGE).read_text(encoding="utf-8")
        api = Path(API).read_text(encoding="utf-8")

        # Act / Assert：倒序由服务端保证，页面不自行重排
        assert "默认按创建时间倒序" in page
        assert ".sort(" not in page, "页面不应自行重排，避免与服务端分页冲突"
        assert "page_size: pageSize" in page
        assert "params.set('page', String(query.page ?? 1))" in api
        # 空态 / 加载态 / 失败态 / 403
        assert 'locale={{ emptyText: t("暂无报表") }}' in page
        assert "loading={loading}" in page
        assert 'type="error"' in page
        assert "加载报表列表失败" in page
        assert "当前账号没有查看报表信息的权限" in page

    # 方法作用：验证状态映射含新语义，且非终态轮询、终态停表。
    # Args: self - pytest 测试类实例。
    # Returns: 无返回值，断言失败时由 pytest 报告。
    def test_status_mapping_and_polling(self) -> None:
        """生成中/未生成/计划失败必须能显示；非终态 2.5 秒轮询，全部终态时停表。"""
        # Arrange
        page = Path(LIST_PAGE).read_text(encoding="utf-8")
        status = Path(STATUS_MODULE).read_text(encoding="utf-8")

        # Act / Assert：状态映射
        assert "planning: t(\"生成中\")" in status
        assert "draft: t(\"未生成\")" in status
        assert "plan_failed: t(\"计划失败\")" in status
        assert "reportStatusLabel" in page and "reportStatusColor" in page
        # 轮询：仅非终态启动，终态停表
        assert "POLL_INTERVAL_MS = 2500" in page
        assert "window.setInterval" in page
        assert "isReportActiveStatus" in page
        assert "if (!needsPolling && !detailActive) return undefined;" in page
        assert "window.clearInterval(timer)" in page
        assert "REPORT_ACTIVE_STATUSES = ['planning', 'queued', 'running']" in status
        for terminal in ("succeeded", "partial", "failed", "cancelled", "expired", "deleted"):
            assert terminal in status

    # 方法作用：验证「生成」只对未生成显示且调 /run，重试计划只对计划失败显示。
    # Args: self - pytest 测试类实例。
    # Returns: 无返回值，断言失败时由 pytest 报告。
    def test_run_and_retry_actions_follow_status(self) -> None:
        """点「生成」才预占额度；计划失败的报表只能重试计划。"""
        # Arrange
        page = Path(LIST_PAGE).read_text(encoding="utf-8")
        api = Path(API).read_text(encoding="utf-8")

        # Act / Assert
        assert "const isEditable = status === 'draft';" in page
        assert "const canRun = status === 'draft' && isOwner;" in page
        assert "{canRun && <Button type=\"primary\"" in page
        assert "runReport(detail.report.id)" in page
        assert "`${REPORT_BASE}/reports/${encodeURIComponent(reportId)}/run`" in api
        assert "status === 'plan_failed' && <Button" in page
        assert "retryReportPlan" in page
        assert "`${REPORT_BASE}/reports/${encodeURIComponent(reportId)}/retry-plan`" in api
        # 重试接口只回传状态：页面必须重新拉取详情，不能把响应当成详情用
        assert "post<{ report_id: string; status: ReportJobStatus }>" in api
        assert "await loadDetail(detail.report.id, { silent: true });" in page
        # 删除只出现在 draft / plan_failed，并带二次确认
        assert "['draft', 'plan_failed'].includes(status || '')" in page
        assert "Popconfirm" in page
        assert "deleteReport" in page
        assert "`${REPORT_BASE}/reports/${encodeURIComponent(reportId)}`" in api

    # 方法作用：验证保存计划只提交被接受的单元，只允许删减。
    # Args: self - pytest 测试类实例。
    # Returns: 无返回值，断言失败时由 pytest 报告。
    def test_save_plan_sends_accepted_units_only(self) -> None:
        """accepted_unit_ids 只传被接受的单元，新增会被服务端 422 拒绝。"""
        # Arrange
        page = Path(LIST_PAGE).read_text(encoding="utf-8")
        api = Path(API).read_text(encoding="utf-8")
        types = Path(TYPES).read_text(encoding="utf-8")

        # Act / Assert：提交体只有 accepted_unit_ids，不夹带其他字段
        assert "await updateReportPlan(detail.report.id, { accepted_unit_ids: acceptedUnitIds });" in page
        assert "filter(unitId => !excludedUnitIds.includes(unitId))" in page
        assert "accepted_unit_ids?: string[];" in types
        assert "patch<ReportDetailResponse>" in api

    # 方法作用：验证勾选框只在可编辑（未生成）状态出现。
    # Args: self - pytest 测试类实例。
    # Returns: 无返回值，断言失败时由 pytest 报告。
    def test_unit_checkbox_only_in_editable_state(self) -> None:
        """只读态不显示勾选框：计划已锁定，勾选没有可提交的落点。"""
        # Arrange
        page = Path(LIST_PAGE).read_text(encoding="utf-8")

        # Act / Assert
        assert "{isEditable && <Checkbox" in page
        assert "纳入本次报表" in page
        assert "仅「未生成」状态的报表可以修改计划与调度。" in page
        # 只读态下调度表单与保存按钮同样不可用
        assert "disabled={!isEditable}" in page
        assert "disabled={!isEditable || !canPermanent}" in page

    # 方法作用：验证用户筛选只对租户管理员显示并下推 user_id。
    # Args: self - pytest 测试类实例。
    # Returns: 无返回值，断言失败时由 pytest 报告。
    def test_user_filter_is_tenant_admin_only(self) -> None:
        """其他角色只看自己的报表，界面不出现用户筛选，也不下发 user_id。"""
        # Arrange
        page = Path(LIST_PAGE).read_text(encoding="utf-8")
        api = Path(API).read_text(encoding="utf-8")

        # Act / Assert
        assert "const isTenantAdmin = user?.role === 'tenant_admin';" in page
        assert "{isTenantAdmin && <InputNumber" in page
        assert "user_id: isTenantAdmin && userFilter ? userFilter : undefined" in page
        assert "按用户 ID 精确筛选" in page
        # 仅租户管理员可用的参数由接口层原样下发，其他角色不下发该键
        assert "if (query.user_id) params.set('user_id', String(query.user_id));" in api

    # 方法作用：验证详情抽屉覆盖计划、调度、页面版本与失败原因。
    # Args: self - pytest 测试类实例。
    # Returns: 无返回值，断言失败时由 pytest 报告。
    def test_detail_drawer_shows_plan_schedule_and_pages(self) -> None:
        """详情要能回答：计划是什么、调度怎么设、有哪些页面版本、失败原因是什么。"""
        # Arrange
        page = Path(LIST_PAGE).read_text(encoding="utf-8")
        types = Path(TYPES).read_text(encoding="utf-8")
        api = Path(API).read_text(encoding="utf-8")

        # Act / Assert：计划详情
        assert "计划详情" in page
        assert "page_ttl_days" in page and "estimated?.query_count" in page and "estimated?.llm_tokens" in page
        # 分析单元分组：用户要求 / 系统建议
        assert "你要求的分析" in page and "建议补充的分析" in page
        assert "unit.rationale" in page
        # 调度编辑
        assert "调度编辑" in page
        assert "Radio.Button value=\"immediate\"" in page and "Radio.Button value=\"scheduled\"" in page
        assert "DatePicker" in page and "InputNumber" in page
        assert "保存调度" in page
        assert "永久保留需要「永久保存」能力" in page
        assert "canPermanent" in page
        # 页面版本列表
        assert "页面列表" in page and "pageExpiryTag" in page
        assert "永久有效" in page and "已过期" in page and "有效至 {value0}" in page
        assert "`${REPORT_BASE}/reports/${encodeURIComponent(reportId)}/pages`" in api
        assert "fetchReportPages" in api
        # 计划失败原因显著展示：记录里的 plan_error 是主来源，详情顶层字段做兜底
        assert "status === 'plan_failed' && <Alert" in page
        assert "detail.report.plan_error || detail.plan_error" in page
        assert "plan_error?: string | null;" in types

    # 方法作用：验证类型与接口按冻结契约追加，旧函数保留。
    # Args: self - pytest 测试类实例。
    # Returns: 无返回值，断言失败时由 pytest 报告。
    def test_types_and_api_follow_frozen_contract(self) -> None:
        """新增字段与接口按契约追加；旧预览/创建接口保留兼容但新页面不再调用。"""
        # Arrange
        types = Path(TYPES).read_text(encoding="utf-8")
        api = Path(API).read_text(encoding="utf-8")

        # Act / Assert：类型
        assert "'plan_failed'" in types
        for name in ("ReportListItem", "ReportListResponse", "ReportDetailResponse", "ReportCreateRequest", "ReportPlanUpdateRequest"):
            assert f"export interface {name}" in types, f"缺少类型 {name}"
        # 接口
        for name in ("createReport", "fetchReportList", "fetchReportDetail", "updateReportPlan", "runReport", "retryReportPlan", "deleteReport", "fetchReportPages"):
            assert f"export function {name}" in api, f"缺少接口 {name}"
        # 旧接口保留不删
        assert "export function previewReportPlan" in api
        assert "export function createReportJob" in api
        # PATCH 由 client 统一提供
        assert "import { del, get, patch, post } from './client';" in api
