import { t, useLanguage } from '../i18n';
import { useEffect, useState } from 'react';
import {
  Button, Card, Form, Input, message, Modal, Popconfirm, Select, Space,
  Table, Tag, Tooltip, Typography,
} from 'antd';
import { ApiOutlined, DeleteOutlined, EditOutlined, PlusOutlined } from '@ant-design/icons';
import { del, get, post, put } from '../api/client';
import type { DatasourceConfig, DatasourceConnectionResult } from '../types';
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
      const data = await get<{ datasources: DatasourceConfig[] }>('/datasources');
      setDatasources(data.datasources || []);
    } catch {
      message.error(t("加载失败"));
    } finally {
      setLoading(false);
    }
  };

  useEffect(() => { void load(); }, []);

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
          ? <Button type="primary" icon={<PlusOutlined />} onClick={openCreate}>{t("添加")}</Button>
          : null
      }>
        <Table
          dataSource={datasources}
          rowKey="name"
          loading={loading}
          scroll={{ x: 1040 }}
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
            ...(canManage ? [{
              title: t("操作"), key: 'action', width: 96, fixed: 'right' as const,
              render: (_: unknown, datasource: DatasourceConfig) => (
                <Space size={4}>
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
