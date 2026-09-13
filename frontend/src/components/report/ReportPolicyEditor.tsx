import { t, useLanguage } from '../../i18n';
import { useEffect, useState } from 'react';
import { Alert, Button, Checkbox, Divider, Form, InputNumber, Modal, Popconfirm, Space, Spin, Typography, message } from 'antd';
import { get, put } from '../../api/client';
import { resetReportQuotaUsage } from '../../api/reportWorkbench';
import type { ReportQuotaResetField } from '../../types/report';
import { REPORT_QUOTA_RESET_FIELDS, quotaResetResultMessage } from './quotaResetFields';

const fields = [
  ['max_jobs_created', t("每月可创建任务数"), 0, 100000],
  ['max_runs_started', t("每月可运行次数"), 0, 100000],
  ['max_concurrent_runs', t("同时运行上限"), 0, 100],
  ['max_analysis_units_per_run', t("单次分析单元上限"), 1, 12],
  ['max_queries_per_run', t("单次查询上限"), 1, 16],
  ['max_rows_per_query', t("单次查询行数上限"), 1, 10000000],
  ['max_llm_tokens', t("每月 Token 预算"), 0, 10000000],
  ['max_page_bytes', t("单页大小上限（字节）"), 1024, 50000000],
  ['max_share_links', t("每月分享链接上限"), 0, 100000],
] as const;

// 能力清单必须覆盖后端 _ROLE_CAPABILITIES 里的全部 report.* 键：
// 保存额度是「整体替换」语义，清单里缺哪个键，保存一次就会把该权限从用户身上抹掉。
// 三个子页键分别对应报表工作台的三个子菜单（创建报表 / 报表信息 / 链接管理）。
const capabilities = [
  ['report.menu', t("工作台菜单")],
  ['report.create_page', t("创建报表页")], ['report.list', t("报表信息页")], ['report.link_manage', t("链接管理页")],
  ['report.create', t("创建报表")], ['report.schedule', t("定时任务")],
  ['report.share', t("分享报表")], ['report.extend', t("页面延期")], ['report.permanent', t("永久保存")],
  ['report.live_drilldown', t("实时钻取")], ['report.view_all', t("查看租户报表")], ['report.download', t("下载")],
].map(([value, label]) => ({ value, label }));

interface PolicyResponse { policy: Record<string, number>; capabilities: string[]; limits?: Record<string, number>; }
interface Props { user: { id: number; username: string }; onClose: () => void; }

// 方法作用：供租户管理员维护本租户用户的报表能力与自然月额度。
// Args: user - 当前租户目标用户；onClose - 关闭弹窗回调。
// Returns: 策略编辑弹窗。
export default function ReportPolicyEditor({ user, onClose }: Props) {
  useLanguage();
  const [form] = Form.useForm();
  const [loading, setLoading] = useState(true);
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState('');
  // 重置区独立于额度表单：它不清零上限，也不参与「保存额度」的提交。
  const [resetFields, setResetFields] = useState<ReportQuotaResetField[]>([]);
  const [resetting, setResetting] = useState(false);
  // 系统上限由后端下发（超级管理员在配置里设定），表单不得自己写死一个更小的值。
  const [limits, setLimits] = useState<Record<string, number>>({});
  useEffect(() => {
    let active = true;
    setLoading(true);
    get<PolicyResponse>(`/report-workbench/admin/users/${user.id}/policy`)
      .then(result => {
        if (!active) return;
        form.setFieldsValue({ ...result.policy, capabilities: result.capabilities });
        setLimits(result.limits || {});
      })
      .catch(failure => { if (active) setError(failure instanceof Error ? failure.message : t("策略读取失败")); })
      .finally(() => { if (active) setLoading(false); });
    return () => { active = false; };
  }, [user.id, form]);

  // 方法作用：验证输入并保存策略，失败时保留表单以便重试。
  // Args: 无。
  // Returns: 保存结束后无返回值。
  const save = async (): Promise<void> => {
    if (saving || loading || error) return;
    setSaving(true);
    try {
      const values = await form.validateFields();
      await put(`/report-workbench/admin/users/${user.id}/policy`, values);
      message.success(t("报表额度已保存"));
      onClose();
    } catch (failure) {
      if (failure instanceof Error) message.error(failure.message);
    } finally {
      setSaving(false);
    }
  };

  // 方法作用：把选中字段在当前自然月的已用量清零，不改动额度上限。
  // Args: 无，读取 resetFields 与当前用户。
  // Returns: 提交结束后无返回值；成功后保留弹窗以便继续调整上限。
  const resetUsage = async (): Promise<void> => {
    if (resetting || resetFields.length === 0) return;
    setResetting(true);
    try {
      const result = await resetReportQuotaUsage({ user_ids: [user.id], fields: resetFields });
      message.success(quotaResetResultMessage(result, 1));
    } catch (failure) {
      message.error(failure instanceof Error ? failure.message : t("已用量重置失败"));
    } finally {
      setResetting(false);
    }
  };

  return <Modal open title={t("报表额度 · {value0}", { value0: user.username })} onCancel={onClose}
    onOk={() => void save()} confirmLoading={saving} okText={t("保存额度")} okButtonProps={{ disabled: loading || Boolean(error) }}>
    <Alert type="info" showIcon message={t("当前按自然月统计（Asia/Shanghai）：保存额度只修改上限、不清空已用量；重置已用量只清零本周期用量、不修改上限。业务额度由租户管理员管理。")} style={{ marginBottom: 16 }} />
    {error ? <Alert type="error" message={error} /> : <Spin spinning={loading}>
      <Form form={form} layout="vertical">
        <Form.Item name="capabilities" label={t("报表能力")}><Checkbox.Group options={capabilities} /></Form.Item>
        {fields.map(([key, label, minimum, maximum]) => {
          const ceiling = limits[key];
          return <Form.Item
            key={key}
            name={key}
            label={label}
            rules={[{ required: true }]}
            // 有系统上限时把上限写出来：否则输入被静默钳住，看起来像"改了没生效"。
            extra={ceiling === undefined ? undefined : t("受系统上限约束，最大 {value0}", { value0: ceiling })}
          >
            {/* 有系统上限时以它为准；没有对应配置项的字段沿用本地上界 */}
            <InputNumber min={minimum} max={ceiling ?? maximum} precision={0} style={{ width: '100%' }} />
          </Form.Item>;
        })}
      </Form>
      <Divider plain style={{ margin: '8px 0 12px' }}>{t("重置本周期已用量")}</Divider>
      <Typography.Paragraph type="secondary" style={{ marginBottom: 12 }}>
        {t("清零选中字段在当前自然月的已用量，不修改上面的额度上限；重置结果不可撤销。")}
      </Typography.Paragraph>
      <Space direction="vertical" size={12} style={{ width: '100%' }}>
        <Checkbox.Group options={REPORT_QUOTA_RESET_FIELDS} value={resetFields}
          onChange={values => setResetFields(values as ReportQuotaResetField[])} />
        <Popconfirm title={t("确认重置本周期已用量？")}
          description={t("将清零选中字段的本月已用量，且不可撤销。")}
          okText={t("确认重置")} cancelText={t("取消")}
          onConfirm={() => void resetUsage()} disabled={resetFields.length === 0}>
          <Button danger loading={resetting} disabled={resetFields.length === 0}>{t("重置已用量")}</Button>
        </Popconfirm>
      </Space>
    </Spin>}
  </Modal>;
}
