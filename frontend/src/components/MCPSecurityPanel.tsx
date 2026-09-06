import { t, useLanguage } from '../i18n';
import { useEffect, useState } from 'react';
import { Alert, Button, Form, Input, InputNumber, Select, Space, Table, Tag, message } from 'antd';
import { get, post } from '../api/client';

interface SecurityBlock { tenant_id: number; scope: string; owner_user_id: number; name: string; blocked: boolean; reason: string; }

// 方法作用：仅提供平台安全停用，不显示或编辑租户连接凭证。
// Args: 无，身份由服务端校验。
// Returns: 安全操作表单和审计元数据列表。
export default function MCPSecurityPanel() {
  useLanguage();
  const [items, setItems] = useState<SecurityBlock[]>([]);
  const [saving, setSaving] = useState(false);
  const [form] = Form.useForm();

  // 方法作用：加载不含凭证的安全状态。
  // Args: 无。
  // Returns: 加载完成。
  const load = async () => {
    try { setItems((await get<{ items: SecurityBlock[] }>('/mcp/security-blocks')).items); }
    catch { message.error(t("安全状态加载失败")); }
  };
  useEffect(() => { void load(); }, []);

  // 方法作用：提交有理由的安全操作，解除阻断不启用业务连接。
  // Args: values - 明确的资源定位与安全状态。
  // Returns: 操作完成。
  const save = async (values: SecurityBlock) => {
    setSaving(true);
    try { await post('/mcp/security-blocks', { ...values }); message.success(t("安全状态已更新并审计")); await load(); }
    catch (error) { message.error(error instanceof Error ? error.message : t("操作失败")); }
    finally { setSaving(false); }
  };

  return <Space direction="vertical" style={{ width: '100%' }}>
    <Alert type="warning" showIcon message={t("平台只进行安全阻断，不代管租户连接")} description={t("必须明确租户、范围、名称及理由；解除安全阻断不会启用租户已停用的连接。")} />
    <Form form={form} layout="inline" onFinish={save} initialValues={{ scope: 'tenant', owner_user_id: 0, blocked: true }}>
      <Form.Item name="tenant_id" label={t("租户 ID")} rules={[{ required: true }]}><InputNumber min={1} precision={0} /></Form.Item>
      <Form.Item name="scope" label={t("范围")}><Select style={{ width: 90 }} options={[{ value: 'tenant', label: t("租户") }, { value: 'private', label: t("个人") }]} /></Form.Item>
      <Form.Item name="owner_user_id" label={t("拥有者 ID")}><InputNumber min={0} precision={0} /></Form.Item>
      <Form.Item name="name" label={t("连接名称")} rules={[{ required: true }]}><Input maxLength={64} /></Form.Item>
      <Form.Item name="blocked" label={t("操作")}><Select style={{ width: 120 }} options={[{ value: true, label: t("安全停用") }, { value: false, label: t("解除阻断") }]} /></Form.Item>
      <Form.Item name="reason" label={t("理由")} rules={[{ required: true, min: 3 }]}><Input maxLength={500} /></Form.Item>
      <Button type="primary" danger htmlType="submit" loading={saving}>{t("提交安全操作")}</Button>
    </Form>
    <Table dataSource={items} rowKey={row => `${row.tenant_id}:${row.scope}:${row.owner_user_id}:${row.name}`} columns={[
      { title: t("租户"), dataIndex: 'tenant_id' }, { title: t("范围"), dataIndex: 'scope' },
      { title: t("拥有者"), dataIndex: 'owner_user_id' }, { title: t("连接名称"), dataIndex: 'name' },
      { title: t("安全状态"), dataIndex: 'blocked', render: (blocked: boolean) => <Tag color={blocked ? 'red' : 'green'}>{blocked ? t("安全停用") : t("已解除阻断")}</Tag> },
      { title: t("理由"), dataIndex: 'reason' },
    ]} />
  </Space>;
}
