import { useEffect, useMemo, useState } from 'react';
import { Alert, Button, Card, Drawer, Empty, Input, List, message, Select, Space, Statistic, Table, Tag, Typography } from 'antd';
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
} from '../api/reportWorkbench';
import type { DatasourceConfig } from '../types';
import type {
  ReportJob,
  ReportJobDetailResponse,
  ReportPlan,
  ReportPlanPreviewResponse,
  ReportQuotaResponse,
  ReportRun,
  ReportRunStep,
} from '../types/report';

const TERMINAL_STATUSES = new Set(['succeeded', 'partial', 'failed', 'cancelled', 'expired', 'deleted']);

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
    draft: '草稿', planning: '规划中', queued: '排队中', running: '运行中',
    succeeded: '已完成', partial: '部分完成', failed: '失败', cancelled: '已取消',
    expired: '已过期', deleted: '已删除',
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
    running: '执行中', succeeded: '完成', failed: '失败', retry: '重试', partial: '部分完成',
  };
  return labels[status] || status;
}

// 方法作用：展示当前身份的自然语言报表创建和任务运行工作台。
// Args: 无。
// Returns: 报表工作台页面。
export default function ReportWorkbenchPage() {
  const navigate = useNavigate();
  const [prompt, setPrompt] = useState('');
  const [datasources, setDatasources] = useState<DatasourceConfig[]>([]);
  const [selectedDatasources, setSelectedDatasources] = useState<string[]>([]);
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
    } catch (error) {
      message.error(error instanceof Error ? error.message : '报表工作台加载失败');
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
      message.error(error instanceof Error ? error.message : '刷新报表任务失败');
    } finally {
      setRefreshing(false);
    }
  };

  // 方法作用：解析自然语言并生成不执行任务的计划预览。
  // Args: 无。
  // Returns: 预览完成后无返回值。
  const previewPlan = async (): Promise<void> => {
    if (!prompt.trim()) {
      message.warning('请输入报表描述');
      return;
    }
    setPreviewing(true);
    try {
      const result = await previewReportPlan(prompt.trim(), selectedDatasources);
      setPreview(result);
    } catch (error) {
      message.error(error instanceof Error ? error.message : '计划解析失败');
    } finally {
      setPreviewing(false);
    }
  };

  // 方法作用：按预览计划创建立即报表任务并刷新任务状态。
  // Args: 无。
  // Returns: 创建完成后无返回值。
  const createJob = async (): Promise<void> => {
    if (!preview) return;
    setCreating(true);
    try {
      const result = await createReportJob(
        prompt.trim(),
        preview.plan_hash,
        null,
        true,
        `report-${Date.now()}-${Math.random().toString(36).slice(2, 8)}`,
      );
      const jobId = typeof result.job_id === 'string' ? result.job_id : '';
      setPreview(null);
      await refreshWorkspace();
      if (jobId) await selectJob(jobId);
      message.success('报表任务已排队');
    } catch (error) {
      message.error(error instanceof Error ? error.message : '创建报表任务失败');
    } finally {
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
      message.error(error instanceof Error ? error.message : '报表任务详情加载失败');
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
      title: '报表任务',
      dataIndex: 'prompt',
      key: 'prompt',
      ellipsis: true,
      render: (value: string, item: ReportJob) => <div>
        <Typography.Text strong>{value}</Typography.Text>
        <Typography.Text type="secondary" style={{ display: 'block', fontSize: 12 }}>{formatTime(item.updated_at)}</Typography.Text>
      </div>,
    },
    {
      title: '状态',
      dataIndex: 'status',
      key: 'status',
      width: 110,
      render: (value: string) => <Tag color={statusColor(value)}>{statusLabel(value)}</Tag>,
    },
    { title: '运行次数', dataIndex: 'runs_started', key: 'runs_started', width: 90 },
  ], []);

  return <div className="report-workbench-page">
    <div className="report-workbench-toolbar">
      <div>
        <Typography.Title level={2} style={{ margin: 0 }}>报表工作台</Typography.Title>
        <Typography.Text type="secondary">自然语言报表任务</Typography.Text>
      </div>
      <Space wrap>
        {quota && <Tag icon={<ClockCircleOutlined />}>本周期剩余 {quota.jobs_remaining} 次</Tag>}
        <Button icon={<ReloadOutlined />} loading={refreshing} onClick={() => void refreshWorkspace}>刷新</Button>
      </Space>
    </div>

    <div className="report-workbench-grid">
      <div className="report-workbench-panel">
        <Card title={<Space><FileTextOutlined />创建报表</Space>} loading={loading}>
          <Input.TextArea
            value={prompt}
            onChange={event => setPrompt(event.target.value)}
            placeholder="输入要分析的业务内容"
            autoSize={{ minRows: 5, maxRows: 10 }}
            maxLength={20000}
            showCount
          />
          <Select
            mode="multiple"
            allowClear
            value={selectedDatasources}
            onChange={setSelectedDatasources}
            placeholder="选择数据源"
            options={datasources.map(item => ({ value: item.name, label: item.name }))}
            style={{ width: '100%', marginTop: 12 }}
          />
          <Button type="primary" block icon={<SearchOutlined />} loading={previewing} onClick={() => void previewPlan} style={{ marginTop: 12 }}>
            生成计划
          </Button>
          {preview && <Card size="small" title="计划预览" style={{ marginTop: 16 }}>
            <div className="report-plan-summary">
              <Statistic title="执行方式" value={preview.plan.execution_mode === 'immediate' ? '立即一次' : '定时'} />
              <Statistic title="分析单元" value={preview.plan.analysis_units.length} />
              <Statistic title="预计查询" value={preview.estimated.query_count} />
            </div>
            <Typography.Paragraph type="secondary">页面有效期：{preview.plan.permanent ? '永久' : `${preview.plan.page_ttl_days} 天`}</Typography.Paragraph>
            {preview.plan.execution_mode === 'scheduled' && <Alert type="info" showIcon message="定时任务将在 Phase 2 开放，当前可预览计划但不能立即创建" style={{ marginBottom: 12 }} />}
            {preview.plan.warnings.length > 0 && <Alert type="warning" showIcon message={<List size="small" dataSource={preview.plan.warnings} renderItem={item => <List.Item>{item}</List.Item>} />} />}
            <List
              size="small"
              header="分析单元"
              dataSource={preview.plan.analysis_units}
              renderItem={unit => <List.Item className="report-plan-unit">
                <Typography.Text strong>{unit.unit_id}</Typography.Text>
                <Typography.Text>{unit.question}</Typography.Text>
                <Typography.Text type="secondary">数据源：{unit.datasource || preview.plan.requested_datasources[0] || '待绑定'}</Typography.Text>
              </List.Item>}
            />
            <Button type="primary" icon={<PlayCircleOutlined />} loading={creating} disabled={preview.plan.execution_mode !== 'immediate'} onClick={() => void createJob} style={{ marginTop: 12 }}>
              确认并立即生成
            </Button>
          </Card>}
        </Card>
      </div>

      <div className="report-workbench-panel">
        <Card title="我的报表任务" extra={<Typography.Text type="secondary">{jobs.length} 项</Typography.Text>}>
          {jobs.length === 0 ? <Empty description="暂无报表任务" /> : <Table<ReportJob>
            size="small"
            rowKey="id"
            dataSource={jobs}
            columns={jobColumns}
            pagination={{ pageSize: 8, hideOnSinglePage: true }}
            onRow={item => ({ onClick: () => void selectJob(item.id), className: 'report-job-row' })}
          />}
          {jobDetail && <Card size="small" title="运行详情" style={{ marginTop: 16 }} extra={<Space>
            {jobDetail.latest_report && <Button type="link" icon={<EyeOutlined />} onClick={() => navigate(`/reports/${encodeURIComponent(jobDetail.latest_report?.report_id || '')}`)}>查看报表</Button>}
            <Tag color={statusColor(jobDetail.job.status)}>{statusLabel(jobDetail.job.status)}</Tag>
          </Space>}>
            <Typography.Paragraph ellipsis={{ rows: 2 }}>{jobDetail.job.prompt}</Typography.Paragraph>
            {jobDetail.latest_run && <div className="report-status-line">
              <Tag color={statusColor(jobDetail.latest_run.status)}>{statusLabel(jobDetail.latest_run.status)}</Tag>
              <Typography.Text type="secondary">节点：{jobDetail.latest_run.last_node || '—'}</Typography.Text>
              <Typography.Text type="secondary">更新时间：{formatTime(jobDetail.latest_run.finished_at || jobDetail.latest_run.started_at)}</Typography.Text>
              <Button type="link" size="small" icon={<BarsOutlined />} onClick={() => setLogOpen(true)}>运行日志</Button>
            </div>}
            {jobDetail.latest_run?.user_error_message && <Alert type="error" showIcon message={jobDetail.latest_run.user_error_message} style={{ marginTop: 12 }} />}
            {runs.length > 0 && <List size="small" header="运行记录" dataSource={runs.slice(0, 5)} renderItem={run => <List.Item>
              <Space wrap><Tag color={statusColor(run.status)}>{statusLabel(run.status)}</Tag><Typography.Text type="secondary">{formatTime(run.started_at)}</Typography.Text>{run.user_error_message && <Typography.Text type="danger">{run.user_error_message}</Typography.Text>}</Space>
            </List.Item>} />}
          </Card>}
        </Card>
      </div>
    </div>
    <Drawer title="节点运行日志" open={logOpen} onClose={() => setLogOpen(false)} width={440}>
      {runSteps.length === 0 ? <Empty description="暂无节点日志" /> : <List
        size="small"
        dataSource={runSteps}
        renderItem={(step) => <List.Item>
          <List.Item.Meta
            title={<Space wrap><Typography.Text strong>{step.seq}. {step.node}</Typography.Text><Tag color={stepStatusColor(step.status)}>{stepStatusLabel(step.status)}</Tag>{step.attempt > 1 && <Tag>第 {step.attempt} 次</Tag>}</Space>}
            description={<Space direction="vertical" size={2}>
              <Typography.Text type="secondary">开始：{formatTime(step.started_at)}</Typography.Text>
              {step.duration_ms !== null && step.duration_ms !== undefined && <Typography.Text type="secondary">耗时：{step.duration_ms} ms</Typography.Text>}
              {step.user_error_message && <Typography.Text type="danger">{step.user_error_message}</Typography.Text>}
            </Space>}
          />
        </List.Item>}
      />}
    </Drawer>
  </div>;
}
