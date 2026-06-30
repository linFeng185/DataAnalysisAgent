import { useState, useEffect } from 'react';
import { Card, Table, Tag, Input, Select, Empty, message, Space } from 'antd';
import { get } from '../api/client';
import type { HistoryItem, DatasourceConfig } from '../types';

export default function HistoryPage() {
  const [items, setItems] = useState<HistoryItem[]>([]);
  const [datasources, setDatasources] = useState<DatasourceConfig[]>([]);
  const [dsFilter, setDsFilter] = useState<string | undefined>();
  const [search, setSearch] = useState('');
  const [loading, setLoading] = useState(false);

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

  const filtered = search
    ? items.filter(i => i.query.includes(search) || (i.sql || '').includes(search))
    : items;

  return (
    <div style={{ maxWidth: 1000, margin: '0 auto' }}>
      <Card
        title="查询历史"
        extra={
          <Space wrap>
            <Select style={{ width: 180 }} value={dsFilter} onChange={v => setDsFilter(v)}
              allowClear placeholder="全部数据源"
              options={datasources.map(d => ({ value: d.name, label: `${d.name} (${d.dialect})` }))} />
            <Input.Search placeholder="搜索查询或 SQL" value={search}
              onChange={e => setSearch(e.target.value)} onSearch={load} style={{ width: 240 }} />
          </Space>
        }>
        <Table<HistoryItem>
          dataSource={filtered}
          rowKey="id"
          loading={loading}
          locale={{ emptyText: <Empty description="暂无查询历史" /> }}
          columns={[
            { title: '时间', dataIndex: 'time', key: 'time', width: 160 },
            { title: '数据源', dataIndex: 'datasource', key: 'datasource', width: 100,
              render: (v: string) => v ? <Tag>{v}</Tag> : '-' },
            { title: '查询', dataIndex: 'query', key: 'query', ellipsis: true },
            { title: 'SQL', dataIndex: 'sql', key: 'sql', ellipsis: true,
              render: (v: string) => v ? <code style={{ fontSize: 12 }}>{v}</code> : '-' },
            { title: '行数', dataIndex: 'row_count', key: 'row_count', width: 80,
              render: (v: number) => v !== undefined ? v : '-' },
            { title: '状态', dataIndex: 'success', key: 'success', width: 80,
              render: (v: boolean) => v ? <Tag color="green">成功</Tag> : <Tag color="red">失败</Tag> },
          ]} />
      </Card>
    </div>
  );
}
