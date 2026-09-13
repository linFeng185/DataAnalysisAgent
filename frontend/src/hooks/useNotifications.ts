import { useCallback, useEffect, useRef, useState } from 'react';
import { t } from '../i18n';
import {
  deleteNotification,
  fetchNotificationInbox,
  fetchNotificationUnreadCount,
  markAllNotificationsRead,
  markNotificationRead,
  openNotificationStream,
  type NotificationItem,
  type NotificationPushMessage,
  type NotificationStreamHandle,
} from '../api/notifications';

// 断线重连的指数退避：1s、2s、4s… 直到上限 30s，退避期间不重复建连。
export const RECONNECT_BASE_DELAY_MS = 1000;
export const MAX_RECONNECT_DELAY_MS = 30000;
// 连续建连失败达到该次数后进入降级轮询；连接仍按上限退避继续重试。
export const DEGRADE_AFTER_ATTEMPTS = 3;
// 降级轮询间隔：WebSocket 不可用时每 60 秒拉一次未读数，保证角标仍会更新。
export const FALLBACK_POLL_INTERVAL_MS = 60000;
export const DEFAULT_PAGE_SIZE = 20;

export type NotificationStreamStatus = 'idle' | 'connecting' | 'open' | 'degraded';
type PushListener = (message: NotificationPushMessage) => void;
type StatusListener = (status: NotificationStreamStatus) => void;

// 全页面共用一条 WebSocket：铃铛与收件箱页各自调用 useNotifications，但不会各建一条连接。
const pushListeners = new Set<PushListener>();
const statusListeners = new Set<StatusListener>();
let streamHandle: NotificationStreamHandle | null = null;
let reconnectTimer: number | null = null;
let deferredConnectTimer: number | null = null;
let reconnectAttempt = 0;
let streamStatus: NotificationStreamStatus = 'idle';

// 方法作用：读取当前推送通道状态，供界面展示实时或轮询模式。
// Args: 无。
// Returns: 连接状态。
export function getNotificationStreamStatus(): NotificationStreamStatus {
  return streamStatus;
}

// 方法作用：更新连接状态并通知订阅者，状态变化是降级轮询的触发条件。
// Args: next - 新的连接状态。
// Returns: 无返回值。
function setStreamStatus(next: NotificationStreamStatus): void {
  if (streamStatus === next) return;
  streamStatus = next;
  statusListeners.forEach(listener => listener(next));
}

// 方法作用：按指数退避安排下一次重连，已有待执行任务时不重复安排。
// Args: 无。
// Returns: 无返回值。
function scheduleReconnect(): void {
  if (reconnectTimer !== null || pushListeners.size === 0) return;
  const delay = Math.min(RECONNECT_BASE_DELAY_MS * 2 ** reconnectAttempt, MAX_RECONNECT_DELAY_MS);
  reconnectAttempt += 1;
  // 连续失败达到阈值先切降级轮询，避免角标长时间停更；连接仍在后台继续重试。
  if (reconnectAttempt >= DEGRADE_AFTER_ATTEMPTS) setStreamStatus('degraded');
  reconnectTimer = window.setTimeout(() => {
    reconnectTimer = null;
    openStream();
  }, delay);
}

// 方法作用：建立唯一的通知 WebSocket，断开时由重连定时器接手，不影响界面渲染。
// Args: 无。
// Returns: 无返回值。
function openStream(): void {
  if (streamHandle || pushListeners.size === 0) return;
  if (typeof WebSocket === 'undefined') {
    // 浏览器不支持 WebSocket：直接降级轮询，不抛错也不影响应用其余部分。
    console.warn('浏览器不支持 WebSocket，通知改为轮询');
    setStreamStatus('degraded');
    return;
  }
  setStreamStatus('connecting');
  streamHandle = openNotificationStream({
    onOpen: () => {
      reconnectAttempt = 0;
      setStreamStatus('open');
    },
    onMessage: message => pushListeners.forEach(listener => listener(message)),
    onClose: () => {
      // 连接对象已被 close() 摘掉回调时不会走到这里；此处只处理真实断线。
      streamHandle = null;
      if (pushListeners.size > 0) scheduleReconnect();
      else setStreamStatus('idle');
    },
    onError: () => {
      // WebSocket 的错误必定伴随 close，统一在 onClose 里安排重连，避免重复计时。
      console.warn('通知推送连接异常，等待自动重连');
    },
  });
}

// 方法作用：清空连接与全部定时器，保证没有订阅者后不残留连接和重连任务。
// Args: 无。
// Returns: 无返回值。
function teardownStream(): void {
  if (deferredConnectTimer !== null) {
    window.clearTimeout(deferredConnectTimer);
    deferredConnectTimer = null;
  }
  if (reconnectTimer !== null) {
    window.clearTimeout(reconnectTimer);
    reconnectTimer = null;
  }
  reconnectAttempt = 0;
  const handle = streamHandle;
  streamHandle = null;
  handle?.close();
  setStreamStatus('idle');
}

// 方法作用：订阅通知推送与连接状态；首个订阅者建立连接，最后一个订阅者释放连接。
// Args: listener - 推送回调；onStatus - 可选的连接状态回调。
// Returns: 取消订阅函数，调用后若无其他订阅者则关闭连接。
export function subscribeNotificationStream(listener: PushListener, onStatus?: StatusListener): () => void {
  pushListeners.add(listener);
  if (onStatus) {
    statusListeners.add(onStatus);
    onStatus(streamStatus);
  }
  if (pushListeners.size === 1 && !streamHandle && deferredConnectTimer === null) {
    // 延迟一个宏任务再建连：StrictMode 的「挂载→立即卸载」会在同一轮内取消它，双挂载不会产生两条连接。
    deferredConnectTimer = window.setTimeout(() => {
      deferredConnectTimer = null;
      openStream();
    }, 0);
  }
  let released = false;
  return () => {
    if (released) return;
    released = true;
    pushListeners.delete(listener);
    if (onStatus) statusListeners.delete(onStatus);
    if (pushListeners.size === 0) teardownStream();
  };
}

export interface UseNotificationsOptions {
  // 未登录时置 false：不建连、不轮询、不发请求。
  enabled?: boolean;
  pageSize?: number;
  // 收件箱当前可见时置 true：收到推送后重拉第 1 页，用带正文的完整记录替换推送摘要。
  refreshOnPush?: boolean;
}

export interface UseNotificationsResult {
  items: NotificationItem[];
  total: number;
  page: number;
  pageSize: number;
  hasMore: boolean;
  unreadCount: number;
  unreadOnly: boolean;
  loading: boolean;
  loadingMore: boolean;
  error: string;
  streamStatus: NotificationStreamStatus;
  degraded: boolean;
  markRead: (notificationId: string) => Promise<void>;
  markAllRead: () => Promise<void>;
  remove: (notificationId: string) => Promise<void>;
  refresh: () => Promise<void>;
  loadMore: () => Promise<void>;
  setUnreadOnly: (value: boolean) => void;
}

// 方法作用：加载失败时把异常转成可展示文案，避免把 undefined 交给界面。
// Args: failure - 捕获到的异常。
// Returns: 界面文案。
function failureText(failure: unknown, fallback: string): string {
  return failure instanceof Error && failure.message ? failure.message : fallback;
}

// 方法作用：管理通知收件箱、未读角标与实时推送，供铃铛、收件箱页与发布入口共用。
// Args: options - enabled 是否启用、pageSize 每页条数、refreshOnPush 收到推送是否重拉首页。
// Returns: 列表状态、未读数与已读/删除/刷新等操作。
export function useNotifications(options: UseNotificationsOptions = {}): UseNotificationsResult {
  const enabled = options.enabled !== false;
  const pageSize = options.pageSize && options.pageSize > 0 ? options.pageSize : DEFAULT_PAGE_SIZE;

  const [items, setItems] = useState<NotificationItem[]>([]);
  const [total, setTotal] = useState(0);
  const [page, setPage] = useState(1);
  const [unreadOnly, setUnreadOnlyState] = useState(false);
  const [unreadCount, setUnreadCount] = useState(0);
  const [loading, setLoading] = useState(false);
  const [loadingMore, setLoadingMore] = useState(false);
  const [error, setError] = useState('');
  const [streamStatus, setStreamStatus] = useState<NotificationStreamStatus>(getNotificationStreamStatus());

  // 卸载后不再写状态；分页与筛选条件放 ref，供推送回调读取最新值而不重建订阅。
  const disposedRef = useRef(false);
  const pageRef = useRef(1);
  const unreadOnlyRef = useRef(false);
  const refreshOnPushRef = useRef(Boolean(options.refreshOnPush));
  refreshOnPushRef.current = Boolean(options.refreshOnPush);

  // 方法作用：拉取指定页的收件箱，append 为真时追加到列表尾部（加载更多）。
  // Args: targetPage - 目标页码；append - 是否追加。
  // Returns: 请求结束无返回值，失败时写入 error 供界面展示。
  const loadInbox = useCallback(async (targetPage: number, append: boolean): Promise<void> => {
    if (disposedRef.current) return;
    if (append) setLoadingMore(true); else setLoading(true);
    try {
      const result = await fetchNotificationInbox(unreadOnlyRef.current, targetPage, pageSize);
      if (disposedRef.current) return;
      setItems(prev => (append ? [...prev, ...result.items] : result.items));
      setTotal(result.total);
      setPage(targetPage);
      pageRef.current = targetPage;
      setError('');
    } catch (failure) {
      if (!disposedRef.current) setError(failureText(failure, t("通知列表加载失败")));
    } finally {
      if (!disposedRef.current) {
        setLoading(false);
        setLoadingMore(false);
      }
    }
  }, [pageSize]);

  // 方法作用：同步服务端未读数，角标始终以服务端值为准。
  // Args: 无。
  // Returns: 请求结束无返回值；失败时保留上一个值，等待下次轮询或推送纠正。
  const syncUnreadCount = useCallback(async (): Promise<void> => {
    try {
      const result = await fetchNotificationUnreadCount();
      if (!disposedRef.current) setUnreadCount(Math.max(0, Math.round(result.count || 0)));
    } catch {
      // 未读数刷新失败不影响列表使用，静默保留旧值。
    }
  }, []);

  const refresh = useCallback(async (): Promise<void> => {
    await Promise.all([loadInbox(1, false), syncUnreadCount()]);
  }, [loadInbox, syncUnreadCount]);

  // 推送回调通过 ref 调用，避免 loadInbox 变化时反复重建订阅。
  const loadInboxRef = useRef(loadInbox);
  loadInboxRef.current = loadInbox;

  // 方法作用：处理服务端推送：未读数取推送值，新通知插到列表头部。
  // Args: message - 推送消息（只含标识与摘要）。
  // Returns: 无返回值。
  const handlePush = useCallback((message: NotificationPushMessage): void => {
    // 未读数以服务端下发的 unread_count 为准，不做本地自增：多标签页或共享账号下本地自增会与真实值漂移。
    setUnreadCount(Math.max(0, Math.round(message.unread_count)));
    setItems(prev => {
      if (prev.some(item => item.id === message.notification_id)) return prev;
      const incoming: NotificationItem = {
        id: message.notification_id,
        kind: message.kind,
        title: message.title,
        body: '',
        link: message.link,
        is_read: false,
        created_at: message.created_at,
      };
      return [incoming, ...prev];
    });
    // 收件箱停在第 1 页时重拉，用带正文的完整记录替换只有摘要的推送项。
    if (refreshOnPushRef.current && pageRef.current === 1) void loadInboxRef.current(1, false);
  }, []);

  const handleStatus = useCallback((status: NotificationStreamStatus): void => {
    if (!disposedRef.current) setStreamStatus(status);
  }, []);

  // 订阅实时推送：卸载（含 StrictMode 的模拟卸载）由 subscribe 返回的清理函数释放连接与定时器。
  useEffect(() => {
    if (!enabled) return;
    return subscribeNotificationStream(handlePush, handleStatus);
  }, [enabled, handlePush, handleStatus]);

  // 首次进入拉取收件箱首页与未读数。
  useEffect(() => {
    if (!enabled) return;
    disposedRef.current = false;
    void refresh();
    return () => { disposedRef.current = true; };
  }, [enabled, refresh]);

  // 降级：推送通道未连通（浏览器不支持、后端未就绪或多次握手失败）时按固定间隔轮询未读数。
  useEffect(() => {
    if (!enabled) return;
    if (streamStatus === 'open') return;
    const timer = window.setInterval(() => { void syncUnreadCount(); }, FALLBACK_POLL_INTERVAL_MS);
    return () => window.clearInterval(timer);
  }, [enabled, streamStatus, syncUnreadCount]);

  // 方法作用：标记单条通知已读并同步未读数。
  // Args: notificationId - 通知标识。
  // Returns: 请求结束无返回值；失败时写入 error。
  const markRead = useCallback(async (notificationId: string): Promise<void> => {
    try {
      await markNotificationRead(notificationId);
      if (disposedRef.current) return;
      setItems(prev => prev.map(item => (item.id === notificationId ? { ...item, is_read: true } : item)));
      await syncUnreadCount();
    } catch (failure) {
      if (!disposedRef.current) setError(failureText(failure, t("标记已读失败")));
    }
  }, [syncUnreadCount]);

  // 方法作用：全部标记已读并清零角标。
  // Args: 无。
  // Returns: 请求结束无返回值；失败时写入 error。
  const markAllRead = useCallback(async (): Promise<void> => {
    try {
      await markAllNotificationsRead();
      if (disposedRef.current) return;
      setItems(prev => prev.map(item => ({ ...item, is_read: true })));
      setUnreadCount(0);
      await syncUnreadCount();
    } catch (failure) {
      if (!disposedRef.current) setError(failureText(failure, t("全部标记已读失败")));
    }
  }, [syncUnreadCount]);

  // 方法作用：软删除单条通知并从列表移除，同步未读数。
  // Args: notificationId - 通知标识。
  // Returns: 请求结束无返回值；失败时写入 error。
  const remove = useCallback(async (notificationId: string): Promise<void> => {
    try {
      await deleteNotification(notificationId);
      if (disposedRef.current) return;
      setItems(prev => prev.filter(item => item.id !== notificationId));
      setTotal(prev => Math.max(0, prev - 1));
      await syncUnreadCount();
    } catch (failure) {
      if (!disposedRef.current) setError(failureText(failure, t("删除通知失败")));
    }
  }, [syncUnreadCount]);

  const loadMore = useCallback(async (): Promise<void> => {
    if (loading || loadingMore) return;
    await loadInbox(pageRef.current + 1, true);
  }, [loadInbox, loading, loadingMore]);

  // 方法作用：切换「只看未读」并回到第 1 页重新加载。
  // Args: value - 是否只看未读。
  // Returns: 无返回值。
  const setUnreadOnly = useCallback((value: boolean): void => {
    unreadOnlyRef.current = value;
    setUnreadOnlyState(value);
    void loadInboxRef.current(1, false);
  }, []);

  return {
    items,
    total,
    page,
    pageSize,
    hasMore: items.length < total,
    unreadCount,
    unreadOnly,
    loading,
    loadingMore,
    error,
    streamStatus,
    degraded: streamStatus !== 'open',
    markRead,
    markAllRead,
    remove,
    refresh,
    loadMore,
    setUnreadOnly,
  };
}
