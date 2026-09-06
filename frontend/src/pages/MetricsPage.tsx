import { t, useLanguage } from '../i18n';
import { useEffect, useState } from 'react';
import { Alert, Button, Card, Form, Input, Modal, Select, Space, Table, Tag, Typography, message } from 'antd';
import { get, post } from '../api/client';
import { useAuth } from '../hooks/AuthContext';
import { fetchDatasourceCatalog } from '../api/datasourceCatalog';
import type { DatasourceConfig } from '../types';
import type { ConnectorPlugin } from '../components/RuntimeControlsPanel';

interface MetricDefinition {
  code: string; name: string; datasource: string; dialect: string;
  expression: string; description: string; unit: string;
}

interface MetricRecord {
  id: string; code: string; version: number;
  status: 'draft' | 'pending' | 'approved' | 'rejected';
  definition: MetricDefinition; effective_at?: string; reference: string;
  created_by?: number; reviewed_by?: number; review_note?: string;
}

const statusLabels = { get draft() { return t("草稿"); }, get pending() { return t("待审核"); }, get approved() { return t("已批准"); }, get rejected() { return t("已驳回"); } };

// 方法作用：提供租户指标的版本化维护、审核和只读口径引用。
// Args: 无。
// Returns: 按当前身份约束操作的指标目录页面。
export default function MetricsPage() {
  useLanguage();
  const { user } = useAuth();
  const allowed = ['tenant_admin', 'analyst', 'viewer'].includes(user?.role || '');
  const canEdit = user?.role === 'tenant_admin';
  const [items, setItems] = useState<MetricRecord[]>([]);
  const [total, setTotal] = useState(0);
  const [page, setPage] = useState(1);
  const [query, setQuery] = useState('');
  const [refresh, setRefresh] = useState(0);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState('');
  const [editing, setEditing] = useState<MetricRecord | null>(null);
  const [editorOpen, setEditorOpen] = useState(false);
  const [reviewing, setReviewing] = useState<MetricRecord | null>(null);
  const [history, setHistory] = useState<MetricRecord[] | null>(null);
  const [busy, setBusy] = useState(false);
  const [sources, setSources] = useState<DatasourceConfig[]>([]);
  const [sourceError, setSourceError] = useState('');
  const [form] = Form.useForm<MetricDefinition>();
  const [reviewForm] = Form.useForm<{ action: 'approve' | 'reject'; effective_at?: string; note?: string }>();

  // 方法作用：打开编辑器时重新读取已授权且插件启用的数据源。
  // Args: 无。
  // Returns: 清理旧响应，避免切换租户或关闭后回写。
  useEffect(() => {
    if (!editorOpen || !canEdit) return;
    let active = true;
    setSources([]); setSourceError('');
    Promise.all([fetchDatasourceCatalog(), get<{ items: ConnectorPlugin[] }>('/datasource-types')])
      .then(([datasources, plugins]) => {
        if (!active) return;
        const enabled = new Set((plugins.items || []).map(plugin => plugin.dialect));
        const available = datasources.filter(source => enabled.has(source.dialect));
        setSources(available);
        const selected = available.find(source => source.name === form.getFieldValue('datasource'));
        form.setFieldValue('dialect', selected?.dialect);
      })
      .catch(() => { if (active) setSourceError(t("数据源目录加载失败，请关闭后重试")); });
    return () => { active = false; };
  }, [editorOpen, canEdit, user?.tenant_id, form]);

  useEffect(() => {
    if (!allowed) return;
    let active = true;
    setLoading(true);
    setError('');
    get<{ items: MetricRecord[]; total: number }>(`/metric-catalog?query=${encodeURIComponent(query)}&limit=20&offset=${(page - 1) * 20}`)
      .then(result => { if (active) { setItems(result.items); setTotal(result.total); } })
      .catch(caught => { if (active) setError(caught instanceof Error ? caught.message : t("指标目录加载失败")); })
      .finally(() => { if (active) setLoading(false); });
    return () => { active = false; };
  }, [allowed, user?.tenant_id, page, query, refresh]);

  // 方法作用：打开空白草稿或基于已读取版本的新版本表单。
  // Args: record - 可选的现有版本。
  // Returns: 无返回值。
  function openEditor(record: MetricRecord | null): void {
    setEditing(record);
    form.resetFields();
    form.setFieldsValue(record?.definition || { unit: '', description: '' });
    setEditorOpen(true);
  }

  // 方法作用：携带乐观版本号提交定义，失败时保留编辑内容。
  // Args: 无。
  // Returns: 完成保存的异步结果。
  async function saveRevision(): Promise<void> {
    if (busy) return;
    setBusy(true);
    try {
      const definition = await form.validateFields();
      await post('/metric-catalog', { definition, expected_version: editing?.version || 0 });
      setEditorOpen(false);
      setRefresh(value => value + 1);
      message.success(t("新版本已保存"));
    } catch (caught) {
      if (caught instanceof Error) message.error(caught.message);
    } finally { setBusy(false); }
  }

  // 方法作用：提交现有草稿审核，状态冲突时不伪造本地成功。
  // Args: record - 草稿版本。
  // Returns: 完成请求的异步结果。
  async function submitReview(record: MetricRecord): Promise<void> {
    setBusy(true);
    try {
      await post(`/metric-catalog/${record.id}/transition`, { action: 'submit' });
      setRefresh(value => value + 1);
      message.success(t("已提交审核"));
    } catch (caught) { message.error(caught instanceof Error ? caught.message : t("提交失败")); }
    finally { setBusy(false); }
  }

  // 方法作用：保存审核结论并将浏览器本地时间转换为带时区的 UTC。
  // Args: 无。
  // Returns: 完成审核的异步结果。
  async function saveReview(): Promise<void> {
    if (!reviewing || busy) return;
    setBusy(true);
    try {
      const values = await reviewForm.validateFields();
      await post(`/metric-catalog/${reviewing.id}/transition`, {
        action: values.action, note: values.note || '',
        effective_at: values.action === 'approve' && values.effective_at ? new Date(values.effective_at).toISOString() : null,
      });
      setReviewing(null);
      setRefresh(value => value + 1);
      message.success(t("审核已保存"));
    } catch (caught) {
      if (caught instanceof Error) message.error(caught.message);
    } finally { setBusy(false); }
  }

  // 方法作用：读取最新可见历史，避免把不同编码的旧历史留在弹窗。
  // Args: code - 指标编码。
  // Returns: 完成历史读取的异步结果。
  async function loadHistory(code: string): Promise<void> {
    setHistory(null);
    try {
      const result = await get<{ items: MetricRecord[] }>(`/metric-catalog/${encodeURIComponent(code)}/versions`);
      setHistory(result.items);
    } catch (caught) { message.error(caught instanceof Error ? caught.message : t("历史加载失败")); }
  }

  // 方法作用：只复制后端确认已批准生效的固定版本引用。
  // Args: code - 指标编码。
  // Returns: 完成剪贴板写入的异步结果。
  async function copyReference(code: string): Promise<void> {
    try {
      const metric = await get<MetricRecord>(`/metric-catalog/${encodeURIComponent(code)}/effective`);
      await navigator.clipboard.writeText(metric.reference);
      message.success(t("已复制生效口径引用，可粘贴到对话问题中"));
    } catch (caught) { message.error(caught instanceof Error ? caught.message : t("复制引用失败")); }
  }

  if (!allowed) return <Alert type="warning" message={t("业务指标由租户管理，请使用租户账号访问")} />;

  return <div style={{ padding: 24 }}>
    <Typography.Title level={3}>{t("指标目录")}</Typography.Title>
    <Alert type="info" showIcon message={t("每次编辑产生新版本；只有已批准且到达生效时间的口径才能用于查询。")} style={{ marginBottom: 16 }} />
    {error && <Alert type="error" message={error} style={{ marginBottom: 16 }} />}
    <Card>
      <Space wrap style={{ marginBottom: 16 }}>
        <Input.Search placeholder={t("搜索指标编码或口径")} allowClear onSearch={value => { setQuery(value); setPage(1); }} />
        <Button onClick={() => setRefresh(value => value + 1)}>{t("刷新")}</Button>
        {canEdit && <Button type="primary" onClick={() => openEditor(null)}>{t("新建指标")}</Button>}
      </Space>
      <Table<MetricRecord> rowKey="id" dataSource={items} loading={loading} scroll={{ x: 1000 }}
        pagination={{ current: page, total, pageSize: 20, showSizeChanger: false, onChange: setPage }} columns={[
          { title: t("名称"), render: (_, record) => record.definition.name },
          { title: t("编码"), dataIndex: 'code' },
          { title: t("版本"), dataIndex: 'version' },
          { title: t("状态"), render: (_, record) => <Tag color={record.status === 'approved' ? 'green' : 'default'}>{statusLabels[record.status]}</Tag> },
          { title: t("数据源"), render: (_, record) => record.definition.datasource },
          { title: t("生效时间"), render: (_, record) => record.effective_at ? new Date(record.effective_at).toLocaleString() : '—' },
          { title: t("操作"), render: (_, record) => <Space wrap>
            <Button size="small" onClick={() => void loadHistory(record.code)}>{t("版本历史")}</Button>
            <Button size="small" onClick={() => void copyReference(record.code)}>{t("复制生效口径")}</Button>
            {canEdit && <Button size="small" onClick={() => openEditor(record)}>{t("编辑新版本")}</Button>}
            {canEdit && record.status === 'draft' && <Button size="small" disabled={busy} onClick={() => void submitReview(record)}>{t("提交审核")}</Button>}
            {canEdit && record.status === 'pending' && <Button size="small" onClick={() => { reviewForm.resetFields(); reviewForm.setFieldsValue({ action: 'approve' }); setReviewing(record); }}>{t("审核")}</Button>}
          </Space> },
        ]} />
    </Card>
    <Modal title={editing ? t("编辑新版本（基于 v{value0}）", { value0: editing.version }) : t("新建指标")} open={editorOpen} onCancel={() => setEditorOpen(false)} onOk={() => void saveRevision()} okText={t("保存新版本")} confirmLoading={busy} width={720}>
      <Form form={form} layout="vertical">
        <Form.Item name="code" label={t("指标编码")} rules={[{ required: true, pattern: /^[a-z][a-z0-9_]{0,63}$/, message: t("使用小写字母、数字和下划线，以字母开头") }]}><Input disabled={Boolean(editing)} maxLength={64} /></Form.Item>
        <Form.Item name="name" label={t("指标名称")} rules={[{ required: true }]}><Input maxLength={128} /></Form.Item>
        {sourceError && <Alert type="error" message={sourceError} />}
        <Form.Item name="datasource" label={t("数据源")} rules={[{ required: true }]}><Select showSearch optionFilterProp="label" placeholder={t("选择数据源")} options={sources.map(source => ({ value: source.name, label: `${source.name} (${source.dialect})` }))} onChange={name => form.setFieldValue('dialect', sources.find(source => source.name === name)?.dialect)} /></Form.Item>
        <Form.Item name="dialect" label={t("数据库方言")} rules={[{ required: true }]}><Input readOnly placeholder={t("由数据源自动带出")} /></Form.Item>
        <Form.Item name="expression" label={t("计算表达式")} rules={[{ required: true }]}><Input.TextArea maxLength={4000} rows={3} placeholder={t("例如 SUM(paid_amount)，不支持完整 SQL")} /></Form.Item>
        <Form.Item name="description" label={t("业务口径说明")}><Input.TextArea rows={4} maxLength={8000} placeholder={t("筛选条件、统计时间、排除项等")} /></Form.Item>
        <Form.Item name="unit" label={t("单位")}><Input maxLength={32} /></Form.Item>
      </Form>
    </Modal>
    <Modal title={t("指标审核")} open={Boolean(reviewing)} onCancel={() => setReviewing(null)} onOk={() => void saveReview()} okText={t("确认审核")} confirmLoading={busy}>
      <Form form={reviewForm} layout="vertical">
        <Form.Item name="action" label={t("审核结论")} rules={[{ required: true }]}><Select options={[{ value: 'approve', label: t("批准") }, { value: 'reject', label: t("驳回") }]} /></Form.Item>
        <Form.Item name="effective_at" label={t("生效时间（留空立即生效）")}><Input type="datetime-local" /></Form.Item>
        <Form.Item name="note" label={t("审核意见")}><Input.TextArea maxLength={2000} /></Form.Item>
      </Form>
    </Modal>
    <Modal title={t("指标版本历史")} open={history !== null} onCancel={() => setHistory(null)} footer={null} width={900}>
      <Table<MetricRecord> rowKey="id" dataSource={history || []} pagination={false} scroll={{ x: 700 }} columns={[
        { title: t("版本"), dataIndex: 'version' },
        { title: t("状态"), render: (_, record) => statusLabels[record.status] },
        { title: t("公式"), render: (_, record) => record.definition.expression },
        { title: t("口径"), render: (_, record) => record.definition.description },
        { title: t("审核人"), dataIndex: 'reviewed_by' },
        { title: t("审核意见"), dataIndex: 'review_note' },
      ]} />
    </Modal>
  </div>;
}
