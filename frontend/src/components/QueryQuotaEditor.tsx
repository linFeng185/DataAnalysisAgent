import { t, useLanguage } from '../i18n';
import { useEffect, useState } from 'react';
import { Alert, Button, Form, InputNumber, Space, message } from 'antd';
import { get, put } from '../api/client';

interface QueryQuota { tenant_per_hour: number; user_per_hour: number; hard_limit: number; window: string; }

// 方法作用：提供租户软额度配置，不允许修改平台硬上限。
// Args: 无，服务端从当前认证身份确定租户。
// Returns: 查询额度配置表单。
export default function QueryQuotaEditor() {
  useLanguage();
  const [form] = Form.useForm();
  const [quota, setQuota] = useState<QueryQuota | null>(null);
  const [saving, setSaving] = useState(false);
  const [failed, setFailed] = useState(false);

  // 方法作用：加载数据库当前策略，失败时不展示可保存的默认额度。
  // Args: 无。
  // Returns: 异步加载完成。
  const load = async () => {
    setFailed(false);
    try {
      const value = await get<QueryQuota>('/admin/query-quota');
      setQuota(value); form.setFieldsValue(value);
    } catch { setFailed(true); }
  };
  useEffect(() => { void load(); }, []);

  // 方法作用：保存当前租户软额度，保留后端失败关闭提示。
  // Args: values - 表单租户与用户额度。
  // Returns: 保存完成。
  const save = async (values: { tenant_per_hour: number; user_per_hour: number }) => {
    setSaving(true);
    try { setQuota(await put<QueryQuota>('/admin/query-quota', values)); message.success(t("查询额度已更新")); }
    catch (error) { message.error(error instanceof Error ? error.message : t("保存失败")); }
    finally { setSaving(false); }
  };

  return <Space direction="vertical" style={{ width: '100%', maxWidth: 560 }}>
    <Alert type="info" showIcon message={t("平台限制资源上限，租户分配业务额度")} description={quota ? t("平台硬上限：{value0} 次/小时；{value1}。0 表示暂停查询。", { value0: quota.hard_limit, value1: quota.window }) : t("正在读取额度配置")} />
    {failed && <Alert type="error" message={t("读取失败，未启用默认配置")} action={<Button onClick={() => void load()}>{t("重试")}</Button>} />}
    <Form form={form} layout="vertical" onFinish={save}>
      <Form.Item name="tenant_per_hour" label={t("租户每小时总次数")} rules={[{ required: true }]}><InputNumber min={0} max={quota?.hard_limit} precision={0} disabled={!quota} /></Form.Item>
      <Form.Item name="user_per_hour" label={t("每位用户每小时次数")} rules={[{ required: true }]}><InputNumber min={0} max={quota?.hard_limit} precision={0} disabled={!quota} /></Form.Item>
      <Button htmlType="submit" type="primary" loading={saving} disabled={!quota || failed}>{t("保存租户额度")}</Button>
    </Form>
  </Space>;
}
