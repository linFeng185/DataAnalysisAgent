import { useState, useEffect, useRef, useCallback } from 'react';
import { Drawer, List, Tag, Typography, Spin, Empty, message } from 'antd';
import { HistoryOutlined, ClockCircleOutlined } from '@ant-design/icons';
import { fetchSessions } from '../api/client';
import type { SessionInfo } from '../types';

interface Props {
  open: boolean;
  onClose: () => void;
  activeSessionId: string;
  onSelect: (session: SessionInfo) => void;
}

function relativeTime(isoStr: string): string {
  if (!isoStr) return '';
  const now = Date.now();
  const t = new Date(isoStr).getTime();
  const diff = now - t;
  const mins = Math.floor(diff / 60000);
  if (mins < 1) return '刚刚';
  if (mins < 60) return `${mins}分钟前`;
  const hours = Math.floor(mins / 60);
  if (hours < 24) return `${hours}小时前`;
  const days = Math.floor(hours / 24);
  if (days < 30) return `${days}天前`;
  return new Date(isoStr).toLocaleDateString('zh-CN');
}

export default function SessionDrawer({ open, onClose, activeSessionId, onSelect }: Props) {
  const [sessions, setSessions] = useState<SessionInfo[]>([]);
  const [loading, setLoading] = useState(false);
  const [loadingMore, setLoadingMore] = useState(false);
  const [hasMore, setHasMore] = useState(true);
  const cursorRef = useRef<string | null>(null);
  const loadingRef = useRef(false);

  const doLoad = useCallback(async (isMore = false) => {
    if (loadingRef.current) return;
    loadingRef.current = true;
    if (isMore) setLoadingMore(true); else setLoading(true);
    try {
      const res = await fetchSessions(cursorRef.current, 20);
      setSessions(prev => isMore ? [...prev, ...res.sessions] : res.sessions);
      cursorRef.current = res.next_cursor;
      setHasMore(res.has_more);
    } catch {
      message.error('加载会话列表失败');
    } finally {
      loadingRef.current = false;
      setLoading(false);
      setLoadingMore(false);
    }
  }, []);

  // 打开时初始加载
  useEffect(() => {
    if (open) {
      setSessions([]);
      cursorRef.current = null;
      setHasMore(true);
      doLoad();
    }
  }, [open, doLoad]);

  const handleScroll = useCallback(() => {
    if (loadingMore || !hasMore) return;
    const el = document.querySelector('.session-drawer-list') as HTMLElement;
    if (!el) return;
    if (el.scrollHeight - el.scrollTop - el.clientHeight < 50) {
      doLoad(true);
    }
  }, [loadingMore, hasMore, doLoad]);

  return (
    <Drawer
      title={<><HistoryOutlined style={{ marginRight: 8 }} />历史会话</>}
      open={open}
      onClose={onClose}
      width={380}
      styles={{ body: { padding: 0 } }}
    >
      <div className="session-drawer-list" onScroll={handleScroll}
        style={{ height: '100%', overflow: 'auto' }}>
        {loading && sessions.length === 0 ? (
          <div style={{ textAlign: 'center', padding: 60 }}>
            <Spin />
            <Typography.Text type="secondary" style={{ display: 'block', marginTop: 12 }}>加载会话列表...</Typography.Text>
          </div>
        ) : sessions.length === 0 && !loading ? (
          <Empty description="暂无历史会话" style={{ marginTop: 60 }} />
        ) : (
          <List
            dataSource={sessions}
            renderItem={(item) => {
              const isActive = item.session_id === activeSessionId;
              return (
                <List.Item
                  onClick={() => onSelect(item)}
                  style={{
                    cursor: 'pointer',
                    padding: '12px 16px',
                    background: isActive ? '#e6f4ff' : undefined,
                    borderLeft: isActive ? '3px solid #1677ff' : '3px solid transparent',
                    transition: 'background 0.15s',
                  }}
                  onMouseEnter={e => !isActive && (e.currentTarget.style.background = '#fafafa')}
                  onMouseLeave={e => !isActive && (e.currentTarget.style.background = '')}
                >
                  <div style={{ width: '100%' }}>
                    <div style={{
                      display: 'flex', justifyContent: 'space-between',
                      alignItems: 'center', marginBottom: 4,
                    }}>
                      <Typography.Text
                        strong ellipsis
                        style={{ fontSize: 14, maxWidth: 220, color: isActive ? '#1677ff' : undefined }}
                      >
                        {item.title || '未命名会话'}
                      </Typography.Text>
                      <Tag color={isActive ? 'blue' : 'default'} style={{ margin: 0, fontSize: 11 }}>
                        {item.datasource}
                      </Tag>
                    </div>
                    <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center' }}>
                      <Typography.Text type="secondary" style={{ fontSize: 12 }}>
                        <ClockCircleOutlined style={{ marginRight: 4 }} />
                        {relativeTime(item.last_active_at)}
                      </Typography.Text>
                      <Typography.Text type="secondary" style={{ fontSize: 12 }}>
                        {item.turn_count} 轮对话
                      </Typography.Text>
                    </div>
                  </div>
                </List.Item>
              );
            }}
          />
        )}
        {loadingMore && (
          <div style={{ textAlign: 'center', padding: 16 }}><Spin size="small" /></div>
        )}
      </div>
    </Drawer>
  );
}
