import { useEffect, useMemo, useState } from 'react';
import type { ReactNode } from 'react';
import {
  Alert, Button, Descriptions, Divider, Form, Input, InputNumber, Modal, Select,
  Space, Switch, Table, Tabs, Tag, Tooltip, Typography, message,
} from 'antd';
import {
  DeleteOutlined, EditOutlined, KeyOutlined, MinusCircleOutlined, PlusOutlined, ReloadOutlined,
  SafetyCertificateOutlined, SettingOutlined, TeamOutlined,
} from '@ant-design/icons';
import { useNavigate } from 'react-router-dom';
import { del, get, patch, post, put } from '../api/client';
import { useAuth } from '../hooks/AuthContext';

interface Tenant {
  id: number;
  code: string;
  name: string;
  is_active: boolean;
  user_count: number;
  created_at: string;
}

interface LLMProvider {
  id: number;
  code: string;
  display_name: string;
  protocol: 'openai_compatible' | 'anthropic';
  default_base_url: string;
  capability_schema: CapabilityFormSchema;
  is_active: boolean;
}

type CapabilityFieldType = 'boolean' | 'integer' | 'number' | 'text' | 'select' | 'multiselect';

interface CapabilityOption {
  label: string;
  value: string;
}

interface CapabilityFieldDefinition {
  key: string;
  label: string;
  type: CapabilityFieldType;
  required?: boolean;
  default?: unknown;
  options?: CapabilityOption[];
  minimum?: number | null;
  maximum?: number | null;
  description?: string;
}

interface CapabilityFormSchema {
  fields: CapabilityFieldDefinition[];
}

interface LLMModel {
  id: number;
  provider_id: number;
  model_id: string;
  display_name: string;
  capabilities: Record<string, unknown>;
  is_active: boolean;
}

interface TenantLLMConnection {
  id: number;
  provider_id: number;
  name: string;
  base_url: string;
  provider_code: string;
  provider_name: string;
  model_catalog_ids: number[];
  is_active: boolean;
  api_key_configured: boolean;
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

interface AccessPolicy {
  id: number | null;
  policy_key: string;
  path: string;
  path_type: 'exact' | 'template';
  methods: string[];
  auth_mode: 'public' | 'optional' | 'jwt' | 'jwt_or_admin_key' | 'super_admin';
  access_log_mode: 'standard' | 'security' | 'audit' | 'none';
  source: 'yaml' | 'database';
  priority: number;
  enabled: boolean;
  description: string;
}

interface AccessIpRule {
  id: number;
  policy_key: string;
  action: 'allow' | 'deny';
  cidr: string;
  enabled: boolean;
  description: string;
}

interface AccessPolicySnapshot {
  policies: AccessPolicy[];
  ip_rules: AccessIpRule[];
  defaults: { auth_mode: string; access_log_mode: string };
}

const ROLE_OPTIONS = [
  { value: 'tenant_admin', label: '租户管理员' },
  { value: 'analyst', label: '分析员' },
  { value: 'viewer', label: '只读用户' },
];

const AUTH_MODE_OPTIONS = [
  { value: 'jwt', label: 'JWT' },
  { value: 'jwt_or_admin_key', label: 'JWT / Admin Key' },
  { value: 'super_admin', label: '固定超级管理员' },
];

const ACCESS_LOG_OPTIONS = [
  { value: 'standard', label: '普通访问日志' },
  { value: 'security', label: '安全日志' },
  { value: 'audit', label: '审计日志' },
  { value: 'none', label: '静默成功访问' },
];

const CAPABILITY_TYPE_OPTIONS = [
  { value: 'boolean', label: '开关' },
  { value: 'integer', label: '整数' },
  { value: 'number', label: '数值' },
  { value: 'text', label: '文本' },
  { value: 'select', label: '单选' },
  { value: 'multiselect', label: '多选' },
];

// 方法作用：把数据库能力表单转换为厂商编辑器使用的字段数组。
// Args: schema - 厂商能力表单定义。
// Returns: options 已转为可编辑字符串数组的字段列表。
function capabilitySchemaToForm(schema?: CapabilityFormSchema): Record<string, unknown>[] {
  return (schema?.fields || []).map(field => ({
    ...field,
    options: (field.options || []).map(option => option.value),
  }));
}

// 方法作用：把厂商编辑器字段转换为后端动态表单定义。
// Args: fields - Form.List 产生的能力字段数组。
// Returns: 可提交给 capability_schema.fields 的字段定义。
function capabilityFieldsToSchema(fields: Record<string, unknown>[] = []): CapabilityFormSchema {
  return {
    fields: fields.map(field => ({
      key: String(field.key || '').trim(),
      label: String(field.label || '').trim(),
      type: field.type as CapabilityFieldType,
      required: Boolean(field.required),
      ...('default' in field ? { default: field.default } : {}),
      options: ['select', 'multiselect'].includes(String(field.type))
        ? ((field.options as string[] | undefined) || []).filter(Boolean).map(value => ({
          label: value,
          value,
        }))
        : [],
      minimum: typeof field.minimum === 'number' ? field.minimum : null,
      maximum: typeof field.maximum === 'number' ? field.maximum : null,
      description: String(field.description || ''),
    })),
  };
}

// 方法作用：从厂商能力定义生成新模型表单默认值。
// Args: provider - 当前选中的模型厂商。
// Returns: 以字段 key 为键的能力默认值对象。
function capabilityDefaults(provider?: LLMProvider): Record<string, unknown> {
  return Object.fromEntries(
    (provider?.capability_schema?.fields || [])
      .filter(field => field.default !== undefined && field.default !== null)
      .map(field => [field.key, field.default]),
  );
}

// 方法作用：根据厂商字段类型渲染模型能力输入控件。
// Args: field - 一个厂商级能力字段定义。
// Returns: 绑定 capabilities.<key> 的 Ant Design 表单项。
function renderCapabilityField(field: CapabilityFieldDefinition): ReactNode {
  const rules = field.required ? [{ required: true, message: `请填写${field.label}` }] : [];
  const name = ['capabilities', field.key];
  if (field.type === 'boolean') {
    return <Form.Item key={field.key} name={name} label={field.label}
      valuePropName="checked" tooltip={field.description} rules={rules}><Switch /></Form.Item>;
  }
  if (field.type === 'integer' || field.type === 'number') {
    return <Form.Item key={field.key} name={name} label={field.label}
      tooltip={field.description} rules={rules}><InputNumber min={field.minimum ?? undefined}
        max={field.maximum ?? undefined} precision={field.type === 'integer' ? 0 : undefined}
        style={{ width: '100%' }} /></Form.Item>;
  }
  if (field.type === 'select' || field.type === 'multiselect') {
    return <Form.Item key={field.key} name={name} label={field.label}
      tooltip={field.description} rules={rules}><Select mode={field.type === 'multiselect' ? 'multiple' : undefined}
        options={field.options || []} /></Form.Item>;
  }
  return <Form.Item key={field.key} name={name} label={field.label}
    tooltip={field.description} rules={rules}><Input /></Form.Item>;
}

// 方法作用：把模型能力对象按厂商字段标签转换为可扫描摘要。
// Args: capabilities - 模型能力值；provider - 所属厂商。
// Returns: 标签化的能力摘要 React 节点。
function renderCapabilitySummary(
  capabilities: Record<string, unknown>,
  provider?: LLMProvider,
): ReactNode {
  const labels = new Map((provider?.capability_schema?.fields || []).map(field => [field.key, field.label]));
  return <Space size={[4, 4]} wrap>{Object.entries(capabilities || {}).map(([key, value]) => (
    <Tag key={key}>{labels.get(key) || key}: {Array.isArray(value) ? value.join(', ') : String(value)}</Tag>
  ))}</Space>;
}

// 方法作用：提供超级管理员的租户、用户和安全配置工作台。
// Args: 无。
// Returns: 平台管理 React 页面。
export default function AdminPage() {
  console.debug('AdminPage 入口');
  const navigate = useNavigate();
  const { user } = useAuth();
  const isTenantAdmin = user?.role === 'tenant_admin';
  const [tenants, setTenants] = useState<Tenant[]>([]);
  const [users, setUsers] = useState<ManagedUser[]>([]);
  const [config, setConfig] = useState<ConfigSummary | null>(null);
  const [accessPolicies, setAccessPolicies] = useState<AccessPolicy[]>([]);
  const [accessIpRules, setAccessIpRules] = useState<AccessIpRule[]>([]);
  const [loading, setLoading] = useState(false);
  const [tenantModal, setTenantModal] = useState(false);
  const [userModal, setUserModal] = useState(false);
  const [passwordUser, setPasswordUser] = useState<ManagedUser | null>(null);
  const [policyModal, setPolicyModal] = useState(false);
  const [editingPolicy, setEditingPolicy] = useState<AccessPolicy | null>(null);
  const [rulePolicy, setRulePolicy] = useState<AccessPolicy | null>(null);
  const [tenantFilter, setTenantFilter] = useState<number | undefined>();
  const [providers, setProviders] = useState<LLMProvider[]>([]);
  const [providerModels, setProviderModels] = useState<LLMModel[]>([]);
  const [connections, setConnections] = useState<TenantLLMConnection[]>([]);
  const [selectedProviderId, setSelectedProviderId] = useState<number | undefined>();
  const [selectedDefaultConnectionId, setSelectedDefaultConnectionId] = useState<number | undefined>();
  const [providerModal, setProviderModal] = useState(false);
  const [modelModal, setModelModal] = useState(false);
  const [editingProvider, setEditingProvider] = useState<LLMProvider | null>(null);
  const [editingModel, setEditingModel] = useState<LLMModel | null>(null);
  const [connectionModal, setConnectionModal] = useState(false);
  const [editingConnection, setEditingConnection] = useState<TenantLLMConnection | null>(null);
  const [tenantForm] = Form.useForm();
  const [userForm] = Form.useForm();
  const [passwordForm] = Form.useForm();
  const [policyForm] = Form.useForm();
  const [ruleForm] = Form.useForm();
  const [providerForm] = Form.useForm();
  const [modelForm] = Form.useForm();
  const [connectionForm] = Form.useForm();
  const [defaultConnectionForm] = Form.useForm();

  // 方法作用：按当前角色加载租户用户治理或平台管理与 LLM 目录数据。
  // Args: 无。
  // Returns: 加载完成后无返回值。
  const load = async () => {
    console.debug('AdminPage.load 入口', { tenantFilter });
    setLoading(true);
    try {
      const providerData = await get<{ providers: LLMProvider[] }>('/admin/llm/providers');
      const nextProviders = providerData.providers || [];
      setProviders(nextProviders);
      const nextProviderId = selectedProviderId && nextProviders.some(item => item.id === selectedProviderId)
        ? selectedProviderId
        : nextProviders[0]?.id;
      setSelectedProviderId(nextProviderId);
      if (nextProviderId) {
        const modelData = await get<{ models: LLMModel[] }>(`/admin/llm/providers/${nextProviderId}/models`);
        setProviderModels(modelData.models || []);
      } else {
        setProviderModels([]);
      }
      if (isTenantAdmin) {
        const [userData, connectionData] = await Promise.all([
          get<{ users: ManagedUser[] }>('/admin/users?page_size=100'),
          get<{ connections: TenantLLMConnection[] }>('/admin/llm/connections'),
        ]);
        setUsers(userData.users || []);
        setConnections(connectionData.connections || []);
        console.info('AdminPage.load 租户工作区完成', {
          users: userData.users.length,
          connections: connectionData.connections.length,
        });
        return;
      }
      const [tenantData, configData, accessData] = await Promise.all([
        get<{ tenants: Tenant[] }>('/admin/tenants?page_size=100'),
        get<ConfigSummary>('/admin/config'),
        get<AccessPolicySnapshot>('/admin/access-policies'),
      ]);
      setTenants(tenantData.tenants || []);
      setConfig(configData);
      setAccessPolicies(accessData.policies || []);
      setAccessIpRules(accessData.ip_rules || []);
      console.info('AdminPage.load 平台工作区完成', { tenants: tenantData.tenants.length });
    } catch (error) {
      console.error('AdminPage.load 异常', error);
      message.error('平台数据加载失败');
    } finally { setLoading(false); }
  };

  useEffect(() => { void load(); }, [tenantFilter, isTenantAdmin]);

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

  // 方法作用：加载选中平台厂商下的模型目录。
  // Args: providerId - 平台厂商目录 ID。
  // Returns: 加载完成后无返回值。
  const loadProviderModels = async (providerId: number) => {
    try {
      const data = await get<{ models: LLMModel[] }>(`/admin/llm/providers/${providerId}/models`);
      setSelectedProviderId(providerId);
      setProviderModels(data.models || []);
    } catch (error) {
      console.error('AdminPage.loadProviderModels 异常', error);
      message.error('模型目录加载失败');
    }
  };

  // 方法作用：打开模型厂商创建或编辑表单并恢复动态字段定义。
  // Args: provider - 可选待编辑厂商，缺失时创建新厂商。
  // Returns: 无返回值。
  const openProviderEditor = (provider?: LLMProvider) => {
    setEditingProvider(provider || null);
    providerForm.setFieldsValue(provider ? {
      code: provider.code,
      display_name: provider.display_name,
      protocol: provider.protocol,
      default_base_url: provider.default_base_url,
      capability_fields: capabilitySchemaToForm(provider.capability_schema),
    } : {
      protocol: 'openai_compatible',
      capability_fields: [],
    });
    setProviderModal(true);
  };

  // 方法作用：创建或更新平台模型厂商及其动态能力表单定义。
  // Args: 无，读取 providerForm 和 editingProvider。
  // Returns: 保存完成后无返回值。
  const saveProvider = async () => {
    try {
      const values = await providerForm.validateFields();
      const { capability_fields: fields, ...metadata } = values;
      const payload = {
        ...metadata,
        ...(editingProvider || (fields || []).length > 0
          ? { capability_schema: capabilityFieldsToSchema(fields) }
          : {}),
      };
      if (editingProvider) {
        const { code: _code, protocol: _protocol, ...updates } = payload;
        await patch(`/admin/llm/providers/${editingProvider.id}`, updates);
      } else {
        await post('/admin/llm/providers', payload);
      }
      providerForm.resetFields();
      setProviderModal(false);
      setEditingProvider(null);
      await load();
      message.success(editingProvider ? '模型厂商已更新' : '模型厂商已创建');
    } catch (error) { console.error('AdminPage.saveProvider 异常', error); }
  };

  // 方法作用：打开模型创建或编辑表单并按厂商 schema 设置能力值。
  // Args: model - 可选待编辑模型，缺失时创建新模型。
  // Returns: 无返回值。
  const openModelEditor = (model?: LLMModel) => {
    const provider = providers.find(item => item.id === selectedProviderId);
    setEditingModel(model || null);
    modelForm.setFieldsValue({
      model_id: model?.model_id || '',
      display_name: model?.display_name || '',
      capabilities: model?.capabilities || capabilityDefaults(provider),
    });
    setModelModal(true);
  };

  // 方法作用：创建或更新选中厂商模型及动态能力值。
  // Args: 无，读取 modelForm、editingModel 和 selectedProviderId。
  // Returns: 保存完成后无返回值。
  const saveModel = async () => {
    if (!selectedProviderId) return;
    try {
      const values = await modelForm.validateFields();
      if (editingModel) {
        await patch(`/admin/llm/models/${editingModel.id}`, {
          display_name: values.display_name,
          capabilities: values.capabilities || {},
        });
      } else {
        await post(`/admin/llm/providers/${selectedProviderId}/models`, {
          ...values,
          capabilities: values.capabilities || {},
        });
      }
      modelForm.resetFields();
      setModelModal(false);
      setEditingModel(null);
      await loadProviderModels(selectedProviderId);
      message.success(editingModel ? '模型已更新' : '模型已创建');
    } catch (error) { console.error('AdminPage.saveModel 异常', error); }
  };

  // 方法作用：创建或更新当前租户的命名 LLM 连接。
  // Args: 无，读取 connectionForm 当前值和编辑状态。
  // Returns: 保存完成后无返回值。
  const saveConnection = async () => {
    try {
      const snapshot = connectionForm.getFieldsValue(true);
      console.info('命名连接保存校验边界', {
        editing: Boolean(editingConnection),
        field_names: Object.keys(snapshot),
        provider_id: snapshot.provider_id,
        model_count: Array.isArray(snapshot.model_catalog_ids) ? snapshot.model_catalog_ids.length : 0,
        api_key_configured: Boolean(snapshot.api_key),
        default_connection_present: Boolean(snapshot.default_connection_id),
        default_model_present: Boolean(snapshot.default_model_catalog_id),
      });
      const values = await connectionForm.validateFields();
      console.info('命名连接表单校验通过', {
        editing: Boolean(editingConnection),
        provider_id: values.provider_id,
        model_count: Array.isArray(values.model_catalog_ids) ? values.model_catalog_ids.length : 0,
      });
      if (editingConnection) {
        await patch(`/admin/llm/connections/${editingConnection.id}`, values);
      } else {
        await post('/admin/llm/connections', values);
      }
      connectionForm.resetFields();
      setEditingConnection(null);
      setConnectionModal(false);
      await load();
      message.success('LLM 连接已保存');
    } catch (error) {
      const errorFields = (
        typeof error === 'object' && error !== null && 'errorFields' in error
          ? (error as { errorFields?: Array<{ name?: Array<string | number> }> }).errorFields
          : []
      ) || [];
      console.info('命名连接表单校验阻断', {
        error_fields: errorFields.map(item => item.name?.join('.') || ''),
      });
      console.error('AdminPage.saveConnection 异常', error);
    }
  };

  // 方法作用：删除当前租户不再使用的命名 LLM 连接。
  // Args: connection - 当前租户连接。
  // Returns: 删除完成后无返回值。
  const deleteConnection = async (connection: TenantLLMConnection) => {
    try {
      await del(`/admin/llm/connections/${connection.id}`);
      await load();
      message.success('LLM 连接已删除');
    } catch (error) { console.error('AdminPage.deleteConnection 异常', error); message.error('LLM 连接删除失败'); }
  };

  // 方法作用：设置当前租户默认的命名连接和对话模型。
  // Args: 无，读取 defaultConnectionForm 当前默认选择字段。
  // Returns: 保存完成后无返回值。
  const setDefaultConnection = async () => {
    try {
      const values = await defaultConnectionForm.validateFields();
      await put('/admin/llm/default', {
        connection_id: values.default_connection_id,
        model_catalog_id: values.default_model_catalog_id,
      });
      message.success('默认 LLM 已更新');
    } catch (error) { console.error('AdminPage.setDefaultConnection 异常', error); message.error('默认 LLM 更新失败'); }
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

  // 方法作用：打开动态访问策略创建或编辑表单。
  // Args: policy - 可选数据库策略，缺失时进入创建模式。
  // Returns: 无返回值。
  const openPolicyEditor = (policy?: AccessPolicy) => {
    console.debug('AdminPage.openPolicyEditor 入口', { policyKey: policy?.policy_key || '' });
    setEditingPolicy(policy || null);
    policyForm.setFieldsValue(policy ? {
      policy_key: policy.policy_key,
      path: policy.path,
      path_type: policy.path_type,
      methods: policy.methods,
      auth_mode: policy.auth_mode,
      access_log_mode: policy.access_log_mode,
      priority: policy.priority,
      description: policy.description,
    } : {
      path_type: 'exact', methods: ['GET'], auth_mode: 'jwt',
      access_log_mode: 'standard', priority: 0,
    });
    setPolicyModal(true);
    console.info('AdminPage.openPolicyEditor 完成', { editing: Boolean(policy) });
  };

  // 方法作用：创建或更新数据库动态访问策略。
  // Args: 无，读取 policyForm 和 editingPolicy。
  // Returns: 保存完成后无返回值。
  const saveAccessPolicy = async () => {
    console.debug('AdminPage.saveAccessPolicy 入口', { policyId: editingPolicy?.id || null });
    try {
      const values = await policyForm.validateFields();
      if (editingPolicy?.id) {
        const { policy_key: _policyKey, ...updates } = values;
        await patch(`/admin/access-policies/${editingPolicy.id}`, updates);
      } else {
        await post('/admin/access-policies', values);
      }
      message.success(editingPolicy ? '访问策略已更新' : '访问策略已创建');
      setPolicyModal(false);
      setEditingPolicy(null);
      policyForm.resetFields();
      await load();
      console.info('AdminPage.saveAccessPolicy 完成');
    } catch (error) { console.error('AdminPage.saveAccessPolicy 异常', error); }
  };

  // 方法作用：启用或停用数据库动态访问策略。
  // Args: policy - 数据库策略；enabled - 目标状态。
  // Returns: 更新完成后无返回值。
  const toggleAccessPolicy = async (policy: AccessPolicy, enabled: boolean) => {
    console.debug('AdminPage.toggleAccessPolicy 入口', { policyId: policy.id, enabled });
    if (!policy.id) return;
    try {
      await patch(`/admin/access-policies/${policy.id}`, { enabled });
      await load();
      console.info('AdminPage.toggleAccessPolicy 完成', { policyId: policy.id });
    } catch (error) { console.error('AdminPage.toggleAccessPolicy 异常', error); message.error('策略状态更新失败'); }
  };

  // 方法作用：删除数据库动态访问策略及其 IP 规则。
  // Args: policy - 待删除数据库策略。
  // Returns: 删除完成后无返回值。
  const deleteAccessPolicy = async (policy: AccessPolicy) => {
    console.debug('AdminPage.deleteAccessPolicy 入口', { policyId: policy.id });
    if (!policy.id) return;
    try {
      await del(`/admin/access-policies/${policy.id}`);
      message.success('访问策略已删除');
      await load();
      console.info('AdminPage.deleteAccessPolicy 完成', { policyId: policy.id });
    } catch (error) { console.error('AdminPage.deleteAccessPolicy 异常', error); message.error('访问策略删除失败'); }
  };

  // 方法作用：打开指定策略的 IP 黑白名单创建表单。
  // Args: policy - 目标 YAML 或数据库策略。
  // Returns: 无返回值。
  const openIpRuleEditor = (policy: AccessPolicy) => {
    console.debug('AdminPage.openIpRuleEditor 入口', { policyKey: policy.policy_key });
    setRulePolicy(policy);
    ruleForm.setFieldsValue({ action: 'deny', enabled: true });
    console.info('AdminPage.openIpRuleEditor 完成', { policyKey: policy.policy_key });
  };

  // 方法作用：为选中策略创建 CIDR allow 或 deny 规则。
  // Args: 无，读取 rulePolicy 和 ruleForm。
  // Returns: 创建完成后无返回值。
  const createIpRule = async () => {
    console.debug('AdminPage.createIpRule 入口', { policyKey: rulePolicy?.policy_key || '' });
    if (!rulePolicy) return;
    try {
      const values = await ruleForm.validateFields();
      await post(`/admin/access-policies/${rulePolicy.policy_key}/ip-rules`, values);
      message.success('IP 规则已创建');
      setRulePolicy(null);
      ruleForm.resetFields();
      await load();
      console.info('AdminPage.createIpRule 完成');
    } catch (error) { console.error('AdminPage.createIpRule 异常', error); }
  };

  // 方法作用：启用或停用接口 IP 规则。
  // Args: rule - IP 规则；enabled - 目标状态。
  // Returns: 更新完成后无返回值。
  const toggleIpRule = async (rule: AccessIpRule, enabled: boolean) => {
    console.debug('AdminPage.toggleIpRule 入口', { ruleId: rule.id, enabled });
    try {
      await patch(`/admin/access-ip-rules/${rule.id}`, { enabled });
      await load();
      console.info('AdminPage.toggleIpRule 完成', { ruleId: rule.id });
    } catch (error) { console.error('AdminPage.toggleIpRule 异常', error); message.error('IP 规则状态更新失败'); }
  };

  // 方法作用：删除接口 IP 黑白名单规则。
  // Args: rule - 待删除规则。
  // Returns: 删除完成后无返回值。
  const deleteIpRule = async (rule: AccessIpRule) => {
    console.debug('AdminPage.deleteIpRule 入口', { ruleId: rule.id });
    try {
      await del(`/admin/access-ip-rules/${rule.id}`);
      message.success('IP 规则已删除');
      await load();
      console.info('AdminPage.deleteIpRule 完成', { ruleId: rule.id });
    } catch (error) { console.error('AdminPage.deleteIpRule 异常', error); message.error('IP 规则删除失败'); }
  };

  // 方法作用：启用或停用平台厂商目录项。
  // Args: provider - 目标厂商；isActive - 目标状态。
  // Returns: 更新完成后无返回值。
  const toggleProvider = async (provider: LLMProvider, isActive: boolean) => {
    try {
      await patch(`/admin/llm/providers/${provider.id}`, { is_active: isActive });
      await load();
    } catch (error) { console.error('AdminPage.toggleProvider 异常', error); message.error('厂商状态更新失败'); }
  };

  // 方法作用：启用或停用平台模型目录项。
  // Args: model - 目标模型；isActive - 目标状态。
  // Returns: 更新完成后无返回值。
  const toggleModel = async (model: LLMModel, isActive: boolean) => {
    try {
      await patch(`/admin/llm/models/${model.id}`, { is_active: isActive });
      if (selectedProviderId) await loadProviderModels(selectedProviderId);
    } catch (error) { console.error('AdminPage.toggleModel 异常', error); message.error('模型状态更新失败'); }
  };

  // 方法作用：物理删除未被租户连接引用的平台模型。
  // Args: model - 待删除模型目录项。
  // Returns: 删除完成后无返回值。
  const deleteModel = async (model: LLMModel) => {
    try {
      await del(`/admin/llm/models/${model.id}`);
      if (selectedProviderId) await loadProviderModels(selectedProviderId);
      message.success('模型已删除');
    } catch (error) {
      console.error('AdminPage.deleteModel 异常', error);
      message.error('模型仍被租户连接使用或删除失败');
    }
  };

  // 方法作用：物理删除无租户连接引用的厂商及其模型目录。
  // Args: provider - 待删除模型厂商。
  // Returns: 删除完成后无返回值。
  const deleteProvider = async (provider: LLMProvider) => {
    try {
      await del(`/admin/llm/providers/${provider.id}`);
      await load();
      message.success('模型厂商已删除');
    } catch (error) {
      console.error('AdminPage.deleteProvider 异常', error);
      message.error('厂商仍被租户连接使用或删除失败');
    }
  };

  // 方法作用：启用或停用当前租户命名连接并刷新列表。
  // Args: connection - 目标连接；isActive - 目标状态。
  // Returns: 更新完成后无返回值。
  const toggleConnection = async (connection: TenantLLMConnection, isActive: boolean) => {
    try {
      await patch(`/admin/llm/connections/${connection.id}`, { is_active: isActive });
      await load();
    } catch (error) { console.error('AdminPage.toggleConnection 异常', error); message.error('连接状态更新失败'); }
  };

  const tenantNames = useMemo(
    () => new Map(tenants.map(tenant => [tenant.id, tenant.name])),
    [tenants],
  );
  const selectedProvider = useMemo(
    () => providers.find(provider => provider.id === selectedProviderId),
    [providers, selectedProviderId],
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
        {!isTenantAdmin && <Select allowClear placeholder="全部租户" style={{ width: 180 }} value={tenantFilter} onChange={setTenantFilter}
          options={tenants.map(tenant => ({ value: tenant.id, label: tenant.name }))} />}
        <Button icon={<ReloadOutlined />} onClick={() => void load()} aria-label="刷新用户" /></Space>}
      columns={[
        { title: 'ID', dataIndex: 'id', width: 72 },
        { title: '用户名', dataIndex: 'username' },
        ...(!isTenantAdmin ? [{ title: '租户', dataIndex: 'tenant_id', render: (value: number) => tenantNames.get(value) || value }] : []),
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

  const accessPolicyWorkspace = <div style={{ width: '100%', minWidth: 0, maxWidth: 'calc(100vw - 48px)', overflow: 'hidden' }}>
    <Table<AccessPolicy> rowKey={policy => `${policy.source}:${policy.policy_key}`}
      dataSource={accessPolicies} loading={loading} pagination={{ pageSize: 15 }} scroll={{ x: 1080 }}
      title={() => <Space>
        <Button type="primary" icon={<PlusOutlined />} onClick={() => openPolicyEditor()}>创建策略</Button>
        <Tooltip title="刷新访问策略"><Button icon={<ReloadOutlined />} onClick={() => void load()} aria-label="刷新访问策略" /></Tooltip>
      </Space>}
      columns={[
        { title: '策略', dataIndex: 'policy_key', width: 180 },
        { title: '来源', dataIndex: 'source', width: 90, render: source => <Tag color={source === 'yaml' ? 'blue' : 'green'}>{source === 'yaml' ? 'YAML' : '数据库'}</Tag> },
        { title: '方法', dataIndex: 'methods', width: 150, render: methods => <Space size={4} wrap>{methods.map((method: string) => <Tag key={method}>{method}</Tag>)}</Space> },
        { title: '路径', dataIndex: 'path', width: 300, ellipsis: true },
        { title: '认证', dataIndex: 'auth_mode', width: 150 },
        { title: '访问日志', dataIndex: 'access_log_mode', width: 120 },
        { title: '状态', width: 80, render: (_, policy) => <Switch checked={policy.enabled} disabled={policy.source === 'yaml'} onChange={value => void toggleAccessPolicy(policy, value)} /> },
        { title: '操作', width: 150, render: (_, policy) => <Space size={4}>
          <Tooltip title="添加 IP 规则"><Button icon={<SafetyCertificateOutlined />} onClick={() => openIpRuleEditor(policy)} aria-label={`为 ${policy.policy_key} 添加 IP 规则`} /></Tooltip>
          {policy.source === 'database' && <>
            <Tooltip title="编辑策略"><Button icon={<EditOutlined />} onClick={() => openPolicyEditor(policy)} aria-label={`编辑 ${policy.policy_key}`} /></Tooltip>
            <Tooltip title="删除策略"><Button danger icon={<DeleteOutlined />} aria-label={`删除 ${policy.policy_key}`}
              onClick={() => Modal.confirm({ title: '删除访问策略', content: `同时删除 ${policy.policy_key} 的 IP 规则。`, okButtonProps: { danger: true }, onOk: () => deleteAccessPolicy(policy) })} /></Tooltip>
          </>}
        </Space> },
      ]} />
    <Divider style={{ margin: '4px 0' }} />
    <Typography.Title level={5} style={{ margin: 0 }}>IP 黑白名单</Typography.Title>
    <Table<AccessIpRule> rowKey="id" dataSource={accessIpRules} loading={loading}
      pagination={{ pageSize: 15 }} scroll={{ x: 760 }} columns={[
        { title: '策略', dataIndex: 'policy_key', width: 180 },
        { title: '动作', dataIndex: 'action', width: 100, render: action => <Tag color={action === 'deny' ? 'red' : 'green'}>{action === 'deny' ? '拒绝' : '允许'}</Tag> },
        { title: 'CIDR', dataIndex: 'cidr', width: 220 },
        { title: '说明', dataIndex: 'description', ellipsis: true },
        { title: '状态', width: 90, render: (_, rule) => <Switch checked={rule.enabled} onChange={value => void toggleIpRule(rule, value)} /> },
        { title: '操作', width: 72, render: (_, rule) => <Tooltip title="删除 IP 规则"><Button danger icon={<DeleteOutlined />} aria-label={`删除 IP 规则 ${rule.id}`}
          onClick={() => Modal.confirm({ title: '删除 IP 规则', content: `${rule.action} ${rule.cidr}`, okButtonProps: { danger: true }, onOk: () => deleteIpRule(rule) })} /></Tooltip> },
      ]} />
  </div>;

  const providerWorkspace = <Space direction="vertical" size={16} style={{ width: '100%' }}>
    <Table<LLMProvider> rowKey="id" dataSource={providers} loading={loading} pagination={{ pageSize: 10 }}
      scroll={{ x: 990 }}
      title={() => <Space>
        <Button type="primary" icon={<PlusOutlined />} onClick={() => openProviderEditor()}>新增厂商</Button>
        <Button icon={<ReloadOutlined />} onClick={() => void load()} aria-label="刷新厂商" />
      </Space>}
      columns={[
        { title: '编码', dataIndex: 'code', width: 130 },
        { title: '名称', dataIndex: 'display_name', width: 140 },
        { title: '协议', dataIndex: 'protocol', width: 180 },
        { title: '默认地址', dataIndex: 'default_base_url', width: 260, ellipsis: true },
        { title: '状态', width: 90, render: (_: unknown, provider: LLMProvider) => <Switch checked={provider.is_active} onChange={value => void toggleProvider(provider, value)} /> },
        { title: '操作', width: 190, render: (_: unknown, provider: LLMProvider) => <Space size={4}>
          <Tooltip title="查看模型"><Button type="link" onClick={() => void loadProviderModels(provider.id)}>模型</Button></Tooltip>
          <Tooltip title="编辑厂商"><Button icon={<EditOutlined />} onClick={() => openProviderEditor(provider)} aria-label={`编辑 ${provider.display_name}`} /></Tooltip>
          <Tooltip title="删除厂商"><Button danger icon={<DeleteOutlined />} aria-label={`删除 ${provider.display_name}`}
            onClick={() => Modal.confirm({ title: '删除模型厂商', content: `将同时删除 ${provider.display_name} 下未被引用的模型。`, okButtonProps: { danger: true }, onOk: () => deleteProvider(provider) })} /></Tooltip>
        </Space> },
      ]} />
    <Table<LLMModel> rowKey="id" dataSource={providerModels} pagination={{ pageSize: 10 }}
      scroll={{ x: 990 }}
      title={() => <Space><Typography.Text>当前厂商模型目录</Typography.Text><Button type="primary" icon={<PlusOutlined />} disabled={!selectedProviderId} onClick={() => openModelEditor()}>新增模型</Button></Space>}
      columns={[
        { title: '模型 ID', dataIndex: 'model_id', width: 180 },
        { title: '展示名称', dataIndex: 'display_name', width: 180 },
        { title: '能力', dataIndex: 'capabilities', width: 420, render: value => renderCapabilitySummary(value || {}, selectedProvider) },
        { title: '状态', width: 90, render: (_: unknown, model: LLMModel) => <Switch checked={model.is_active} onChange={value => void toggleModel(model, value)} /> },
        { title: '操作', width: 120, render: (_: unknown, model: LLMModel) => <Space size={4}>
          <Tooltip title="编辑模型"><Button icon={<EditOutlined />} onClick={() => openModelEditor(model)} aria-label={`编辑 ${model.display_name}`} /></Tooltip>
          <Tooltip title="删除模型"><Button danger icon={<DeleteOutlined />} aria-label={`删除 ${model.display_name}`}
            onClick={() => Modal.confirm({ title: '删除模型', content: `确认物理删除 ${model.display_name}？`, okButtonProps: { danger: true }, onOk: () => deleteModel(model) })} /></Tooltip>
        </Space> },
      ]} />
  </Space>;

  const tenantLLMWorkspace = <Space direction="vertical" size={16} style={{ width: '100%' }}>
    <Table<TenantLLMConnection> rowKey="id" dataSource={connections} loading={loading} pagination={{ pageSize: 10 }}
      title={() => <Space>
        <Button type="primary" icon={<PlusOutlined />} onClick={() => { setEditingConnection(null); connectionForm.resetFields(); setConnectionModal(true); }}>新增命名连接</Button>
        <Button icon={<ReloadOutlined />} onClick={() => void load()} aria-label="刷新 LLM 连接" />
      </Space>}
      columns={[
        { title: '连接名', dataIndex: 'name' },
        { title: '厂商', render: (_: unknown, connection: TenantLLMConnection) => `${connection.provider_name} (${connection.provider_code})` },
        { title: '请求地址', dataIndex: 'base_url', ellipsis: true },
        { title: 'API Key', dataIndex: 'api_key_configured', render: value => value ? '已配置' : '未配置' },
        { title: '模型数', render: (_: unknown, connection: TenantLLMConnection) => connection.model_catalog_ids?.length || 0 },
        { title: '状态', render: (_: unknown, connection: TenantLLMConnection) => <Switch checked={connection.is_active} onChange={value => void toggleConnection(connection, value)} /> },
        { title: '操作', render: (_: unknown, connection: TenantLLMConnection) => <Space>
          <Button icon={<EditOutlined />} onClick={() => { setEditingConnection(connection); void loadProviderModels(connection.provider_id); connectionForm.setFieldsValue({ provider_id: connection.provider_id, name: connection.name, base_url: connection.base_url, model_catalog_ids: connection.model_catalog_ids }); setConnectionModal(true); }}>编辑</Button>
          <Button danger icon={<DeleteOutlined />} onClick={() => void deleteConnection(connection)} />
        </Space> },
      ]} />
    <Form form={defaultConnectionForm} layout="inline" onFinish={() => void setDefaultConnection()}>
      <Form.Item name="default_connection_id" label="默认连接" rules={[{ required: true }]}>
        <Select placeholder="选择默认连接" style={{ minWidth: 190 }} onChange={value => { setSelectedDefaultConnectionId(value); const connection = connections.find(item => item.id === value); if (connection) void loadProviderModels(connection.provider_id); }} options={connections.filter(item => item.is_active).map(item => ({ value: item.id, label: item.name }))} />
      </Form.Item>
      <Form.Item name="default_model_catalog_id" label="默认模型" rules={[{ required: true }]}>
        <Select placeholder="选择默认模型" style={{ minWidth: 220 }} options={providerModels.filter(item => item.is_active && (connections.find(connection => connection.id === selectedDefaultConnectionId)?.model_catalog_ids || []).includes(item.id)).map(item => ({ value: item.id, label: item.display_name }))} />
      </Form.Item>
      <Button type="primary" htmlType="submit">保存默认</Button>
    </Form>
  </Space>;

  console.info('AdminPage 完成', { tenantCount: tenants.length, userCount: users.length });
  return <div style={{ padding: 24, maxWidth: 1280, margin: '0 auto' }}>
    <Typography.Title level={3} style={{ marginTop: 0 }}>{isTenantAdmin ? '租户管理' : '平台管理'}</Typography.Title>
    <Tabs items={isTenantAdmin ? [
      { key: 'users', label: '当前租户用户', children: userWorkspace },
      { key: 'connections', label: 'LLM 命名连接', children: tenantLLMWorkspace },
    ] : [
      { key: 'tenants', label: '租户管理', children: tenantWorkspace },
      { key: 'llm', label: 'LLM 厂商与模型', children: providerWorkspace },
      { key: 'security', label: '安全配置', children: configWorkspace },
      { key: 'access', label: '访问策略', children: accessPolicyWorkspace },
    ]} />

    <Modal title="创建租户" open={tenantModal} onOk={() => void createTenant()} onCancel={() => setTenantModal(false)} destroyOnClose>
      <Form form={tenantForm} layout="vertical">
        <Form.Item name="code" label="租户编码" rules={[{ required: true, pattern: /^[a-z0-9][a-z0-9-]{0,31}$/ }]}><Input /></Form.Item>
        <Form.Item name="name" label="租户名称" rules={[{ required: true, max: 128 }]}><Input /></Form.Item>
        <Form.Item name="admin_username" label="管理员用户名" rules={[{ required: true, max: 64 }]}><Input /></Form.Item>
        <Form.Item name="admin_password" label="管理员密码" rules={[{ required: true, min: 8, max: 72 }]}><Input.Password /></Form.Item>
      </Form>
    </Modal>

    <Modal title="创建用户" open={userModal} onOk={() => void createUser()} onCancel={() => setUserModal(false)} destroyOnClose>
      <Form form={userForm} layout="vertical" initialValues={{ role: 'analyst' }}>
        {!isTenantAdmin && <Form.Item name="tenant_id" label="所属租户" rules={[{ required: true }]}><Select options={tenants.filter(t => t.is_active).map(t => ({ value: t.id, label: t.name }))} /></Form.Item>}
        <Form.Item name="username" label="用户名" rules={[{ required: true, max: 64 }]}><Input /></Form.Item>
        <Form.Item name="password" label="初始密码" rules={[{ required: true, min: 8, max: 72 }]}><Input.Password /></Form.Item>
        <Form.Item name="role" label="角色" rules={[{ required: true }]}><Select options={ROLE_OPTIONS} /></Form.Item>
      </Form>
    </Modal>

    <Modal title={editingProvider ? '编辑模型厂商' : '新增模型厂商'} open={providerModal}
      onOk={() => void saveProvider()} onCancel={() => { setProviderModal(false); setEditingProvider(null); }}
      width={920} destroyOnClose>
      <Form form={providerForm} layout="vertical">
        <Form.Item name="code" label="厂商编码" rules={[{ required: true, pattern: /^[A-Za-z0-9][A-Za-z0-9_-]*$/ }]}><Input disabled={Boolean(editingProvider)} /></Form.Item>
        <Form.Item name="display_name" label="展示名称" rules={[{ required: true }]}><Input /></Form.Item>
        <Form.Item name="protocol" label="协议" rules={[{ required: true }]}><Select disabled={Boolean(editingProvider)} options={[{ value: 'openai_compatible', label: 'OpenAI Compatible' }, { value: 'anthropic', label: 'Anthropic' }]} /></Form.Item>
        <Form.Item name="default_base_url" label="默认请求地址"><Input /></Form.Item>
        <Divider orientation="left" plain>模型能力表单</Divider>
        <Form.List name="capability_fields">
          {(fields, { add, remove }) => <Space direction="vertical" size={12} style={{ width: '100%' }}>
            {fields.map(field => <div key={field.key} style={{ paddingBottom: 12, borderBottom: '1px solid #f0f0f0' }}>
              <Space wrap align="start">
                <Form.Item {...field} name={[field.name, 'key']} label="字段键"
                  rules={[{ required: true, pattern: /^[a-z][a-z0-9_]*$/ }]}><Input style={{ width: 180 }} /></Form.Item>
                <Form.Item {...field} name={[field.name, 'label']} label="显示名称"
                  rules={[{ required: true }]}><Input style={{ width: 160 }} /></Form.Item>
                <Form.Item {...field} name={[field.name, 'type']} label="控件类型"
                  rules={[{ required: true }]}><Select options={CAPABILITY_TYPE_OPTIONS} style={{ width: 120 }} /></Form.Item>
                <Form.Item {...field} name={[field.name, 'required']} label="必填" valuePropName="checked"><Switch /></Form.Item>
                <Tooltip title="删除能力字段"><Button danger type="text" icon={<MinusCircleOutlined />}
                  onClick={() => remove(field.name)} aria-label="删除能力字段" style={{ marginTop: 30 }} /></Tooltip>
              </Space>
              <Form.Item noStyle shouldUpdate>
                {({ getFieldValue }) => {
                  const type = getFieldValue(['capability_fields', field.name, 'type']) as CapabilityFieldType;
                  const options = (getFieldValue(['capability_fields', field.name, 'options']) || []) as string[];
                  return <Space wrap align="start">
                    {(type === 'select' || type === 'multiselect') && <Form.Item name={[field.name, 'options']} label="可选值"
                      rules={[{ required: true }]}><Select mode="tags" tokenSeparators={[',']} style={{ minWidth: 260 }} /></Form.Item>}
                    {(type === 'integer' || type === 'number') && <>
                      <Form.Item name={[field.name, 'minimum']} label="最小值"><InputNumber /></Form.Item>
                      <Form.Item name={[field.name, 'maximum']} label="最大值"><InputNumber /></Form.Item>
                    </>}
                    <Form.Item name={[field.name, 'default']} label="默认值" valuePropName={type === 'boolean' ? 'checked' : 'value'}>
                      {type === 'boolean' ? <Switch />
                        : type === 'integer' || type === 'number' ? <InputNumber precision={type === 'integer' ? 0 : undefined} />
                          : type === 'select' || type === 'multiselect' ? <Select mode={type === 'multiselect' ? 'multiple' : undefined}
                            options={options.map(value => ({ label: value, value }))} style={{ minWidth: 180 }} />
                            : <Input style={{ width: 180 }} />}
                    </Form.Item>
                    <Form.Item name={[field.name, 'description']} label="说明"><Input style={{ width: 260 }} /></Form.Item>
                  </Space>;
                }}
              </Form.Item>
            </div>)}
            <Button type="dashed" icon={<PlusOutlined />} onClick={() => add({ type: 'boolean', required: false })}>添加能力字段</Button>
          </Space>}
        </Form.List>
      </Form>
    </Modal>

    <Modal title={editingModel ? '编辑模型目录' : '新增模型目录'} open={modelModal}
      onOk={() => void saveModel()} onCancel={() => { setModelModal(false); setEditingModel(null); }} destroyOnClose>
      <Form form={modelForm} layout="vertical">
        <Form.Item name="model_id" label="模型 ID" rules={[{ required: true }]}><Input disabled={Boolean(editingModel)} /></Form.Item>
        <Form.Item name="display_name" label="展示名称" rules={[{ required: true }]}><Input /></Form.Item>
        {(selectedProvider?.capability_schema?.fields || []).map(renderCapabilityField)}
      </Form>
    </Modal>

    <Modal title={editingConnection ? '编辑命名连接' : '新增命名连接'} open={connectionModal} onOk={() => void saveConnection()} onCancel={() => { setEditingConnection(null); setConnectionModal(false); }} destroyOnClose>
      <Form form={connectionForm} layout="vertical">
        <Form.Item name="provider_id" label="模型厂商" rules={[{ required: true }]}><Select disabled={Boolean(editingConnection)} onChange={value => void loadProviderModels(value)} options={providers.filter(item => item.is_active).map(item => ({ value: item.id, label: `${item.display_name} (${item.code})` }))} /></Form.Item>
        <Form.Item name="name" label="连接名称" rules={[{ required: true, max: 128 }]}><Input /></Form.Item>
        <Form.Item name="base_url" label="请求地址"><Input placeholder="留空使用厂商默认地址" /></Form.Item>
        <Form.Item name="api_key" label="API Key" rules={editingConnection ? [] : [{ required: true }]}><Input.Password placeholder={editingConnection ? '留空沿用原凭证' : ''} /></Form.Item>
        <Form.Item name="model_catalog_ids" label="可用模型" rules={[{ required: true }]}><Select mode="multiple" options={providerModels.filter(item => item.is_active).map(item => ({ value: item.id, label: item.display_name }))} /></Form.Item>
      </Form>
    </Modal>

    <Modal title={`重置 ${passwordUser?.username || ''} 的密码`} open={Boolean(passwordUser)} onOk={() => void resetPassword()}
      onCancel={() => setPasswordUser(null)} destroyOnClose>
      <Form form={passwordForm} layout="vertical">
        <Form.Item name="password" label="新密码" rules={[{ required: true, min: 8, max: 72 }]}><Input.Password /></Form.Item>
      </Form>
    </Modal>

    <Modal title={editingPolicy ? '编辑访问策略' : '创建访问策略'} open={policyModal}
      onOk={() => void saveAccessPolicy()} onCancel={() => { setPolicyModal(false); setEditingPolicy(null); }} destroyOnClose>
      <Form form={policyForm} layout="vertical">
        <Form.Item name="policy_key" label="策略编号" rules={[{ required: true, pattern: /^[a-z][a-z0-9_-]+$/ }]}> <Input disabled={Boolean(editingPolicy)} /> </Form.Item>
        <Form.Item name="path" label="接口路径" rules={[{ required: true, pattern: /^\// }]}><Input placeholder="/api/v1/reports" /></Form.Item>
        <Space size={12} style={{ width: '100%' }} align="start">
          <Form.Item name="path_type" label="匹配方式" rules={[{ required: true }]}><Select style={{ width: 130 }} options={[{ value: 'exact', label: '精确路径' }, { value: 'template', label: '路径模板' }]} /></Form.Item>
          <Form.Item name="methods" label="HTTP 方法" rules={[{ required: true }]}><Select mode="multiple" style={{ width: 220 }} options={['GET', 'POST', 'PUT', 'PATCH', 'DELETE'].map(value => ({ value, label: value }))} /></Form.Item>
        </Space>
        <Space size={12} style={{ width: '100%' }} align="start">
          <Form.Item name="auth_mode" label="认证模式" rules={[{ required: true }]}><Select style={{ width: 190 }} options={AUTH_MODE_OPTIONS} /></Form.Item>
          <Form.Item name="access_log_mode" label="访问日志" rules={[{ required: true }]}><Select style={{ width: 180 }} options={ACCESS_LOG_OPTIONS} /></Form.Item>
          <Form.Item name="priority" label="优先级" rules={[{ required: true }]}><InputNumber min={-100000} max={100000} style={{ width: 110 }} /></Form.Item>
        </Space>
        <Form.Item name="description" label="说明"><Input.TextArea maxLength={500} rows={2} /></Form.Item>
      </Form>
    </Modal>

    <Modal title={`为 ${rulePolicy?.policy_key || ''} 添加 IP 规则`} open={Boolean(rulePolicy)}
      onOk={() => void createIpRule()} onCancel={() => setRulePolicy(null)} destroyOnClose>
      <Form form={ruleForm} layout="vertical">
        <Form.Item name="action" label="规则动作" rules={[{ required: true }]}><Select options={[{ value: 'deny', label: '拒绝' }, { value: 'allow', label: '允许' }]} /></Form.Item>
        <Form.Item name="cidr" label="IP / CIDR" rules={[{ required: true }]}><Input placeholder="203.0.113.0/24" /></Form.Item>
        <Form.Item name="description" label="说明"><Input maxLength={500} /></Form.Item>
      </Form>
    </Modal>
  </div>;
}
