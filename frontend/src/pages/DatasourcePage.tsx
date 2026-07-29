import { useEffect, useState } from 'react';
import {
  Button, Card, Form, Input, message, Modal, Popconfirm, Select, Space,
  Table, Tag, Tooltip, Typography,
} from 'antd';
import { ApiOutlined, DeleteOutlined, EditOutlined, PlusOutlined } from '@ant-design/icons';
import { del, get, post, put } from '../api/client';
import type { DatasourceConfig, DatasourceConnectionResult } from '../types';
import { useAuth } from '../hooks/AuthContext';

const DIALECTS = [
  { value: 'mysql', label: 'MySQL' },
  { value: 'postgres', label: 'PostgreSQL' },
  { value: 'clickhouse', label: 'ClickHouse' },
  { value: 'oracle', label: 'Oracle' },
  { value: 'mssql', label: 'SQL Server' },
  { value: 'sqlite', label: 'SQLite' },
];

const VERSIONS: Record<string, string[]> = {
  mysql: ['5.7', '8.0', '8.1', '8.4'],
  postgres: ['14', '15', '16', '17'],
  clickhouse: ['23.x', '24.x', '25.x'],
  oracle: ['19c', '21c', '23ai'],
  mssql: ['2019', '2022'],
  sqlite: [],
};

// 方法作用：按数据库方言返回需要展示的连接字段。
// Args: dialect - 当前选择的数据库方言。
// Returns: 表单字段名列表。
function dialectFields(dialect: string): string[] {
  switch (dialect) {
    case 'oracle': return ['host', 'port', 'service_name', 'tablespace', 'username', 'password'];
    case 'mssql': return ['host', 'port', 'instance', 'database', 'schema', 'username', 'password'];
    case 'postgres': return ['host', 'port', 'database', 'schema', 'username', 'password'];
    case 'sqlite': return ['file_path'];
    default: return ['host', 'port', 'database', 'username', 'password'];
  }
}

const LABELS: Record<string, string> = {
  host: '主机', port: '端口', database: '数据库名', username: '用户名',
  password: '密码', schema: 'Schema', tablespace: '表空间',
  service_name: '服务名/SID', instance: '实例名', file_path: '文件路径',
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

  // 方法作用：加载当前身份可见的数据源列表。
  // Args: 无。
  // Returns: 请求完成后无返回值。
  const load = async (): Promise<void> => {
    setLoading(true);
    try {
      const data = await get<{ datasources: DatasourceConfig[] }>('/datasources');
      setDatasources(data.datasources || []);
    } catch {
      message.error('加载失败');
    } finally {
      setLoading(false);
    }
  };

  useEffect(() => { void load(); }, []);

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
    form.setFieldsValue({
      ...datasource,
      version: datasource.version ? [datasource.version] : undefined,
      password: '',
    });
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
    try {
      const values = await form.validateFields();
      const payload = normalizePayload(values, dialect, editing?.name || 'connection-test');
      setTesting(true);
      const result = await post<DatasourceConnectionResult>('/datasources/test', payload);
      if (result.success) message.success(result.message || '连接成功');
      else message.error(result.message || '连接失败');
    } catch (error) {
      if (error instanceof Error) message.error(error.message || '连接测试失败');
    } finally {
      setTesting(false);
    }
  };

  // 方法作用：创建新数据源或提交当前数据源的编辑结果。
  // Args: 无。
  // Returns: 请求完成后无返回值。
  const handleSave = async (): Promise<void> => {
    try {
      const values = await form.validateFields();
      const payload = normalizePayload(values, dialect, editing?.name);
      setSaving(true);
      if (editing) {
        await put(`/datasources/${encodeURIComponent(editing.name)}`, payload);
        message.success('数据源已更新');
      } else {
        await post('/datasources', payload);
        message.success('数据源已添加');
      }
      closeModal();
      await load();
    } catch (error) {
      if (error instanceof Error) message.error(error.message || '保存失败');
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
      message.success('已删除');
      await load();
    } catch {
      message.error('删除失败');
    }
  };

  return (
    <div style={{ maxWidth: 1120, margin: '0 auto' }}>
      <Card title="数据源管理" extra={
        canManage
          ? <Button type="primary" icon={<PlusOutlined />} onClick={openCreate}>添加</Button>
          : null
      }>
        <Table
          dataSource={datasources}
          rowKey="name"
          loading={loading}
          scroll={{ x: 1040 }}
          columns={[
            { title: '名称', dataIndex: 'name', key: 'name', width: 120, fixed: 'left' },
            { title: '数据库类型', dataIndex: 'dialect', key: 'dialect', width: 110,
              render: (value: string) => DIALECTS.find(option => option.value === value)?.label || value },
            { title: '版本', dataIndex: 'version', key: 'version', width: 80,
              render: (value: string) => value
                ? <Tag>{value}</Tag>
                : <Typography.Text type="secondary">-</Typography.Text> },
            { title: '主机', dataIndex: 'host', key: 'host', width: 150, ellipsis: true },
            { title: '端口', dataIndex: 'port', key: 'port', width: 80 },
            { title: '数据库', dataIndex: 'database', key: 'database', width: 130, ellipsis: true },
            { title: '用户', dataIndex: 'username', key: 'username', width: 110, ellipsis: true },
            { title: '描述', dataIndex: 'description', key: 'description', width: 180, ellipsis: true },
            ...(canManage ? [{
              title: '操作', key: 'action', width: 96, fixed: 'right' as const,
              render: (_: unknown, datasource: DatasourceConfig) => (
                <Space size={4}>
                  <Tooltip title="编辑">
                    <Button
                      aria-label={`编辑 ${datasource.name}`}
                      icon={<EditOutlined />}
                      size="small"
                      onClick={() => openEdit(datasource)}
                    />
                  </Tooltip>
                  <Popconfirm title="确定删除?" onConfirm={() => handleDelete(datasource.name)}>
                    <Tooltip title="删除">
                      <Button
                        aria-label={`删除 ${datasource.name}`}
                        danger
                        icon={<DeleteOutlined />}
                        size="small"
                      />
                    </Tooltip>
                  </Popconfirm>
                </Space>
              ),
            }] : []),
          ]}
        />
      </Card>

      <Modal
        title={editing ? `编辑数据源：${editing.name}` : '添加数据源'}
        open={open}
        onCancel={closeModal}
        width={560}
        destroyOnClose
        footer={[
          <Button key="test" icon={<ApiOutlined />} loading={testing} onClick={handleTest}>测试连接</Button>,
          <Button key="cancel" onClick={closeModal}>取消</Button>,
          <Button key="save" type="primary" loading={saving} onClick={handleSave}>
            {editing ? '保存' : '添加'}
          </Button>,
        ]}
      >
        <Form form={form} layout="vertical" preserve={false}>
          {!editing && (
            <Form.Item name="name" label="名称" rules={[{ required: true }]}>
              <Input placeholder="如 mysql_prod" />
            </Form.Item>
          )}

          <Form.Item label="数据库类型" required>
            <Select
              value={dialect}
              onChange={(value) => {
                setDialect(value);
                form.resetFields([
                  'host', 'port', 'database', 'username', 'password', 'schema',
                  'tablespace', 'service_name', 'instance', 'file_path',
                ]);
              }}
              options={DIALECTS}
            />
          </Form.Item>

          <Form.Item name="version" label="版本">
            <Select
              allowClear
              placeholder="选择或输入版本号"
              showSearch
              mode="tags"
              maxCount={1}
              options={(VERSIONS[dialect] || []).map(value => ({ value, label: value }))}
            />
          </Form.Item>

          {dialectFields(dialect).map(field => (
            <Form.Item
              key={field}
              name={field}
              label={LABELS[field] || field}
              extra={editing && field === 'password' ? '留空表示沿用当前凭证' : undefined}
              rules={['password', 'schema', 'tablespace', 'instance'].includes(field)
                ? []
                : [{ required: dialect !== 'sqlite', message: '必填' }]}
            >
              {field === 'password'
                ? <Input.Password placeholder={placeholder(field, dialect)} />
                : <Input placeholder={placeholder(field, dialect)} />}
            </Form.Item>
          ))}

          <Form.Item name="description" label="描述">
            <Input.TextArea placeholder="用途说明（可选）" rows={2} />
          </Form.Item>
        </Form>
      </Modal>
    </div>
  );
}
