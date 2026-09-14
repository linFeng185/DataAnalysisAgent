import { del, get, post, put } from './client';

// 通知接口的通用前缀；HTTP 由 client 统一补 /api/v1，WebSocket 需要完整地址所以单独保留基址。
const NOTIFICATIONS_BASE = '/notifications';
const API_BASE = '/api/v1';

// 四类通知：平台通知（超管）、租户通知（租户管理员）、指定用户群、任务完成的个人通知。
export type NotificationKind = 'platform' | 'tenant' | 'group' | 'personal';
// 发布入口只开放前三类，personal 由系统在任务结束时产生。
export type NotificationPublishKind = 'platform' | 'tenant' | 'group';
export type NotificationAudienceScope = 'all' | 'users' | 'role';
export type NotificationChannelName = 'in_app' | 'email' | 'feishu' | 'slack';

export interface NotificationItem {
  id: string;
  kind: NotificationKind;
  title: string;
  body: string;
  link: string;
  is_read: boolean;
  created_at: string;
}

export interface NotificationInboxResponse {
  items: NotificationItem[];
  total: number;
  page: number;
  page_size: number;
}

export interface NotificationUnreadCountResponse {
  count: number;
}

export interface NotificationAudience {
  scope: NotificationAudienceScope;
  user_ids: number[];
  roles: string[];
}

export interface NotificationPublishRequest {
  kind: NotificationPublishKind;
  title: string;
  body: string;
  link: string;
  audience: NotificationAudience;
}

// 推送只带标识与摘要，正文需要用户打开收件箱后再拉取。
export interface NotificationPushMessage {
  type: string;
  notification_id: string;
  kind: NotificationKind;
  title: string;
  link: string;
  created_at: string;
  unread_count: number;
}

export interface NotificationChannelState {
  channel: NotificationChannelName;
  enabled: boolean;
  configured: boolean;
  config: Record<string, unknown>;
}

export interface NotificationChannelsResponse {
  channels: NotificationChannelState[];
}

export interface NotificationChannelSavePayload {
  channel: NotificationChannelName;
  enabled: boolean;
  config: Record<string, unknown>;
}

// 方法作用：读取当前用户的收件箱，按创建时间倒序分页。
// Args: unreadOnly - 只看未读；page - 页码；pageSize - 每页条数。
// Returns: 收件箱列表与总数。
export function fetchNotificationInbox(
  unreadOnly = false, page = 1, pageSize = 20,
): Promise<NotificationInboxResponse> {
  const params = new URLSearchParams();
  params.set('unread_only', unreadOnly ? 'true' : 'false');
  params.set('page', String(page));
  params.set('page_size', String(pageSize));
  return get<NotificationInboxResponse>(`${NOTIFICATIONS_BASE}/inbox?${params.toString()}`);
}

// 方法作用：读取未读数量，供角标与降级轮询使用。
// Args: 无。
// Returns: 未读通知条数。
export function fetchNotificationUnreadCount(): Promise<NotificationUnreadCountResponse> {
  return get<NotificationUnreadCountResponse>(`${NOTIFICATIONS_BASE}/unread-count`);
}

// 方法作用：把单条通知标记为已读。
// Args: notificationId - 通知标识。
// Returns: 服务端处理结果。
export function markNotificationRead(notificationId: string): Promise<Record<string, unknown>> {
  return post<Record<string, unknown>>(
    `${NOTIFICATIONS_BASE}/${encodeURIComponent(notificationId)}/read`, {},
  );
}

// 方法作用：把当前用户收件箱全部标记为已读。
// Args: 无。
// Returns: 服务端处理结果。
export function markAllNotificationsRead(): Promise<Record<string, unknown>> {
  return post<Record<string, unknown>>(`${NOTIFICATIONS_BASE}/read-all`, {});
}

// 方法作用：软删除当前用户的一条通知。
// Args: notificationId - 通知标识。
// Returns: 无返回内容。
export function deleteNotification(notificationId: string): Promise<void> {
  return del<void>(`${NOTIFICATIONS_BASE}/${encodeURIComponent(notificationId)}`);
}

// 方法作用：发布平台、租户或指定用户群通知，投递范围由 audience 决定。
// Args: payload - 通知类型、标题正文链接与投递范围。
// Returns: 服务端发布结果（含实际投递范围说明）。
export function publishNotification(payload: NotificationPublishRequest): Promise<Record<string, unknown>> {
  return post<Record<string, unknown>>(`${NOTIFICATIONS_BASE}/publish`, {
    kind: payload.kind,
    title: payload.title,
    body: payload.body,
    link: payload.link,
    audience: {
      scope: payload.audience.scope,
      user_ids: payload.audience.user_ids,
      roles: payload.audience.roles,
    },
  });
}

// 方法作用：读取本租户的通知渠道配置，敏感项只回显是否已配置。
// Args: 无。
// Returns: 四个渠道的启用状态与已配置标记。
export function fetchNotificationChannels(): Promise<NotificationChannelsResponse> {
  return get<NotificationChannelsResponse>(`${NOTIFICATIONS_BASE}/admin/channels`);
}

// 方法作用：保存本租户的通知渠道配置，敏感项按字段名提交明文由后端加密入库。
// Args: channels - 各渠道的启用状态与非敏感配置项。
// Returns: 保存后的渠道配置。
export function saveNotificationChannels(
  channels: NotificationChannelSavePayload[],
): Promise<NotificationChannelsResponse> {
  return put<NotificationChannelsResponse>(`${NOTIFICATIONS_BASE}/admin/channels`, { channels });
}

export interface NotificationStreamHandle {
  close: () => void;
}

export interface NotificationStreamHandlers {
  onOpen: () => void;
  onMessage: (message: NotificationPushMessage) => void;
  onClose: () => void;
  onError?: () => void;
}

// 通知流地址与页面同源同协议：https 页面推导出 wss，http 页面推导出 ws。
// 身份靠浏览器自动携带的 HttpOnly Cookie，地址上不附加任何凭据参数。
export function buildNotificationStreamUrl(): string {
  const scheme = window.location.protocol === 'https:' ? 'wss:' : 'ws:';
  return `${scheme}//${window.location.host}${API_BASE}${NOTIFICATIONS_BASE}/stream`;
}

// 方法作用：解析服务端推送帧，只接受通知类消息并丢弃非法内容。
// Args: raw - WebSocket 帧文本。
// Returns: 通知推送对象；非通知消息或解析失败时返回 null。
export function parseNotificationPush(raw: unknown): NotificationPushMessage | null {
  if (typeof raw !== 'string') return null;
  try {
    const parsed = JSON.parse(raw) as Partial<NotificationPushMessage>;
    if (parsed?.type !== 'notification' || typeof parsed.notification_id !== 'string') return null;
    return {
      type: 'notification',
      notification_id: parsed.notification_id,
      kind: (parsed.kind as NotificationKind) || 'personal',
      title: typeof parsed.title === 'string' ? parsed.title : '',
      link: typeof parsed.link === 'string' ? parsed.link : '',
      created_at: typeof parsed.created_at === 'string' ? parsed.created_at : '',
      unread_count: typeof parsed.unread_count === 'number' ? parsed.unread_count : 0,
    };
  } catch {
    console.warn('通知推送不是合法 JSON，已丢弃');
    return null;
  }
}

// 心跳间隔与存活判定：
// 反向代理（frontend/nginx.conf 的 proxy_read_timeout / proxy_send_timeout 均为 600s）按
// 「读空闲」掐连接，而通知链路平时没有数据流动，空闲十分钟必被断开。客户端每 25 秒发一次
// 文本 ping，服务端（src/api/routes/notifications.py 的 notifications_stream）收到即回
// {"type":"pong"}，双向都有流量，读空闲计时被持续重置。取 25 秒是因为浏览器会限制后台
// 标签页的定时器（Chrome 约每分钟一次），即使被节流到 60 秒也远小于 600 秒。
export const NOTIFICATION_PING_INTERVAL_MS = 25000;
// 连续两个心跳周期收不到任何消息就认为链路已死（半开连接不会触发 onclose），
// 主动关闭以复用既有的指数退避重连；预留一个周期的余量给网络抖动。
export const NOTIFICATION_PONG_TIMEOUT_MS = 70000;
// 看门狗检查频率：只需比存活阈值精细，不必与心跳同频。
const NOTIFICATION_LIVENESS_CHECK_MS = 10000;

// 方法作用：建立一条通知 WebSocket 并把推送解析成消息，重连与降级由调用方负责。
// Args: handlers - 打开、收到推送、断开与错误回调。
// Returns: 连接句柄；close() 会先摘掉回调与心跳定时器，主动关闭不再触发断开回调。
export function openNotificationStream(handlers: NotificationStreamHandlers): NotificationStreamHandle {
  const socket = new WebSocket(buildNotificationStreamUrl());
  // 最近一次收到任何消息（含 pong）的时刻；半开连接只能靠它判定，不能只看 onclose。
  let lastSeenAt = Date.now();
  let pingTimer: number | null = null;
  let watchdogTimer: number | null = null;

  // 方法作用：清掉心跳与看门狗定时器，避免连接结束后留下空转的 interval。
  // Args: 无。
  // Returns: 无返回值。
  const stopTimers = (): void => {
    if (pingTimer !== null) { window.clearInterval(pingTimer); pingTimer = null; }
    if (watchdogTimer !== null) { window.clearInterval(watchdogTimer); watchdogTimer = null; }
  };

  // 方法作用：在已打开的连接上发送一次心跳，未打开时静默跳过。
  // Args: 无。
  // Returns: 无返回值；发送异常只写调试日志，交由看门狗与重连兜底。
  const sendPing = (): void => {
    if (socket.readyState !== WebSocket.OPEN) return;
    try {
      socket.send('ping');
    } catch (error) {
      console.debug('通知心跳发送失败', error);
    }
  };

  socket.onopen = () => {
    // 立刻打一次心跳，避免刚建立的连接在首个周期内完全静默。
    lastSeenAt = Date.now();
    stopTimers();
    sendPing();
    pingTimer = window.setInterval(sendPing, NOTIFICATION_PING_INTERVAL_MS);
    watchdogTimer = window.setInterval(() => {
      if (Date.now() - lastSeenAt <= NOTIFICATION_PONG_TIMEOUT_MS) return;
      console.debug('通知长连接心跳超时，主动触发重连');
      // 关闭会触发 onclose，由调用方按既有指数退避重连，这里不另做重连。
      socket.close();
    }, NOTIFICATION_LIVENESS_CHECK_MS);
    handlers.onOpen();
  };
  socket.onmessage = (event: MessageEvent) => {
    // 任何消息都算链路存活，因此必须在解析之前刷新时刻——pong 会被解析层丢弃。
    lastSeenAt = Date.now();
    const message = parseNotificationPush(event.data);
    if (message) handlers.onMessage(message);
  };
  socket.onclose = () => {
    stopTimers();
    handlers.onClose();
  };
  socket.onerror = () => { handlers.onError?.(); };
  return {
    close: () => {
      // 先摘掉回调：主动关闭不算断线，避免被当成需要重连的失败。
      stopTimers();
      socket.onopen = null;
      socket.onmessage = null;
      socket.onclose = null;
      socket.onerror = null;
      socket.close();
    },
  };
}
