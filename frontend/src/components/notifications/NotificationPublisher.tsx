import { t, useLanguage } from '../../i18n';
import { useEffect, useState } from 'react';
import { Alert, Checkbox, Form, Input, Modal, Radio, Select, Space, Typography, message } from 'antd';
import {
  publishNotification,
  type NotificationAudience,
  type NotificationAudienceScope,
  type NotificationPublishKind,
} from '../../api/notifications';
import { useAuth } from '../../hooks/AuthContext';

// 可投递角色与界面文案一一对应，提交时用角色键。
const ROLE_OPTIONS = [
  { label: t("租户管理员"), value: 'tenant_admin' },
  { label: t("分析师"), value: 'analyst' },
  { label: t("只读用户"), value: 'viewer' },
];

// 方法作用：把角色键转成界面文案。
// Args: role - 角色键。
// Returns: 中文角色名；未知角色原样返回。
export function notificationRoleLabel(role: string): string {
  return ROLE_OPTIONS.find(option => option.value === role)?.label || role;
}

// 方法作用：把用户输入的标签文本解析成正整数用户 ID，过滤非法输入。
// Args: values - Select 标签值数组。
// Returns: 去重后的用户 ID 数组。
export function parseAudienceUserIds(values?: string[]): number[] {
  const ids = (values || [])
    .map(value => Number(String(value).trim()))
    .filter(value => Number.isInteger(value) && value > 0);
  return Array.from(new Set(ids));
}

// 方法作用：把投递范围转成可读文案，用于发布成功提示。
// Args: audience - 投递范围。
// Returns: 「全部用户」或具体范围说明。
export function audienceSummary(audience: NotificationAudience): string {
  if (audience.scope === 'users') return t("指定 {value0} 个用户", { value0: audience.user_ids.length });
  if (audience.scope === 'role') {
    const labels = audience.roles.map(notificationRoleLabel);
    return labels.length > 0 ? t("角色：{value0}", { value0: labels.join('、') }) : t("未选择角色");
  }
  return t("全部用户");
}

export interface NotificationPublisherProps {
  open: boolean;
  onClose: () => void;
  onPublished?: () => void;
  // 可见性覆盖：缺省按登录角色判断（平台通知仅超管，租户/用户群通知仅租户管理员）。
  canPublishPlatform?: boolean;
  canPublishTenant?: boolean;
}

// 方法作用：渲染通知发布表单，按角色提供平台/租户/指定用户群三类发布选项。
// Args: open - 是否显示；onClose - 关闭回调；onPublished - 发布成功回调；canPublishPlatform/canPublishTenant - 可见性与权限覆盖。
// Returns: 发布弹窗 React 元素；无发布权限时返回 null。
export default function NotificationPublisher(props: NotificationPublisherProps) {
  useLanguage();
  const { user } = useAuth();
  const [form] = Form.useForm();
  const [kind, setKind] = useState<NotificationPublishKind>('tenant');
  const [scope, setScope] = useState<NotificationAudienceScope>('all');
  const [submitting, setSubmitting] = useState(false);
  const [error, setError] = useState('');

  // 发布权限默认取当前登录角色：平台通知只允许超管，租户与用户群通知只允许租户管理员。
  const canPublishPlatform = props.canPublishPlatform ?? user?.role === 'super_admin';
  const canPublishTenant = props.canPublishTenant ?? user?.role === 'tenant_admin';
  const kindOptions = [
    canPublishPlatform ? { label: t("平台通知"), value: 'platform' } : null,
    canPublishTenant ? { label: t("租户通知"), value: 'tenant' } : null,
    canPublishTenant ? { label: t("指定用户群通知"), value: 'group' } : null,
  ].filter((option): option is { label: string; value: NotificationPublishKind } => option !== null);

  // 打开时重置表单与选择项，避免上一次的输入串到下一次发布。
  useEffect(() => {
    if (!open) return;
    form.resetFields();
    setKind(canPublishTenant ? 'tenant' : 'platform');
    setScope('all');
    setError('');
  }, [open, canPublishTenant, form]);

  // 方法作用：提交发布请求；成功后提示实际投递范围，失败展示后端可读原因。
  // Args: 无，读取表单与当前选择项。
  // Returns: 提交结束无返回值。
  const submit = async (): Promise<void> => {
    if (submitting) return;
    let values: { title?: string; body?: string; link?: string; user_ids?: string[]; roles?: string[] };
    try {
      values = await form.validateFields();
    } catch {
      // 表单校验失败由字段下方提示说明，这里不再额外弹错误。
      return;
    }
    const audience: NotificationAudience = {
      scope,
      user_ids: scope === 'users' ? parseAudienceUserIds(values.user_ids) : [],
      roles: scope === 'role' ? values.roles || [] : [],
    };
    if (scope === 'users' && audience.user_ids.length === 0) {
      setError(t("请至少填写一个用户 ID"));
      return;
    }
    if (scope === 'role' && audience.roles.length === 0) {
      setError(t("请至少选择一个角色"));
      return;
    }
    setSubmitting(true);
    setError('');
    try {
      const result = await publishNotification({
        kind,
        title: values.title || '',
        body: values.body || '',
        link: values.link || '',
        audience,
      });
      // 后端若回传实际投递人数则一并展示，否则只展示本次选择的范围。
      const delivered = (result as { delivered_count?: unknown }).delivered_count;
      const deliveredText = typeof delivered === 'number' ? t("，实际投递 {value0} 人", { value0: delivered }) : '';
      message.success(t("通知已发布，投递范围：{value0}{value1}", {
        value0: audienceSummary(audience), value1: deliveredText,
      }));
      props.onPublished?.();
      props.onClose();
    } catch (failure) {
      setError(failure instanceof Error ? failure.message : t("通知发布失败"));
    } finally {
      setSubmitting(false);
    }
  };

  if (kindOptions.length === 0) return null;

  return (
    <Modal
      open={props.open}
      title={t("发布通知")}
      onCancel={props.onClose}
      onOk={() => void submit()}
      confirmLoading={submitting}
      okText={t("发布")}
      cancelText={t("取消")}
      destroyOnHidden
    >
      <Alert
        type="info"
        showIcon
        style={{ marginBottom: 16 }}
        message={t("平台通知投递给所有租户，租户通知与用户群通知只投递给本租户；个人通知由系统在任务完成时自动产生，不在此发布。邮件与飞书/Slack 目前不会实际外发，本次发布只送达站内信。")}
      />
      {error && <Alert type="error" showIcon message={error} style={{ marginBottom: 16 }} />}
      <Form form={form} layout="vertical">
        <Form.Item label={t("通知类型")} required>
          <Radio.Group
            value={kind}
            onChange={event => setKind(event.target.value as NotificationPublishKind)}
            options={kindOptions}
          />
        </Form.Item>
        <Form.Item name="title" label={t("标题")} rules={[{ required: true, message: t("请填写通知标题") }]}>
          <Input maxLength={200} placeholder={t("通知标题")} />
        </Form.Item>
        <Form.Item name="body" label={t("正文")} rules={[{ required: true, message: t("请填写通知正文") }]}>
          <Input.TextArea rows={4} maxLength={2000} placeholder={t("通知正文")} />
        </Form.Item>
        <Form.Item name="link" label={t("跳转链接（可选）")} extra={t("填写后点击通知会跳转到该站内路径")}>
          <Input maxLength={500} placeholder="/report-workbench" />
        </Form.Item>
        <Form.Item label={t("投递范围")} required>
          <Radio.Group value={scope} onChange={event => setScope(event.target.value as NotificationAudienceScope)}>
            <Space direction="vertical">
              <Radio value="all">{t("全部用户")}</Radio>
              <Radio value="users">{t("指定用户")}</Radio>
              <Radio value="role">{t("按角色")}</Radio>
            </Space>
          </Radio.Group>
        </Form.Item>
        {scope === 'users' && (
          <Form.Item name="user_ids" label={t("用户 ID 列表")} extra={t("输入用户 ID 后回车，可填写多个")}>
            <Select mode="tags" open={false} placeholder={t("例如：12")} tokenSeparators={[',', ' ']} />
          </Form.Item>
        )}
        {scope === 'role' && (
          <Form.Item name="roles" label={t("角色")}>
            <Checkbox.Group options={ROLE_OPTIONS} />
          </Form.Item>
        )}
        <Typography.Text type="secondary" style={{ fontSize: 12 }}>
          {t("发布后不可撤回，请确认标题与投递范围。")}
        </Typography.Text>
      </Form>
    </Modal>
  );
}
