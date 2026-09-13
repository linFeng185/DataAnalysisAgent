import { t, useLanguage } from '../i18n';
import { useEffect, useMemo, useRef, useState } from 'react';
import { Alert, Button, Card, Checkbox, Drawer, Empty, Input, List, message, Segmented, Select, Space, Statistic, Table, Tag, Typography } from 'antd';
import {
  ClockCircleOutlined,
  BarsOutlined,
  EyeOutlined,
  FileTextOutlined,
  PlayCircleOutlined,
  ReloadOutlined,
  SearchOutlined,
} from '@ant-design/icons';
import { useNavigate } from 'react-router-dom';
import { get } from '../api/client';
import {
  createReportJob,
  fetchReportJob,
  fetchReportRun,
  fetchReportJobs,
  fetchReportQuota,
  fetchReportRuns,
  previewReportPlan,
  setReportJobPaused,
} from '../api/reportWorkbench';
import type { DatasourceConfig } from '../types';
import type {
  ReportDivergenceLevel,
  ReportJob,
  ReportJobDetailResponse,
  ReportPlan,
  ReportPlanPreviewResponse,
  ReportQuotaResponse,
  ReportRun,
  ReportRunStep,
} from '../types/report';

const TERMINAL_STATUSES = new Set(['succeeded', 'partial', 'failed', 'cancelled', 'expired', 'deleted']);

type ModelOption = {
  id: string;
  name: string;
  connection_id?: number;
  connection_name?: string;
  capabilities?: Record<string, unknown>;
};

// 方法作用：生成连接与模型组合的稳定选择值，避免同一模型跨连接重名。
// Args: option - 当前租户模型选项。
// Returns: 可用于 Select value 的组合字符串。
function modelOptionValue(option: ModelOption): string {
  return `${option.connection_id ?? 0}:${option.id}`;
}

// 方法作用：把报表任务状态映射为稳定的视觉标签颜色。
// Args: status - 任务或运行状态。
// Returns: Ant Design Tag 颜色。
function statusColor(status: string): string {
  if (status === 'succeeded') return 'green';
  if (status === 'partial') return 'gold';
  if (status === 'failed') return 'red';
  if (status === 'running') return 'blue';
  if (status === 'queued' || status === 'planning') return 'processing';
  return 'default';
}

// 方法作用：把执行状态转换为用户可读文本。
// Args: status - 后端状态值。
// Returns: 中文状态文本。
function statusLabel(status: string): string {
  const labels: Record<string, string> = {
    draft: t("草稿"), planning: t("规划中"), queued: t("排队中"), running: t("运行中"),
    succeeded: t("已完成"), partial: t("部分完成"), failed: t("失败"), cancelled: t("已取消"),
    expired: t("已过期"), deleted: t("已删除"),
  };
  return labels[status] || status;
}

// 方法作用：格式化时间并为无值状态提供稳定占位。
// Args: value - ISO 时间文本或空值。
// Returns: 本地化时间文本。
function formatTime(value: string | null | undefined): string {
  if (!value) return '—';
  const date = new Date(value);
  return Number.isNaN(date.getTime()) ? '—' : date.toLocaleString();
}

// 方法作用：把节点步骤状态映射为日志抽屉中的标签颜色。
// Args: status - 节点步骤状态。
// Returns: Ant Design Tag 颜色。
function stepStatusColor(status: string): string {
  if (status === 'succeeded') return 'green';
  if (status === 'failed') return 'red';
  if (status === 'retry') return 'gold';
  if (status === 'running') return 'blue';
  return 'default';
}

// 方法作用：将节点步骤状态转换为用户可读文本。
// Args: status - 节点步骤状态。
// Returns: 中文状态文本。
function stepStatusLabel(status: string): string {
  const labels: Record<string, string> = {
    running: t("执行中"), succeeded: t("完成"), failed: t("失败"), retry: t("重试"), partial: t("部分完成"),
  };
  return labels[status] || status;
}

// 方法作用：展示当前身份的自然语言报表创建和任务运行工作台。
// Args: 无。
// Returns: 报表工作台页面。
export default function ReportWorkbenchPage() {
  const language = useLanguage();
  const navigate = useNavigate();
  const [prompt, setPrompt] = useState('');
  const [divergence, setDivergence] = useState<ReportDivergenceLevel>('standard');
  // 被用户取消勾选的单元。只允许删减，服务端会拒绝任何不属于预览计划的标识。
  const [excludedUnitIds, setExcludedUnitIds] = useState<string[]>([]);
  const [datasources, setDatasources] = useState<DatasourceConfig[]>([]);
  const [selectedDatasources, setSelectedDatasources] = useState<string[]>([]);
  const [models, setModels] = useState<ModelOption[]>([]);
  const [modelId, setModelId] = useState('');
  const [llmConnectionId, setLlmConnectionId] = useState<number | undefined>();
  const [preview, setPreview] = useState<ReportPlanPreviewResponse | null>(null);
  const [jobs, setJobs] = useState<ReportJob[]>([]);
  const [quota, setQuota] = useState<ReportQuotaResponse | null>(null);
  const [selectedJobId, setSelectedJobId] = useState('');
  const [jobDetail, setJobDetail] = useState<ReportJobDetailResponse | null>(null);
  const [runs, setRuns] = useState<ReportRun[]>([]);
  const [runSteps, setRunSteps] = useState<ReportRunStep[]>([]);
  const [logOpen, setLogOpen] = useState(false);
  const [loading, setLoading] = useState(false);
  const [previewing, setPreviewing] = useState(false);
  const [creating, setCreating] = useState(false);
  const [refreshing, setRefreshing] = useState(false);
  const creationKey = useRef('');
  const creationInFlight = useRef(false);

  // 方法作用：加载数据源、任务和周期配额，初始化工作台状态。
  // Args: 无。
  // Returns: 异步加载完成后无返回值。
  const loadWorkspace = async (): Promise<void> => {
    setLoading(true);
    try {
      const [datasourceResult, jobResult, quotaResult] = await Promise.all([
        get<{ datasources: DatasourceConfig[] }>('/datasources'),
        fetchReportJobs(),
        fetchReportQuota(),
      ]);
      setDatasources(datasourceResult.datasources || []);
      setJobs(jobResult.jobs || []);
      setQuota(quotaResult);
      if (selectedDatasources.length === 0 && datasourceResult.datasources?.[0]?.name) {
        setSelectedDatasources([datasourceResult.datasources[0].name]);
      }
      // 模型列表单独加载：失败只提示，不影响工作台其余内容；成功时预选租户默认命名连接。
      get<{ models: ModelOption[]; default: string | { connection_id?: number; model_id?: string } }>('/models')
        .then(data => {
          setModels((data.models || []).filter(item => Number(item.connection_id) > 0));
          const selectedDefault = typeof data.default === 'string' ? undefined : data.default;
          if (selectedDefault?.connection_id && selectedDefault.model_id) {
            setLlmConnectionId(selectedDefault.connection_id);
            setModelId(selectedDefault.model_id);
          }
        })
        .catch(() => message.warning(t("无法加载模型列表")));
    } catch (error) {
      message.error(error instanceof Error ? error.message : t("报表工作台加载失败"));
    } finally {
      setLoading(false);
    }
  };

  useEffect(() => { void loadWorkspace(); }, []);

  // 方法作用：刷新任务列表和周期配额，不清空用户正在编辑的计划。
  // Args: 无。
  // Returns: 异步刷新完成后无返回值。
  const refreshWorkspace = async (): Promise<void> => {
    setRefreshing(true);
    try {
      const [jobResult, quotaResult] = await Promise.all([fetchReportJobs(), fetchReportQuota()]);
      setJobs(jobResult.jobs || []);
      setQuota(quotaResult);
    } catch (error) {
      message.error(error instanceof Error ? error.message : t("刷新报表任务失败"));
    } finally {
      setRefreshing(false);
    }
  };

  // 方法作用：解析自然语言并生成不执行任务的计划预览。
  // Args: 无。
  // Returns: 预览完成后无返回值。
  const previewPlan = async (): Promise<void> => {
    if (!prompt.trim()) {
      message.warning(t("请输入报表描述"));
      return;
    }
    setPreviewing(true);
    try {
      const result = await previewReportPlan(prompt.trim(), selectedDatasources, undefined, modelId, llmConnectionId, divergence);
      setPreview(result);
      // 新预览意味着单元集合可能变化，旧的取消勾选不再适用。
      setExcludedUnitIds([]);
      creationKey.current = crypto.randomUUID();
    } catch (error) {
      message.error(error instanceof Error ? error.message : t("计划解析失败"));
    } finally {
      setPreviewing(false);
    }
  };

  // 方法作用：切换调度暂停状态，错误时保留原任务状态。
  // Args: job - 当前任务。
  // Returns: 异步更新完成后无返回值。
  const toggleSchedule = async (job: ReportJob): Promise<void> => {
    try {
      await setReportJobPaused(job.id, !job.is_paused);
      await selectJob(job.id);
      await refreshWorkspace();
      message.success(job.is_paused ? t("调度已恢复") : t("调度已暂停"));
    } catch (error) {
      message.error(error instanceof Error ? error.message : t("调度状态更新失败"));
    }
  };

  // 方法作用：按预览计划创建报表任务并刷新任务状态。
  // Args: 无。
  // Returns: 创建完成后无返回值。
  const createJob = async (): Promise<void> => {
    if (!preview || creationInFlight.current) return;
    creationInFlight.current = true;
    setCreating(true);
    try {
      // 有取消勾选时只保留被接受的单元；没有取消则传 null 走"全部接受"，与既有行为一致。
      const acceptedUnitIds = excludedUnitIds.length > 0
        ? preview.plan.analysis_units.map(unit => unit.unit_id).filter(id => !excludedUnitIds.includes(id))
        : null;
      const result = await createReportJob(
        prompt.trim(),
        preview.plan_hash,
        null,
        true,
        creationKey.current || (creationKey.current = crypto.randomUUID()),
        acceptedUnitIds,
      );
      const jobId = typeof result.job_id === 'string' ? result.job_id : '';
      setPreview(null);
      creationKey.current = '';
      await refreshWorkspace();
      if (jobId) await selectJob(jobId);
      message.success(t("报表任务已排队"));
    } catch (error) {
      message.error(error instanceof Error ? error.message : t("创建报表任务失败"));
    } finally {
      creationInFlight.current = false;
      setCreating(false);
    }
  };

  // 方法作用：读取指定运行的节点级步骤日志，供日志抽屉展示。
  // Args: runId - 报表运行 ID。
  // Returns: 异步读取完成后无返回值。
  const loadRunDetails = async (runId: string): Promise<void> => {
    try {
      const result = await fetchReportRun(runId);
      setRunSteps(result.steps || []);
    } catch (error) {
      // 运行状态仍可用时不阻断工作台，只在控制台保留排障线索。
      console.debug('报表节点日志读取失败', error instanceof Error ? error.message : error);
    }
  };

  // 方法作用：读取任务详情和运行日志，供状态面板和页面跳转使用。
  // Args: jobId - 报表任务 ID。
  // Returns: 加载完成后无返回值。
  const selectJob = async (jobId: string): Promise<void> => {
    setSelectedJobId(jobId);
    setRunSteps([]);
    try {
      const [detail, runResult] = await Promise.all([fetchReportJob(jobId), fetchReportRuns(jobId)]);
      setJobDetail(detail);
      setRuns(runResult.runs || []);
      const latestRun = runResult.runs?.[0] || detail.latest_run;
      if (latestRun) await loadRunDetails(latestRun.id);
    } catch (error) {
      message.error(error instanceof Error ? error.message : t("报表任务详情加载失败"));
    }
  };

  // 方法作用：在任务运行中定期拉取状态，避免页面切换后丢失结果。
  // Args: 无。
  // Returns: 清理定时器的函数。
  useEffect(() => {
    if (!selectedJobId || !jobDetail || TERMINAL_STATUSES.has(jobDetail.job.status)) return undefined;
    const timer = window.setInterval(() => {
      void (async () => {
        try {
          const [detail, runResult] = await Promise.all([fetchReportJob(selectedJobId), fetchReportRuns(selectedJobId)]);
          setJobDetail(detail);
          setRuns(runResult.runs || []);
          const latestRun = runResult.runs?.[0] || detail.latest_run;
          if (latestRun) await loadRunDetails(latestRun.id);
          if (detail.latest_report) {
            setJobs(current => current.map(item => item.id === selectedJobId ? { ...item, status: detail.job.status } : item));
          }
        } catch (error) {
          console.debug('报表任务轮询失败', error instanceof Error ? error.message : error);
        }
      })();
    }, 2500);
    return () => window.clearInterval(timer);
  }, [selectedJobId, jobDetail?.job.status]);

  const jobColumns = useMemo(() => [
    {
      title: t("报表任务"),
      dataIndex: 'prompt',
      key: 'prompt',
      ellipsis: true,
      render: (value: string, item: ReportJob) => <div>
        <Typography.Text strong>{value}</Typography.Text>
        <Typography.Text type="secondary" style={{ display: 'block', fontSize: 12 }}>{formatTime(item.updated_at)}</Typography.Text>
      </div>,
    },
    {
      title: t("状态"),
      dataIndex: 'status',
      key: 'status',
      width: 110,
      render: (value: string) => <Tag color={statusColor(value)}>{statusLabel(value)}</Tag>,
    },
    { title: t("运行次数"), dataIndex: 'runs_started', key: 'runs_started', width: 90 },
  ], [language]);

  return <div className="report-workbench-page">
    <div className="report-workbench-toolbar">
      <div>
        <Typography.Title level={2} style={{ margin: 0 }}>{t("报表工作台")}</Typography.Title>
        <Typography.Text type="secondary">{t("自然语言报表任务")}</Typography.Text>
      </div>
      <Space wrap>
        {quota && <Tag icon={<ClockCircleOutlined />}>{t("本月任务剩余")} {quota.jobs_remaining} {t("次 · 运行剩余")} {quota.runs_remaining} {t("次")}</Tag>}
        <Button icon={<ReloadOutlined />} loading={refreshing} onClick={() => void refreshWorkspace()}>{t("刷新")}</Button>
      </Space>
    </div>

    <div className="report-workbench-grid">
      <div className="report-workbench-panel">
        <Card title={<Space><FileTextOutlined />{t("创建报表")}</Space>} loading={loading}>
          <Input.TextArea
            value={prompt}
            onChange={event => { setPrompt(event.target.value); setPreview(null); }}
            placeholder={t("输入要分析的业务内容")}
            autoSize={{ minRows: 5, maxRows: 10 }}
            maxLength={20000}
            showCount
          />
          <Select
            mode="multiple"
            allowClear
            value={selectedDatasources}
            onChange={values => { setSelectedDatasources(values); setPreview(null); }}
            placeholder={t("选择数据源")}
            options={datasources.map(item => ({ value: item.name, label: item.name }))}
            style={{ width: '100%', marginTop: 12 }}
          />
          {models.length > 0 && (
            <Select
              value={models.find(m => m.id === modelId && m.connection_id === llmConnectionId)
                ? modelOptionValue(models.find(m => m.id === modelId && m.connection_id === llmConnectionId)!)
                : undefined}
              onChange={value => {
                const selected = models.find(option => modelOptionValue(option) === value);
                setLlmConnectionId(selected?.connection_id);
                setModelId(selected?.id || '');
                setPreview(null);
              }}
              placeholder={t("选择规划模型")}
              options={models.map(m => ({ value: modelOptionValue(m), label: `${m.connection_name || t("连接")} / ${m.name}` }))}
              style={{ width: '100%', marginTop: 8 }}
            />
          )}
          <div style={{ marginTop: 12 }}>
            <Typography.Text type="secondary">{t("发散档位")}</Typography.Text>
            <Segmented
              block
              value={divergence}
              onChange={value => { setDivergence(value as ReportDivergenceLevel); setPreview(null); }}
              options={[
                { value: 'conservative', label: t("保守") },
                { value: 'standard', label: t("标准") },
                { value: 'deep', label: t("深度") },
              ]}
              style={{ marginTop: 6 }}
            />
            <Typography.Paragraph type="secondary" style={{ marginTop: 6, marginBottom: 0, fontSize: 12 }}>
              {t("档位决定系统可自主追加的分析单元总数（计划期与运行期共享）：保守 0 个、标准 3 个、深度 6 个。")}
            </Typography.Paragraph>
          </div>
          <Button type="primary" block icon={<SearchOutlined />} loading={previewing} onClick={() => void previewPlan()} style={{ marginTop: 12 }}>
            {t("生成计划")}
          </Button>
          {preview && <Card size="small" title={t("计划预览")} style={{ marginTop: 16 }}>
            <div className="report-plan-summary">
              <Statistic title={t("执行方式")} value={preview.plan.execution_mode === 'immediate' ? t("立即一次") : t("定时")} />
              <Statistic title={t("分析单元")} value={preview.plan.analysis_units.length} />
              <Statistic title={t("预计查询")} value={preview.estimated.query_count} />
            </div>
            <Typography.Paragraph type="secondary">{t("页面有效期：")}{preview.plan.permanent ? t("永久") : t("{value0} 天", { value0: preview.plan.page_ttl_days })}</Typography.Paragraph>
            {preview.plan.execution_mode === 'scheduled' && <Alert type="info" showIcon message={t("定时频率：{value0}；次数：{value1}；时区：{value2}", { value0: preview.plan.frequency || t("待明确"), value1: preview.plan.max_runs ?? t("持续执行（受配额限制）"), value2: preview.plan.timezone })} description={t("首次执行：{value0}。服务端持久化调度，每次重新校验权限。", { value0: preview.plan.run_at ? new Date(preview.plan.run_at).toLocaleString() : t("确认后执行") })} style={{ marginBottom: 12 }} />}
            {preview.plan.warnings.length > 0 && <Alert type="warning" showIcon message={<List size="small" dataSource={preview.plan.warnings} renderItem={item => <List.Item>{item}</List.Item>} />} />}
            <List
              size="small"
              header={t("你要求的分析")}
              dataSource={preview.plan.analysis_units.filter(unit => (unit.origin ?? 'user') === 'user')}
              renderItem={unit => <List.Item className="report-plan-unit">
                <Typography.Text strong>{unit.unit_id}</Typography.Text>
                <Typography.Text>{unit.question}</Typography.Text>
                <Typography.Text type="secondary">{t("数据源：")}{unit.datasource || preview.plan.requested_datasources[0] || t("待绑定")}</Typography.Text>
              </List.Item>}
            />
            {preview.plan.analysis_units.some(unit => unit.origin === 'diverged') && <List
              size="small"
              header={t("建议补充的分析")}
              dataSource={preview.plan.analysis_units.filter(unit => unit.origin === 'diverged')}
              renderItem={unit => <List.Item className="report-plan-unit">
                <Checkbox
                  checked={!excludedUnitIds.includes(unit.unit_id)}
                  onChange={event => setExcludedUnitIds(current => event.target.checked
                    ? current.filter(id => id !== unit.unit_id)
                    : [...current, unit.unit_id])}
                >
                  {t("纳入本次报表")}
                </Checkbox>
                <Typography.Text strong>{unit.unit_id}</Typography.Text>
                <Typography.Text>{unit.question}</Typography.Text>
                <Typography.Text type="secondary">{t("建议理由：")}{unit.rationale || t("未说明")}</Typography.Text>
                <Typography.Text type="secondary">{t("数据源：")}{unit.datasource || t("待绑定")}</Typography.Text>
              </List.Item>}
            />}
            <Button type="primary" icon={<PlayCircleOutlined />} loading={creating} disabled={preview.plan.execution_mode === 'scheduled' && !preview.plan.frequency} onClick={() => void createJob()} style={{ marginTop: 12 }}>
              {preview.plan.execution_mode === 'scheduled' ? t("确认定时任务") : t("确认并立即生成")}
            </Button>
          </Card>}
        </Card>
      </div>

      <div className="report-workbench-panel">
        <Card title={t("我的报表任务")} extra={<Typography.Text type="secondary">{jobs.length} {t("项")}</Typography.Text>}>
          {jobs.length === 0 ? <Empty description={t("暂无报表任务")} /> : <Table<ReportJob>
            size="small"
            rowKey="id"
            dataSource={jobs}
            columns={jobColumns}
            pagination={{ pageSize: 8, hideOnSinglePage: true }}
            onRow={item => ({ onClick: () => void selectJob(item.id), className: 'report-job-row' })}
          />}
          {jobDetail && <Card size="small" title={t("运行详情")} style={{ marginTop: 16 }} extra={<Space>
            {jobDetail.job.plan.execution_mode === 'scheduled' && <Button onClick={() => void toggleSchedule(jobDetail.job)}>{jobDetail.job.is_paused ? t("恢复调度") : t("暂停调度")}</Button>}
            {jobDetail.latest_report && <Button type="link" icon={<EyeOutlined />} onClick={() => navigate(`/reports/${encodeURIComponent(jobDetail.latest_report?.report_id || '')}`)}>{t("查看报表")}</Button>}
            <Tag color={statusColor(jobDetail.job.status)}>{statusLabel(jobDetail.job.status)}</Tag>
          </Space>}>
            <Typography.Paragraph ellipsis={{ rows: 2 }}>{jobDetail.job.prompt}</Typography.Paragraph>
            {jobDetail.latest_run && <div className="report-status-line">
              <Tag color={statusColor(jobDetail.latest_run.status)}>{statusLabel(jobDetail.latest_run.status)}</Tag>
              <Typography.Text type="secondary">{t("节点：")}{jobDetail.latest_run.last_node || '—'}</Typography.Text>
              <Typography.Text type="secondary">{t("更新时间：")}{formatTime(jobDetail.latest_run.finished_at || jobDetail.latest_run.started_at)}</Typography.Text>
              <Button type="link" size="small" icon={<BarsOutlined />} onClick={() => setLogOpen(true)}>{t("运行日志")}</Button>
            </div>}
            {jobDetail.latest_run?.user_error_message && <Alert type="error" showIcon message={jobDetail.latest_run.user_error_message} style={{ marginTop: 12 }} />}
            {runs.length > 0 && <List size="small" header={t("运行记录")} dataSource={runs.slice(0, 5)} renderItem={run => <List.Item>
              <Space wrap><Tag color={statusColor(run.status)}>{statusLabel(run.status)}</Tag><Typography.Text type="secondary">{formatTime(run.started_at)}</Typography.Text>{run.user_error_message && <Typography.Text type="danger">{run.user_error_message}</Typography.Text>}</Space>
            </List.Item>} />}
          </Card>}
        </Card>
      </div>
    </div>
    <Drawer title={t("节点运行日志")} open={logOpen} onClose={() => setLogOpen(false)} width={440}>
      {runs.length > 0 && <Space direction="vertical" size={2} className="report-run-stats">
        <Typography.Text strong>{t("本次运行计数")}</Typography.Text>
        <Typography.Text type={runs[0].llm_failure_count > 0 ? "danger" : "secondary"}>
          {t("大模型调用 {value0} 次：成功 {value1}，失败 {value2}", {
            value0: runs[0].llm_call_count,
            value1: Math.max(0, runs[0].llm_call_count - runs[0].llm_failure_count),
            value2: runs[0].llm_failure_count,
          })}
        </Typography.Text>
        <Typography.Text type={runs[0].sql_failure_count > 0 ? "danger" : "secondary"}>
          {t("SQL 执行 {value0} 次：成功 {value1}，失败 {value2}", {
            value0: runs[0].sql_query_count,
            value1: runs[0].sql_success_count,
            value2: runs[0].sql_failure_count,
          })}
        </Typography.Text>
        <Typography.Text type="secondary">
          {t("预估查询 {value0} 次 · token 用量 {value1}", { value0: runs[0].query_count, value1: runs[0].token_usage })}
        </Typography.Text>
      </Space>}
      {runSteps.length === 0 ? <Empty description={t("暂无节点日志")} /> : <List
        size="small"
        dataSource={runSteps}
        renderItem={(step) => <List.Item>
          <List.Item.Meta
            title={<Space wrap><Typography.Text strong>{step.seq}. {step.node}</Typography.Text><Tag color={stepStatusColor(step.status)}>{stepStatusLabel(step.status)}</Tag>{step.attempt > 1 && <Tag>{t("第")} {step.attempt} {t("次")}</Tag>}</Space>}
            description={<Space direction="vertical" size={2}>
              <Typography.Text type="secondary">{t("开始：")}{formatTime(step.started_at)}</Typography.Text>
              {step.duration_ms !== null && step.duration_ms !== undefined && <Typography.Text type="secondary">{t("耗时：")}{step.duration_ms} ms</Typography.Text>}
              {step.user_error_message && <Typography.Text type="danger">{step.user_error_message}</Typography.Text>}
            </Space>}
          />
        </List.Item>}
      />}
    </Drawer>
  </div>;
}
