import { t, useLanguage } from '../i18n';
import { useEffect, useState } from 'react';
import { Alert, Button, Form, Input, InputNumber, Select, Space, Typography, message } from 'antd';
import { del, get, post } from '../api/client';
import type { KnowledgeDoc } from '../types';

// 方法作用：由拥有者签发限时只读授权，平台必须持授权 ID 显式下载。
// Args: docs - 已授权可见文档；platform - 是否为平台账号。
// Returns: 知识应急授权工作区。
export default function EmergencyKnowledgePanel({ docs, platform }: { docs: KnowledgeDoc[]; platform: boolean }) {
  useLanguage();
  const [grantId, setGrantId] = useState('');
  const [expires, setExpires] = useState('');
  const [busy, setBusy] = useState(false);
  const [query, setQuery] = useState('');
  const [recipients, setRecipients] = useState<{ id: number; username: string }[]>([]);
  const [searching, setSearching] = useState(false);
  const [searchError, setSearchError] = useState('');

  // 方法作用：防抖搜索平台账号，卸载或新搜索后忽略旧响应。
  // Args: 无，读取当前查询。
  // Returns: 清理计时器和旧响应。
  useEffect(() => {
    if (platform) return;
    let active = true;
    setSearching(true);
    const timer = window.setTimeout(() => {
      get<{ items: { id: number; username: string }[] }>(`/knowledge/emergency-grants/recipients?query=${encodeURIComponent(query)}`)
        .then(result => { if (active) { setRecipients(result.items); setSearchError(''); } })
        .catch(() => { if (active) { setRecipients([]); setSearchError(t("用户目录加载失败，请重新搜索")); } })
        .finally(() => { if (active) setSearching(false); });
    }, 250);
    return () => { active = false; window.clearTimeout(timer); };
  }, [query, platform]);

  // 方法作用：保存有理由、最长一小时的单文件授权。
  // Args: values - 授权文件、目标账号、理由及期限。
  // Returns: 完成后显示授权 ID，供授权者交给运维人员。
  const grant = async (values: Record<string, unknown>) => {
    setBusy(true);
    try { const result = await post<{ id: string; expires_at: string }>('/knowledge/emergency-grants', values); setGrantId(result.id); setExpires(result.expires_at); message.success(t("只读授权已签发")); }
    catch (error) { message.error(error instanceof Error ? error.message : t("授权失败")); }
    finally { setBusy(false); }
  };

  // 方法作用：撤销本人签发的授权，服务端重新校验拥有者。
  // Args: 无，读取当前授权 ID。
  // Returns: 撤销完成。
  const revoke = async () => {
    try { await del(`/knowledge/emergency-grants/${encodeURIComponent(grantId)}`); message.success(t("授权已撤销")); setExpires(t("已撤销")); }
    catch (error) { message.error(error instanceof Error ? error.message : t("撤销失败")); }
  };

  return <Space direction="vertical" style={{ width: '100%', maxWidth: 600 }}>
    <Alert type="warning" showIcon message={t("应急访问必须由知识拥有者单独授权")} description={t("只允许指定平台账号读取一个文件，最长 60 分钟。签发、读取、撤销均审计，不扩展到搜索或其他私有知识。")} />
    {!platform && <Form layout="vertical" onFinish={grant} initialValues={{ minutes: 15 }}>
      <Form.Item name="file_id" label={t("授权文档")} rules={[{ required: true }]}><Select options={docs.filter(doc => doc.id && doc.can_delete && doc.scope !== 'system').map(doc => ({ value: doc.id, label: `${doc.name} (${doc.scope})` }))} /></Form.Item>
      <Form.Item name="grantee_user_id" label={t("平台账号")} rules={[{ required: true }]} help={searchError || undefined} validateStatus={searchError ? 'error' : undefined}><Select showSearch filterOption={false} onSearch={setQuery} loading={searching} placeholder={t("搜索并选择平台用户")} options={recipients.map(recipient => ({ value: recipient.id, label: recipient.username }))} /></Form.Item>
      <Form.Item name="minutes" label={t("有效分钟数")} rules={[{ required: true }]}><InputNumber min={1} max={60} precision={0} /></Form.Item>
      <Form.Item name="reason" label={t("工单或排障理由")} rules={[{ required: true, min: 3 }]}><Input.TextArea maxLength={500} /></Form.Item>
      <Button htmlType="submit" type="primary" loading={busy}>{t("签发只读授权")}</Button>
    </Form>}
    <Typography.Text>{t("授权 ID（签发后复制给指定运维账号，勿公开传播）")}</Typography.Text>
    <Input value={grantId} onChange={event => setGrantId(event.target.value)} placeholder={t("授权 UUID")} />
    {expires && <Typography.Text>{t("到期状态：")}{expires}</Typography.Text>}
    {platform ? <Button disabled={!grantId} href={`/api/v1/knowledge/emergency-grants/${encodeURIComponent(grantId)}/content`}>{t("读取授权文件（审计下载）")}</Button>
      : <Button danger disabled={!grantId} onClick={() => void revoke()}>{t("撤销本人授权")}</Button>}
  </Space>;
}
