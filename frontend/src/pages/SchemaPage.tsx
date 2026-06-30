import { useState, useEffect } from 'react';
import { Card, Table, Input, Button, Tag, Drawer, Typography, Form, message, Space, Select } from 'antd';
import { EditOutlined } from '@ant-design/icons';
import { get, put } from '../api/client';
import type { TableInfo, ColumnInfo, DatasourceConfig } from '../types';

export default function SchemaPage() {
  const [tables, setTables] = useState<TableInfo[]>([]);
  const [datasources, setDatasources] = useState<DatasourceConfig[]>([]);
  const [ds, setDs] = useState('demo');
  const [search, setSearch] = useState('');
  const [loading, setLoading] = useState(false);
  const [drawerOpen, setDrawerOpen] = useState(false);
  const [selectedTable, setSelectedTable] = useState<TableInfo | null>(null);
  const [editingCol, setEditingCol] = useState<string | null>(null);
  const [commentForm] = Form.useForm();

  useEffect(() => {
    get<{ datasources: DatasourceConfig[] }>('/datasources')
      .then(data => setDatasources(data.datasources || []))
      .catch(() => message.warning('无法加载数据源列表'));
  }, []);

  const load = async () => {
    setLoading(true);
    try {
      const params = new URLSearchParams({ datasource: ds });
      if (search) params.set('search', search);
      const data = await get<{ tables: TableInfo[] }>(`/schema/tables?${params.toString()}`);
      setTables(data.tables || []);
    } catch { message.error('加载表结构失败'); }
    setLoading(false);
  };

  useEffect(() => { load(); }, [ds]);

  const handleRefresh = async () => {
    try {
      await fetch(`/api/v1/schema/refresh?datasource=${encodeURIComponent(ds)}`, { method: 'POST' });
      message.success('Schema 已刷新'); load();
    } catch { message.error('刷新失败'); }
  };

  const openDrawer = (table: TableInfo) => {
    setSelectedTable(table); setEditingCol(null); setDrawerOpen(true);
  };

  const handleSaveComment = async (values: { comment: string }) => {
    if (!selectedTable || !editingCol) return;
    try {
      await put(
        `/schema/tables/${encodeURIComponent(selectedTable.name)}/columns/${encodeURIComponent(editingCol)}/comment`,
        { comment: values.comment } as Record<string, unknown>,
      );
      message.success('注释已更新'); setEditingCol(null); load();
    } catch { message.error('更新失败'); }
  };

  return (
    <div style={{ maxWidth: 1000, margin: '0 auto' }}>
      <Card title="表结构浏览" extra={
        <Space wrap>
          <Select style={{ width: 180 }} value={ds} onChange={v => setDs(v)}
            options={datasources.map(d => ({ value: d.name, label: `${d.name} (${d.dialect})` }))}
            placeholder="选择数据源" />
          <Input.Search placeholder="搜索表名" value={search}
            onChange={e => setSearch(e.target.value)} onSearch={load} style={{ width: 200 }} />
          <Button onClick={handleRefresh}>刷新 Schema</Button>
        </Space>
      }>
        <Table<TableInfo> dataSource={tables} rowKey="name" loading={loading}
          onRow={r => ({ onClick: () => openDrawer(r), style: { cursor: 'pointer' } })}
          expandable={{ expandedRowRender: (r: TableInfo) => (
            <Table<ColumnInfo> dataSource={r.columns} rowKey="name" size="small" pagination={false}
              columns={[
                { title: '字段', dataIndex: 'name', width: 150 },
                { title: '类型', dataIndex: 'type', width: 120, render: (v: string) => <Tag>{v}</Tag> },
                { title: '注释', dataIndex: 'comment', ellipsis: true },
                { title: '可空', dataIndex: 'is_nullable', width: 70, render: (v: boolean) => v ? <Tag color="orange">是</Tag> : <Tag>否</Tag> },
                { title: '主键', dataIndex: 'is_primary_key', width: 70, render: (v: boolean) => v && <Tag color="blue">PK</Tag> },
              ]} />
          )}} columns={[
            { title: '表名', dataIndex: 'name', key: 'name', ellipsis: true },
            { title: '描述', dataIndex: 'description', key: 'description', ellipsis: true },
            { title: '字段数', key: 'cols', width: 80, render: (_: unknown, r: TableInfo) => r.columns?.length || 0 },
          ]} />
      </Card>

      <Drawer title={selectedTable ? `表详情: ${selectedTable.name}` : ''}
        open={drawerOpen} onClose={() => { setDrawerOpen(false); setSelectedTable(null); setEditingCol(null); }}
        width={600}>
        {selectedTable && (
          <div>
            <Typography.Title level={5}>描述</Typography.Title>
            <Typography.Paragraph>{selectedTable.description || '无描述'}</Typography.Paragraph>
            {selectedTable.relations && selectedTable.relations.length > 0 && (
              <>
                <Typography.Title level={5}>关联关系</Typography.Title>
                {selectedTable.relations.map((r, i) => (
                  <Tag key={i} color="purple" style={{ marginBottom: 4 }}>→ {r.target_table} ON {r.join_key}</Tag>
                ))}
              </>
            )}
            <Typography.Title level={5}>字段列表</Typography.Title>
            <Table<ColumnInfo> dataSource={selectedTable.columns} rowKey="name" size="small" pagination={false}
              columns={[
                { title: '名称', dataIndex: 'name', width: 120 },
                { title: '类型', dataIndex: 'type', width: 100, render: (v: string) => <Tag>{v}</Tag> },
                { title: '注释', dataIndex: 'comment', render: (v: string, record: ColumnInfo) => (
                  editingCol === record.name ? (
                    <Form form={commentForm} initialValues={{ comment: v }} onFinish={handleSaveComment} layout="inline">
                      <Form.Item name="comment" style={{ marginBottom: 0 }}><Input size="small" style={{ width: 180 }} /></Form.Item>
                      <Button size="small" type="link" htmlType="submit">保存</Button>
                      <Button size="small" type="link" onClick={() => setEditingCol(null)}>取消</Button>
                    </Form>
                  ) : (
                    <Space>
                      <span>{v || '-'}</span>
                      <Button size="small" type="link" icon={<EditOutlined />}
                        onClick={() => { setEditingCol(record.name); commentForm.setFieldsValue({ comment: v }); }} />
                    </Space>
                  )
                )},
                { title: '可空', dataIndex: 'is_nullable', width: 60, render: (v: boolean) => v ? <Tag color="orange">Y</Tag> : <Tag>N</Tag> },
                { title: 'PK', dataIndex: 'is_primary_key', width: 50, render: (v: boolean) => v && <Tag color="blue">PK</Tag> },
              ]} />
          </div>
        )}
      </Drawer>
    </div>
  );
}
