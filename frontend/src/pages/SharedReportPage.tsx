import { t, useLanguage } from '../i18n';
import { useCallback, useEffect, useRef, useState } from 'react';
import { Alert, Button, Form, Input, Modal, Space, Spin, Tag, Typography, message } from 'antd';
import { LockOutlined, ReloadOutlined } from '@ant-design/icons';
import { useParams } from 'react-router-dom';
import ReportDocumentRenderer from '../components/report/ReportDocumentRenderer';
import ReportPageShell from '../components/report/ReportPageShell';
import ReportDrilldownModal from '../components/report/ReportDrilldownModal';
import TemplateSwitcher from '../components/report/TemplateSwitcher';
import { useReportTemplate } from '../hooks/useReportTemplate';
import {
  fetchSharedDrilldown,
  fetchSharedReport,
  reportSharedReportVisitDuration,
  startSharedReportVisit,
  unlockSharedReport,
} from '../api/reportWorkbench';
import type { ReportDrilldownDefinition, ReportDrilldownResponse, ReportSharedResponse } from '../types/report';

/** 停留时长心跳间隔：每 30 秒上报一次累积值。 */
const VISIT_HEARTBEAT_MS = 30000;

// 方法作用：公开分享页停留时长埋点：快照加载成功后登记一次访问，只累计页面可见时间并按累积值上报。
// Args: token - URL 分享 token；loaded - 公开快照是否已成功加载；unlocked - 是否已通过授权码解锁。
// Returns: 无返回值，副作用为访问登记、可见时长累计与心跳上报。
function useShareVisitBeacon(token: string | undefined, loaded: boolean, unlocked: boolean): void {
  // 访问 ID：登记成功前不上报时长，避免把停留时长挂到不存在的访问记录上。
  const visitIdRef = useRef('');
  // StrictMode 会重复执行一次挂载副作用，用 ref 记住已登记的 token，保证只产生一条访问记录。
  const registeredTokenRef = useRef('');
  const unlockedRef = useRef(unlocked);
  // 累积停留秒数与当前可见区段起点；隐藏期间起点为 null，因此后台时间不计入。
  const visibleSecondsRef = useRef(0);
  const visibleSinceRef = useRef<number | null>(null);

  // 解锁状态只影响后续心跳里的 unlocked 标记，不重新登记访问，也不单独补发请求。
  useEffect(() => { unlockedRef.current = unlocked; }, [unlocked]);

  // 方法作用：结算当前可见区段的停留时长，隐藏期间不计时。
  // Args: 无。
  // Returns: 无返回值，结果累加到 visibleSecondsRef。
  const settleVisible = useCallback((): void => {
    if (visibleSinceRef.current === null) return;
    const now = Date.now();
    visibleSecondsRef.current += Math.max(0, now - visibleSinceRef.current) / 1000;
    // 仍然可见时从当前时刻继续累计，保证心跳拿到的始终是累积值。
    visibleSinceRef.current = document.visibilityState === 'visible' ? now : null;
  }, []);

  // 登记一次访问：同一 token 只登记一次，StrictMode 双挂载第二次直接跳过。
  useEffect(() => {
    if (!token || !loaded) return;
    if (registeredTokenRef.current === token) return;
    registeredTokenRef.current = token;
    void startSharedReportVisit(token, unlockedRef.current)
      .then(result => { visitIdRef.current = result.visit_id; })
      .catch(error => console.debug('分享页访问登记失败', error));
  }, [token, loaded]);

  // 计时与上报：组件挂载时开始，卸载时清理定时器和监听，不给用户留后台请求。
  useEffect(() => {
    if (!token) return undefined;
    // 页面在后台打开时先不计时，等变为可见再开始累计。
    visibleSinceRef.current = document.visibilityState === 'visible' ? Date.now() : null;

    // 方法作用：把当前累积停留时长上报给服务端，卸载路径用 keepalive 让请求活到页面销毁之后。
    // Args: keepalive - 页面卸载时为 true，普通心跳为 false。
    // Returns: 无返回值，失败只写调试日志，不打扰访客。
    const flush = (keepalive: boolean): void => {
      settleVisible();
      const visitId = visitIdRef.current;
      if (!visitId) return;
      void reportSharedReportVisitDuration(token, visitId, Math.round(visibleSecondsRef.current), unlockedRef.current, keepalive)
        .catch(error => console.debug('分享页停留时长上报失败', error));
    };

    // 方法作用：可见性变化时开始或结算计时，隐藏期间的时间不计入停留时长。
    // Args: 无。
    // Returns: 无返回值。
    const onVisibilityChange = (): void => {
      if (document.visibilityState === 'visible') {
        if (visibleSinceRef.current === null) visibleSinceRef.current = Date.now();
        return;
      }
      settleVisible();
      visibleSinceRef.current = null;
    };
    // 方法作用：页面卸载（关闭标签、刷新、前进后退）时补发最后一次累积值。
    // Args: 无。
    // Returns: 无返回值。
    const onPageHide = (): void => flush(true);

    const heartbeat = window.setInterval(() => flush(false), VISIT_HEARTBEAT_MS);
    document.addEventListener('visibilitychange', onVisibilityChange);
    window.addEventListener('pagehide', onPageHide);
    return () => {
      window.clearInterval(heartbeat);
      document.removeEventListener('visibilitychange', onVisibilityChange);
      window.removeEventListener('pagehide', onPageHide);
    };
  }, [token, settleVisible]);
}

// 方法作用：展示不依赖登录的公开报表快照和授权码解锁状态。
// Args: 无，分享 token 从路由参数读取。
// Returns: 公开报表查看器页面。
export default function SharedReportPage() {
  useLanguage();
  const { token } = useParams<{ token: string }>();
  const [report, setReport] = useState<ReportSharedResponse | null>(null);
  const [loading, setLoading] = useState(true);
  // 初始值取文档记录的模板；分享对象也能按自己的偏好临时换风格。
  const [templateId, setTemplateId] = useReportTemplate(report?.document ?? null);
  const [error, setError] = useState('');
  const [locked, setLocked] = useState(false);
  const [unlockOpen, setUnlockOpen] = useState(false);
  const [unlocking, setUnlocking] = useState(false);
  const [sessionToken, setSessionToken] = useState('');
  const [drilldown, setDrilldown] = useState<{ definition: ReportDrilldownDefinition; filters: Record<string, unknown> } | null>(null);
  const [drilldownResult, setDrilldownResult] = useState<ReportDrilldownResponse | null>(null);
  const [drilldownLoading, setDrilldownLoading] = useState(false);
  const [drilldownError, setDrilldownError] = useState('');

  // 停留时长埋点：快照加载成功后才登记访问，授权码解锁状态随心跳一起上报。
  useShareVisitBeacon(token, Boolean(report), Boolean(sessionToken));

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

  // 加载态、错误态与授权码提示同样落在统一外壳内，避免加载完成前后是两套观感。
  if (loading) return <ReportPageShell templateId={templateId}><div className="report-document"><Spin tip={t("分享报表加载中")} /></div></ReportPageShell>;
  if (error) return <ReportPageShell templateId={templateId}><div className="report-document"><Alert type="error" showIcon message={error} action={<Button icon={<ReloadOutlined />} onClick={() => void loadReport}>{t("重试")}</Button>} /></div></ReportPageShell>;
  if (!report && !locked) return <ReportPageShell templateId={templateId}><div className="report-document"><Alert type="warning" showIcon message={t("分享报表暂时不可用")} /></div></ReportPageShell>;
  // 分享页保留顶栏（公开快照标识、版本与模板切换），并作为外壳槽位进入 TemplateScope。
  const toolbar = report ? <>
    <Tag color="blue">{t("公开快照 · 版本")} {report.version}</Tag>
    <Typography.Text type="secondary">{t("页面内容来自固定脱敏版本")}</Typography.Text>
    <TemplateSwitcher value={templateId} onChange={setTemplateId} />
  </> : null;
  return <>
    <ReportPageShell templateId={templateId} toolbar={toolbar}>
      {locked && <div className="report-document"><Alert type="info" showIcon icon={<LockOutlined />} message={t("该分享需要授权码")} action={<Button type="primary" onClick={() => setUnlockOpen(true)}>{t("输入授权码")}</Button>} /></div>}
      {report && <ReportDocumentRenderer document={report.document} dataManifest={report.data_manifest} templateId={templateId} onDrilldown={(definition, filters) => void loadDrilldown(definition, filters)} />}
    </ReportPageShell>
    {/* 弹窗不进模板作用域：模板不为 Modal 的样式负责 */}
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
  </>;
}
