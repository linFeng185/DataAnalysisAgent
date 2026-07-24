import { useEffect, useMemo, useState } from 'react';
import {
  Alert, Button, Descriptions, Form, Input, Modal, Select, Space, Switch,
  Table, Tabs, Tag, Typography, message,
} from 'antd';
import {
  KeyOutlined, PlusOutlined, ReloadOutlined, SettingOutlined,
  SafetyCertificateOutlined, TeamOutlined,
} from '@ant-design/icons';
import { useNavigate } from 'react-router-dom';
import { get, patch, post } from '../api/client';

interface Tenant {
  id: number;
  name: string;
  is_active: boolean;
  user_count: number;
  created_at: string;
}

interface ManagedUser {
  id: number;
  username: string;
  tenant_id: number;
  role: string;
  is_active: boolean;
  failed_login_attempts: number;
  locked_until?: string | null;
  last_login_at?: string | null;
}

interface ConfigSummary {
  environment: string;
  multi_tenant: boolean;
  registration_enabled: boolean;
  database_configured: boolean;
  jwt_configured: boolean;
  credential_key_configured: boolean;
  vector_store_type: string;
  datasource_cache_backend: string;
  login_max_per_hour: number;
  login_lockout_threshold: number;
  login_lockout_minutes: number;
  max_queries_per_hour: number;
  mcp_server_count: number;
}

const ROLE_OPTIONS = [
  { value: 'tenant_admin', label: '租户管理员' },
  { value: 'analyst', label: '分析员' },
  { value: 'viewer', label: '只读用户' },
];

// 方法作用：提供超级管理员的租户、用户和安全配置工作台。
// Args: 无。
// Returns: 平台管理 React 页面。
export default function AdminPage() {
  console.debug('AdminPage 入口');
  const navigate = useNavigate();
  const [tenants, setTenants] = useState<Tenant[]>([]);
  const [users, setUsers] = useState<ManagedUser[]>([]);
  const [config, setConfig] = useState<ConfigSummary | null>(null);
  const [loading, setLoading] = useState(false);
  const [tenantModal, setTenantModal] = useState(false);
  const [userModal, setUserModal] = useState(false);
  const [passwordUser, setPasswordUser] = useState<ManagedUser | null>(null);
  const [tenantFilter, setTenantFilter] = useState<number | undefined>();
  const [tenantForm] = Form.useForm();
  const [userForm] = Form.useForm();
  const [passwordForm] = Form.useForm();

  // 方法作用：并行加载平台管理所需的租户、用户和脱敏配置摘要。
  // Args: 无。
  // Returns: 加载完成后无返回值。
  const load = async () => {
    console.debug('AdminPage.load 入口', { tenantFilter });
    setLoading(true);
    try {
      const query = tenantFilter ? `?tenant_id=${tenantFilter}` : '';
      const [tenantData, userData, configData] = await Promise.all([
        get<{ tenants: Tenant[] }>('/admin/tenants?page_size=100'),
        get<{ users: ManagedUser[] }>(`/admin/users${query}${query ? '&' : '?'}page_size=100`),
        get<ConfigSummary>('/admin/config'),
      ]);
      setTenants(tenantData.tenants || []);
      setUsers(userData.users || []);
      setConfig(configData);
      console.info('AdminPage.load 完成', { tenants: tenantData.tenants.length, users: userData.users.length });
    } catch (error) {
      console.error('AdminPage.load 异常', error);
      message.error('平台数据加载失败');
    } finally { setLoading(false); }
  };

  useEffect(() => { void load(); }, [tenantFilter]);

  // 方法作用：提交租户及首个租户管理员。
  // Args: 无，读取 tenantForm 当前值。
  // Returns: 创建完成后无返回值。
  const createTenant = async () => {
    console.debug('AdminPage.createTenant 入口');
    try {
      const values = await tenantForm.validateFields();
      await post('/admin/tenants', values);
      message.success('租户已创建');
      setTenantModal(false);
      tenantForm.resetFields();
      await load();
      console.info('AdminPage.createTenant 完成');
    } catch (error) { console.error('AdminPage.createTenant 异常', error); }
  };

  // 方法作用：提交普通用户或租户管理员账号。
  // Args: 无，读取 userForm 当前值。
  // Returns: 创建完成后无返回值。
  const createUser = async () => {
    console.debug('AdminPage.createUser 入口');
    try {
      const values = await userForm.validateFields();
      await post('/admin/users', values);
      message.success('用户已创建');
      setUserModal(false);
      userForm.resetFields();
      await load();
      console.info('AdminPage.createUser 完成');
    } catch (error) { console.error('AdminPage.createUser 异常', error); }
  };

  // 方法作用：切换租户启用状态并刷新工作台。
  // Args: tenant - 目标租户；isActive - 新状态。
  // Returns: 更新完成后无返回值。
  const toggleTenant = async (tenant: Tenant, isActive: boolean) => {
    console.debug('AdminPage.toggleTenant 入口', { tenantId: tenant.id, isActive });
    try {
      await patch(`/admin/tenants/${tenant.id}`, { is_active: isActive });
      await load();
      console.info('AdminPage.toggleTenant 完成', { tenantId: tenant.id });
    } catch (error) { console.error('AdminPage.toggleTenant 异常', error); message.error('租户状态更新失败'); }
  };

  // 方法作用：切换用户启用状态并刷新工作台。
  // Args: user - 目标用户；isActive - 新状态。
  // Returns: 更新完成后无返回值。
  const toggleUser = async (user: ManagedUser, isActive: boolean) => {
    console.debug('AdminPage.toggleUser 入口', { userId: user.id, isActive });
    try {
      await patch(`/admin/users/${user.id}`, { is_active: isActive });
      await load();
      console.info('AdminPage.toggleUser 完成', { userId: user.id });
    } catch (error) { console.error('AdminPage.toggleUser 异常', error); message.error('用户状态更新失败'); }
  };

  // 方法作用：修改普通用户角色。
  // Args: user - 目标用户；role - 新角色。
  // Returns: 更新完成后无返回值。
  const updateRole = async (user: ManagedUser, role: string) => {
    console.debug('AdminPage.updateRole 入口', { userId: user.id, role });
    try {
      await patch(`/admin/users/${user.id}`, { role });
      await load();
      console.info('AdminPage.updateRole 完成', { userId: user.id });
    } catch (error) { console.error('AdminPage.updateRole 异常', error); message.error('角色更新失败'); }
  };

  // 方法作用：重置选中用户密码并清除锁定状态。
  // Args: 无，读取 passwordUser 和 passwordForm。
  // Returns: 重置完成后无返回值。
  const resetPassword = async () => {
    console.debug('AdminPage.resetPassword 入口', { userId: passwordUser?.id });
    if (!passwordUser) return;
    try {
      const values = await passwordForm.validateFields();
      await post(`/admin/users/${passwordUser.id}/reset-password`, { password: values.password });
      message.success('密码已重置');
      setPasswordUser(null);
      passwordForm.resetFields();
      await load();
      console.info('AdminPage.resetPassword 完成', { userId: passwordUser.id });
    } catch (error) { console.error('AdminPage.resetPassword 异常', error); }
  };

  const tenantNames = useMemo(
    () => new Map(tenants.map(tenant => [tenant.id, tenant.name])),
    [tenants],
  );

  const tenantWorkspace = (
    <Table<Tenant> rowKey="id" dataSource={tenants} loading={loading} pagination={{ pageSize: 20 }}
      title={() => <Space><Button type="primary" icon={<PlusOutlined />} onClick={() => setTenantModal(true)}>创建租户</Button>
        <Button icon={<ReloadOutlined />} onClick={() => void load()} aria-label="刷新租户" /></Space>}
      columns={[
        { title: 'ID', dataIndex: 'id', width: 72 },
        { title: '租户名称', dataIndex: 'name' },
        { title: '用户数', dataIndex: 'user_count', width: 100 },
        { title: '状态', dataIndex: 'is_active', width: 100, render: value => <Tag color={value ? 'green' : 'default'}>{value ? '启用' : '停用'}</Tag> },
        { title: '操作', width: 120, render: (_, tenant) => <Switch checked={tenant.is_active} disabled={tenant.id === 1}
          onChange={value => void toggleTenant(tenant, value)} /> },
      ]} />
  );

  const userWorkspace = (
    <Table<ManagedUser> rowKey="id" dataSource={users} loading={loading} pagination={{ pageSize: 20 }}
      title={() => <Space wrap><Button type="primary" icon={<PlusOutlined />} onClick={() => setUserModal(true)}>创建用户</Button>
        <Select allowClear placeholder="全部租户" style={{ width: 180 }} value={tenantFilter} onChange={setTenantFilter}
          options={tenants.map(tenant => ({ value: tenant.id, label: tenant.name }))} />
        <Button icon={<ReloadOutlined />} onClick={() => void load()} aria-label="刷新用户" /></Space>}
      columns={[
        { title: 'ID', dataIndex: 'id', width: 72 },
        { title: '用户名', dataIndex: 'username' },
        { title: '租户', dataIndex: 'tenant_id', render: value => tenantNames.get(value) || value },
        { title: '角色', dataIndex: 'role', width: 150, render: (role, user) => user.id === 1
          ? <Tag color="red">super_admin</Tag>
          : <Select value={role} style={{ width: 130 }} options={ROLE_OPTIONS} onChange={value => void updateRole(user, value)} /> },
        { title: '失败次数', dataIndex: 'failed_login_attempts', width: 100 },
        { title: '状态', width: 90, render: (_, user) => <Switch checked={user.is_active} disabled={user.id === 1}
          onChange={value => void toggleUser(user, value)} /> },
        { title: '操作', width: 110, render: (_, user) => <Button icon={<KeyOutlined />}
          onClick={() => setPasswordUser(user)}>重置密码</Button> },
      ]} />
  );

  const configWorkspace = config ? <Space direction="vertical" size={20} style={{ width: '100%' }}>
    {(!config.database_configured || !config.jwt_configured || !config.credential_key_configured) &&
      <Alert type="warning" showIcon message="关键安全配置不完整" />}
    <Descriptions bordered size="small" column={{ xs: 1, md: 2 }} items={[
      { key: 'env', label: '运行环境', children: config.environment },
      { key: 'tenant', label: '多租户隔离', children: config.multi_tenant ? '启用' : '单租户' },
      { key: 'register', label: '公开注册', children: config.registration_enabled ? '启用' : '关闭' },
      { key: 'database', label: '状态数据库', children: config.database_configured ? '已配置' : '未配置' },
      { key: 'jwt', label: 'JWT 密钥', children: config.jwt_configured ? '已配置' : '未配置' },
      { key: 'credential', label: '凭证主密钥', children: config.credential_key_configured ? '已配置' : '未配置' },
      { key: 'lockout', label: '登录锁定', children: `${config.login_lockout_threshold} 次 / ${config.login_lockout_minutes} 分钟` },
      { key: 'rate', label: '登录频率', children: `${config.login_max_per_hour} 次/小时` },
      { key: 'cache', label: '数据源缓存', children: config.datasource_cache_backend },
      { key: 'mcp', label: '内置 MCP', children: config.mcp_server_count },
    ]} />
    <Space wrap>
      <Button icon={<SafetyCertificateOutlined />} onClick={() => navigate('/skills')}>系统 Skills</Button>
      <Button icon={<SettingOutlined />} onClick={() => navigate('/knowledge')}>系统知识库</Button>
      <Button icon={<TeamOutlined />} onClick={() => navigate('/mcp')}>系统 MCP</Button>
    </Space>
  </Space> : null;

  console.info('AdminPage 完成', { tenantCount: tenants.length, userCount: users.length });
  return <div style={{ padding: 24, maxWidth: 1280, margin: '0 auto' }}>
    <Typography.Title level={3} style={{ marginTop: 0 }}>平台管理</Typography.Title>
    <Tabs items={[
      { key: 'tenants', label: '租户管理', children: tenantWorkspace },
      { key: 'users', label: '用户管理', children: userWorkspace },
      { key: 'security', label: '安全配置', children: configWorkspace },
    ]} />

    <Modal title="创建租户" open={tenantModal} onOk={() => void createTenant()} onCancel={() => setTenantModal(false)} destroyOnClose>
      <Form form={tenantForm} layout="vertical">
        <Form.Item name="name" label="租户名称" rules={[{ required: true, max: 128 }]}><Input /></Form.Item>
        <Form.Item name="admin_username" label="管理员用户名" rules={[{ required: true, max: 64 }]}><Input /></Form.Item>
        <Form.Item name="admin_password" label="管理员密码" rules={[{ required: true, min: 8, max: 72 }]}><Input.Password /></Form.Item>
      </Form>
    </Modal>

    <Modal title="创建用户" open={userModal} onOk={() => void createUser()} onCancel={() => setUserModal(false)} destroyOnClose>
      <Form form={userForm} layout="vertical" initialValues={{ role: 'analyst' }}>
        <Form.Item name="tenant_id" label="所属租户" rules={[{ required: true }]}><Select options={tenants.filter(t => t.is_active).map(t => ({ value: t.id, label: t.name }))} /></Form.Item>
        <Form.Item name="username" label="用户名" rules={[{ required: true, max: 64 }]}><Input /></Form.Item>
        <Form.Item name="password" label="初始密码" rules={[{ required: true, min: 8, max: 72 }]}><Input.Password /></Form.Item>
        <Form.Item name="role" label="角色" rules={[{ required: true }]}><Select options={ROLE_OPTIONS} /></Form.Item>
      </Form>
    </Modal>

    <Modal title={`重置 ${passwordUser?.username || ''} 的密码`} open={Boolean(passwordUser)} onOk={() => void resetPassword()}
      onCancel={() => setPasswordUser(null)} destroyOnClose>
      <Form form={passwordForm} layout="vertical">
        <Form.Item name="password" label="新密码" rules={[{ required: true, min: 8, max: 72 }]}><Input.Password /></Form.Item>
      </Form>
    </Modal>
  </div>;
}
