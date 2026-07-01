import { useState, useRef, useEffect, useCallback } from 'react';
import { Input, Select, Tag, Typography, Space, Tooltip, message, Card, Button, Spin } from 'antd';
import {
  SendOutlined, RobotOutlined, ClearOutlined,
  LoadingOutlined, ThunderboltOutlined, ReadOutlined,
  BulbOutlined, BarChartOutlined, RiseOutlined,
  HistoryOutlined,
} from '@ant-design/icons';
import { useChat, ChatTurn } from '../hooks/useChat';
import { get, fetchSession } from '../api/client';
import type { DatasourceConfig, SessionInfo } from '../types';
import ProgressBar from '../components/ProgressBar';
import ReasoningPanel from '../components/ReasoningPanel';
import ResultCard from '../components/ResultCard';
import SqlPanel from '../components/SqlPanel';
import SessionDrawer from '../components/SessionDrawer';

const SUGGESTIONS = [
  { icon: <BarChartOutlined />, text: '本月各产品的销售额排名是怎样的？', color: '#1677ff' },
  { icon: <RiseOutlined />, text: '最近一周活跃用户数变化趋势如何？', color: '#52c41a' },
  { icon: <BulbOutlined />, text: '上个月订单量和金额对比去年同期？', color: '#722ed1' },
];

const DS_STORAGE_KEY = 'selected_datasource';

function loadDs(): string {
  try { return sessionStorage.getItem(DS_STORAGE_KEY) || ''; } catch { return ''; }
}
function saveDs(name: string) {
  try { sessionStorage.setItem(DS_STORAGE_KEY, name); } catch { /* */ }
}

export default function ChatPage() {
  const [query, setQuery] = useState('');
  const [ds, setDs] = useState(loadDs);
  const [datasources, setDatasources] = useState<DatasourceConfig[]>([]);
  const [drawerOpen, setDrawerOpen] = useState(false);
  const {
    turns, loading, sessionId, send, cancel, clearSession,
    restoreTurns, loadMoreTurns, hasMore, loadingMore,
  } = useChat();
  const bottomRef = useRef<HTMLDivElement>(null);
  const msgAreaRef = useRef<HTMLDivElement>(null);
  const prevTurnsLen = useRef(turns.length);

  useEffect(() => {
    get<{ datasources: DatasourceConfig[] }>('/datasources')
      .then(data => {
        const list = data.datasources || [];
        setDatasources(list);
        if (list.length > 0 && !ds) { setDs(list[0].name); saveDs(list[0].name); }
      })
      .catch(() => message.warning('无法加载数据源列表'));
  }, []);

  // 新消息时自动滚到底部
  useEffect(() => {
    if (turns.length > prevTurnsLen.current) {
      bottomRef.current?.scrollIntoView({ behavior: 'smooth' });
    }
    prevTurnsLen.current = turns.length;
  }, [turns]);

  // 恢复历史会话后滚到底部
  useEffect(() => {
    if (msgAreaRef.current && turns.length > 0) {
      msgAreaRef.current.scrollTop = msgAreaRef.current.scrollHeight;
    }
  }, [sessionId]);

  const handleSend = () => {
    if (!query.trim() || loading) return;
    if (!ds) { message.warning('请选择数据源'); return; }
    send(query, ds);
    setQuery('');
  };

  // 滚动到顶部时加载更多历史轮次
  const handleMsgScroll = useCallback(() => {
    const el = msgAreaRef.current;
    if (!el || loadingMore || !hasMore) return;
    if (el.scrollTop <= 20) {
      const prevHeight = el.scrollHeight;
      loadMoreTurns(sessionId, ds).then(() => {
        requestAnimationFrame(() => {
          if (msgAreaRef.current) {
            msgAreaRef.current.scrollTop = msgAreaRef.current.scrollHeight - prevHeight;
          }
        });
      });
    }
  }, [loadingMore, hasMore, sessionId, ds, loadMoreTurns]);

  // 选择历史会话
  const handleSelectSession = async (session: SessionInfo) => {
    setDrawerOpen(false);
    try {
      const detail = await fetchSession(session.session_id);
      restoreTurns(detail.turns, session.session_id, session.datasource, detail.latest_state);
      setDs(session.datasource);
      message.success('已恢复会话');
    } catch {
      message.error('恢复会话失败');
    }
  };

  const dsOptions = datasources.map(d => ({ value: d.name, label: `${d.name} (${d.dialect})` }));
  const lastAnalysis = turns.length > 0
    ? turns[turns.length - 1].finalResult?.analysis as Record<string, unknown> | undefined
    : undefined;
  const isEmpty = turns.length === 0 && !loading;

  return (
    <div style={{ height: 'calc(100vh - 64px)', display: 'flex', flexDirection: 'column', maxWidth: 960, margin: '0 auto' }}>
      {/* 消息区 */}
      <div ref={msgAreaRef} onScroll={handleMsgScroll}
        style={{ flex: 1, overflow: 'auto', padding: isEmpty ? 0 : '16px 20px 0' }}>
        {/* 加载更多指示器 */}
        {loadingMore && (
          <div style={{ textAlign: 'center', padding: '8px 0' }}>
            <Spin size="small" />
            <Typography.Text type="secondary" style={{ marginLeft: 8, fontSize: 12 }}>
              加载更早的对话...
            </Typography.Text>
          </div>
        )}

        {isEmpty ? (
          /* 欢迎页 */
          <div style={{
            display: 'flex', flexDirection: 'column', alignItems: 'center',
            justifyContent: 'center', height: '100%', padding: '0 40px',
          }}>
            <div style={{
              width: 72, height: 72, borderRadius: 20,
              background: 'linear-gradient(135deg, #1677ff 0%, #722ed1 100%)',
              display: 'flex', alignItems: 'center', justifyContent: 'center',
              marginBottom: 20, boxShadow: '0 8px 24px rgba(22,119,255,0.3)',
            }}>
              <RobotOutlined style={{ fontSize: 36, color: '#fff' }} />
            </div>
            <Typography.Title level={3} style={{ marginBottom: 4, fontWeight: 700 }}>
              数据分析智能体
            </Typography.Title>
            <Typography.Text type="secondary" style={{ fontSize: 15, marginBottom: 36 }}>
              用自然语言提问，自动生成 SQL 并完成数据洞察
            </Typography.Text>

            <div style={{ display: 'grid', gridTemplateColumns: 'repeat(auto-fit, minmax(260px, 1fr))', gap: 12, width: '100%', maxWidth: 600 }}>
              {SUGGESTIONS.map((s, i) => (
                <Card
                  key={i} hoverable size="small"
                  onClick={() => setQuery(s.text)}
                  style={{ cursor: 'pointer', borderRadius: 12, border: '1px solid #f0f0f0' }}
                  styles={{ body: { padding: '14px 16px' } }}>
                  <Space>
                    <span style={{ color: s.color, fontSize: 18 }}>{s.icon}</span>
                    <Typography.Text style={{ fontSize: 13, lineHeight: 1.5 }}>{s.text}</Typography.Text>
                  </Space>
                </Card>
              ))}
            </div>

            <Typography.Text type="secondary" style={{ marginTop: 40, fontSize: 12 }}>
              在下方输入框中选择数据源并输入问题即可开始分析
            </Typography.Text>
          </div>
        ) : (
          /* 对话列表 */
          turns.map((t) => <TurnBubble key={t.id} turn={t} />)
        )}
        <div ref={bottomRef} />
      </div>

      {/* 输入栏 */}
      <div style={{
        padding: '12px 20px 16px', borderTop: '1px solid #ececec',
        background: 'linear-gradient(180deg, rgba(255,255,255,0) 0%, #fff 20%)',
      }}>
        {lastAnalysis && (
          <div style={{ marginBottom: 8, display: 'flex', flexWrap: 'wrap', gap: 4 }}>
            {(lastAnalysis.follow_up_questions as string[])?.slice(0, 4).map((q, i) => (
              <Tag key={i} color="processing" style={{ cursor: 'pointer', fontSize: 12, borderRadius: 6 }}
                onClick={() => send(q, ds)}>{q}</Tag>
            ))}
          </div>
        )}

        <div style={{
          display: 'flex', gap: 10, alignItems: 'flex-end',
          background: '#fff', borderRadius: 16, padding: '8px 8px 8px 16px',
          border: '1px solid #e8e8e8', boxShadow: '0 2px 8px rgba(0,0,0,0.04)',
          transition: 'box-shadow 0.2s',
        }}>
          <Input.TextArea
            value={query} onChange={e => setQuery(e.target.value)}
            onPressEnter={e => { if (!e.shiftKey) { e.preventDefault(); handleSend(); } }}
            placeholder="输入分析问题，Shift+Enter 换行"
            autoSize={{ minRows: 1, maxRows: 5 }}
            disabled={loading}
            style={{
              flex: 1, border: 'none', boxShadow: 'none', resize: 'none',
              fontSize: 14, lineHeight: 1.6, padding: '4px 0',
            }}
          />
          <Select
            value={ds || undefined} onChange={v => { setDs(v); saveDs(v); }}
            options={dsOptions} disabled={loading}
            size="small" style={{ minWidth: 130, flexShrink: 0 }}
            dropdownMatchSelectWidth={false}
          />
          <Space size={4} style={{ flexShrink: 0 }}>
            {loading ? (
              <Button size="small" onClick={cancel} style={{ borderRadius: 10 }}>取消</Button>
            ) : null}
            <Button type="primary" icon={<SendOutlined />} onClick={handleSend} loading={loading}
              disabled={!query.trim()}
              style={{ borderRadius: 10, minWidth: 40, boxShadow: loading ? 'none' : '0 2px 8px rgba(22,119,255,0.3)' }}>
              {!loading && '发送'}
            </Button>
          </Space>
        </div>

        {/* 工具栏 */}
        <div style={{ display: 'flex', justifyContent: 'space-between', marginTop: 6, padding: '0 4px' }}>
          <Space size={8}>
            {sessionId && (
              <Typography.Text type="secondary" style={{ fontSize: 11 }}>
                会话: {sessionId.slice(-8)}
              </Typography.Text>
            )}
          </Space>
          <Space size={4}>
            <Tooltip title="历史会话列表">
              <Button size="small" icon={<HistoryOutlined />}
                onClick={() => setDrawerOpen(true)}
                style={{ fontSize: 12 }}>历史会话</Button>
            </Tooltip>
            <Tooltip title="清空会话，开始新对话">
              <Button size="small" type="text" icon={<ClearOutlined />}
                onClick={clearSession} disabled={loading}
                style={{ fontSize: 12, color: '#999' }}>清空</Button>
            </Tooltip>
          </Space>
        </div>
      </div>

      <SessionDrawer
        open={drawerOpen}
        onClose={() => setDrawerOpen(false)}
        activeSessionId={sessionId}
        onSelect={handleSelectSession}
      />
    </div>
  );
}

/* 单条消息气泡 */
function TurnBubble({ turn }: { turn: ChatTurn }) {
  const { userQuery, assistant, finalResult, status } = turn;
  const hasResult = status === 'done' && !!finalResult;
  const hasError = status === 'error';
  const isStreaming = status === 'streaming';
  const isRestored = status === 'done' && !finalResult;
  const activeSkills = (finalResult?.activated_skills as string[]) || [];
  const activeKnowledge = (finalResult?.activated_knowledge as string) || '';

  return (
    <div style={{ marginBottom: 18 }}>
      {/* 用户消息 */}
      <div style={{ display: 'flex', justifyContent: 'flex-end', marginBottom: 10 }}>
        <div style={{
          background: 'linear-gradient(135deg, #1677ff 0%, #4096ff 100%)',
          color: '#fff', padding: '10px 18px', borderRadius: '18px 18px 4px 18px',
          maxWidth: '75%', fontSize: 14, lineHeight: 1.65,
          boxShadow: '0 2px 8px rgba(22,119,255,0.2)',
        }}>
          {userQuery}
        </div>
      </div>

      {/* 助手消息 */}
      <div style={{ display: 'flex', gap: 10 }}>
        <div style={{
          width: 32, height: 32, borderRadius: 10, flexShrink: 0,
          background: 'linear-gradient(135deg, #52c41a 0%, #73d13d 100%)',
          display: 'flex', alignItems: 'center', justifyContent: 'center',
          boxShadow: '0 2px 6px rgba(82,196,26,0.25)',
        }}>
          <RobotOutlined style={{ color: '#fff', fontSize: 16 }} />
        </div>
        <div style={{ flex: 1, minWidth: 0 }}>
          {(activeSkills.length > 0 || activeKnowledge) && hasResult && (
            <div style={{
              marginBottom: 6, padding: '4px 10px',
              background: 'linear-gradient(135deg, #f6ffed 0%, #f9f0ff 100%)',
              borderRadius: 8, display: 'flex', gap: 12, alignItems: 'center',
              border: '1px solid #e8f5e9',
            }}>
              {activeSkills.length > 0 && (
                <span style={{ fontSize: 11 }}>
                  <ThunderboltOutlined style={{ color: '#1677ff', marginRight: 3 }} />
                  {activeSkills.map((s, i) => (
                    <Tag key={i} color="blue" style={{ marginLeft: 2, fontSize: 10, lineHeight: '18px' }}>{s}</Tag>
                  ))}
                </span>
              )}
              {activeKnowledge && (
                <span style={{ fontSize: 11 }}>
                  <ReadOutlined style={{ color: '#722ed1', marginRight: 3 }} />
                  <Typography.Text type="secondary" style={{ fontSize: 11, maxWidth: 160 }} ellipsis>
                    {activeKnowledge.slice(0, 60)}
                  </Typography.Text>
                </span>
              )}
            </div>
          )}

          <ProgressBar nodes={assistant.progressNodes} />

          {isStreaming && <ReasoningPanel reasoning={assistant.reasoning} />}

          {isStreaming && assistant.tokens && (
            <div style={{
              background: '#fafafa', padding: '12px 16px', borderRadius: 12,
              lineHeight: 1.7, whiteSpace: 'pre-wrap', fontSize: 14,
              border: '1px solid #f0f0f0',
            }}>
              {assistant.tokens}<LoadingOutlined style={{ color: '#1677ff', marginLeft: 2 }} />
            </div>
          )}

          {isStreaming && !assistant.reasoning && !assistant.tokens
            && Object.keys(assistant.progressNodes).length === 0 && (
            <div style={{
              display: 'flex', alignItems: 'center', gap: 8, padding: '10px 16px',
              color: '#999', fontSize: 13,
            }}>
              <LoadingOutlined style={{ color: '#1677ff' }} /> 正在连接分析服务...
            </div>
          )}

          {hasResult && (
            <ResultCard sql={assistant.sql} reasoning={assistant.reasoning}
              tokens={assistant.tokens} finalResult={finalResult}
              validationErrors={assistant.validationErrors} />
          )}

          {/* 恢复的历史会话 — 含 SQL 折叠面板 */}
          {isRestored && (
            <Card size="small" style={{ background: '#fafafa', borderRadius: 12 }}>
              {assistant.tokens && (
                <Typography.Text style={{ fontSize: 13, whiteSpace: 'pre-wrap', display: 'block', marginBottom: assistant.sql ? 8 : 0 }}>
                  {assistant.tokens}
                </Typography.Text>
              )}
              {assistant.sql && (
                <SqlPanel sqlCode={assistant.sql} />
              )}
              {!assistant.tokens && !assistant.sql && (
                <Typography.Text type="secondary" style={{ fontSize: 12 }}>（历史对话记录）</Typography.Text>
              )}
            </Card>
          )}

          {hasError && (
            <Card size="small" style={{
              background: '#fff2f0', borderColor: '#ffccc7', borderRadius: 12,
            }}>
              <Typography.Text type="danger" style={{ fontSize: 13 }}>
                错误: {turn.errorMessage}
              </Typography.Text>
            </Card>
          )}
        </div>
      </div>
    </div>
  );
}
