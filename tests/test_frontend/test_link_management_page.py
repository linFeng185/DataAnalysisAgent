"""链接管理页与公开分享页访问时长埋点前端契约测试。"""

from pathlib import Path

PAGE = "frontend/src/pages/LinkManagementPage.tsx"
API = "frontend/src/api/reportWorkbench.ts"
TYPES = "frontend/src/types/report.ts"
SHARED = "frontend/src/pages/SharedReportPage.tsx"
APP = "frontend/src/App.tsx"


class TestLinkManagementPage:
    """覆盖功能 23.x：链接管理页的筛选、访问统计展示与批量取消分享。"""

    # 方法作用：验证页面具备筛选区、表格、行多选与最近访问抽屉四类要素。
    # Args: self - pytest 测试类实例。
    # Returns: 无返回值，断言失败时由 pytest 报告。
    def test_link_page_has_filter_table_selection_and_visit_drawer(self) -> None:
        """链接管理页必须有筛选区、表格、行多选和最近访问抽屉，缺一项都不满足交互契约。"""
        # Arrange
        page = Path(PAGE).read_text(encoding="utf-8")

        # Act / Assert
        assert "Select" in page and "Input" in page
        assert "Table<ReportShareLinkSummary>" in page
        assert "rowSelection" in page
        assert "selectedKeys" in page
        assert "Drawer" in page
        assert "fetchReportShareVisits" in page

    # 方法作用：验证页面在空数据、加载中与请求失败（含 403）时都有明确状态。
    # Args: self - pytest 测试类实例。
    # Returns: 无返回值，断言失败时由 pytest 报告。
    def test_link_page_renders_empty_loading_and_error_states(self) -> None:
        """任何请求结果都要有可读状态，不允许出现空白页面。"""
        # Arrange
        page = Path(PAGE).read_text(encoding="utf-8")
        api = Path(API).read_text(encoding="utf-8")

        # Act / Assert
        assert 'emptyText: t("暂无分享链接")' in page
        assert "loading={loading}" in page
        assert 'type="error"' in page
        assert "链接列表加载失败" in page
        # 403 必须给出权限说明，而不是把后端原文直接甩给用户
        assert "当前账号没有链接管理权限" in page
        # 服务端错误响应不能触发登录跳转，公开接口走 publicError
        assert "publicError" in api
        assert "fetchReportShareLinks" in api
        assert "`${REPORT_BASE}/shares?${params.toString()}`" in api

    # 方法作用：验证表格列覆盖用户要求的七类语义列。
    # Args: self - pytest 测试类实例。
    # Returns: 无返回值，断言失败时由 pytest 报告。
    def test_link_page_renders_seven_semantic_columns(self) -> None:
        """七类语义列：报表标题、版本号、作用范围、授权码、有效时间、访问统计、操作。"""
        # Arrange
        page = Path(PAGE).read_text(encoding="utf-8")

        # Act / Assert
        # 第 1 列：关联报表与页面信息（标题 + 版本 + 作用范围 + 授权码）
        assert "关联的报表与页面信息" in page
        assert "share.report_title || share.report_id" in page
        assert "share.version" in page
        assert "share.scope === 'public'" in page and "指定用户" in page
        assert "share.require_code" in page and "需要授权码" in page
        # 第 2 列：有效时间（生效时间 → 失效时间，永久与终止状态各有 Tag）
        assert "有效时间" in page
        assert "share.created_at" in page and "share.expires_at" in page and "share.revoked_at" in page
        assert 't("永久有效")' in page
        assert 'Tag color="red"' in page
        assert "已过期" in page and "已撤销" in page
        # 第 3 列：访问量与访问时长；第 4 列：操作
        assert "访问量与访问时长" in page
        assert 'title: t("操作")' in page
        assert "查看最近访问" in page and "取消分享" in page

    # 方法作用：验证访问量与访问时长分开展示系统用户与非系统用户。
    # Args: self - pytest 测试类实例。
    # Returns: 无返回值，断言失败时由 pytest 报告。
    def test_link_page_separates_system_and_anonymous_visits(self) -> None:
        """系统用户访问量与非系统用户访问量必须分开显示，时长按分秒格式化。"""
        # Arrange
        page = Path(PAGE).read_text(encoding="utf-8")
        types = Path(TYPES).read_text(encoding="utf-8")

        # Act / Assert
        assert "系统用户 {value0} · 非系统用户 {value1}" in page
        assert "share.system_visitor_count" in page
        assert "share.anonymous_visitor_count" in page
        assert "share.total_duration_seconds" in page
        assert "share.average_duration_seconds" in page
        assert "system_visitor_count" in types and "anonymous_visitor_count" in types
        assert "total_duration_seconds" in types and "average_duration_seconds" in types
        # 时长格式化：不足一分钟显示「M 秒」，否则显示「N 分 M 秒」
        assert 't("{value0} 秒", { value0: safeSeconds })' in page
        assert 't("{value0} 分 {value1} 秒", { value0: minutes, value1: safeSeconds % 60 })' in page

    # 方法作用：验证最近访问抽屉展示系统用户、开始时间、最后活跃时间与停留时长。
    # Args: self - pytest 测试类实例。
    # Returns: 无返回值，断言失败时由 pytest 报告。
    def test_visit_drawer_shows_user_and_dwell_time(self) -> None:
        """抽屉列必须能回答「谁在什么时候看了多久」。"""
        # Arrange
        page = Path(PAGE).read_text(encoding="utf-8")
        api = Path(API).read_text(encoding="utf-8")

        # Act / Assert
        for label in ("系统用户", "开始访问时间", "最后活跃时间", "停留时长"):
            assert f'title: t("{label}")' in page
        assert "visit.user_id === null" in page and "t(\"匿名访客\")" in page
        assert "dataIndex: 'username'" in page
        assert "dataIndex: 'started_at'" in page and "dataIndex: 'last_seen_at'" in page
        assert "dataIndex: 'duration_seconds'" in page
        assert "`${REPORT_BASE}/shares/${encodeURIComponent(shareId)}/visits`" in api

    # 方法作用：验证批量取消分享调用 batch-revoke 接口并回显取消与跳过数量。
    # Args: self - pytest 测试类实例。
    # Returns: 无返回值，断言失败时由 pytest 报告。
    def test_link_page_batch_revoke_uses_batch_endpoint(self) -> None:
        """批量撤销必须走后端冻结的 batch-revoke 契约，并展示 revoked/skipped 结果。"""
        # Arrange
        page = Path(PAGE).read_text(encoding="utf-8")
        api = Path(API).read_text(encoding="utf-8")
        types = Path(TYPES).read_text(encoding="utf-8")

        # Act / Assert
        assert "batchRevokeReportShares" in page
        assert "Popconfirm" in page
        assert "确认取消所选 {value0} 条分享链接？" in page
        assert "已取消 {value0} 条分享，跳过 {value1} 条" in page
        assert "`${REPORT_BASE}/shares/batch-revoke`" in api
        assert "share_ids: shareIds" in api
        assert "revoked" in types and "skipped" in types


class TestSharedReportVisitBeacon:
    """覆盖需求 3.1：公开分享页只按可见时间累计停留时长。"""

    # 方法作用：验证埋点只在页面可见时累计时长，隐藏期间不计时。
    # Args: self - pytest 测试类实例。
    # Returns: 无返回值，断言失败时由 pytest 报告。
    def test_beacon_counts_visible_time_only(self) -> None:
        """可见性变化必须结算可见区段并把隐藏期间的起点清空。"""
        # Arrange
        page = Path(SHARED).read_text(encoding="utf-8")

        # Act / Assert
        assert "document.visibilityState === 'visible'" in page
        assert "addEventListener('visibilitychange'" in page
        assert "visibleSecondsRef.current +=" in page
        assert "visibleSinceRef.current = null" in page

    # 方法作用：验证心跳按累积值上报，卸载路径用 keepalive 的 PATCH 补发最后一次。
    # Args: self - pytest 测试类实例。
    # Returns: 无返回值，断言失败时由 pytest 报告。
    def test_beacon_heartbeat_is_cumulative_and_unload_uses_keepalive(self) -> None:
        """心跳必须是幂等覆盖的累积值而不是增量；卸载只能靠 fetch keepalive。"""
        # Arrange
        page = Path(SHARED).read_text(encoding="utf-8")
        api = Path(API).read_text(encoding="utf-8")

        # Act / Assert
        assert "VISIT_HEARTBEAT_MS = 30000" in page
        assert "setInterval" in page
        # 上报的是累计秒数，重复上报不会重复计数
        assert "Math.round(visibleSecondsRef.current)" in page
        assert "reportSharedReportVisitDuration" in page
        assert "method: 'PATCH'" in api
        assert "keepalive" in api
        assert "duration_seconds: Math.round(durationSeconds)" in api
        # 埋点失败只写调试日志，不弹用户提示
        assert "console.debug('分享页停留时长上报失败'" in page
        assert "console.debug('分享页访问登记失败'" in page

    # 方法作用：验证 StrictMode 双挂载只登记一条访问，且卸载清理计时器与监听。
    # Args: self - pytest 测试类实例。
    # Returns: 无返回值，断言失败时由 pytest 报告。
    def test_beacon_registration_is_guarded_and_cleaned_up(self) -> None:
        """StrictMode 重复挂载不得产生两条访问记录，卸载不得留下定时器和监听。"""
        # Arrange
        page = Path(SHARED).read_text(encoding="utf-8")

        # Act / Assert
        assert "registeredTokenRef" in page
        assert "if (registeredTokenRef.current === token) return;" in page
        assert "startSharedReportVisit" in page
        assert "clearInterval" in page
        assert "removeEventListener('visibilitychange'" in page
        assert "removeEventListener('pagehide'" in page
        # 登记只在快照成功加载后进行
        assert "useShareVisitBeacon(token, Boolean(report), Boolean(sessionToken))" in page

    # 方法作用：验证授权码解锁后不补发单独请求，只把 unlocked 交给下一次心跳。
    # Args: self - pytest 测试类实例。
    # Returns: 无返回值，断言失败时由 pytest 报告。
    def test_unlock_flag_is_carried_by_next_heartbeat(self) -> None:
        """解锁状态只影响后续心跳的 unlocked 标记，不额外登记或补发请求。"""
        # Arrange
        page = Path(SHARED).read_text(encoding="utf-8")
        api = Path(API).read_text(encoding="utf-8")

        # Act / Assert
        assert "unlockedRef" in page
        assert "unlockedRef.current = unlocked" in page
        assert "unlockedRef.current, keepalive" in page
        assert "body: JSON.stringify({ duration_seconds: Math.round(durationSeconds), unlocked })" in api
        assert "body: JSON.stringify({ unlocked })" in api


class TestLinkManagementRoute:
    """覆盖需求 3.3：链接管理页的路由与菜单入口。"""

    # 方法作用：验证应用注册链接管理路由与菜单项，且菜单排在报表工作台之后。
    # Args: self - pytest 测试类实例。
    # Returns: 无返回值，断言失败时由 pytest 报告。
    def test_app_registers_link_route_and_menu_after_workbench(self) -> None:
        """路由必须是 /report-workbench/links，菜单顺序便于后续批次合并成子菜单。"""
        # Arrange
        app = Path(APP).read_text(encoding="utf-8")

        # Act / Assert
        assert "const LinkManagementPage = lazy(() => import('./pages/LinkManagementPage'));" in app
        assert 'path="/report-workbench/links"' in app
        assert '<ReportRoute><LinkManagementPage /></ReportRoute>' in app
        assert '{t("链接管理")}' in app
        assert app.index('{t("链接管理")}') > app.index('{t("报表工作台")}')
        # 菜单沿用现有粗粒度能力键，细分能力键由后续批次引入
        assert "reportMenuAllowed === true" in app
