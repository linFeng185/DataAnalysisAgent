import { t, useLanguage } from '../i18n';
import { useCallback, useState } from 'react';
import { Button, Card, Space, Typography } from 'antd';
import { BellOutlined, NotificationOutlined, SettingOutlined } from '@ant-design/icons';
import { useNavigate } from 'react-router-dom';
import NotificationList from '../components/notifications/NotificationList';
import NotificationPublisher from '../components/notifications/NotificationPublisher';
import NotificationChannels from '../components/notifications/NotificationChannels';
import { useNotifications } from '../hooks/useNotifications';
import { useAuth } from '../hooks/AuthContext';
import type { NotificationItem } from '../api/notifications';

// 方法作用：渲染通知中心整页收件箱，与顶栏抽屉复用同一列表组件。
// Args: 无，读取当前登录身份与通知状态。
// Returns: 通知中心页面 React 元素。
export default function NotificationsPage() {
  useLanguage();
  const navigate = useNavigate();
  const { user } = useAuth();
  const [publisherOpen, setPublisherOpen] = useState(false);
  const [channelsOpen, setChannelsOpen] = useState(false);
  // 整页始终可见，收到推送后重拉首页补齐正文。
  const notifications = useNotifications({ refreshOnPush: true });

  const canPublish = ['super_admin', 'tenant_admin'].includes(user?.role || '');
  const canManageChannels = user?.role === 'tenant_admin';

  // 方法作用：点击单条通知：先标记已读，再按链接跳转。
  // Args: item - 被点击的通知。
  // Returns: 无返回值。
  const openItem = useCallback((item: NotificationItem): void => {
    void notifications.markRead(item.id);
    if (item.link) navigate(item.link);
  }, [navigate, notifications]);

  return (
    <div className="notification-page">
      <div className="app-page">
        <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center', gap: 12, flexWrap: 'wrap', marginBottom: 12 }}>
          <Space size={8} align="center">
            <BellOutlined />
            <Typography.Title level={4} style={{ margin: 0 }}>{t("通知中心")}</Typography.Title>
            <Typography.Text type="secondary">{t("未读 {value0} 条", { value0: notifications.unreadCount })}</Typography.Text>
          </Space>
          <Space size={8} wrap>
            {canPublish && (
              <Button type="primary" icon={<NotificationOutlined />} onClick={() => setPublisherOpen(true)}>
                {t("发布通知")}
              </Button>
            )}
            {canManageChannels && (
              <Button icon={<SettingOutlined />} onClick={() => setChannelsOpen(true)}>{t("渠道配置")}</Button>
            )}
          </Space>
        </div>
        <Card styles={{ body: { padding: 12 } }}>
          <NotificationList
            items={notifications.items}
            total={notifications.total}
            loading={notifications.loading}
            loadingMore={notifications.loadingMore}
            error={notifications.error}
            unreadOnly={notifications.unreadOnly}
            hasMore={notifications.hasMore}
            streamStatus={notifications.streamStatus}
            onUnreadOnlyChange={notifications.setUnreadOnly}
            onOpenItem={openItem}
            onRemove={item => void notifications.remove(item.id)}
            onMarkAllRead={() => void notifications.markAllRead()}
            onRefresh={() => void notifications.refresh()}
            onLoadMore={() => void notifications.loadMore()}
          />
        </Card>
      </div>
      <NotificationPublisher open={publisherOpen} onClose={() => setPublisherOpen(false)}
        onPublished={() => void notifications.refresh()} />
      <NotificationChannels open={channelsOpen} onClose={() => setChannelsOpen(false)} />
    </div>
  );
}
