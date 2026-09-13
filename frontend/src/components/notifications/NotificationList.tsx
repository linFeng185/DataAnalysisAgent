import { t, useLanguage } from '../../i18n';
import type { ReactNode } from 'react';
import { Alert, Button, Empty, List, Popconfirm, Segmented, Space, Spin, Tag, Typography } from 'antd';
import { CheckOutlined, DeleteOutlined, ReloadOutlined } from '@ant-design/icons';
import dayjs from 'dayjs';
import type { NotificationItem, NotificationKind } from '../../api/notifications';
import type { NotificationStreamStatus } from '../../hooks/useNotifications';

// 四类通知的标签配色：平台最醒目，个人通知保持中性。
const KIND_COLORS: Record<NotificationKind, string> = {
  platform: 'red',
  tenant: 'blue',
  group: 'purple',
  personal: 'default',
};

// 方法作用：把通知类型转成界面标签文案。
// Args: kind - 通知类型。
// Returns: 中文标签文案。
export function notificationKindLabel(kind: NotificationKind): string {
  if (kind === 'platform') return t("平台通知");
  if (kind === 'tenant') return t("租户通知");
  if (kind === 'group') return t("用户群通知");
  if (kind === 'personal') return t("个人通知");
  return t("通知");
}

// 方法作用：把创建时间格式化为列表可直接展示的文本，近期用相对时间。
// Args: iso - 服务端返回的时间字符串。
// Returns: 时间文案；为空时返回空串。
export function formatNotificationTime(iso: string): string {
  if (!iso) return '';
  const timestamp = new Date(iso).getTime();
  if (Number.isNaN(timestamp)) return '';
  const diffMinutes = Math.floor((Date.now() - timestamp) / 60000);
  if (diffMinutes < 1) return t("刚刚");
  if (diffMinutes < 60) return t("{value0}分钟前", { value0: diffMinutes });
  const diffHours = Math.floor(diffMinutes / 60);
  if (diffHours < 24) return t("{value0}小时前", { value0: diffHours });
  const diffDays = Math.floor(diffHours / 24);
  if (diffDays < 30) return t("{value0}天前", { value0: diffDays });
  return dayjs(iso).format('YYYY-MM-DD HH:mm');
}

export interface NotificationListProps {
  items: NotificationItem[];
  total: number;
  loading: boolean;
  loadingMore: boolean;
  error: string;
  unreadOnly: boolean;
  hasMore: boolean;
  streamStatus: NotificationStreamStatus;
  onUnreadOnlyChange: (value: boolean) => void;
  onOpenItem: (item: NotificationItem) => void;
  onRemove: (item: NotificationItem) => void;
  onMarkAllRead: () => void;
  onRefresh: () => void;
  onLoadMore: () => void;
  // 工具栏右侧的额外按钮（发布通知、渠道配置等），由调用方决定是否传入。
  toolbarExtra?: ReactNode;
}

// 方法作用：渲染通知收件箱列表，供顶栏抽屉与通知页共用同一套交互。
// Args: items/total/loading/loadingMore/error/unreadOnly/hasMore/streamStatus - 列表状态；onXxx - 各交互回调；toolbarExtra - 工具栏附加按钮。
// Returns: 收件箱列表 React 元素。
export default function NotificationList(props: NotificationListProps) {
  useLanguage();
  const {
    items, total, loading, loadingMore, error, unreadOnly, hasMore, streamStatus,
    onUnreadOnlyChange, onOpenItem, onRemove, onMarkAllRead, onRefresh, onLoadMore, toolbarExtra,
  } = props;

  return (
    <div style={{ display: 'flex', flexDirection: 'column', height: '100%' }}>
      <div style={{ display: 'flex', alignItems: 'center', justifyContent: 'space-between', gap: 8, flexWrap: 'wrap', padding: '0 0 12px' }}>
        <Segmented
          value={unreadOnly ? 'unread' : 'all'}
          onChange={value => onUnreadOnlyChange(value === 'unread')}
          options={[{ label: t("全部"), value: 'all' }, { label: t("未读"), value: 'unread' }]}
        />
        <Space size={8} wrap>
          {toolbarExtra}
          <Button size="small" icon={<ReloadOutlined />} onClick={onRefresh} disabled={loading}>{t("刷新")}</Button>
          <Button size="small" icon={<CheckOutlined />} onClick={onMarkAllRead}
            disabled={loading || items.length === 0}>{t("全部已读")}</Button>
        </Space>
      </div>
      {/* 推送通道异常时明确告知已降级轮询，避免用户以为通知不再更新 */}
      {streamStatus !== 'open' && (
        <Typography.Text type="secondary" style={{ display: 'block', marginBottom: 8, fontSize: 12 }}>
          {t("实时推送暂不可用，已切换为定时刷新，通知仍会到达")}
        </Typography.Text>
      )}
      {error && (
        <Alert type="error" showIcon message={error} style={{ marginBottom: 12 }}
          action={<Button size="small" onClick={onRefresh}>{t("重试")}</Button>} />
      )}
      {loading && items.length === 0 ? (
        <div style={{ textAlign: 'center', padding: 48 }}>
          <Spin />
          <Typography.Text type="secondary" style={{ display: 'block', marginTop: 12 }}>{t("加载通知...")}</Typography.Text>
        </div>
      ) : items.length === 0 ? (
        <Empty description={unreadOnly ? t("暂无未读通知") : t("暂无通知")} style={{ marginTop: 48 }} />
      ) : (
        <>
          <div style={{ flex: 1, overflow: 'auto', minHeight: 0 }}>
            <List
              dataSource={items}
              renderItem={item => (
                <List.Item
                  onClick={() => onOpenItem(item)}
                  style={{
                    cursor: 'pointer',
                    padding: '12px 12px',
                    background: item.is_read ? undefined : '#e6f4ff',
                    borderLeft: item.is_read ? '3px solid transparent' : '3px solid #1677ff',
                    transition: 'background 0.15s',
                  }}
                >
                  <div style={{ width: '100%' }}>
                    <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center', gap: 8, marginBottom: 4 }}>
                      <Space size={6} style={{ minWidth: 0 }}>
                        {!item.is_read && <Tag color="processing" style={{ margin: 0, fontSize: 11 }}>{t("未读")}</Tag>}
                        <Tag color={KIND_COLORS[item.kind] || 'default'} style={{ margin: 0, fontSize: 11 }}>
                          {notificationKindLabel(item.kind)}
                        </Tag>
                        <Typography.Text strong={!item.is_read} ellipsis style={{ fontSize: 14, maxWidth: 200 }}>
                          {item.title || t("无标题通知")}
                        </Typography.Text>
                      </Space>
                      <Space size={4}>
                        <Typography.Text type="secondary" style={{ fontSize: 12, whiteSpace: 'nowrap' }}>
                          {formatNotificationTime(item.created_at)}
                        </Typography.Text>
                        <Popconfirm
                          title={t("确认删除该通知？")}
                          okText={t("删除")}
                          cancelText={t("取消")}
                          onConfirm={() => onRemove(item)}
                        >
                          <Button
                            type="text"
                            size="small"
                            aria-label={t("删除通知")}
                            icon={<DeleteOutlined />}
                            onClick={event => event.stopPropagation()}
                          />
                        </Popconfirm>
                      </Space>
                    </div>
                    {item.body && (
                      <Typography.Paragraph type="secondary" ellipsis={{ rows: 2 }} style={{ marginBottom: 0, fontSize: 12 }}>
                        {item.body}
                      </Typography.Paragraph>
                    )}
                  </div>
                </List.Item>
              )}
            />
          </div>
          <div style={{ paddingTop: 12, textAlign: 'center' }}>
            {hasMore ? (
              <Button block onClick={onLoadMore} loading={loadingMore}>{t("加载更多")}</Button>
            ) : (
              <Typography.Text type="secondary" style={{ fontSize: 12 }}>
                {t("已显示全部 {value0} 条通知", { value0: total })}
              </Typography.Text>
            )}
          </div>
        </>
      )}
    </div>
  );
}
