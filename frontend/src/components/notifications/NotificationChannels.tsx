import { t, useLanguage } from '../../i18n';
import { useCallback, useEffect, useState } from 'react';
import { Alert, Button, Col, Input, InputNumber, Modal, Row, Space, Spin, Switch, Tag, Typography, message } from 'antd';
import {
  fetchNotificationChannels,
  saveNotificationChannels,
  type NotificationChannelName,
  type NotificationChannelSavePayload,
  type NotificationChannelState,
} from '../../api/notifications';
import { useAuth } from '../../hooks/AuthContext';

// 敏感项：后端只回显「是否已配置」，保存时按字段名提交明文，留空表示不修改。
const SENSITIVE_KEYS = new Set(['smtp_password', 'feishu_webhook_url', 'slack_webhook_url']);

type ChannelFieldKind = 'text' | 'number' | 'secret' | 'switch';
interface ChannelField {
  key: string;
  label: string;
  kind: ChannelFieldKind;
  placeholder: string;
}

// 各渠道的可编辑字段；站内信无需配置所以为空。
const CHANNEL_FIELDS: Record<NotificationChannelName, ChannelField[]> = {
  in_app: [],
  email: [
    { key: 'smtp_host', label: "SMTP 主机", kind: 'text', placeholder: "smtp.example.com" },
    { key: 'smtp_port', label: "SMTP 端口", kind: 'number', placeholder: "587" },
    { key: 'smtp_starttls', label: "启用 STARTTLS", kind: 'switch', placeholder: "" },
    { key: 'smtp_username', label: "SMTP 用户名", kind: 'text', placeholder: "noreply@example.com" },
    { key: 'smtp_password', label: "SMTP 口令", kind: 'secret', placeholder: "请输入 SMTP 口令" },
  ],
  feishu: [
    { key: 'feishu_webhook_url', label: "飞书 Webhook 地址", kind: 'secret', placeholder: "https://open.feishu.cn/open-apis/bot/v2/hook/..." },
  ],
  slack: [
    { key: 'slack_webhook_url', label: "Slack Webhook 地址", kind: 'secret', placeholder: "https://hooks.slack.com/services/..." },
  ],
};

// 渠道名称与说明文案。
const CHANNEL_TITLES: Record<NotificationChannelName, string> = {
  in_app: "站内信",
  email: "邮件",
  feishu: "飞书",
  slack: "Slack",
};

const CHANNEL_DESCRIPTIONS: Record<NotificationChannelName, string> = {
  in_app: "站内信始终可用，无需配置，也无法关闭。",
  email: "预留给邮件外发；保存后不会立即发送邮件。",
  feishu: "预留给飞书机器人外发；保存后不会立即推送。",
  slack: "预留给 Slack 外发；保存后不会立即推送。",
};

// 方法作用：把服务端返回的非敏感配置项转成表单可编辑的文本草稿。
// Args: channel - 渠道配置项。
// Returns: 键到文本值的草稿对象，敏感项一律留空。
function toDraft(channel: NotificationChannelState): Record<string, string> {
  const draft: Record<string, string> = {};
  for (const field of CHANNEL_FIELDS[channel.channel] || []) {
    if (SENSITIVE_KEYS.has(field.key)) {
      // 敏感项不回显明文，留空即表示保留服务端已存的值。
      draft[field.key] = '';
      continue;
    }
    const value = channel.config?.[field.key];
    draft[field.key] = value === undefined || value === null ? '' : String(value);
  }
  return draft;
}

export interface NotificationChannelsProps {
  open: boolean;
  onClose: () => void;
  // 可见性覆盖：缺省只允许租户管理员打开（超级管理员通过平台配置入口管理，不使用本弹窗）。
  canManage?: boolean;
}

// 方法作用：渲染租户级通知渠道配置，含启用开关、SMTP 与 Webhook 字段以及敏感项占位提示。
// Args: open - 是否显示；onClose - 关闭回调；canManage - 权限覆盖。
// Returns: 渠道配置弹窗 React 元素；无权限时返回 null。
export default function NotificationChannels(props: NotificationChannelsProps) {
  useLanguage();
  const { user } = useAuth();
  const canManage = props.canManage ?? user?.role === 'tenant_admin';
  const [channels, setChannels] = useState<NotificationChannelState[]>([]);
  const [enabledMap, setEnabledMap] = useState<Record<string, boolean>>({});
  const [draft, setDraft] = useState<Record<string, Record<string, string>>>({});
  const [loading, setLoading] = useState(false);
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState('');

  // 方法作用：读取渠道配置并初始化开关与表单草稿。
  // Args: 无。
  // Returns: 请求结束无返回值；失败时写入 error。
  const load = useCallback(async (): Promise<void> => {
    setLoading(true);
    try {
      const result = await fetchNotificationChannels();
      const list = result.channels || [];
      setChannels(list);
      const nextEnabled: Record<string, boolean> = {};
      const nextDraft: Record<string, Record<string, string>> = {};
      for (const channel of list) {
        // 站内信恒为启用，避免被误关后收不到任何通知。
        nextEnabled[channel.channel] = channel.channel === 'in_app' ? true : Boolean(channel.enabled);
        nextDraft[channel.channel] = toDraft(channel);
      }
      setEnabledMap(nextEnabled);
      setDraft(nextDraft);
      setError('');
    } catch (failure) {
      setError(failure instanceof Error ? failure.message : t("渠道配置加载失败"));
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    if (props.open && canManage) void load();
  }, [props.open, canManage, load]);

  // 方法作用：更新某个渠道某个字段的草稿值。
  // Args: channel - 渠道；key - 字段名；value - 新值。
  // Returns: 无返回值。
  const updateField = (channel: NotificationChannelName, key: string, value: string): void => {
    setDraft(prev => ({ ...prev, [channel]: { ...(prev[channel] || {}), [key]: value } }));
  };

  // 方法作用：提交四个渠道的启用状态与配置，敏感项仅在填写时才提交。
  // Args: 无。
  // Returns: 提交结束无返回值；成功后刷新列表。
  const save = async (): Promise<void> => {
    if (saving || loading || error) return;
    setSaving(true);
    try {
      const payload: NotificationChannelSavePayload[] = channels.map(channel => {
        const channelDraft = draft[channel.channel] || {};
        const config: Record<string, unknown> = {};
        for (const field of CHANNEL_FIELDS[channel.channel] || []) {
          const raw = (channelDraft[field.key] ?? '').trim();
          if (SENSITIVE_KEYS.has(field.key)) {
            // 留空不覆盖服务端已存明文；填了才提交新值。
            if (raw) config[field.key] = raw;
            continue;
          }
          if (field.kind === 'switch') config[field.key] = raw === 'true';
          // 数字项留空时不提交该键，避免把 null 当成端口写进后端配置。
          else if (field.kind === 'number') { if (raw) config[field.key] = Number(raw); }
          else config[field.key] = raw;
        }
        return {
          channel: channel.channel,
          enabled: channel.channel === 'in_app' ? true : Boolean(enabledMap[channel.channel]),
          config,
        };
      });
      const result = await saveNotificationChannels(payload);
      message.success(t("渠道配置已保存；邮件与飞书/Slack 目前不会实际外发"));
      if (result?.channels?.length) {
        setChannels(result.channels);
        const nextEnabled: Record<string, boolean> = {};
        const nextDraft: Record<string, Record<string, string>> = {};
        for (const channel of result.channels) {
          nextEnabled[channel.channel] = channel.channel === 'in_app' ? true : Boolean(channel.enabled);
          nextDraft[channel.channel] = toDraft(channel);
        }
        setEnabledMap(nextEnabled);
        setDraft(nextDraft);
      }
    } catch (failure) {
      setError(failure instanceof Error ? failure.message : t("渠道配置保存失败"));
    } finally {
      setSaving(false);
    }
  };

  if (!canManage) return null;

  return (
    <Modal
      open={props.open}
      title={t("通知渠道配置")}
      onCancel={props.onClose}
      onOk={() => void save()}
      confirmLoading={saving}
      okText={t("保存渠道配置")}
      cancelText={t("取消")}
      okButtonProps={{ disabled: loading || Boolean(error) }}
      width={720}
      destroyOnHidden
    >
      <Alert
        type="warning"
        showIcon
        style={{ marginBottom: 16 }}
        message={t("邮件与飞书/Slack 目前不会实际外发，配置仅作预留；站内信始终可用。")}
        description={t("口令与 Webhook 地址属于敏感项，保存后只显示是否已配置，不再回显明文；留空表示不修改已保存的值。")}
      />
      {error && <Alert type="error" showIcon message={error} style={{ marginBottom: 16 }}
        action={<Button size="small" onClick={() => void load()}>{t("重试")}</Button>} />}
      <Spin spinning={loading}>
        <Space direction="vertical" size={12} style={{ width: '100%' }}>
          {channels.map(channel => {
            const enabled = channel.channel === 'in_app' ? true : Boolean(enabledMap[channel.channel]);
            const fields = CHANNEL_FIELDS[channel.channel] || [];
            return (
              <div key={channel.channel} style={{ border: '1px solid #f0f0f0', borderRadius: 8, padding: 12 }}>
                <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center', gap: 8 }}>
                  <Space size={8}>
                    <Typography.Text strong>{t(CHANNEL_TITLES[channel.channel])}</Typography.Text>
                    {channel.configured && <Tag color="green">{t("已配置")}</Tag>}
                    {channel.channel === 'in_app' && <Tag color="blue">{t("始终启用")}</Tag>}
                  </Space>
                  <Switch
                    checked={enabled}
                    disabled={channel.channel === 'in_app' || saving}
                    checkedChildren={t("启用")}
                    unCheckedChildren={t("停用")}
                    onChange={value => setEnabledMap(prev => ({ ...prev, [channel.channel]: value }))}
                  />
                </div>
                <Typography.Text type="secondary" style={{ fontSize: 12 }}>{t(CHANNEL_DESCRIPTIONS[channel.channel])}</Typography.Text>
                {enabled && fields.length > 0 && (
                  <Row gutter={12} style={{ marginTop: 12 }}>
                    {fields.map(field => {
                      const value = draft[channel.channel]?.[field.key] ?? '';
                      // 敏感项已配置时用占位提示代替明文，用户留空即保留原值。
                      const placeholder = SENSITIVE_KEYS.has(field.key) && channel.configured
                        ? t("已配置，留空表示不修改")
                        : t(field.placeholder);
                      return (
                        <Col span={field.kind === 'switch' ? 24 : 12} key={field.key}>
                          <div style={{ marginBottom: 8 }}>
                            <Typography.Text style={{ display: 'block', fontSize: 12, marginBottom: 4 }}>{t(field.label)}</Typography.Text>
                            {field.kind === 'switch' ? (
                              <Switch checked={value === 'true'} disabled={saving}
                                onChange={checked => updateField(channel.channel, field.key, checked ? 'true' : 'false')} />
                            ) : field.kind === 'number' ? (
                              <InputNumber
                                style={{ width: '100%' }}
                                min={1}
                                max={65535}
                                placeholder={placeholder}
                                value={value ? Number(value) : undefined}
                                onChange={input => updateField(channel.channel, field.key, input === null || input === undefined ? '' : String(input))}
                              />
                            ) : field.kind === 'secret' ? (
                              <Input.Password
                                autoComplete="new-password"
                                placeholder={placeholder}
                                value={value}
                                onChange={event => updateField(channel.channel, field.key, event.target.value)}
                              />
                            ) : (
                              <Input
                                placeholder={placeholder}
                                value={value}
                                onChange={event => updateField(channel.channel, field.key, event.target.value)}
                              />
                            )}
                          </div>
                        </Col>
                      );
                    })}
                  </Row>
                )}
              </div>
            );
          })}
        </Space>
      </Spin>
    </Modal>
  );
}
