import { t, useLanguage } from '../i18n';
import { useCallback, useEffect, useState } from 'react';
import { Alert, Button, Form, Input, InputNumber, List, Modal, Popconfirm, Select, Space, Spin, Switch, Tag, Typography, message } from 'antd';
import { ArrowLeftOutlined, DeleteOutlined, ReloadOutlined, ShareAltOutlined, TeamOutlined } from '@ant-design/icons';
import { useNavigate, useParams } from 'react-router-dom';
import ReportDocumentRenderer from '../components/report/ReportDocumentRenderer';
import ReportPageShell from '../components/report/ReportPageShell';
import ReportDrilldownModal from '../components/report/ReportDrilldownModal';
import TemplateSwitcher from '../components/report/TemplateSwitcher';
import { useReportTemplate } from '../hooks/useReportTemplate';
import { createReportShare, fetchPrivateDrilldown, fetchPrivateReport, fetchReportShares, revokeReportShare } from '../api/reportWorkbench';
import type { ReportDrilldownDefinition, ReportDrilldownResponse, PrivateReportResponse, ReportShareRequest, ReportShareResponse, ReportShareSummary } from '../types/report';
import { fetchPageLifecycle, updatePageLifecycle, type PageLifecycle } from '../api/reportWorkbench';

// 方法作用：格式化页面有效期并区分永久、已过期和剩余期限。
// Args: expiresAt - 页面过期时间；now - 当前时间。
// Returns: 用户可读有效期文本和状态颜色。
function expiryInfo(expiresAt: string | null, now = Date.now()): { text: string; color: string } {
  if (!expiresAt) return { text: t("永久有效"), color: 'green' };
  const timestamp = new Date(expiresAt).getTime();
  if (!Number.isFinite(timestamp) || timestamp <= now) return { text: t("已过期"), color: 'red' };
  return { text: t("有效至 {value0}", { value0: new Date(timestamp).toLocaleString() }), color: 'blue' };
}

// 方法作用：展示当前用户可访问的不可变报表页面和快照 Drilldown。
// Args: 无，报表 ID 从路由参数读取。
// Returns: 私有报表查看器页面。
export default function ReportViewerPage() {
  useLanguage();
  const { reportId } = useParams<{ reportId: string }>();
  const navigate = useNavigate();
  const [report, setReport] = useState<PrivateReportResponse | null>(null);
  const [loading, setLoading] = useState(true);
  // 初始值取文档记录的模板，加载完成前回退默认模板。
  const [templateId, setTemplateId] = useReportTemplate(report?.document ?? null);
  const [error, setError] = useState('');
  const [lifecycle, setLifecycle] = useState<PageLifecycle | null>(null);
  const [lifecycleLoading, setLifecycleLoading] = useState(false);
  const [drilldown, setDrilldown] = useState<{ definition: ReportDrilldownDefinition; filters: Record<string, unknown> } | null>(null);
  const [drilldownResult, setDrilldownResult] = useState<ReportDrilldownResponse | null>(null);
  const [drilldownLoading, setDrilldownLoading] = useState(false);
  const [drilldownError, setDrilldownError] = useState('');
  const [shareOpen, setShareOpen] = useState(false);
  const [shareLoading, setShareLoading] = useState(false);
  const [shareResult, setShareResult] = useState<ReportShareResponse | null>(null);
  const [sharesOpen, setSharesOpen] = useState(false);
  const [sharesLoading, setSharesLoading] = useState(false);
  const [shares, setShares] = useState<ReportShareSummary[]>([]);
  const [shareForm] = Form.useForm();

  // 方法作用：加载私有报表页面并将过期状态显式展示给用户。
  // Args: 无。
  // Returns: 异步加载完成后无返回值。
  const loadReport = useCallback(async (): Promise<void> => {
    if (!reportId) {
      setError(t("报表 ID 缺失"));
      setLoading(false);
      return;
    }
    setLoading(true);
    setError('');
    setReport(null);
    setLifecycle(null);
    const lifecycleRequest = fetchPageLifecycle(reportId).then(setLifecycle).catch(() => setLifecycle(null));
    try {
      const result = await fetchPrivateReport(reportId);
      setReport(result);
    } catch (loadError) {
      setError(loadError instanceof Error ? loadError.message : t("报表加载失败"));
    } finally {
      await lifecycleRequest;
      setLoading(false);
    }
  }, [reportId]);

  useEffect(() => { void loadReport(); }, [loadReport]);

  // 方法作用：更新页面期限并重新加载过期页面，既有分享不会自动恢复。
  // Args: days - 延期天数，null 表示永久保留。
  // Returns: 操作完成后无返回值。
  const changeLifecycle = async (days: number | null): Promise<void> => {
    if (!reportId) return;
    setLifecycleLoading(true);
    try {
      await updatePageLifecycle(reportId, days);
      message.success(t("页面期限已更新，既有分享授权保持不变"));
      await loadReport();
    } catch (updateError) {
      message.error(updateError instanceof Error ? updateError.message : t("页面期限更新失败"));
    } finally {
      setLifecycleLoading(false);
    }
  };

  const lifecycleActions = <Space wrap>
    {lifecycle?.can_extend && <><Button loading={lifecycleLoading} onClick={() => void changeLifecycle(7)}>{t("延长 7 天")}</Button><Button loading={lifecycleLoading} onClick={() => void changeLifecycle(30)}>{t("延长 30 天")}</Button></>}
    {lifecycle?.can_permanent && lifecycle.expires_at && <Popconfirm title={t("永久保留此页面？旧分享不会自动延期。")} onConfirm={() => void changeLifecycle(null)}><Button loading={lifecycleLoading}>{t("设置永久")}</Button></Popconfirm>}
  </Space>;

  // 方法作用：按页面签发的 Drilldown 定义读取当前版本明细。
  // Args: definition - 页面签发定义；filters - 图表点击生成的过滤条件。
  // Returns: 异步请求完成后无返回值。
  const loadDrilldown = async (definition: ReportDrilldownDefinition, filters: Record<string, unknown>): Promise<void> => {
    if (!reportId) return;
    setDrilldown({ definition, filters });
    setDrilldownLoading(true);
    setDrilldownError('');
    try {
      const result = await fetchPrivateDrilldown(reportId, definition.drilldown_id, filters);
      setDrilldownResult(result);
    } catch (loadError) {
      setDrilldownResult(null);
      setDrilldownError(loadError instanceof Error ? loadError.message : t("明细加载失败"));
    } finally {
      setDrilldownLoading(false);
    }
  };

  // 方法作用：在 Drilldown 弹窗分页时复用当前签发过滤条件。
  // Args: page - 页码；pageSize - 每页行数。
  // Returns: 异步请求完成后无返回值。
  const changeDrilldownPage = async (page: number, pageSize: number): Promise<void> => {
    if (!drilldown || !reportId) return;
    setDrilldownLoading(true);
    try {
      setDrilldownResult(await fetchPrivateDrilldown(reportId, drilldown.definition.drilldown_id, drilldown.filters, page, pageSize));
    } catch (loadError) {
      setDrilldownError(loadError instanceof Error ? loadError.message : t("明细分页失败"));
    } finally {
      setDrilldownLoading(false);
    }
  };

  // 方法作用：按当前报表版本创建公开或指定用户分享链接。
  // Args: values - 分享范围、授权码、有效期和交互权限表单值。
  // Returns: 创建完成后无返回值。
  const submitShare = async (values: Record<string, unknown>): Promise<void> => {
    if (!reportId) return;
    setShareLoading(true);
    try {
      const userIds = String(values.user_ids_text || '')
        .split(/[，,\s]+/)
        .map(item => Number(item))
        .filter(item => Number.isInteger(item) && item > 0);
      const request: ReportShareRequest = {
        scope: values.scope === 'user' ? 'user' : 'public',
        require_code: Boolean(values.require_code),
        authorization_code: String(values.authorization_code || ''),
        expires_in_days: Number(values.expires_in_days || 7),
        allow_drilldown: Boolean(values.allow_drilldown),
        allow_download: false,
        pin_latest: false,
        user_ids: userIds,
      };
      const result = await createReportShare(reportId, request);
      setShareResult(result);
      void loadShares();
      message.success(t("分享链接已创建"));
    } catch (shareError) {
      message.error(shareError instanceof Error ? shareError.message : t("创建分享失败"));
    } finally {
      setShareLoading(false);
    }
  };

  // 方法作用：读取当前报表的分享摘要，供管理员撤销失效链接。
  // Args: 无，报表 ID 从路由参数读取。
  // Returns: 异步读取完成后无返回值。
  const loadShares = async (): Promise<void> => {
    if (!reportId) return;
    setSharesLoading(true);
    try {
      const result = await fetchReportShares(reportId);
      setShares(result.shares || []);
    } catch (loadError) {
      message.error(loadError instanceof Error ? loadError.message : t("分享列表加载失败"));
    } finally {
      setSharesLoading(false);
    }
  };

  // 方法作用：撤销当前用户可管理的分享链接并刷新摘要。
  // Args: shareId - 分享记录 ID。
  // Returns: 异步撤销完成后无返回值。
  const revokeShare = async (shareId: string): Promise<void> => {
    try {
      await revokeReportShare(shareId);
      setShares(current => current.map(item => item.id === shareId ? { ...item, revoked_at: new Date().toISOString() } : item));
      message.success(t("分享链接已撤销"));
    } catch (revokeError) {
      message.error(revokeError instanceof Error ? revokeError.message : t("撤销分享失败"));
    }
  };

  // 加载态与错误态同样落在统一外壳内，避免加载完成前后是两套观感。
  if (loading) return <ReportPageShell templateId={templateId}><div className="report-document"><Spin tip={t("报表加载中")} /></div></ReportPageShell>;
  if (error || !report) return <ReportPageShell templateId={templateId}><div className="report-document"><Alert type="error" showIcon message={error || t("报表不存在")} action={<Button icon={<ReloadOutlined />} onClick={() => void loadReport()}>{t("重试")}</Button>} />{lifecycle?.expired && lifecycleActions}</div></ReportPageShell>;
  const expiry = expiryInfo(report.document.expires_at);
  // 顶栏作为外壳的 toolbar 槽位传入，从而在 TemplateScope 内消费同一套 --report-* 变量。
  const toolbar = <>
    <Space wrap>
      <Button icon={<ArrowLeftOutlined />} onClick={() => navigate('/report-workbench')}>{t("返回工作台")}</Button>
      <Tag color={expiry.color}>{expiry.text}</Tag>
      <Tag>{t("版本")} {report.version}</Tag>
    </Space>
    <Space>
      {/* 模板切换只影响本机这次浏览，不写回服务端、不改报表版本 */}
      <TemplateSwitcher value={templateId} onChange={setTemplateId} />
      {lifecycleActions}
      <Button icon={<TeamOutlined />} onClick={() => { setSharesOpen(true); void loadShares(); }}>{t("管理分享")}</Button>
      <Button icon={<ShareAltOutlined />} onClick={() => { setShareResult(null); shareForm.resetFields(); setShareOpen(true); }}>{t("分享")}</Button>
    </Space>
  </>;
  return <>
    <ReportPageShell templateId={templateId} toolbar={toolbar}>
      <ReportDocumentRenderer document={report.document} dataManifest={report.data_manifest} templateId={templateId} onDrilldown={(definition, filters) => void loadDrilldown(definition, filters)} />
    </ReportPageShell>
    {/* 弹窗不进模板作用域：模板不为 Modal 的样式负责 */}
    <ReportDrilldownModal
      open={Boolean(drilldown)}
      loading={drilldownLoading}
      title={drilldown ? t("明细：{value0}", { value0: drilldown.definition.source_block_id }) : t("明细")}
      result={drilldownResult}
      error={drilldownError}
      onClose={() => { setDrilldown(null); setDrilldownResult(null); setDrilldownError(''); }}
      onPageChange={(page, pageSize) => void changeDrilldownPage(page, pageSize)}
    />
    <Modal title={t("创建分享链接")} open={shareOpen} onCancel={() => setShareOpen(false)} footer={null} destroyOnHidden>
      {shareResult ? <Space direction="vertical" style={{ width: '100%' }}>
        <Alert type="success" showIcon message={t("链接创建成功")} description={shareResult.authorization_code_hint || undefined} />
        <Input.TextArea className="report-share-url" readOnly value={`${window.location.origin}${shareResult.url}`} autoSize={{ minRows: 2, maxRows: 4 }} />
        <Space>
          <Button onClick={() => void navigator.clipboard?.writeText(`${window.location.origin}${shareResult.url}`)}>{t("复制链接")}</Button>
          <Button type="primary" onClick={() => setShareOpen(false)}>{t("完成")}</Button>
        </Space>
      </Space> : <Form
        form={shareForm}
        layout="vertical"
        initialValues={{ scope: 'public', require_code: false, expires_in_days: 7, allow_drilldown: false }}
        onFinish={values => void submitShare(values as Record<string, unknown>)}
      >
        <Form.Item name="scope" label={t("访问范围")}>
          <Select options={[{ value: 'public', label: t("公开链接") }, { value: 'user', label: t("指定用户") }]} />
        </Form.Item>
        <Form.Item noStyle shouldUpdate={(previous, current) => previous.scope !== current.scope}>
          {({ getFieldValue }) => getFieldValue('scope') === 'user' ? <Form.Item name="user_ids_text" label={t("用户 ID")} rules={[{ required: true, message: t("请输入用户 ID") }]}>
            <Input placeholder={t("多个 ID 用逗号分隔")} />
          </Form.Item> : null}
        </Form.Item>
        <Form.Item name="expires_in_days" label={t("有效期（天）")} rules={[{ required: true, type: 'number', min: 1, max: 90 }]}>
          <InputNumber min={1} max={90} style={{ width: '100%' }} />
        </Form.Item>
        <Form.Item name="require_code" label={t("需要授权码")} valuePropName="checked">
          <Switch />
        </Form.Item>
        <Form.Item noStyle shouldUpdate={(previous, current) => previous.require_code !== current.require_code}>
          {({ getFieldValue }) => getFieldValue('require_code') ? <Form.Item name="authorization_code" label={t("授权码")} rules={[{ required: true, min: 6, message: t("授权码至少 6 个字符") }]}>
            <Input.Password autoComplete="off" />
          </Form.Item> : null}
        </Form.Item>
        <Form.Item name="allow_drilldown" label={t("允许快照明细")} valuePropName="checked">
          <Switch />
        </Form.Item>
        <Button type="primary" htmlType="submit" loading={shareLoading} block>{t("创建链接")}</Button>
      </Form>}
    </Modal>
    <Modal title={t("分享管理")} open={sharesOpen} onCancel={() => setSharesOpen(false)} footer={null} destroyOnHidden>
      <List
        loading={sharesLoading}
        dataSource={shares}
        locale={{ emptyText: t("暂无分享链接") }}
        renderItem={share => <List.Item actions={[
          <Popconfirm key={share.id} title={t("确认撤销此分享？")} onConfirm={() => void revokeShare(share.id)} okText={t("撤销")} cancelText={t("取消")}>
            <Button danger type="link" size="small" icon={<DeleteOutlined />} disabled={Boolean(share.revoked_at)}>{t("撤销")}</Button>
          </Popconfirm>,
        ]}>
          <List.Item.Meta
            title={<Space><Tag>{share.scope === 'public' ? t("公开") : t("指定用户")}</Tag><Typography.Text>{share.revoked_at ? t("已撤销") : t("有效")}</Typography.Text></Space>}
            description={<Typography.Text type="secondary">{share.revoked_at ? t("撤销于 {value0}", { value0: new Date(share.revoked_at).toLocaleString() }) : t("有效至 {value0}", { value0: share.expires_at ? new Date(share.expires_at).toLocaleString() : t("永久") })} {t("· 查看")} {share.view_count ?? 0} {t("次")}</Typography.Text>}
          />
        </List.Item>}
      />
    </Modal>
  </>;
}
