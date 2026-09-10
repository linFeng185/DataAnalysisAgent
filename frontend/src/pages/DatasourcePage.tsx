import { t, useLanguage } from '../i18n';
import { useEffect, useRef, useState } from 'react';
import {
  Button, Card, Form, Input, message, Modal, Popconfirm, Select, Space,
  Table, Tag, Tooltip, Typography, Upload,
} from 'antd';
import { ApiOutlined, DeleteOutlined, EditOutlined, PlusOutlined } from '@ant-design/icons';
import { del, downloadConfig, get, post, put, uploadConfig } from '../api/client';
import { fetchDatasourceCatalog } from '../api/datasourceCatalog';
import type { DatasourceConfig, DatasourceConnectionResult, SchemaSyncStatus } from '../types';
import { useAuth } from '../hooks/AuthContext';
import type { ConnectorPlugin } from '../components/RuntimeControlsPanel';


const VERSIONS: Record<string, string[]> = {
  mysql: ['5.7', '8.0', '8.1', '8.4'],
  postgres: ['14', '15', '16', '17'],
  clickhouse: ['23.x', '24.x', '25.x'],
  oracle: ['19c', '21c', '23ai'],
  mssql: ['2019', '2022'],
  sqlite: [],
};


const LABELS: Record<string, string> = {
  get host() { return t("主机"); }, get port() { return t("端口"); }, get database() { return t("数据库名"); }, get username() { return t("用户名"); },
  get password() { return t("密码"); }, schema: 'Schema', get tablespace() { return t("表空间"); },
  get service_name() { return t("服务名/SID"); }, get instance() { return t("实例名"); }, get file_path() { return t("文件路径"); },
};

const PLACEHOLDERS: Record<string, Record<string, string>> = {
  port: { mysql: '3306', postgres: '5432', clickhouse: '9000', oracle: '1521', mssql: '1433', sqlite: '' },
  schema: { postgres: 'public', mssql: 'dbo' },
  service_name: { oracle: 'XEPDB1' },
  file_path: { sqlite: './data.db' },
};

// 方法作用：返回当前方言下连接字段的输入提示。
// Args: field - 字段名；dialect - 数据库方言。
// Returns: 输入框提示文本。
function placeholder(field: string, dialect: string): string {
  return (PLACEHOLDERS[field] || {})[dialect] || '';
}

// 方法作用：把表单值归一化为后端数据源请求契约。
// Args: values - 表单原始值；dialect - 数据库方言；name - 可选固定名称。
// Returns: 可提交给 API 的数据源配置。
function normalizePayload(
  values: Record<string, unknown>,
  dialect: string,
  name?: string,
): Record<string, unknown> {
  const rawVersion = values.version;
  const version = Array.isArray(rawVersion) ? rawVersion[0] || '' : rawVersion || '';
  const payload: Record<string, unknown> = { ...values, dialect, version };
  if (name) payload.name = name;
  if (dialect === 'sqlite') {
    payload.host = '';
    payload.port = 0;
    payload.database = values.file_path || '';
  }
  return payload;
}

// 方法作用：展示并维护当前租户可见的数据源。
// Args: 无。
// Returns: 数据源管理页面。
export default function DatasourcePage() {
  useLanguage();
  const { user } = useAuth();
  const canManage = ['super_admin', 'tenant_admin'].includes(user?.role || '');
  const [datasources, setDatasources] = useState<DatasourceConfig[]>([]);
  const [open, setOpen] = useState(false);
  const [editing, setEditing] = useState<DatasourceConfig | null>(null);
  const [form] = Form.useForm();
  const [dialect, setDialect] = useState('mysql');
  const [loading, setLoading] = useState(false);
  const [saving, setSaving] = useState(false);
  const [testing, setTesting] = useState(false);
  const [plugins, setPlugins] = useState<ConnectorPlugin[]>([]);
  const selectedPlugin = plugins.find(plugin => plugin.dialect === dialect);
  const [transferring, setTransferring] = useState(false);
  const [pendingSync, setPendingSync] = useState<string[]>([]);
  const [selectedNames, setSelectedNames] = useState<string[]>([]);
  const syncLocks = useRef(new Set<string>());
  const revision = useRef(0);
  const syncLabels: Record<string, string> = { idle: t("未同步"), running: t("同步中"), cancelling: t("终止中"), succeeded: t("同步完成"), cancelled: t("已终止"), failed: t("同步失败"), unknown: t("同步状态读取失败") };

  // 方法作用：每次打开表单刷新插件目录，不使用前端硬编码数据库枚举。
  // Args: 无。
  // Returns: 丢弃已关闭弹窗的旧响应。
  useEffect(() => {
    if (!open) return;
    let active = true;
    setPlugins([]);
    get<{ items: ConnectorPlugin[] }>('/datasource-types')
      .then(result => { if (active) setPlugins(result.items || []); })
      .catch(() => { if (active) message.error(t("数据库插件目录加载失败")); });
    return () => { active = false; };
  }, [open]);

  // 方法作用：加载当前身份可见的数据源列表。
  // Args: 无。
  // Returns: 请求完成后无返回值。
  const load = async (): Promise<void> => {
    setLoading(true);
    try {
      const currentRevision = ++revision.current;
      const items = await fetchDatasourceCatalog();
      if (currentRevision === revision.current) setDatasources(items);
    } catch {
      message.error(t("加载失败"));
    } finally {
      setLoading(false);
    }
  };

  useEffect(() => { void load(); }, []);

  // 方法作用：仅在存在进行中的同步时轮询列表，空闲即停。
  // Args: 无，读取当前列表状态。
  // Returns: 卸载或进入空闲时停止轮询。
  useEffect(() => {
    const active = datasources.some(item =>
      ['running', 'cancelling'].includes(item.schema_sync?.status || ''));
    if (!active) return undefined;
    let alive = true;
    let timer: number;
    // 方法作用：刷新列表而不打断用户表单，并丢弃跨操作的旧结果。
    // Args: 无。
    // Returns: 安排下一次轮询。
    const poll = async (): Promise<void> => {
      const currentRevision = revision.current;
      try {
        const items = await fetchDatasourceCatalog();
        if (alive && currentRevision === revision.current) setDatasources(items);
      } catch {
        if (alive && currentRevision === revision.current) setDatasources(items => items.map(item => ({ ...item, schema_sync: { status: 'unknown' } })));
      } finally {
        if (alive) timer = window.setTimeout(() => void poll(), 1500);
      }
    };
    timer = window.setTimeout(() => void poll(), 1500);
    return () => { alive = false; window.clearTimeout(timer); };
  }, [datasources]);

  // 方法作用：以数据源为单位提交启动或终止信号，阻止重复点击与旧轮询覆盖。
  // Args: source - 当前数据源；cancel - 是否终止。
  // Returns: 更新对应行的共享状态。
  const changeSync = async (source: DatasourceConfig, cancel = false): Promise<void> => {
    if (syncLocks.current.has(source.name)) return;
    syncLocks.current.add(source.name);
    setPendingSync([...syncLocks.current]);
    revision.current += 1;
    try {
      const state = await post<SchemaSyncStatus>(`/schema/sync${cancel ? '/cancel' : ''}?datasource=${encodeURIComponent(source.name)}`, {});
      revision.current += 1;
      setDatasources(items => items.map(item => item.name === source.name ? { ...item, schema_sync: state } : item));
      if (!cancel) message.success(t("表结构同步已启动"));
    } catch (error) {
      message.error(error instanceof Error ? error.message : t("同步失败"));
      await load();
    } finally {
      syncLocks.current.delete(source.name);
      setPendingSync([...syncLocks.current]);
    }
  };

  // 方法作用：上传 JSON 配置并在原子导入成功后刷新列表。
  // Args: file - 本地配置文件。
  // Returns: 禁止 Upload 自行发送第二次请求。
  const importSources = async (file: File): Promise<false> => {
    if (file.size > 1024 * 1024) { message.error(t("配置文件不能超过 1 MiB")); return false; }
    setTransferring(true);
    try {
      const result = await uploadConfig<{ imported: string[]; renamed: { from: string; to: string }[] }>('/datasources/import', file);
      if (result.renamed?.length) {
        message.warning(t("已导入 {value0} 个数据源；其中 {value1} 个因重名已改名：{value2}", {
          value0: result.imported.length,
          value1: result.renamed.length,
          value2: result.renamed.map(item => `${item.from} → ${item.to}`).join('、'),
        }), 8);
      } else {
        message.success(t("已导入 {value0} 个数据源，请补充连接凭证后同步", { value0: result.imported.length }));
      }
      await load();
    } catch (error) { message.error(error instanceof Error ? error.message : t("导入失败")); }
    finally { setTransferring(false); }
    return false;
  };

  // 方法作用：导出所选数据源；未勾选时导出全部。
  // Args: 无，读取当前勾选状态。
  // Returns: 无返回值。
  const exportSources = async (): Promise<void> => {
    setTransferring(true);
    try {
      const query = selectedNames.map(name => `names=${encodeURIComponent(name)}`).join('&');
      const blob = await downloadConfig(query ? `/datasources/export?${query}` : '/datasources/export');
      const url = URL.createObjectURL(blob);
      const anchor = document.createElement('a');
      anchor.href = url; anchor.download = 'datasources.json'; anchor.click();
      window.setTimeout(() => URL.revokeObjectURL(url), 1000);
      setSelectedNames([]);
    } catch (error) { message.error(error instanceof Error ? error.message : t("导出失败")); }
    finally { setTransferring(false); }
  };

  // 方法作用：在编辑弹窗完成挂载后回填配置，避免销毁隐藏内容时丢弃未挂载字段值。
  // Args: 无，读取 open/editing/datasource 状态。
  // Returns: 无返回值。
  useEffect(() => {
    if (!open || !editing) return;
    form.setFieldsValue({
      ...editing,
      version: editing.version ? [editing.version] : undefined,
      password: '',
    });
  }, [open, editing, form]);

  // 方法作用：打开新增数据源表单并清理上一轮状态。
  // Args: 无。
  // Returns: 无返回值。
  const openCreate = (): void => {
    setEditing(null);
    setDialect('mysql');
    form.resetFields();
    setOpen(true);
  };

  // 方法作用：打开编辑表单并回填不含密码的已有配置。
  // Args: datasource - 待编辑的数据源摘要。
  // Returns: 无返回值。
  const openEdit = (datasource: DatasourceConfig): void => {
    setEditing(datasource);
    setDialect(datasource.dialect);
    setOpen(true);
  };

  // 方法作用：关闭表单并重置编辑和连接测试状态。
  // Args: 无。
  // Returns: 无返回值。
  const closeModal = (): void => {
    setOpen(false);
    setEditing(null);
    setDialect('mysql');
    form.resetFields();
  };

  // 方法作用：使用当前表单临时探测连接且不保存配置。
  // Args: 无。
  // Returns: 请求完成后无返回值。
  const handleTest = async (): Promise<void> => {
    if (!selectedPlugin) { message.error(t("请选择已启用的数据库类型")); return; }
    try {
      const values = await form.validateFields();
      const payload = normalizePayload(values, dialect, editing?.name || 'connection-test');
      setTesting(true);
      const result = await post<DatasourceConnectionResult>('/datasources/test', payload);
      if (result.success) message.success(result.message || t("连接成功"));
      else message.error(result.message || t("连接失败"));
    } catch (error) {
      if (error instanceof Error) message.error(error.message || t("连接测试失败"));
    } finally {
      setTesting(false);
    }
  };

  // 方法作用：创建新数据源或提交当前数据源的编辑结果。
  // Args: 无。
  // Returns: 请求完成后无返回值。
  const handleSave = async (): Promise<void> => {
    if (!selectedPlugin) { message.error(t("请选择已启用的数据库类型")); return; }
    try {
      const values = await form.validateFields();
      const payload = normalizePayload(values, dialect, editing?.name);
      setSaving(true);
      if (editing) {
        await put(`/datasources/${encodeURIComponent(editing.name)}`, payload);
        message.success(t("数据源已更新"));
      } else {
        await post('/datasources', payload);
        message.success(t("数据源已添加"));
      }
      closeModal();
      await load();
    } catch (error) {
      if (error instanceof Error) message.error(error.message || t("保存失败"));
    } finally {
      setSaving(false);
    }
  };

  // 方法作用：删除指定数据源并刷新列表。
  // Args: name - 待删除的数据源名称。
  // Returns: 请求完成后无返回值。
  const handleDelete = async (name: string): Promise<void> => {
    try {
      await del(`/datasources/${encodeURIComponent(name)}`);
      message.success(t("已删除"));
      await load();
    } catch {
      message.error(t("删除失败"));
    }
  };

  return (
    <div style={{ maxWidth: 1120, margin: '0 auto' }}>
      <Card title={t("数据源管理")} extra={
        canManage
          ? <Space wrap>
              <Upload accept=".json,application/json" showUploadList={false} beforeUpload={importSources} disabled={transferring}>
                <Button disabled={transferring}>{t("导入数据源")}</Button>
              </Upload>
              <Tooltip title={t("导出不含密码和敏感连接参数，导入后需重新填写")}>
                <Button loading={transferring} onClick={() => void exportSources()}>
                  {selectedNames.length ? t("导出所选 ({value0})", { value0: selectedNames.length }) : t("导出数据源")}
                </Button>
              </Tooltip>
              <Button type="primary" icon={<PlusOutlined />} onClick={openCreate}>{t("添加")}</Button>
            </Space>
          : null
      }>
        <Table
          dataSource={datasources}
          rowKey="name"
          loading={loading}
          scroll={{ x: 1040 }}
          rowSelection={{
            selectedRowKeys: selectedNames,
            onChange: keys => setSelectedNames(keys as string[]),
          }}
          columns={[
            { title: t("名称"), dataIndex: 'name', key: 'name', width: 120, fixed: 'left' },
            { title: t("数据库类型"), dataIndex: 'dialect', key: 'dialect', width: 110,
              render: (value: string) => plugins.find(option => option.dialect === value)?.label || value },
            { title: t("版本"), dataIndex: 'version', key: 'version', width: 80,
              render: (value: string) => value
                ? <Tag>{value}</Tag>
                : <Typography.Text type="secondary">-</Typography.Text> },
            { title: t("主机"), dataIndex: 'host', key: 'host', width: 150, ellipsis: true },
            { title: t("端口"), dataIndex: 'port', key: 'port', width: 80 },
            { title: t("数据库"), dataIndex: 'database', key: 'database', width: 130, ellipsis: true },
            { title: t("用户"), dataIndex: 'username', key: 'username', width: 110, ellipsis: true },
            { title: t("描述"), dataIndex: 'description', key: 'description', width: 180, ellipsis: true },
            { title: t("更新时间"), dataIndex: 'updated_at', key: 'updated_at', width: 170, render: (value: string) => value
              ? new Date(value).toLocaleString()
              : <Typography.Text type="secondary">-</Typography.Text> },
            { title: t("同步状态"), key: 'sync', width: 150, render: (_: unknown, source: DatasourceConfig) => {
              const state = source.schema_sync || { status: 'idle' };
              return <Tooltip title={state.error || state.finished_at}><Tag color={['running', 'cancelling'].includes(state.status) ? 'processing' : state.status === 'succeeded' ? 'success' : ['failed', 'unknown'].includes(state.status) ? 'error' : 'default'}>{syncLabels[state.status] || state.status}</Tag></Tooltip>;
            } },
            ...(canManage ? [{
              title: t("操作"), key: 'action', width: 300, fixed: 'right' as const,
              render: (_: unknown, datasource: DatasourceConfig) => (
                <Space size={4}>
                  <Button size="small" disabled={pendingSync.includes(datasource.name) || ['running', 'cancelling', 'unknown'].includes(datasource.schema_sync?.status || '')} onClick={() => void changeSync(datasource)}>{t("同步表结构")}</Button>
                  {['running', 'cancelling'].includes(datasource.schema_sync?.status || '') && <Button danger size="small" disabled={pendingSync.includes(datasource.name) || datasource.schema_sync?.status === 'cancelling' || datasource.schema_sync?.phase === 'publishing'} onClick={() => void changeSync(datasource, true)}>{t("终止同步")}</Button>}
                  <Tooltip title={t("编辑")}>
                    <Button
                      aria-label={t("编辑 {value0}", { value0: datasource.name })}
                      icon={<EditOutlined />}
                      size="small"
                      onClick={() => openEdit(datasource)}
                    />
                  </Tooltip>
                  <Popconfirm title={t("确定删除?")} onConfirm={() => handleDelete(datasource.name)}>
                    <span>
                      <Tooltip title={t("删除")}>
                        <Button
                          aria-label={t("删除 {value0}", { value0: datasource.name })}
                          danger
                          icon={<DeleteOutlined />}
                          size="small"
                        />
                      </Tooltip>
                    </span>
                  </Popconfirm>
                </Space>
              ),
            }] : []),
          ]}
        />
      </Card>

      <Modal
        title={editing ? t("编辑数据源：{value0}", { value0: editing.name }) : t("添加数据源")}
        open={open}
        onCancel={closeModal}
        width={560}
        destroyOnHidden
        footer={[
          <Button key="test" icon={<ApiOutlined />} loading={testing} onClick={handleTest}>{t("测试连接")}</Button>,
          <Button key="cancel" onClick={closeModal}>{t("取消")}</Button>,
          <Button key="save" type="primary" loading={saving} onClick={handleSave}>
            {editing ? t("保存") : t("添加")}
          </Button>,
        ]}
      >
        <Form form={form} layout="vertical">
          {!editing && (
            <Form.Item name="name" label={t("名称")} rules={[{ required: true }]}>
              <Input placeholder={t("如 mysql_prod")} />
            </Form.Item>
          )}

          <Form.Item label={t("数据库类型")} required>
            <Select
              value={dialect}
              onChange={(value) => {
                setDialect(value);
                form.resetFields([
                  'host', 'port', 'database', 'username', 'password', 'schema',
                  'tablespace', 'service_name', 'instance', 'file_path',
                ]);
              }}
              options={plugins.map(plugin => ({ value: plugin.dialect, label: plugin.label }))}
            />
          </Form.Item>

          <Form.Item name="version" label={t("版本")}>
            <Select
              allowClear
              placeholder={t("选择或输入版本号")}
              showSearch
              mode="tags"
              maxCount={1}
              options={(VERSIONS[dialect] || []).map(value => ({ value, label: value }))}
            />
          </Form.Item>

          {(selectedPlugin?.fields || []).map(field => (
            <Form.Item
              key={field}
              name={field in LABELS ? field : ['extra_params', field]}
              label={LABELS[field] || field}
              extra={editing && field === 'password' ? t("留空表示沿用当前凭证") : undefined}
              rules={['password', 'schema', 'tablespace', 'instance'].includes(field)
                ? []
                : [{ required: dialect !== 'sqlite', message: t("必填") }]}
            >
              {field === 'password'
                ? <Input.Password placeholder={placeholder(field, dialect)} />
                : <Input placeholder={placeholder(field, dialect)} />}
            </Form.Item>
          ))}

          <Form.Item name="description" label={t("描述")}>
            <Input.TextArea placeholder={t("用途说明（可选）")} rows={2} />
          </Form.Item>
        </Form>
      </Modal>
    </div>
  );
}
