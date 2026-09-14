import { t, useLanguage } from '../i18n';
import { useCallback, useEffect, useMemo, useState } from 'react';
import type { ReactNode } from 'react';
import {
  Alert, Button, Card, Checkbox, DatePicker, Descriptions, Drawer, Empty, InputNumber,
  List, Popconfirm, Radio, Select, Skeleton, Space, Table, Tag, Tooltip, Typography, message,
} from 'antd';
import type { TableColumnsType } from 'antd';
import { ClockCircleOutlined, EyeOutlined, PlayCircleOutlined, ReloadOutlined } from '@ant-design/icons';
import { useNavigate, useSearchParams } from 'react-router-dom';
import dayjs from 'dayjs';
import type { Dayjs } from 'dayjs';
import { ApiError } from '../api/client';
import {
  deleteReport,
  extendReportPage,
  fetchReportDetail,
  fetchReportList,
  fetchReportPages,
  fetchReportQuota,
  fetchReportUsers,
  makeReportPagePermanent,
  retryReportPlan,
  runReport,
  updateReportPlan,
} from '../api/reportWorkbench';
import type { ReportUserOption } from '../api/reportWorkbench';
import ReportPlanUnitRow from '../components/report/ReportPlanUnitRow';
import { useAuth } from '../hooks/AuthContext';
import { useReportPermissions } from '../hooks/useReportPermissions';
import {
  formatReportTime,
  isReportActiveStatus,
  reportDivergenceLabel,
  reportExecutionModeLabel,
  reportStatusColor,
  reportStatusLabel,
} from '../report/status';
import type {
  ReportDetailResponse,
  ReportDivergenceLevel,
  ReportJobStatus,
  ReportListItem,
  ReportPageSummary,
  ReportPlan,
  ReportQuotaResponse,
} from '../types/report';

/** 轮询间隔：与工作台保持一致，非终态时定时刷新。 */
const POLL_INTERVAL_MS = 2500;

/** 状态筛选可选项；「生成中/未生成/计划失败」由共享映射给出统一说法。 */
const STATUS_OPTIONS: ReportJobStatus[] = [
  'planning', 'draft', 'plan_failed', 'queued', 'running',
  'succeeded', 'partial', 'failed', 'cancelled', 'expired', 'deleted',
];

/** 定时频率选项；取值与 ReportPlan.frequency 联合类型一致，避免提交时类型漂移。 */
const FREQUENCY_OPTIONS: Array<NonNullable<ReportPlan['frequency']>> = ['hourly', 'daily', 'weekly', 'monthly'];

// 方法作用：把定时频率转换为用户可读文本。
// Args: frequency - hourly/daily/weekly/monthly。
// Returns: 每小时/每天/每周/每月；未知取值原样返回。
function frequencyLabel(frequency: string | null | undefined): string {
  const labels: Record<string, string> = {
    hourly: t("每小时"), daily: t("每天"), weekly: t("每周"), monthly: t("每月"),
  };
  return labels[frequency || ''] || frequency || '—';
}

// 方法作用：把页面有效期转换为「永久/有效至某时/已过期」三种可读状态。
// Args: expiresAt - 页面有效期时间戳，null 表示永久。
// Returns: 带状态的标签或文本。
function pageExpiryTag(expiresAt: string | null | undefined): ReactNode {
  if (!expiresAt) return <Tag color="green">{t("永久有效")}</Tag>;
  const expired = new Date(expiresAt).getTime() <= Date.now();
  return expired
    ? <Tag color="red">{t("已过期")} {formatReportTime(expiresAt)}</Tag>
    : <Typography.Text>{t("有效至 {value0}", { value0: formatReportTime(expiresAt) })}</Typography.Text>;
}

// 方法作用：判断页面是否已过期；永久页面（expires_at 为空）永不过期。
// Args: expiresAt - 页面有效期时间戳，null 表示永久。
// Returns: 已过期返回 true。
function isPageExpired(expiresAt: string | null | undefined): boolean {
  if (!expiresAt) return false;
  const timestamp = new Date(expiresAt).getTime();
  return Number.isFinite(timestamp) && timestamp <= Date.now();
}

// 方法作用：把逐页生命周期操作的失败原因原样展示；409 超上限等业务原因不得被泛化成「操作失败」。
// Args: error - 捕获到的异常；fallback - 无可用消息时的回退文案。
// Returns: 展示给用户的中文错误消息。
function lifecycleErrorText(error: unknown, fallback: string): string {
  // ApiError.message 就是服务端 detail（例如「永久报表已达上限 3，请联系租户管理员调整」），必须原样透传。
  if (error instanceof ApiError && error.message) return error.message;
  return error instanceof Error && error.message ? error.message : fallback;
}

// 悬停摘要的最大字符数：需求原文可达数千字，全量塞进 tooltip 根本读不动。
const PROMPT_TOOLTIP_MAX = 100;

// 方法作用：把报表标题回退为提示词摘要，避免标题为空时整列空白。
// Args: item - 报表摘要；maxLength - 摘要最大字符数。
// Returns: 展示用标题文本。
function summaryTitle(item: ReportListItem, maxLength = 40): string {
  if (item.title) return item.title;
  const prompt = (item.prompt || '').trim();
  if (!prompt) return t("未命名报表");
  return prompt.length > maxLength ? `${prompt.slice(0, maxLength)}…` : prompt;
}

// 方法作用：把需求原文截断成悬停摘要。
// Args: prompt - 需求原文；maxLength - 截断长度。
// Returns: 截断后的文本；原文为空时给出占位说明。
function promptSummary(prompt: string, maxLength = PROMPT_TOOLTIP_MAX): string {
  const text = (prompt || '').trim();
  if (!text) return t("未填写报表需求");
  return text.length > maxLength ? `${text.slice(0, maxLength)}…` : text;
}

// 方法作用：把接口错误转换为用户可读文案，403 单独说明权限不足。
// Args: error - 捕获到的异常；fallback - 无可用消息时的回退文案。
// Returns: 展示给用户的中文错误消息。
function errorText(error: unknown, fallback: string): string {
  if (error instanceof ApiError && error.status === 403) return t("当前账号没有查看报表信息的权限");
  return error instanceof Error && error.message ? error.message : fallback;
}

// 方法作用：展示并管理当前身份可见的报表，含计划核对、调度修改、生成与页面版本。
// Args: 无。
// Returns: 报表信息页面。
export default function ReportListPage() {
  useLanguage();
  const navigate = useNavigate();
  const { user } = useAuth();
  const [reports, setReports] = useState<ReportListItem[]>([]);
  const [total, setTotal] = useState(0);
  const [page, setPage] = useState(1);
  const [pageSize, setPageSize] = useState(20);
  const [statusFilter, setStatusFilter] = useState<ReportJobStatus[]>([]);
  const [userFilter, setUserFilter] = useState<number | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState('');
  // 能力来自共享上下文（应用挂载时拉取一次），本页不再自己查 permissions/me。
  const { capabilities } = useReportPermissions();
  const [quota, setQuota] = useState<ReportQuotaResponse | null>(null);
  const [detailOpen, setDetailOpen] = useState(false);
  const [detail, setDetail] = useState<ReportDetailResponse | null>(null);
  const [detailLoading, setDetailLoading] = useState(false);
  const [detailError, setDetailError] = useState('');
  // 页面列表单独读 GET /reports/{id}/pages：最新页面过期时合并入口会 410，但逐页生命周期仍要可管理。
  const [pageList, setPageList] = useState<ReportPageSummary[]>([]);
  // 当前打开的报表记录 id：最新页面过期时详情返回 410，不能依赖 detail.report.id 继续操作页面。
  const [detailReportId, setDetailReportId] = useState('');
  // 正在执行的逐页操作标识，形如「页面id:7」，用于对应按钮的 loading 态。
  const [pageActionId, setPageActionId] = useState('');
  // 被取消勾选的发散单元：只允许删减，服务端会拒绝任何不在计划里的标识。
  const [excludedUnitIds, setExcludedUnitIds] = useState<string[]>([]);
  const [savingPlan, setSavingPlan] = useState(false);
  const [savingSchedule, setSavingSchedule] = useState(false);
  const [running, setRunning] = useState(false);
  const [retrying, setRetrying] = useState(false);
  // 调度表单：打开详情时用计划初始化，用户改动后由「保存调度」提交。
  const [scheduleMode, setScheduleMode] = useState<'immediate' | 'scheduled'>('immediate');
  const [frequency, setFrequency] = useState<NonNullable<ReportPlan['frequency']> | undefined>();
  const [runAt, setRunAt] = useState<Dayjs | null>(null);
  const [maxRuns, setMaxRuns] = useState<number | null>(null);
  const [pageTtlDays, setPageTtlDays] = useState<number>(7);
  const [permanent, setPermanent] = useState(false);

  const [userOptions, setUserOptions] = useState<ReportUserOption[]>([]);

  // 方法作用：按关键字拉取用户下拉选项，供租户管理员按创建者筛选。
  // Args: search - 用户名关键字。
  // Returns: 异步加载完成后无返回值。
  const loadUserOptions = useCallback(async (search = ''): Promise<void> => {
    try {
      setUserOptions(await fetchReportUsers(search));
    } catch (userError) {
      // 下拉不可用不阻塞列表：筛选框按空选项降级，列表仍可正常浏览。
      console.debug('报表用户下拉加载失败', userError instanceof Error ? userError.message : userError);
    }
  }, []);

  // 已选用户必须在选项里，否则搜索换词后选中项会退化成裸数字。
  const userSelectOptions = useMemo(() => {
    if (!userFilter || userOptions.some(item => item.value === userFilter)) return userOptions;
    return [...userOptions, { value: userFilter, label: `#${userFilter}` }];
  }, [userOptions, userFilter]);

  // 用户筛选只对租户管理员开放：其他角色传 user_id 会被服务端拒绝。
  const isTenantAdmin = user?.role === 'tenant_admin';
  // 延期与永久保留都需要独立能力；能力未加载成功时按无能力处理（隐藏并说明原因，避免必然 403 的按钮）。
  const canExtend = capabilities.includes('report.extend');
  const canPermanent = capabilities.includes('report.permanent');

  // 方法作用：按筛选与分页读取报表列表；静默模式用于轮询时不闪加载态。
  // Args: options.silent - 为 true 时不切换 loading 与非终态错误提示。
  // Returns: 异步读取完成后无返回值。
  const loadReports = useCallback(async (options: { silent?: boolean } = {}): Promise<void> => {
    if (!options.silent) setLoading(true);
    try {
      const result = await fetchReportList({
        user_id: isTenantAdmin && userFilter ? userFilter : undefined,
        // 服务端只支持单个 status 参数：单选时下推，多选时改为在本页内过滤。
        status: statusFilter.length === 1 ? statusFilter[0] : undefined,
        page,
        page_size: pageSize,
      });
      setReports(result.reports || []);
      setTotal(result.total || 0);
      setError('');
    } catch (loadError) {
      if (options.silent) {
        // 轮询失败保留上一份数据，只留排障线索，避免列表突然清空。
        console.debug('报表列表轮询失败', loadError instanceof Error ? loadError.message : loadError);
        return;
      }
      setReports([]);
      setTotal(0);
      setError(errorText(loadError, t("加载报表列表失败")));
    } finally {
      if (!options.silent) setLoading(false);
    }
  }, [isTenantAdmin, page, pageSize, statusFilter, userFilter]);

  useEffect(() => { void loadReports(); }, [loadReports]);

  // 配额独立加载：失败不阻断列表本身。能力改从共享上下文读取——此前本页也自己查一次
  // permissions/me，加上应用挂载与路由守卫，一次进入报表页要打三次同一个接口。
  useEffect(() => {
    let active = true;
    fetchReportQuota().then(result => { if (active) setQuota(result); })
      .catch(() => { if (active) setQuota(null); });
    return () => { active = false; };
  }, [user?.role]);

  // 方法作用：用计划初始化调度表单，只在打开详情时调用，避免轮询覆盖用户正在编辑的值。
  // Args: plan - 服务端返回的计划，可能为空。
  // Returns: 无返回值。
  const applyPlanToScheduleForm = (plan: ReportPlan | null): void => {
    setScheduleMode(plan?.execution_mode === 'scheduled' ? 'scheduled' : 'immediate');
    setFrequency(plan?.frequency ?? undefined);
    setRunAt(plan?.run_at ? dayjs(plan.run_at) : null);
    setMaxRuns(plan?.max_runs ?? null);
    setPageTtlDays(plan?.page_ttl_days ?? 7);
    setPermanent(Boolean(plan?.permanent));
  };

  // 方法作用：读取单条报表详情；静默模式用于轮询刷新，不重置勾选与调度表单。
  // Args: reportId - 报表标识；options.silent - 是否静默刷新。
  // Returns: 异步读取完成后无返回值。
  const loadDetail = useCallback(async (reportId: string, options: { silent?: boolean } = {}): Promise<void> => {
    if (!options.silent) {
      setDetailLoading(true);
      setDetailError('');
    }
    try {
      const result = await fetchReportDetail(reportId);
      setDetail(result);
      if (!options.silent) {
        // 勾选状态只在打开详情时重置：轮询重置会丢掉用户刚取消的勾选。
        setExcludedUnitIds([]);
        applyPlanToScheduleForm(result.plan);
      }
      setDetailError('');
    } catch (loadError) {
      if (options.silent) {
        console.debug('报表详情轮询失败', loadError instanceof Error ? loadError.message : loadError);
        return;
      }
      setDetail(null);
      setDetailError(errorText(loadError, t("报表详情加载失败")));
    } finally {
      if (!options.silent) setDetailLoading(false);
    }
  }, []);

  // 方法作用：读取该报表全部页面版本摘要，作为页面列表的唯一数据源。
  // Args: reportId - 报表记录标识。
  // Returns: 异步读取完成后无返回值。
  const loadPages = useCallback(async (reportId: string): Promise<void> => {
    if (!reportId) return;
    try {
      const result = await fetchReportPages(reportId);
      setPageList(result.pages || []);
    } catch (pageError) {
      // 页面列表失败只留排障线索，不把详情整体打回错误态（详情主体仍可用）。
      console.debug('报表页面列表读取失败', pageError instanceof Error ? pageError.message : pageError);
    }
  }, []);

  // 方法作用：页面操作成功后统一刷新，保证详情与页面列表看到同一份最新数据。
  // Args: reportId - 报表记录标识。
  // Returns: 异步刷新完成后无返回值。
  const refreshAfterPageAction = async (reportId: string): Promise<void> => {
    if (reportId) await loadDetail(reportId, { silent: true });
    await loadPages(reportId);
  };

  // 方法作用：按页面 id 延长有效期，每次最多 30 天由服务端校验。
  // Args: page - 目标页面摘要；days - 延期天数（7 或 30）。
  // Returns: 异步操作完成后无返回值。
  const extendPage = async (page: ReportPageSummary, days: number): Promise<void> => {
    const reportId = detailReportId;
    setPageActionId(`${page.id}:${days}`);
    try {
      const result = await extendReportPage(page.id, days);
      message.success(result.expires_at
        ? t("已延长 {value0} 天，新的有效期至 {value1}", { value0: days, value1: formatReportTime(result.expires_at) })
        : t("已延长 {value0} 天", { value0: days }));
      await refreshAfterPageAction(reportId);
    } catch (extendError) {
      message.error(lifecycleErrorText(extendError, t("页面延期失败")));
    } finally {
      setPageActionId('');
    }
  };

  // 方法作用：把单个页面设为永久保留，超出永久报表上限时原样展示服务端原因。
  // Args: page - 目标页面摘要。
  // Returns: 异步操作完成后无返回值。
  const makePermanent = async (page: ReportPageSummary): Promise<void> => {
    const reportId = detailReportId;
    setPageActionId(`${page.id}:permanent`);
    try {
      await makeReportPagePermanent(page.id);
      message.success(t("已设为永久有效"));
      await refreshAfterPageAction(reportId);
    } catch (permanentError) {
      message.error(lifecycleErrorText(permanentError, t("设为永久失败")));
    } finally {
      setPageActionId('');
    }
  };

  // 方法作用：打开某条报表的详情抽屉并加载计划、运行与页面版本。
  // Args: reportId - 报表标识。
  // Returns: 异步读取完成后无返回值。
  const openDetail = async (reportId: string): Promise<void> => {
    setDetailOpen(true);
    setDetail(null);
    setDetailReportId(reportId);
    setPageList([]);
    setExcludedUnitIds([]);
    // 详情与页面列表并行读取：最新页面过期时详情 410，页面列表仍要能支撑延期操作。
    await Promise.all([loadDetail(reportId), loadPages(reportId)]);
  };

  // 带 ?report=<id> 进来时直接打开那条报表的详情，供通知等入口直达；
  // 参数消费后立刻从地址栏移除，避免关闭抽屉或刷新时被反复拉起。
  const [searchParams, setSearchParams] = useSearchParams();
  useEffect(() => {
    const target = searchParams.get('report');
    if (!target) return;
    const next = new URLSearchParams(searchParams);
    next.delete('report');
    setSearchParams(next, { replace: true });
    void openDetail(target);
  }, [searchParams, setSearchParams, openDetail]);

  // 列表或详情仍在变化时按固定间隔刷新；全部落到终态后停表，不再空转请求。
  const needsPolling = useMemo(() => reports.some(item => isReportActiveStatus(item.status)), [reports]);
  const detailActive = Boolean(detail && isReportActiveStatus(detail.report.status));
  useEffect(() => {
    if (!needsPolling && !detailActive) return undefined;
    const timer = window.setInterval(() => {
      if (needsPolling) void loadReports({ silent: true });
      if (detailActive && detail) {
        void loadDetail(detail.report.id, { silent: true });
        // 运行中新生成的页面要能在页面列表里看到，因此与详情同频刷新。
        void loadPages(detail.report.id);
      }
    }, POLL_INTERVAL_MS);
    return () => window.clearInterval(timer);
  }, [needsPolling, detailActive, detail, loadReports, loadDetail, loadPages]);

  // 方法作用：提交生成，把未生成的报表投入执行并预占额度。
  // Args: 无，读取当前详情。
  // Returns: 异步提交完成后无返回值。
  const submitRun = async (): Promise<void> => {
    if (!detail) return;
    setRunning(true);
    try {
      const result = await runReport(detail.report.id);
      message.success(t("已提交生成，执行方式：{value0}", { value0: reportExecutionModeLabel(result.execution_mode) }));
      await loadDetail(detail.report.id, { silent: true });
      await loadReports({ silent: true });
    } catch (runError) {
      message.error(errorText(runError, t("提交生成失败")));
    } finally {
      setRunning(false);
    }
  };

  // 方法作用：对计划失败的报表重试后台规划。
  // Args: 无，读取当前详情。
  // Returns: 异步重试完成后无返回值。
  const retryPlan = async (): Promise<void> => {
    if (!detail) return;
    setRetrying(true);
    try {
      const result = await retryReportPlan(detail.report.id);
      // 重试接口只回传状态：先就地切到「生成中」让轮询接管，再重新拉取详情。
      setDetail(current => (current
        ? { ...current, plan_error: '', report: { ...current.report, status: result.status } }
        : current));
      // 重试后是新计划：旧的勾选状态不再适用，必须清空。
      setExcludedUnitIds([]);
      message.success(t("已重新提交计划生成"));
      await loadDetail(detail.report.id, { silent: true });
      await loadReports({ silent: true });
    } catch (retryError) {
      message.error(errorText(retryError, t("重试计划失败")));
    } finally {
      setRetrying(false);
    }
  };

  // 方法作用：删除未生成或计划失败的报表。
  // Args: 无，读取当前详情。
  // Returns: 异步删除完成后无返回值。
  const removeReport = async (): Promise<void> => {
    if (!detail) return;
    try {
      await deleteReport(detail.report.id);
      message.success(t("报表已删除"));
      setDetailOpen(false);
      setDetail(null);
      await loadReports({ silent: true });
    } catch (deleteError) {
      message.error(errorText(deleteError, t("删除报表失败")));
    }
  };

  // 方法作用：把保留的发散单元提交为新的计划（只允许删减）。
  // Args: 无，读取当前详情与勾选状态。
  // Returns: 异步保存完成后无返回值。
  const savePlan = async (): Promise<void> => {
    if (!detail?.plan) return;
    setSavingPlan(true);
    try {
      // 只提交被接受的单元：accepted_unit_ids 只允许删减，新增会被服务端 422 拒绝。
      const acceptedUnitIds = detail.plan.analysis_units
        .map(unit => unit.unit_id)
        .filter(unitId => !excludedUnitIds.includes(unitId));
      await updateReportPlan(detail.report.id, { accepted_unit_ids: acceptedUnitIds });
      message.success(t("计划已保存"));
      await loadDetail(detail.report.id, { silent: true });
    } catch (saveError) {
      message.error(errorText(saveError, t("保存计划失败")));
    } finally {
      setSavingPlan(false);
    }
  };

  // 方法作用：保存执行方式与调度设置。
  // Args: 无，读取调度表单状态。
  // Returns: 异步保存完成后无返回值。
  const saveSchedule = async (): Promise<void> => {
    if (!detail) return;
    setSavingSchedule(true);
    try {
      const scheduled = scheduleMode === 'scheduled';
      await updateReportPlan(detail.report.id, {
        execution_mode: scheduleMode,
        // 切回立即执行时必须把调度字段清空，否则残留旧频率会被服务端继续使用。
        frequency: scheduled ? frequency ?? null : null,
        run_at: scheduled && runAt ? runAt.toISOString() : null,
        max_runs: scheduled ? maxRuns : null,
        page_ttl_days: pageTtlDays,
        permanent,
        timezone: detail.plan?.timezone || 'Asia/Shanghai',
      });
      message.success(t("调度已保存"));
      await loadDetail(detail.report.id, { silent: true });
    } catch (saveError) {
      message.error(errorText(saveError, t("保存调度失败")));
    } finally {
      setSavingSchedule(false);
    }
  };

  // 多条状态同时勾选时服务端无法表达，改为在当前页内过滤，界面提示里说明该边界。
  const visibleReports = useMemo(
    () => (statusFilter.length > 1 ? reports.filter(item => statusFilter.includes(item.status)) : reports),
    [reports, statusFilter],
  );

  const columns: TableColumnsType<ReportListItem> = [
    {
      title: t("报表摘要标题"),
      dataIndex: 'title',
      key: 'title',
      render: (_, item) => <Tooltip title={promptSummary(item.prompt)}>{summaryTitle(item)}</Tooltip>,
    },
    {
      title: t("选择的数据源"),
      dataIndex: 'datasources',
      key: 'datasources',
      render: (value: string[]) => (value && value.length > 0
        ? <Space wrap size={4}>{value.map(name => <Tag key={name}>{name}</Tag>)}</Space>
        : '—'),
    },
    {
      title: t("发散档位"),
      dataIndex: 'divergence',
      key: 'divergence',
      width: 110,
      render: (value: ReportDivergenceLevel) => reportDivergenceLabel(value),
    },
    {
      title: t("任务状态"),
      dataIndex: 'status',
      key: 'status',
      width: 110,
      render: (value: ReportJobStatus) => <Tag color={reportStatusColor(value)}>{reportStatusLabel(value)}</Tag>,
    },
    { title: t("运行次数"), dataIndex: 'runs_started', key: 'runs_started', width: 90 },
    {
      title: t("下次运行时间"),
      dataIndex: 'next_run_at',
      key: 'next_run_at',
      width: 170,
      render: (value: string | null) => formatReportTime(value),
    },
    {
      title: t("创建时间"),
      dataIndex: 'created_at',
      key: 'created_at',
      width: 170,
      render: (value: string | null) => formatReportTime(value),
    },
  ];

  const pageColumns: TableColumnsType<ReportPageSummary> = [
    { title: t("版本"), dataIndex: 'version', width: 80 },
    {
      title: t("有效期"),
      dataIndex: 'expires_at',
      render: (_, item) => <Space size={4} wrap>
        {pageExpiryTag(item.expires_at)}
        {/* 过期只是正文不可读：后端允许延期，界面必须把「已过期但可延期」说清楚 */}
        {isPageExpired(item.expires_at) && canExtend && <Tag color="orange">{t("已过期，仍可延期")}</Tag>}
      </Space>,
    },
    {
      title: t("最新"),
      dataIndex: 'is_latest',
      width: 80,
      render: (value: boolean) => (value ? <Tag color="green">{t("最新")}</Tag> : null),
    },
    { title: t("生成时间"), dataIndex: 'created_at', width: 170, render: (value: string | null) => formatReportTime(value) },
    {
      title: t("操作"),
      key: 'page_actions',
      width: 300,
      render: (_, item) => {
        const expired = isPageExpired(item.expires_at);
        // 永久页面（expires_at 为空）不再提供「设为永久」。
        const permanent = !item.expires_at;
        return <Space size={4} wrap>
          {/* 过期页面点「查看」必然 410，因此禁用并说明原因（禁用按钮需要 span 包裹才能触发 Tooltip） */}
          <Tooltip title={expired ? t("页面已过期，查看会返回 410；可先延期再查看") : ''}>
            <span>
              <Button
                type="link"
                size="small"
                icon={<EyeOutlined />}
                disabled={expired}
                onClick={() => navigate(`/reports/${encodeURIComponent(item.id)}`)}
              >{t("查看")}</Button>
            </span>
          </Tooltip>
          {canExtend && <Button
            size="small"
            loading={pageActionId === `${item.id}:7`}
            onClick={() => void extendPage(item, 7)}
          >{t("延长 7 天")}</Button>}
          {canExtend && <Button
            size="small"
            loading={pageActionId === `${item.id}:30`}
            onClick={() => void extendPage(item, 30)}
          >{t("延长 30 天")}</Button>}
          {canPermanent && !permanent && <Popconfirm
            title={t("确认把该页面设为永久？")}
            description={t("设为永久后不再过期；受租户永久报表上限约束，超出上限时服务端会拒绝并给出原因。")}
            okText={t("设为永久")}
            cancelText={t("取消")}
            onConfirm={() => void makePermanent(item)}
          ><Button size="small" loading={pageActionId === `${item.id}:permanent`}>{t("设为永久")}</Button></Popconfirm>}
        </Space>;
      },
    },
  ];

  // 方法作用：渲染页面版本列表与逐页生命周期操作，详情 410（最新页面过期）时同样可用。
  // Args: 无，读取 pageList、能力状态与加载态。
  // Returns: 页面列表区块。
  const renderPageList = (): ReactNode => <div>
    <Typography.Title level={5}>{t("页面列表")}</Typography.Title>
    {pageList.length === 0
      ? <Empty description={t("暂无页面版本")} />
      : <Table<ReportPageSummary>
        size="small"
        rowKey="id"
        pagination={false}
        scroll={{ x: 'max-content' }}
        dataSource={pageList}
        columns={pageColumns}
      />}
    <Typography.Text type="secondary" style={{ display: 'block', marginTop: 8 }}>
      {t("每次运行都会保留为独立页面，可按页面分别延期或设为永久；延期每次最多 30 天。")}
    </Typography.Text>
    {/* 能力缺失时按钮已隐藏：这里把「为什么看不到」说清楚 */}
    {!canExtend && <Typography.Text type="secondary" style={{ display: 'block' }}>
      {t("当前账号没有「页面延期」能力，如需延长页面有效期，请联系租户管理员在报表权限里开通。")}
    </Typography.Text>}
    {!canPermanent && <Typography.Text type="secondary" style={{ display: 'block' }}>
      {t("当前账号没有「永久保存」能力，「设为永久」按钮已隐藏；请联系租户管理员开通。")}
    </Typography.Text>}
  </div>;

  const plan = detail?.plan ?? null;
  const status = detail?.report.status;
  // 只有「报表未生成」可以改计划与调度：其他状态服务端会返回 409。
  const isEditable = status === 'draft';
  // 卡死判定（含阈值）由服务端下发，前端不自算，避免两处阈值漂移。
  const planStuck = Boolean(detail?.report.plan_stuck);
  // 后端只允许报表创建者执行：租户管理员看别人的报表时不给出必然 403 的按钮。
  const isOwner = !detail?.report.user_id || detail.report.user_id === user?.user_id;
  const canRun = status === 'draft' && isOwner;
  const scheduledPlan = plan?.execution_mode === 'scheduled';
  const userUnits = (plan?.analysis_units || []).filter(unit => (unit.origin ?? 'user') === 'user');
  const divergedUnits = (plan?.analysis_units || []).filter(unit => unit.origin === 'diverged');
  // 被采纳的单元数：两个分组都能取消勾选后，空选集成为可能，服务端会 422 拒绝空数组。
  const acceptedUnitCount = (plan?.analysis_units || []).length - excludedUnitIds.length;

  // 方法作用：切换单个分析单元的采纳状态，供共享单元行组件回调。
  // Args: unitId - 单元标识；checked - 是否采纳。
  // Returns: 无返回值。
  const toggleUnit = (unitId: string, checked: boolean): void => {
    setExcludedUnitIds(current => checked
      ? current.filter(id => id !== unitId)
      : [...current, unitId]);
  };

  return <Space direction="vertical" size={16} className="app-page" style={{ width: '100%' }}>
    <Card>
      <Space style={{ width: '100%', justifyContent: 'space-between' }} align="start" wrap>
        <Space direction="vertical" size={4}>
          <Typography.Title level={4} style={{ margin: 0 }}>{t("报表信息")}</Typography.Title>
          <Typography.Text type="secondary">{t("查看报表的计划、调度、运行状态与页面版本；点「生成」时才预占额度。")}</Typography.Text>
        </Space>
        <Space wrap>
          {quota && <Tag icon={<ClockCircleOutlined />}>{t("本月任务剩余")} {quota.jobs_remaining} {t("次 · 运行剩余")} {quota.runs_remaining} {t("次")}</Tag>}
          <Button icon={<ReloadOutlined />} loading={loading} onClick={() => void loadReports()}>{t("刷新")}</Button>
        </Space>
      </Space>
    </Card>

    <Card size="small">
      <Space wrap align="center">
        <Select<ReportJobStatus[]>
          mode="multiple"
          allowClear
          maxTagCount="responsive"
          style={{ minWidth: 260 }}
          value={statusFilter}
          placeholder={t("按状态筛选")}
          options={STATUS_OPTIONS.map(value => ({ value, label: reportStatusLabel(value) }))}
          onChange={next => { setStatusFilter(next); setPage(1); }}
        />
        {isTenantAdmin && <Select<number>
          style={{ width: 180 }}
          showSearch
          allowClear
          value={userFilter ?? undefined}
          placeholder={t("按用户名搜索")}
          options={userSelectOptions}
          // 关键字交给服务端模糊匹配，避免只在已加载的前 50 条里筛
          filterOption={false}
          onSearch={value => void loadUserOptions(value)}
          onChange={value => { setUserFilter(typeof value === 'number' ? value : null); setPage(1); }}
        />}
        <Select<number>
          style={{ width: 140 }}
          value={pageSize}
          options={[10, 20, 50].map(size => ({ value: size, label: t("每页 {value0} 条", { value0: size }) }))}
          onChange={next => { setPageSize(next); setPage(1); }}
        />
        <Typography.Text type="secondary">{t("共 {value0} 条", { value0: total })}</Typography.Text>
      </Space>
      <Typography.Text type="secondary" style={{ display: 'block', marginTop: 8 }}>
        {t("默认按创建时间倒序；状态多选时在当前页内过滤，单选时由服务端过滤。")}
      </Typography.Text>
      {isTenantAdmin && <Typography.Text type="secondary" style={{ display: 'block' }}>
        {t("按用户 ID 精确筛选（用户 ID 可在「管理工作区 → 用户」查看）；不填时显示本租户全部用户的报表。")}
      </Typography.Text>}
    </Card>

    {error && <Alert
      type="error"
      showIcon
      message={error}
      action={<Button icon={<ReloadOutlined />} onClick={() => void loadReports()}>{t("重试")}</Button>}
    />}

    <Table<ReportListItem>
      size="small"
      rowKey="id"
      loading={loading}
      dataSource={visibleReports}
      columns={columns}
      locale={{ emptyText: t("暂无报表") }}
      pagination={{
        current: page,
        pageSize,
        total,
        showSizeChanger: false,
        onChange: (nextPage, nextPageSize) => { setPage(nextPage); setPageSize(nextPageSize); },
      }}
      onRow={item => ({ onClick: () => void openDetail(item.id), className: 'report-job-row', style: { cursor: 'pointer' } })}
    />

    <Drawer
      title={<Space wrap>
        <Typography.Text strong>{detail ? summaryTitle(detail.report) : t("报表详情")}</Typography.Text>
        {status && <Tag color={reportStatusColor(status)}>{reportStatusLabel(status)}</Tag>}
      </Space>}
      width={760}
      open={detailOpen}
      destroyOnHidden
      onClose={() => { setDetailOpen(false); setDetail(null); setDetailError(''); setExcludedUnitIds([]); setPageList([]); setDetailReportId(''); }}
    >
      {detailError
        ? <Space direction="vertical" size={16} style={{ width: '100%' }}>
          <Alert
            type="error"
            showIcon
            message={detailError}
            description={pageList.length > 0
              ? t("页面列表仍可管理：最新页面过期后查看会返回 410，但每个页面都可以延期或设为永久。")
              : undefined}
          />
          {pageList.length > 0 && renderPageList()}
        </Space>
        : detailLoading || !detail
          ? <Skeleton active />
          : <Space direction="vertical" size={16} style={{ width: '100%' }}>
            {status === 'plan_failed' && <Alert
              type="error"
              showIcon
              message={t("计划生成失败")}
              description={detail.report.plan_error || detail.plan_error || detail.latest_run?.user_error_message || t("未提供失败原因，可点「重试计划」重新生成")}
            />}

            {planStuck && <Alert
              type="warning"
              showIcon
              message={t("计划生成已中断")}
              description={t("后台规划任务多半已随服务重启丢失，计划不会再自行完成。可中断并重新生成。")}
            />}

            {status === 'draft' && !isOwner && <Alert
              type="info"
              showIcon
              message={t("只有报表创建者可以点「生成」，管理员不能代替业务用户执行。")}
            />}

            <Space wrap>
              {canRun && <Button type="primary" icon={<PlayCircleOutlined />} loading={running} onClick={() => void submitRun()}>{t("生成")}</Button>}
              {(status === 'plan_failed' || planStuck) && <Button icon={<ReloadOutlined />} loading={retrying} onClick={() => void retryPlan()}>
                {status === 'plan_failed' ? t("重试计划") : t("中断并重新生成")}
              </Button>}
              {['draft', 'plan_failed'].includes(status || '') && <Popconfirm
                title={t("确认删除该报表？")}
                description={t("删除后计划与页面列表都无法恢复。")}
                okText={t("删除")}
                cancelText={t("取消")}
                onConfirm={() => void removeReport()}
              ><Button danger>{t("删除")}</Button></Popconfirm>}
              {detail.report.latest_report_id && <Button
                type="link"
                icon={<EyeOutlined />}
                onClick={() => navigate(`/reports/${encodeURIComponent(detail.report.latest_report_id || '')}`)}
              >{t("查看报表")}</Button>}
            </Space>

            <div>
              <Typography.Title level={5} style={{ marginTop: 0 }}>{t("计划详情")}</Typography.Title>
              {plan ? <Descriptions size="small" column={2} bordered items={[
                { key: 'mode', label: t("执行方式"), children: reportExecutionModeLabel(plan.execution_mode) },
                // 立即执行不使用频率与次数：显示占位符，避免误读成还在按周调度。
                { key: 'frequency', label: t("频率"), children: scheduledPlan && plan.frequency ? frequencyLabel(plan.frequency) : '—' },
                { key: 'max_runs', label: t("次数上限"), children: scheduledPlan ? (plan.max_runs ?? t("持续执行（受配额限制）")) : '—' },
                { key: 'timezone', label: t("时区"), children: plan.timezone },
                { key: 'ttl', label: t("页面有效期"), children: plan.permanent ? t("永久") : t("{value0} 天", { value0: plan.page_ttl_days }) },
                { key: 'query_count', label: t("预计查询"), children: plan.estimated?.query_count ?? '—' },
                { key: 'llm_tokens', label: t("预计 token"), children: plan.estimated?.llm_tokens ?? '—' },
              ]} /> : <Empty description={t("计划尚未就绪")} />}
              {/* 需求原文只在这里完整展示：列表列宽装不下，悬停摘要也只截 100 字 */}
              <Typography.Title level={5} style={{ marginTop: 16 }}>{t("计划需求")}</Typography.Title>
              <Typography.Paragraph type="secondary" style={{ whiteSpace: 'pre-wrap', marginBottom: 0 }}>
                {detail.report.prompt || t("未填写报表需求")}
              </Typography.Paragraph>
            </div>

            {plan && <div>
              <Typography.Title level={5}>{t("你要求的分析")}</Typography.Title>
              {userUnits.length === 0
                ? <Typography.Text type="secondary">{t("计划里没有用户显式要求的单元")}</Typography.Text>
                : <List size="small" dataSource={userUnits} renderItem={unit => <ReportPlanUnitRow
                  unit={unit}
                  editable={isEditable}
                  excludedUnitIds={excludedUnitIds}
                  onToggle={toggleUnit}
                />} />}

              <Typography.Title level={5}>{t("建议补充的分析")}</Typography.Title>
              {divergedUnits.length === 0
                ? <Typography.Text type="secondary">{t("本计划没有系统补充的单元")}</Typography.Text>
                : <List size="small" dataSource={divergedUnits} renderItem={unit => <ReportPlanUnitRow
                  unit={unit}
                  editable={isEditable}
                  excludedUnitIds={excludedUnitIds}
                  onToggle={toggleUnit}
                />} />}
              {isEditable && acceptedUnitCount === 0 && <Alert type="warning" showIcon message={t("至少保留一个分析单元才能生成报表")} style={{ marginTop: 12 }} />}
              {isEditable && <Button type="primary" loading={savingPlan} disabled={acceptedUnitCount === 0} onClick={() => void savePlan()} style={{ marginTop: 12 }}>{t("保存计划")}</Button>}
            </div>}

            <div>
              <Typography.Title level={5}>{t("调度编辑")}</Typography.Title>
              {!isEditable && <Typography.Text type="secondary">{t("仅「报表未生成」状态的报表可以修改计划与调度。")}</Typography.Text>}
              <Space direction="vertical" size={10} style={{ width: '100%', marginTop: 8 }}>
                <Radio.Group value={scheduleMode} disabled={!isEditable} onChange={event => setScheduleMode(event.target.value as 'immediate' | 'scheduled')}>
                  <Radio.Button value="immediate">{t("立即一次")}</Radio.Button>
                  <Radio.Button value="scheduled">{t("定时")}</Radio.Button>
                </Radio.Group>
                {scheduleMode === 'scheduled' && <Space wrap>
                  <Select
                    style={{ width: 140 }}
                    value={frequency}
                    disabled={!isEditable}
                    placeholder={t("频率")}
                    options={FREQUENCY_OPTIONS.map(value => ({ value, label: frequencyLabel(value) }))}
                    onChange={setFrequency}
                  />
                  <DatePicker
                    showTime
                    value={runAt}
                    disabled={!isEditable}
                    placeholder={t("首次执行时间")}
                    onChange={value => setRunAt(value)}
                  />
                  <InputNumber
                    min={1}
                    style={{ width: 160 }}
                    value={maxRuns ?? undefined}
                    disabled={!isEditable}
                    placeholder={t("次数上限")}
                    onChange={value => setMaxRuns(typeof value === 'number' ? value : null)}
                  />
                </Space>}
                <Space wrap>
                  <InputNumber
                    min={1}
                    max={365}
                    style={{ width: 160 }}
                    value={pageTtlDays}
                    disabled={!isEditable}
                    addonAfter={t("天")}
                    onChange={value => setPageTtlDays(typeof value === 'number' ? value : 7)}
                  />
                  <Tooltip title={canPermanent ? '' : t("永久保留需要「永久保存」能力，请联系租户管理员开通")}>
                    <Checkbox
                      checked={permanent}
                      disabled={!isEditable || !canPermanent}
                      onChange={event => setPermanent(event.target.checked)}
                    >{t("永久保留")}</Checkbox>
                  </Tooltip>
                </Space>
                <Button type="primary" loading={savingSchedule} disabled={!isEditable} onClick={() => void saveSchedule()}>{t("保存调度")}</Button>
              </Space>
            </div>

            {renderPageList()}
          </Space>}
    </Drawer>
  </Space>;
}
