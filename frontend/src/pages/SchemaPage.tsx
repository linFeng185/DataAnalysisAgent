import { t, useLanguage } from '../i18n';
import { useState, useEffect, useRef } from 'react';
import { Alert, Card, Table, Input, Button, Tag, Drawer, Typography, Form, message, Space, Select } from 'antd';
import { EditOutlined } from '@ant-design/icons';
import { get, post, put } from '../api/client';
import type { TableInfo, ColumnInfo, DatasourceConfig } from '../types';
import { fetchDatasourceCatalog } from '../api/datasourceCatalog';
import { useAuth } from '../hooks/AuthContext';

interface SyncStatus { datasource?: string; status: string; phase?: string; error?: string; run_id?: string; table_count?: number }
const syncLabels: Record<string, string> = { get idle() { return t("未同步"); }, get running() { return t("同步中"); }, get cancelling() { return t("终止中"); }, get succeeded() { return t("同步完成"); }, get cancelled() { return t("已终止"); }, get failed() { return t("同步失败"); } };

// 方法作用：展示当前数据源表结构和共享同步任务状态。
// Args: 无。
// Returns: 可启动和终止同步的表结构页面。
export default function SchemaPage() {
  useLanguage();
  const { user } = useAuth();
  const canManage = ['super_admin', 'tenant_admin'].includes(user?.role || '');
  const [tables, setTables] = useState<TableInfo[]>([]);
  const [datasources, setDatasources] = useState<DatasourceConfig[]>([]);
  const [ds, setDs] = useState('');
  const [search, setSearch] = useState('');
  const [loading, setLoading] = useState(false);
  const [drawerOpen, setDrawerOpen] = useState(false);
  const [selectedTable, setSelectedTable] = useState<TableInfo | null>(null);
  const [editingCol, setEditingCol] = useState<string | null>(null);
  const [commentForm] = Form.useForm();
  const [sync, setSync] = useState<SyncStatus>({ status: 'idle' });
  const [syncError, setSyncError] = useState('');
  const [submitting, setSubmitting] = useState(false);
  const [refresh, setRefresh] = useState(0);
  const currentSource = useRef(ds);
  currentSource.current = ds;
  const active = ['running', 'cancelling'].includes(sync.status);

  useEffect(() => {
    fetchDatasourceCatalog()
      .then(items => {
        setDatasources(items);
        setDs(current => current && items.some(item => item.name === current)
          ? current : (items[0]?.name || ''));
      })
      .catch(() => message.warning(t("无法加载数据源列表")));
  }, []);

  const load = async () => {
    setLoading(true);
    try {
      if (!ds) {
        setTables([]);
        setLoading(false);
        return;
      }
      const params = new URLSearchParams({ datasource: ds });
      if (search) params.set('search', search);
      const data = await get<{ tables: TableInfo[] }>(`/schema/tables?${params.toString()}`);
      if (currentSource.current === ds) setTables(data.tables || []);
    } catch { message.error(t("加载表结构失败")); }
    setLoading(false);
  };

  useEffect(() => { void load(); }, [ds, refresh]);

  // 方法作用：顺序轮询当前数据源状态，切换后旧响应不污染新数据源。
  // Args: 无。
  // Returns: 清除轮询并丢弃旧请求。
  useEffect(() => {
    if (!ds) return;
    let mounted = true;
    let timer: number;
    let previous = '';
    setSync({ status: 'idle' }); setSyncError('');
    // 方法作用：读取共享状态并在完成时重新加载表结构。
    // Args: 无。
    // Returns: 请求完成后安排下一次轮询。
    const poll = async (): Promise<void> => {
      try {
        const result = await get<SyncStatus>(`/schema/sync?datasource=${encodeURIComponent(ds)}`);
        if (!mounted) return;
        setSync(result); setSyncError('');
        if (result.status === 'succeeded' && previous && previous !== `${result.run_id}:${result.status}`) setRefresh(value => value + 1);
        previous = `${result.run_id}:${result.status}`;
      } catch (error) { if (mounted) setSyncError(error instanceof Error ? error.message : t("同步状态读取失败")); }
      finally { if (mounted) timer = window.setTimeout(() => void poll(), 1500); }
    };
    void poll();
    return () => { mounted = false; window.clearTimeout(timer); };
  }, [ds]);

  // 方法作用：创建后台同步任务，服务端按数据源拒绝重复操作。
  // Args: 无。
  // Returns: 更新共享任务状态。
  const handleRefresh = async () => {
    if (submitting || active || !ds) return;
    const source = ds;
    setSubmitting(true);
    try {
      const result = await post<SyncStatus>(`/schema/sync?datasource=${encodeURIComponent(source)}`, {});
      if (currentSource.current === source) { setSync(result); setSyncError(''); }
      message.success(t("表结构同步已启动"));
    } catch (error) { message.error(error instanceof Error ? error.message : t("同步失败")); }
    finally { setSubmitting(false); }
  };

  // 方法作用：只发送当前任务的终止信号，等待实际退出后再允许同步。
  // Args: 无。
  // Returns: 显示终止中的共享状态。
  const cancelSync = async (): Promise<void> => {
    const source = ds;
    setSubmitting(true);
    try { const result = await post<SyncStatus>(`/schema/sync/cancel?datasource=${encodeURIComponent(source)}`, {}); if (currentSource.current === source) setSync(result); }
    catch (error) { message.error(error instanceof Error ? error.message : t("终止失败")); }
    finally { setSubmitting(false); }
  };

  const openDrawer = (table: TableInfo) => {
    setSelectedTable(table); setEditingCol(null); setDrawerOpen(true);
  };

  const handleSaveComment = async (values: { comment: string }) => {
    if (!selectedTable || !editingCol) return;
    console.info('字段备注保存请求边界', {
      datasource: ds,
      table: selectedTable.name,
      column: editingCol,
    });
    try {
      await put(
        `/schema/tables/${encodeURIComponent(selectedTable.name)}/columns/${encodeURIComponent(editingCol)}/comment?datasource=${encodeURIComponent(ds)}`,
        { comment: values.comment } as Record<string, unknown>,
      );
      message.success(t("注释已更新")); setEditingCol(null); load();
    } catch { message.error(t("更新失败")); }
  };

  return (
    <div style={{ maxWidth: 1000, margin: '0 auto' }}>
      <Card title={t("表结构浏览")} extra={
        <Space wrap>
          <Select style={{ width: 180 }} value={ds} onChange={v => setDs(v)}
            options={datasources.map(d => ({ value: d.name, label: `${d.name} (${d.dialect})` }))}
            placeholder={t("选择数据源")} />
          <Input.Search placeholder={t("搜索表名")} value={search}
            onChange={e => setSearch(e.target.value)} onSearch={load} style={{ width: 200 }} />
          <Tag color={active ? 'processing' : sync.status === 'failed' ? 'error' : 'default'}>{sync.phase === 'publishing' && active ? t("正在发布表结构") : syncLabels[sync.status] || sync.status}</Tag>
          {canManage && <Button onClick={handleRefresh} loading={submitting} disabled={!ds || active || Boolean(syncError)}>{t("同步表结构")}</Button>}
          {canManage && active && <Button danger onClick={() => void cancelSync()} disabled={submitting || sync.status === 'cancelling' || sync.phase === 'publishing'}>{t("终止同步")}</Button>}
        </Space>
      }>
        {(syncError || sync.error) && <Alert type="error" message={syncError || sync.error} style={{ marginBottom: 12 }} />}
        <Table<TableInfo> dataSource={tables} rowKey="name" loading={loading}
          onRow={r => ({ onClick: () => openDrawer(r), style: { cursor: 'pointer' } })}
          expandable={{ expandedRowRender: (r: TableInfo) => (
            <Table<ColumnInfo> dataSource={r.columns} rowKey="name" size="small" pagination={false}
              columns={[
                { title: t("字段"), dataIndex: 'name', width: 150 },
                { title: t("类型"), dataIndex: 'type', width: 120, render: (v: string) => <Tag>{v}</Tag> },
                { title: t("注释"), dataIndex: 'comment', ellipsis: true },
                { title: t("可空"), dataIndex: 'is_nullable', width: 70, render: (v: boolean) => v ? <Tag color="orange">{t("是")}</Tag> : <Tag>{t("否")}</Tag> },
                { title: t("主键"), dataIndex: 'is_primary_key', width: 70, render: (v: boolean) => v && <Tag color="blue">PK</Tag> },
                { title: t("索引"), dataIndex: 'is_indexed', width: 70, render: (v: boolean) => v && <Tag color="green">{t("索引")}</Tag> },
              ]} />
          )}} columns={[
            { title: t("表名"), dataIndex: 'name', key: 'name', ellipsis: true },
            { title: t("描述"), dataIndex: 'description', key: 'description', ellipsis: true },
            { title: t("字段数"), key: 'cols', width: 80, render: (_: unknown, r: TableInfo) => r.columns?.length || 0 },
          ]} />
      </Card>

      <Drawer title={selectedTable ? t("表详情: {value0}", { value0: selectedTable.name }) : ''}
        open={drawerOpen} onClose={() => { setDrawerOpen(false); setSelectedTable(null); setEditingCol(null); }}
        width={600}>
        {selectedTable && (
          <div>
            <Typography.Title level={5}>{t("描述")}</Typography.Title>
            <Typography.Paragraph>{selectedTable.description || t("无描述")}</Typography.Paragraph>
            {selectedTable.relations && selectedTable.relations.length > 0 && (
              <>
                <Typography.Title level={5}>{t("关联关系")}</Typography.Title>
                {selectedTable.relations.map((r, i) => (
                  <Tag key={i} color="purple" style={{ marginBottom: 4 }}>→ {r.target_table} ON {r.join_key}</Tag>
                ))}
              </>
            )}
            <Typography.Title level={5}>{t("字段列表")}</Typography.Title>
            <Table<ColumnInfo> dataSource={selectedTable.columns} rowKey="name" size="small" pagination={false}
              columns={[
                { title: t("名称"), dataIndex: 'name', width: 120 },
                { title: t("类型"), dataIndex: 'type', width: 100, render: (v: string) => <Tag>{v}</Tag> },
                { title: t("注释"), dataIndex: 'comment', render: (v: string, record: ColumnInfo) => (
                  editingCol === record.name ? (
                    <Form form={commentForm} initialValues={{ comment: v }} onFinish={handleSaveComment} layout="inline">
                      <Form.Item name="comment" style={{ marginBottom: 0 }}><Input size="small" style={{ width: 180 }} /></Form.Item>
                      <Button size="small" type="link" htmlType="submit">{t("保存")}</Button>
                      <Button size="small" type="link" onClick={() => setEditingCol(null)}>{t("取消")}</Button>
                    </Form>
                  ) : (
                    <Space>
                      <span>{v || '-'}</span>
                      <Button size="small" type="link" icon={<EditOutlined />}
                        onClick={() => { setEditingCol(record.name); commentForm.setFieldsValue({ comment: v }); }} />
                    </Space>
                  )
                )},
                { title: t("可空"), dataIndex: 'is_nullable', width: 60, render: (v: boolean) => v ? <Tag color="orange">Y</Tag> : <Tag>N</Tag> },
                { title: 'PK', dataIndex: 'is_primary_key', width: 50, render: (v: boolean) => v && <Tag color="blue">PK</Tag> },
              ]} />
          </div>
        )}
      </Drawer>
    </div>
  );
}
