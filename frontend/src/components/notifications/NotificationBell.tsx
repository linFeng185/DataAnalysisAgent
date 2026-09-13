import { t, useLanguage } from '../../i18n';
import { useCallback, useState } from 'react';
import { Badge, Button, Drawer, Space } from 'antd';
import { BellOutlined, NotificationOutlined, SettingOutlined } from '@ant-design/icons';
import { Link, useNavigate } from 'react-router-dom';
import NotificationList from './NotificationList';
import NotificationPublisher from './NotificationPublisher';
import NotificationChannels from './NotificationChannels';
import { useNotifications } from '../../hooks/useNotifications';
import { useAuth } from '../../hooks/AuthContext';
import type { NotificationItem } from '../../api/notifications';

// 角标上限：超过 99 显示 99+，避免顶栏被长数字撑开。
export const UNREAD_BADGE_OVERFLOW = 99;

export interface NotificationBellProps {
  // 认证完成后由调用方置 true；未登录时不建连、不轮询。
  enabled?: boolean;
  // 传入后抽屉底部显示「查看全部通知」入口，指向收件箱整页（路由由 App 注册）。
  viewAllPath?: string;
}

// 方法作用：渲染顶栏通知铃铛、未读角标与收件箱抽屉，并按角色提供发布与渠道配置入口。
// Args: enabled - 是否启用实时推送；viewAllPath - 可选的「查看全部」路由路径。
// Returns: 铃铛与抽屉 React 元素。
export default function NotificationBell({ enabled = true, viewAllPath }: NotificationBellProps) {
  useLanguage();
  const navigate = useNavigate();
  const { user } = useAuth();
  const [drawerOpen, setDrawerOpen] = useState(false);
  const [publisherOpen, setPublisherOpen] = useState(false);
  const [channelsOpen, setChannelsOpen] = useState(false);
  // 抽屉可见时收到推送直接重拉首页，用带正文的完整记录替换推送摘要。
  const notifications = useNotifications({ enabled, refreshOnPush: drawerOpen });

  const canPublish = ['super_admin', 'tenant_admin'].includes(user?.role || '');
  const canManageChannels = user?.role === 'tenant_admin';

  // 方法作用：点击单条通知：先标记已读，再按链接跳转并关闭抽屉。
  // Args: item - 被点击的通知。
  // Returns: 无返回值。
  const openItem = useCallback((item: NotificationItem): void => {
    void notifications.markRead(item.id);
    if (item.link) {
      navigate(item.link);
      setDrawerOpen(false);
    }
  }, [navigate, notifications]);

  return (
    <>
      <Badge
        count={notifications.unreadCount}
        overflowCount={UNREAD_BADGE_OVERFLOW}
        showZero={false}
        size="small"
        offset={[-2, 2]}
      >
        <Button
          className="notification-bell"
          type="text"
          aria-label={t("通知")}
          icon={<BellOutlined />}
          onClick={() => setDrawerOpen(true)}
          style={{ color: '#fff' }}
        />
      </Badge>
      <Drawer
        className="notification-drawer"
        title={t("通知中心")}
        placement="right"
        width={420}
        open={drawerOpen}
        onClose={() => setDrawerOpen(false)}
        styles={{ body: { padding: 12, display: 'flex', flexDirection: 'column' } }}
      >
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
          toolbarExtra={(
            <Space size={8}>
              {canPublish && (
                <Button size="small" icon={<NotificationOutlined />} onClick={() => setPublisherOpen(true)}>
                  {t("发布通知")}
                </Button>
              )}
              {canManageChannels && (
                <Button size="small" icon={<SettingOutlined />} onClick={() => setChannelsOpen(true)}>
                  {t("渠道配置")}
                </Button>
              )}
            </Space>
          )}
        />
        {viewAllPath && (
          <div style={{ paddingTop: 8, textAlign: 'center' }}>
            <Link to={viewAllPath} onClick={() => setDrawerOpen(false)}>{t("查看全部通知")}</Link>
          </div>
        )}
      </Drawer>
      <NotificationPublisher open={publisherOpen} onClose={() => setPublisherOpen(false)}
        onPublished={() => void notifications.refresh()} />
      <NotificationChannels open={channelsOpen} onClose={() => setChannelsOpen(false)} />
    </>
  );
}
