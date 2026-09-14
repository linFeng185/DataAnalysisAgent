import { t, useLanguage } from '../i18n';
import { useState, useEffect } from 'react';
import { useNavigate } from 'react-router-dom';
import { Card, Table, Tag, Input, Select, Empty, message, Space, Button, Tooltip, Typography } from 'antd';
import { PlayCircleOutlined } from '@ant-design/icons';
import { fetchSession, get } from '../api/client';
import type { HistoryItem, DatasourceConfig } from '../types';

// 方法作用：展示查询历史并把可关联会话恢复到对话页。
// Args: 无。
// Returns: 查询历史页面。
export default function HistoryPage() {
  useLanguage();
  const navigate = useNavigate();
  const [items, setItems] = useState<HistoryItem[]>([]);
  const [datasources, setDatasources] = useState<DatasourceConfig[]>([]);
  const [dsFilter, setDsFilter] = useState<string | undefined>();
  const [search, setSearch] = useState('');
  const [loading, setLoading] = useState(false);
  const [replayId, setReplayId] = useState<string | null>(null);

  useEffect(() => {
    get<{ datasources: DatasourceConfig[] }>('/datasources')
      .then(data => setDatasources(data.datasources || []))
      .catch(() => {});
  }, []);

  const load = async () => {
    setLoading(true);
    try {
      const params = new URLSearchParams();
      if (search) params.set('search', search);
      if (dsFilter) params.set('datasource', dsFilter);
      const data = await get<{ history: HistoryItem[] }>(`/history?${params.toString()}`);
      setItems(data.history || []);
    } catch {
      // 后端 /history API 尚未实现，显示空列表
    }
    setLoading(false);
  };

  useEffect(() => { load(); }, [dsFilter]);

  // 方法作用：读取历史会话的结构化轮次并交给 ChatPage 恢复渲染。
  // Args: item - 当前历史查询记录。
  // Returns: 无返回值；没有会话关联时提示用户。
  const handleReplay = async (item: HistoryItem): Promise<void> => {
    if (!item.session_id) {
      message.warning(t("该历史记录没有可回放的会话"));
      return;
    }
    setReplayId(item.id);
    try {
      const detail = await fetchSession(item.session_id);
      navigate('/', { state: { replaySession: detail } });
    } catch (error) {
      message.error(error instanceof Error ? error.message : t("回放历史会话失败"));
    } finally {
      setReplayId(null);
    }
  };

  const filtered = search
    ? items.filter(i => i.query.includes(search) || (i.sql || '').includes(search))
    : items;

  return (
    <div className="app-page">
      <Card
        title={t("查询历史")}
        extra={
          <Space wrap>
            <Select style={{ width: 180 }} value={dsFilter} onChange={v => setDsFilter(v)}
              allowClear placeholder={t("全部数据源")}
              options={datasources.map(d => ({ value: d.name, label: `${d.name} (${d.dialect})` }))} />
            <Input.Search placeholder={t("搜索查询或 SQL")} value={search}
              onChange={e => setSearch(e.target.value)} onSearch={load} style={{ width: 240 }} />
          </Space>
        }>
        <Table<HistoryItem>
          dataSource={filtered}
          rowKey="id"
          loading={loading}
          locale={{ emptyText: <Empty description={t("暂无查询历史")} /> }}
          columns={[
            { title: t("时间"), dataIndex: 'time', key: 'time', width: 160 },
            { title: t("数据源"), dataIndex: 'datasource', key: 'datasource', width: 100,
              render: (v: string) => v ? <Tag>{v}</Tag> : '-' },
            { title: t("查询"), dataIndex: 'query', key: 'query', ellipsis: true },
            { title: 'SQL', dataIndex: 'sql', key: 'sql', ellipsis: true,
              render: (v: string) => v ? <code style={{ fontSize: 12 }}>{v}</code> : '-' },
            { title: t("行数"), dataIndex: 'row_count', key: 'row_count', width: 80,
              render: (v: number) => v !== undefined ? v : '-' },
            { title: t("状态"), dataIndex: 'success', key: 'success', width: 80,
              render: (v: boolean) => v ? <Tag color="green">{t("成功")}</Tag> : <Tag color="red">{t("失败")}</Tag> },
            { title: t("失败原因"), dataIndex: 'error_message', key: 'error_message', width: 210,
              render: (v: string, record: HistoryItem) => record.success || !v ? '-' : (
                <Tooltip title={v}>
                  <Typography.Text type="danger" ellipsis style={{ maxWidth: 190, display: 'inline-block' }}>
                    {v}
                  </Typography.Text>
                </Tooltip>
              ) },
            { title: t("回放"), key: 'replay', width: 60,
              render: (_: unknown, record: HistoryItem) => (
                <Tooltip title={record.session_id ? t("回放会话") : t("该记录无会话")}>
                  <Button
                    type="text"
                    size="small"
                    aria-label={t("回放 {value0}", { value0: record.query })}
                    icon={<PlayCircleOutlined />}
                    loading={replayId === record.id}
                    disabled={!record.session_id}
                    onClick={event => { event.stopPropagation(); void handleReplay(record); }}
                  />
                </Tooltip>
              ) },
          ]}
          onRow={record => ({
            onClick: () => { void handleReplay(record); },
            style: { cursor: record.session_id ? 'pointer' : 'default' },
          })}
        />
      </Card>
    </div>
  );
}
