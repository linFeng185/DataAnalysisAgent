import { t, useLanguage } from '../i18n';
import { useCallback, useEffect, useState } from 'react';
import { Alert, Button, Form, Input, Modal, Space, Spin, Tag, Typography, message } from 'antd';
import { LockOutlined, ReloadOutlined } from '@ant-design/icons';
import { useParams } from 'react-router-dom';
import ReportDocumentRenderer from '../components/report/ReportDocumentRenderer';
import ReportDrilldownModal from '../components/report/ReportDrilldownModal';
import {
  fetchSharedDrilldown,
  fetchSharedReport,
  unlockSharedReport,
} from '../api/reportWorkbench';
import type { ReportDrilldownDefinition, ReportDrilldownResponse, ReportSharedResponse } from '../types/report';

// 方法作用：展示不依赖登录的公开报表快照和授权码解锁状态。
// Args: 无，分享 token 从路由参数读取。
// Returns: 公开报表查看器页面。
export default function SharedReportPage() {
  useLanguage();
  const { token } = useParams<{ token: string }>();
  const [report, setReport] = useState<ReportSharedResponse | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState('');
  const [locked, setLocked] = useState(false);
  const [unlockOpen, setUnlockOpen] = useState(false);
  const [unlocking, setUnlocking] = useState(false);
  const [sessionToken, setSessionToken] = useState('');
  const [drilldown, setDrilldown] = useState<{ definition: ReportDrilldownDefinition; filters: Record<string, unknown> } | null>(null);
  const [drilldownResult, setDrilldownResult] = useState<ReportDrilldownResponse | null>(null);
  const [drilldownLoading, setDrilldownLoading] = useState(false);
  const [drilldownError, setDrilldownError] = useState('');

  // 方法作用：读取公开快照，并将授权码锁定响应转换为解锁界面状态。
  // Args: 无。
  // Returns: 异步加载完成后无返回值。
  const loadReport = useCallback(async (): Promise<void> => {
    if (!token) {
      setError(t("分享 token 缺失"));
      setLoading(false);
      return;
    }
    setLoading(true);
    setError('');
    try {
      setReport(await fetchSharedReport(token, sessionToken));
      setLocked(false);
    } catch (loadError) {
      const messageText = loadError instanceof Error ? loadError.message : t("分享报表加载失败");
      if (messageText.includes(t("授权码")) || messageText.includes(t("解锁"))) {
        setLocked(true);
        setUnlockOpen(true);
        setError('');
      } else {
        setError(messageText);
      }
    } finally {
      setLoading(false);
    }
  }, [token, sessionToken]);

  useEffect(() => { void loadReport(); }, [loadReport]);

  // 方法作用：验证用户输入的分享授权码并刷新快照。
  // Args: values - 授权码表单值。
  // Returns: 解锁完成后无返回值。
  const unlock = async (values: { authorization_code: string }): Promise<void> => {
    if (!token) return;
    setUnlocking(true);
    try {
      const result = await unlockSharedReport(token, values.authorization_code);
      setSessionToken(result.session_token);
      setUnlockOpen(false);
      message.success(t("分享已解锁"));
    } catch (unlockError) {
      message.error(unlockError instanceof Error ? unlockError.message : t("授权码验证失败"));
    } finally {
      setUnlocking(false);
    }
  };

  // 方法作用：按分享授权的 snapshot Drilldown 读取脱敏明细。
  // Args: definition - 页面签发定义；filters - 图表点击过滤条件。
  // Returns: 异步请求完成后无返回值。
  const loadDrilldown = async (definition: ReportDrilldownDefinition, filters: Record<string, unknown>): Promise<void> => {
    if (!token) return;
    setDrilldown({ definition, filters });
    setDrilldownLoading(true);
    setDrilldownError('');
    try {
      setDrilldownResult(await fetchSharedDrilldown(token, definition.drilldown_id, sessionToken, filters));
    } catch (loadError) {
      setDrilldownResult(null);
      setDrilldownError(loadError instanceof Error ? loadError.message : t("公开明细加载失败"));
    } finally {
      setDrilldownLoading(false);
    }
  };

  // 方法作用：在公开 Drilldown 弹窗分页时复用当前授权范围和过滤条件。
  // Args: page - 页码；pageSize - 每页行数。
  // Returns: 异步请求完成后无返回值。
  const changeDrilldownPage = async (page: number, pageSize: number): Promise<void> => {
    if (!drilldown || !token) return;
    setDrilldownLoading(true);
    try {
      setDrilldownResult(await fetchSharedDrilldown(token, drilldown.definition.drilldown_id, sessionToken, drilldown.filters, page, pageSize));
    } catch (loadError) {
      setDrilldownError(loadError instanceof Error ? loadError.message : t("公开明细分页失败"));
    } finally {
      setDrilldownLoading(false);
    }
  };

  if (loading) return <div className="report-document"><Spin tip={t("分享报表加载中")} /></div>;
  if (error) return <div className="report-document"><Alert type="error" showIcon message={error} action={<Button icon={<ReloadOutlined />} onClick={() => void loadReport}>{t("重试")}</Button>} /></div>;
  if (!report && !locked) return <div className="report-document"><Alert type="warning" showIcon message={t("分享报表暂时不可用")} /></div>;
  return <div>
    {locked && <div className="report-document"><Alert type="info" showIcon icon={<LockOutlined />} message={t("该分享需要授权码")} action={<Button type="primary" onClick={() => setUnlockOpen(true)}>{t("输入授权码")}</Button>} /></div>}
    {report && <>
      <div className="report-document report-viewer-toolbar"><Tag color="blue">{t("公开快照 · 版本")} {report.version}</Tag><Typography.Text type="secondary">{t("页面内容来自固定脱敏版本")}</Typography.Text></div>
      <ReportDocumentRenderer document={report.document} dataManifest={report.data_manifest} onDrilldown={(definition, filters) => void loadDrilldown(definition, filters)} />
    </>}
    <Modal title={t("解锁分享报表")} open={unlockOpen} onCancel={() => setUnlockOpen(false)} footer={null} destroyOnHidden>
      <Form layout="vertical" onFinish={values => void unlock(values as { authorization_code: string })}>
        <Form.Item name="authorization_code" label={t("授权码")} rules={[{ required: true, min: 1, message: t("请输入授权码") }]}>
          <Input.Password autoComplete="off" placeholder={t("输入分享创建者提供的授权码")} />
        </Form.Item>
        <Button type="primary" htmlType="submit" loading={unlocking} block>{t("解锁")}</Button>
      </Form>
    </Modal>
    <ReportDrilldownModal
      open={Boolean(drilldown)}
      loading={drilldownLoading}
      title={drilldown ? t("公开明细：{value0}", { value0: drilldown.definition.source_block_id }) : t("公开明细")}
      result={drilldownResult}
      error={drilldownError}
      onClose={() => { setDrilldown(null); setDrilldownResult(null); setDrilldownError(''); }}
      onPageChange={(page, pageSize) => void changeDrilldownPage(page, pageSize)}
    />
  </div>;
}
