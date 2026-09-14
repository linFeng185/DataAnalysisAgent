"""通知中心前端契约测试：铃铛角标、收件箱、发布界面、渠道配置与 WebSocket 实时推送。"""

import re
from pathlib import Path

API = "frontend/src/api/notifications.ts"
HOOK = "frontend/src/hooks/useNotifications.ts"
BELL = "frontend/src/components/notifications/NotificationBell.tsx"
LIST = "frontend/src/components/notifications/NotificationList.tsx"
PAGE = "frontend/src/pages/NotificationsPage.tsx"
PUBLISHER = "frontend/src/components/notifications/NotificationPublisher.tsx"
CHANNELS = "frontend/src/components/notifications/NotificationChannels.tsx"


# 方法作用：截取指定函数的源码片段，便于只对该函数做断言。
# Args: source - 文件全文；name - 函数名。
# Returns: 从函数声明到函数体结束的源码文本。
def function_source(source: str, name: str) -> str:
    start = source.index(f"function {name}")
    end = source.index("\n}", start)
    return source[start:end]


# 方法作用：截取两个标记之间的源码片段，用于 const 箭头函数等非 function 声明。
# Args: source - 文件全文；start_marker - 起始标记；end_marker - 结束标记。
# Returns: 两个标记之间的源码文本。
def source_between(source: str, start_marker: str, end_marker: str) -> str:
    start = source.index(start_marker)
    end = source.index(end_marker, start)
    return source[start:end]


class TestNotificationApiContract:
    """覆盖通知模块冻结的 HTTP/WebSocket 接口契约。"""

    # 方法作用：验证收件箱、未读数、已读、删除与发布五个接口路径与查询参数。
    # Args: self - pytest 测试类实例。
    # Returns: 无返回值，断言失败时由 pytest 报告。
    def test_api_exposes_frozen_notification_endpoints(self) -> None:
        """接口路径必须与后端冻结契约一致，前端不得自行改造路由。"""
        # Arrange
        api = Path(API).read_text(encoding="utf-8")

        # Act / Assert
        assert "`${NOTIFICATIONS_BASE}/inbox?${params.toString()}`" in api
        assert "params.set('unread_only', unreadOnly ? 'true' : 'false')" in api
        assert "params.set('page', String(page))" in api
        assert "params.set('page_size', String(pageSize))" in api
        assert "`${NOTIFICATIONS_BASE}/unread-count`" in api
        assert "/read`, {}," in api
        assert "`${NOTIFICATIONS_BASE}/read-all`" in api
        assert "return del<void>(`${NOTIFICATIONS_BASE}/${encodeURIComponent(notificationId)}`)" in api
        assert "`${NOTIFICATIONS_BASE}/publish`" in api
        assert "`${NOTIFICATIONS_BASE}/admin/channels`" in api
        assert "put<NotificationChannelsResponse>" in api
        # 发布请求体形状：kind/title/body/link/audience
        assert "kind: payload.kind" in api and "title: payload.title" in api
        assert "body: payload.body" in api and "link: payload.link" in api
        assert "scope: payload.audience.scope" in api
        assert "user_ids: payload.audience.user_ids" in api and "roles: payload.audience.roles" in api

    # 方法作用：验证四类通知类型与发布类型定义，personal 不属于发布类型。
    # Args: self - pytest 测试类实例。
    # Returns: 无返回值，断言失败时由 pytest 报告。
    def test_notification_kinds_match_frozen_contract(self) -> None:
        """收件箱类型含 personal，发布类型只允许 platform/tenant/group。"""
        # Arrange
        api = Path(API).read_text(encoding="utf-8")

        # Act / Assert
        assert "export type NotificationKind = 'platform' | 'tenant' | 'group' | 'personal';" in api
        assert "export type NotificationPublishKind = 'platform' | 'tenant' | 'group';" in api
        # 收件箱行字段与后端返回字段一一对应
        for field in ("id: string", "title: string", "body: string", "link: string", "is_read: boolean", "created_at: string"):
            assert field in api
        assert "export interface NotificationInboxResponse" in api
        assert "total: number" in api and "page_size: number" in api
        # 推送消息体只含标识与摘要，不假设有正文
        push = api[api.index("export interface NotificationPushMessage"):api.index("export interface NotificationChannelState")]
        assert "notification_id: string" in push and "unread_count: number" in push
        assert "body" not in push

    # 方法作用：验证 WebSocket 地址按站点协议推导且不携带任何凭据参数。
    # Args: self - pytest 测试类实例。
    # Returns: 无返回值，断言失败时由 pytest 报告。
    def test_stream_url_derives_scheme_and_carries_no_credential(self) -> None:
        """https 页面必须推导出 wss，地址里不得出现 token 或查询串。"""
        # Arrange
        api = Path(API).read_text(encoding="utf-8")

        # Act
        url_source = function_source(api, "buildNotificationStreamUrl")

        # Assert
        assert "window.location.protocol === 'https:' ? 'wss:' : 'ws:'" in url_source
        assert "window.location.host" in url_source
        assert "${API_BASE}${NOTIFICATIONS_BASE}/stream" in url_source
        assert "const NOTIFICATIONS_BASE = '/notifications';" in api
        assert "const API_BASE = '/api/v1';" in api
        assert "token" not in url_source
        assert "access_token" not in api
        # URL 模板本身不带查询串，凭据只能走 Cookie 头
        return_line = next(line for line in url_source.splitlines() if line.strip().startswith("return "))
        assert "?" not in return_line
        assert "=" not in return_line
        # 通道为同源 Cookie 认证：OpenAPI 客户端不把令牌拼到地址上
        assert "WebSocket(buildNotificationStreamUrl())" in api

    # 方法作用：验证推送帧解析只接受通知类消息，非法内容被丢弃。
    # Args: self - pytest 测试类实例。
    # Returns: 无返回值，断言失败时由 pytest 报告。
    def test_push_parser_rejects_non_notification_frames(self) -> None:
        """非 notification 类型或非法 JSON 不得进入业务处理，避免脏数据污染列表。"""
        # Arrange
        api = Path(API).read_text(encoding="utf-8")

        # Act / Assert
        assert "export function parseNotificationPush(" in api
        assert "parsed?.type !== 'notification'" in api
        assert "return null;" in api
        assert "JSON.parse(raw)" in api
        # 主动关闭必须摘掉回调，避免被当成断线而重连
        assert "socket.onclose = null;" in api
        assert "openNotificationStream" in api and "close: () =>" in api


class TestNotificationRealtimeLifecycle:
    """覆盖 WebSocket 建连、指数退避重连、降级轮询与卸载清理。"""

    # 方法作用：验证断线后按指数退避重连且上限 30 秒，退避期间不重复建连。
    # Args: self - pytest 测试类实例。
    # Returns: 无返回值，断言失败时由 pytest 报告。
    def test_reconnect_uses_capped_exponential_backoff(self) -> None:
        """退避间隔必须按次数翻倍并在上限处封顶，同时只存在一个待执行重连。"""
        # Arrange
        hook = Path(HOOK).read_text(encoding="utf-8")

        # Act
        scheduler = function_source(hook, "scheduleReconnect")

        # Assert
        assert "export const RECONNECT_BASE_DELAY_MS = 1000;" in hook
        assert "export const MAX_RECONNECT_DELAY_MS = 30000;" in hook
        assert "Math.min(RECONNECT_BASE_DELAY_MS * 2 ** reconnectAttempt, MAX_RECONNECT_DELAY_MS)" in scheduler
        assert "reconnectAttempt += 1;" in scheduler
        # 已有待执行的重连或已无订阅者时不再安排，避免重复建连
        assert "if (reconnectTimer !== null || pushListeners.size === 0) return;" in scheduler
        assert "if (streamHandle || pushListeners.size === 0) return;" in hook
        # 连接成功后退避计数归零
        assert "reconnectAttempt = 0;" in hook

    # 方法作用：验证 StrictMode 双挂载不会产生两条连接，卸载时清定时器并关闭连接。
    # Args: self - pytest 测试类实例。
    # Returns: 无返回值，断言失败时由 pytest 报告。
    def test_stream_is_single_flight_and_cleaned_on_unmount(self) -> None:
        """首个订阅者延迟一个宏任务建连以消化 StrictMode 的挂载→卸载，最后一个订阅者释放连接。"""
        # Arrange
        hook = Path(HOOK).read_text(encoding="utf-8")

        # Act
        subscriber = hook[hook.index("export function subscribeNotificationStream"):hook.index("export interface UseNotificationsOptions")]
        teardown = function_source(hook, "teardownStream")

        # Assert
        assert "StrictMode" in hook
        assert "if (pushListeners.size === 1 && !streamHandle && deferredConnectTimer === null) {" in subscriber
        assert "deferredConnectTimer = window.setTimeout(" in subscriber
        # 取消订阅必须幂等：StrictMode 的模拟卸载会调用一次，真实卸载再调用一次
        assert "let released = false;" in subscriber and "if (released) return;" in subscriber
        # 清理：清掉建连与重连定时器并关闭连接
        assert "window.clearTimeout(deferredConnectTimer)" in teardown
        assert "window.clearTimeout(reconnectTimer)" in teardown
        assert "handle?.close();" in teardown
        assert "if (pushListeners.size === 0) teardownStream();" in subscriber
        # 组件卸载即退订
        assert "return subscribeNotificationStream(handlePush, handleStatus);" in hook

    # 方法作用：验证未读数以推送里的 unread_count 为准，不存在本地自增写法。
    # Args: self - pytest 测试类实例。
    # Returns: 无返回值，断言失败时由 pytest 报告。
    def test_unread_count_follows_server_push_value(self) -> None:
        """本地自增会与真实未读数漂移，角标必须直接采用服务端下发值。"""
        # Arrange
        hook = Path(HOOK).read_text(encoding="utf-8")

        # Act
        push_handler = source_between(hook, "const handlePush = useCallback", "const handleStatus = useCallback")

        # Assert
        assert "setUnreadCount(Math.max(0, Math.round(message.unread_count)));" in push_handler
        assert "prev + 1" not in hook
        assert "unreadCount + 1" not in hook
        assert "unread_count + 1" not in hook
        assert "increment" not in hook
        # 新通知插入列表头部并按 id 去重
        assert "return [incoming, ...prev];" in push_handler
        assert "if (prev.some(item => item.id === message.notification_id)) return prev;" in push_handler
        # 未读数也可单独从服务端刷新，作为推送之外的权威来源
        assert "fetchNotificationUnreadCount()" in hook

    # 方法作用：验证 WebSocket 不可用时降级为定时轮询未读数。
    # Args: self - pytest 测试类实例。
    # Returns: 无返回值，断言失败时由 pytest 报告。
    def test_falls_back_to_polling_when_stream_unavailable(self) -> None:
        """浏览器不支持、后端未就绪或多次握手失败都必须降级轮询而不是报错白屏。"""
        # Arrange
        hook = Path(HOOK).read_text(encoding="utf-8")

        # Act / Assert
        assert "export const FALLBACK_POLL_INTERVAL_MS = 60000;" in hook
        assert "typeof WebSocket === 'undefined'" in hook
        assert "setStreamStatus('degraded');" in hook
        assert "export const DEGRADE_AFTER_ATTEMPTS = 3;" in hook
        assert "if (reconnectAttempt >= DEGRADE_AFTER_ATTEMPTS) setStreamStatus('degraded');" in hook
        # 通道连通时不再轮询，断开或降级时启动定时器
        assert "if (streamStatus === 'open') return;" in hook
        assert "window.setInterval(() => { void syncUnreadCount(); }, FALLBACK_POLL_INTERVAL_MS)" in hook
        assert "return () => window.clearInterval(timer);" in hook
        # 轮询只拉未读数，失败不抛给界面
        assert "degraded: streamStatus !== 'open'," in hook


class TestNotificationBellBadge:
    """覆盖顶栏铃铛的未读角标、抽屉与收件箱交互。"""

    # 方法作用：验证铃铛显示未读角标、0 时隐藏且超过 99 显示 99+。
    # Args: self - pytest 测试类实例。
    # Returns: 无返回值，断言失败时由 pytest 报告。
    def test_bell_shows_capped_unread_badge_hidden_at_zero(self) -> None:
        """角标必须绑定未读数，0 时不显示，超过 99 截断为 99+。"""
        # Arrange
        bell = Path(BELL).read_text(encoding="utf-8")

        # Act / Assert
        assert "import { Badge, Button, Drawer, Space } from 'antd';" in bell
        assert "count={notifications.unreadCount}" in bell
        assert "showZero={false}" in bell
        assert "export const UNREAD_BADGE_OVERFLOW = 99;" in bell
        assert "overflowCount={UNREAD_BADGE_OVERFLOW}" in bell
        # 点击铃铛打开收件箱抽屉
        assert "onClick={() => setDrawerOpen(true)}" in bell
        assert 'title={t("通知中心")}' in bell

    # 方法作用：验证抽屉内具备未读切换、单条已读跳转、删除、全部已读、加载更多与空/错误态。
    # Args: self - pytest 测试类实例。
    # Returns: 无返回值，断言失败时由 pytest 报告。
    def test_inbox_list_covers_read_delete_paging_and_states(self) -> None:
        """抽屉与整页共用同一列表组件，交互元素缺一项都不满足需求。"""
        # Arrange
        bell = Path(BELL).read_text(encoding="utf-8")
        page = Path(PAGE).read_text(encoding="utf-8")
        listing = Path(LIST).read_text(encoding="utf-8")

        # Act / Assert
        # 未读 Tab 切换与全部已读、刷新、加载更多
        assert "Segmented" in listing
        assert "onUnreadOnlyChange(value === 'unread')" in listing
        assert '{ label: t("全部"), value: \'all\' }' in listing
        assert '{ label: t("未读"), value: \'unread\' }' in listing
        assert 't("全部已读")' in listing
        assert 't("加载更多")' in listing
        # 单条删除与已读视觉区分
        assert "Popconfirm" in listing
        assert 'title={t("确认删除该通知？")}' in listing
        assert "background: item.is_read ? undefined : '#e6f4ff'" in listing
        assert "onClick={() => onOpenItem(item)}" in listing
        # 空态与错误态
        assert 't("暂无未读通知")' in listing and 't("暂无通知")' in listing
        assert 'type="error"' in listing and 't("重试")' in listing
        # 铃铛与整页复用同一列表组件
        assert "import NotificationList from './NotificationList';" in bell
        assert "import NotificationList from '../components/notifications/NotificationList';" in page
        # 单条点击先标记已读再按 link 跳转
        assert "void notifications.markRead(item.id);" in bell
        assert "if (item.link) {" in bell and "navigate(item.link);" in bell

    # 方法作用：验证通知中心整页存在但不自行注册路由，路由由 App 统一注册。
    # Args: self - pytest 测试类实例。
    # Returns: 无返回值，断言失败时由 pytest 报告。
    def test_notifications_page_defers_route_registration_to_app(self) -> None:
        """页面只导出组件，避免与 App 的路由表重复注册。"""
        # Arrange
        page = Path(PAGE).read_text(encoding="utf-8")

        # Act / Assert
        assert "export default function NotificationsPage()" in page
        assert "<Route" not in page
        assert "useNotifications({ refreshOnPush: true })" in page
        assert "NotificationPublisher" in page and "NotificationChannels" in page


class TestNotificationPublisher:
    """覆盖发布界面的类型、投递范围与角色可见性。"""

    # 方法作用：验证发布类型齐全且 personal 不在发布选项中。
    # Args: self - pytest 测试类实例。
    # Returns: 无返回值，断言失败时由 pytest 报告。
    def test_publisher_kinds_exclude_personal_and_respect_roles(self) -> None:
        """平台通知只给超管，租户与用户群通知只给租户管理员；personal 由系统产生。"""
        # Arrange
        publisher = Path(PUBLISHER).read_text(encoding="utf-8")

        # Act / Assert
        assert "{ label: t(\"平台通知\"), value: 'platform' }" in publisher
        assert "{ label: t(\"租户通知\"), value: 'tenant' }" in publisher
        assert "{ label: t(\"指定用户群通知\"), value: 'group' }" in publisher
        assert "value: 'personal'" not in publisher
        assert "user?.role === 'super_admin'" in publisher
        assert "user?.role === 'tenant_admin'" in publisher
        assert "if (kindOptions.length === 0) return null;" in publisher

    # 方法作用：验证投递范围三种模式与角色多选项齐全，提交走发布接口。
    # Args: self - pytest 测试类实例。
    # Returns: 无返回值，断言失败时由 pytest 报告。
    def test_publisher_scopes_and_roles_are_complete(self) -> None:
        """范围必须支持全部用户/指定用户/按角色，角色含租户管理员、分析师与只读用户。"""
        # Arrange
        publisher = Path(PUBLISHER).read_text(encoding="utf-8")

        # Act / Assert
        assert '<Radio value="all">{t("全部用户")}</Radio>' in publisher
        assert '<Radio value="users">{t("指定用户")}</Radio>' in publisher
        assert '<Radio value="role">{t("按角色")}</Radio>' in publisher
        assert "{ label: t(\"租户管理员\"), value: 'tenant_admin' }" in publisher
        assert "{ label: t(\"分析师\"), value: 'analyst' }" in publisher
        assert "{ label: t(\"只读用户\"), value: 'viewer' }" in publisher
        assert '<Select mode="tags"' in publisher
        assert "<Checkbox.Group options={ROLE_OPTIONS} />" in publisher
        assert "required: true" in publisher
        # 标题、正文必填，跳转链接可选
        assert "name=\"title\"" in publisher and "name=\"body\"" in publisher and "name=\"link\"" in publisher
        assert 't("跳转链接（可选）")' in publisher

    # 方法作用：验证发布成功提示实际投递范围，失败展示后端可读原因。
    # Args: self - pytest 测试类实例。
    # Returns: 无返回值，断言失败时由 pytest 报告。
    def test_publisher_reports_delivery_scope_and_surfaces_errors(self) -> None:
        """成功要说明投递到谁，失败要把后端原因原样展示，不允许静默失败。"""
        # Arrange
        publisher = Path(PUBLISHER).read_text(encoding="utf-8")

        # Act / Assert
        assert "await publishNotification({" in publisher
        assert 't("通知已发布，投递范围：{value0}{value1}"' in publisher
        assert "value0: audienceSummary(audience)" in publisher
        assert 't("指定 {value0} 个用户", { value0: audience.user_ids.length })' in publisher
        assert "setError(failure instanceof Error ? failure.message : t(\"通知发布失败\"));" in publisher
        assert "parseAudienceUserIds(values.user_ids)" in publisher
        assert "if (scope === 'users' && audience.user_ids.length === 0) {" in publisher


class TestNotificationChannels:
    """覆盖租户级渠道配置的开关、字段与敏感项占位。"""

    # 方法作用：验证四个渠道各一行且由 Switch 控制启用，站内信始终启用。
    # Args: self - pytest 测试类实例。
    # Returns: 无返回值，断言失败时由 pytest 报告。
    def test_channels_render_four_switches_with_in_app_always_on(self) -> None:
        """站内信不可关闭，邮件/飞书/Slack 默认停用但保留配置面。"""
        # Arrange
        channels = Path(CHANNELS).read_text(encoding="utf-8")

        # Act / Assert
        assert "in_app: \"站内信\"" in channels
        assert "email: \"邮件\"" in channels
        assert "feishu: \"飞书\"" in channels
        assert "slack: \"Slack\"" in channels
        assert "<Switch" in channels
        assert "disabled={channel.channel === 'in_app' || saving}" in channels
        assert "channel.channel === 'in_app' ? true : Boolean(enabledMap[channel.channel])" in channels
        assert 't("始终启用")' in channels
        assert "saveNotificationChannels(payload)" in channels
        assert "channel: channel.channel," in channels and "enabled: channel.channel === 'in_app' ? true : Boolean" in channels

    # 方法作用：验证 SMTP 与 Webhook 字段齐全，敏感项只显示已配置占位不回显明文。
    # Args: self - pytest 测试类实例。
    # Returns: 无返回值，断言失败时由 pytest 报告。
    def test_channel_fields_cover_smtp_and_webhook_without_secret_echo(self) -> None:
        """口令与 Webhook 属于敏感项，界面只能提示「已配置」，重新保存留空即不修改。"""
        # Arrange
        channels = Path(CHANNELS).read_text(encoding="utf-8")

        # Act / Assert
        for field in ("smtp_host", "smtp_port", "smtp_starttls", "smtp_username", "smtp_password"):
            assert field in channels
        assert "feishu_webhook_url" in channels and "slack_webhook_url" in channels
        assert "const SENSITIVE_KEYS = new Set(['smtp_password', 'feishu_webhook_url', 'slack_webhook_url']);" in channels
        assert 't("已配置，留空表示不修改")' in channels
        assert "<Input.Password" in channels
        # 敏感项在加载与提交两侧都不回显、不覆盖
        assert "draft[field.key] = '';" in channels
        assert "if (raw) config[field.key] = raw;" in channels
        assert 't("已配置")' in channels

    # 方法作用：验证界面顶部明确说明邮件与飞书/Slack 不会实际外发。
    # Args: self - pytest 测试类实例。
    # Returns: 无返回值，断言失败时由 pytest 报告。
    def test_channels_declare_external_delivery_is_not_enabled(self) -> None:
        """用户必须一眼看到「先不发」的事实，避免以为配置完就能收到邮件。"""
        # Arrange
        channels = Path(CHANNELS).read_text(encoding="utf-8")

        # Act / Assert
        assert 't("邮件与飞书/Slack 目前不会实际外发，配置仅作预留；站内信始终可用。")' in channels
        assert 'type="warning"' in channels
        assert 't("通知渠道配置")' in channels
        assert "canManage ?? user?.role === 'tenant_admin'" in channels


class TestNotificationStreamHeartbeat:
    """验证长连接有心跳，不会被反向代理的空闲超时掐断。

    背景：frontend/nginx.conf 对 /api/ 设了 proxy_read_timeout / proxy_send_timeout 各 600s，
    这是「读空闲」超时；通知链路平时没有数据流动，十分钟必然断连。心跳必须由客户端发起，
    因为服务端只在收到 ping 时才回 pong（不主动推送），只有单向流量无法重置读空闲计时。
    """

    # 方法作用：验证心跳间隔小于 nginx 的读空闲超时并留足余量。
    # Args: self - pytest 测试类实例。
    # Returns: 无返回值，断言失败时由 pytest 报告。
    def test_ping_interval_is_below_proxy_idle_timeout(self) -> None:
        """间隔必须显著小于代理超时，否则仍会被掐断。"""
        # Arrange
        api = Path(API).read_text(encoding="utf-8")
        nginx = Path("frontend/nginx.conf").read_text(encoding="utf-8")
        idle = int(re.search(r"proxy_read_timeout\s+(\d+)s", nginx).group(1))

        # Act / Assert
        ping_ms = int(re.search(r"NOTIFICATION_PING_INTERVAL_MS\s*=\s*(\d+)", api).group(1))
        # 至少留 5 倍余量：后台标签页的定时器会被浏览器节流（Chrome 约每分钟一次）。
        assert idle * 1000 >= ping_ms * 5, f"心跳 {ping_ms}ms 相对代理空闲超时 {idle}s 余量不足"

    # 方法作用：验证心跳发的是服务端认识的那个令牌。
    # Args: self - pytest 测试类实例。
    # Returns: 无返回值，断言失败时由 pytest 报告。
    def test_ping_token_matches_server_contract(self) -> None:
        """跨端契约：前端发什么，服务端就得认什么。"""
        # Arrange
        api = Path(API).read_text(encoding="utf-8")
        server = Path("src/api/routes/notifications.py").read_text(encoding="utf-8")

        # Act / Assert
        # 服务端把消息转小写后与 "ping" 比较，前端发送的文本必须是同一个词。
        assert "socket.send('ping')" in api
        assert 'str(message).strip().lower() == "ping"' in server
        assert '"type": "pong"' in server

    # 方法作用：验证未打开的连接不会尝试发送心跳。
    # Args: self - pytest 测试类实例。
    # Returns: 无返回值，断言失败时由 pytest 报告。
    def test_ping_skips_when_socket_not_open(self) -> None:
        """CONNECTING/CLOSING 状态下 send 会抛异常，必须先用 readyState 挡住。"""
        # Arrange
        api = Path(API).read_text(encoding="utf-8")

        # Act / Assert
        assert "socket.readyState !== WebSocket.OPEN" in api

    # 方法作用：验证收到任何消息都刷新存活时刻（pong 会被解析层丢弃）。
    # Args: self - pytest 测试类实例。
    # Returns: 无返回值，断言失败时由 pytest 报告。
    def test_any_message_refreshes_liveness(self) -> None:
        """只认通知消息会把 pong 当成超时证据，造成假断线重连。"""
        # Arrange
        api = Path(API).read_text(encoding="utf-8")
        onmessage = source_between(api, "socket.onmessage =", "socket.onclose =")

        # Act / Assert
        assert "lastSeenAt = Date.now()" in onmessage
        # 刷新必须发生在解析之前：解析对 pong 返回 null。
        assert onmessage.index("lastSeenAt = Date.now()") < onmessage.index("parseNotificationPush")

    # 方法作用：验证收不到消息时主动关闭以复用既有重连。
    # Args: self - pytest 测试类实例。
    # Returns: 无返回值，断言失败时由 pytest 报告。
    def test_watchdog_forces_reconnect_when_silent(self) -> None:
        """半开连接不会触发 onclose，只能靠心跳超时主动断开。"""
        # Arrange
        api = Path(API).read_text(encoding="utf-8")
        onopen = source_between(api, "socket.onopen =", "socket.onmessage =")

        # Act / Assert
        assert "NOTIFICATION_PONG_TIMEOUT_MS" in onopen
        assert "socket.close()" in onopen

    # 方法作用：验证定时器在关闭与断线时都被清理。
    # Args: self - pytest 测试类实例。
    # Returns: 无返回值，断言失败时由 pytest 报告。
    def test_timers_are_cleared_on_close_and_disconnect(self) -> None:
        """漏清 interval 会在重连后累积多条心跳与看门狗。"""
        # Arrange
        api = Path(API).read_text(encoding="utf-8")
        onclose = source_between(api, "socket.onclose =", "socket.onerror =")

        # Act / Assert
        assert "stopTimers()" in onclose
        assert "stopTimers()" in api.split("return {")[-1]
        assert "window.clearInterval" in api
