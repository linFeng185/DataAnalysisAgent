import { t, useLanguage } from '../i18n';
import { useCallback, useEffect, useMemo, useState } from 'react';
import {
  Alert, Button, Card, Descriptions, Drawer, Empty, Segmented, Select, Space, Statistic,
  Table, Tag, Tooltip, Typography,
} from 'antd';
import type { TableColumnsType } from 'antd';
import { ReloadOutlined, SyncOutlined } from '@ant-design/icons';
import { useSearchParams } from 'react-router-dom';
import { ApiError } from '../api/client';
import { fetchReportList, fetchReportRun, fetchRunDetails } from '../api/reportWorkbench';
import { formatReportTime, isReportActiveStatus, reportStatusColor, reportStatusLabel } from '../report/status';
import type {
  ReportRunDetailRow,
  ReportRunStep,
  ReportUnitSnapshot,
} from '../types/report';

/** 轮询间隔：存在非终态运行时定时刷新，全部终态后自动停止。 */
const POLL_INTERVAL_MS = 4000;

/** 状态筛选可选项，与报表信息页共用同一套状态口径。 */
const STATUS_OPTIONS = [
  'queued', 'running', 'succeeded', 'partial', 'failed', 'cancelled',
];

// 方法作用：把毫秒时长格式化为人体可读文本。
// Args: value - 毫秒数；空值返回占位符。
// Returns: 如「1.2s」「3m20s」的文本。
function formatDuration(value: number | null | undefined): string {
  if (value === null || value === undefined || Number.isNaN(Number(value))) return '—';
  const ms = Number(value);
  if (ms < 1000) return `${ms}ms`;
  const seconds = ms / 1000;
  if (seconds < 60) return `${seconds.toFixed(1)}s`;
  const minutes = Math.floor(seconds / 60);
  return `${minutes}m${Math.round(seconds - minutes * 60)}s`;
}

// 方法作用：由开始/结束时间推算运行耗时；未结束时按占位符展示。
// Args: startedAt - 开始时间；finishedAt - 结束时间。
// Returns: 格式化耗时文本。
function runDuration(startedAt: string | null, finishedAt: string | null): string {
  if (!startedAt || !finishedAt) return '—';
  const start = new Date(startedAt).getTime();
  const end = new Date(finishedAt).getTime();
  if (Number.isNaN(start) || Number.isNaN(end)) return '—';
  return formatDuration(Math.max(0, end - start));
}

// 方法作用：把成功/失败计数渲染为带颜色的「成功 x 次 / 失败 y 次」。
// Args: success - 成功次数；failure - 失败次数。
// Returns: 成功失败计数节点。
function successFailure(success: number, failure: number) {
  return (
    <Space size={4}>
      <span>{t("成功")} {success}{t("次")}</span>
      {failure > 0
        ? <Typography.Text type="danger">/ {t("失败")} {failure}{t("次")}</Typography.Text>
        : <Typography.Text type="secondary">/ {t("失败")} 0{t("次")}</Typography.Text>}
    </Space>
  );
}

// 方法作用：报表执行明细页，按运行维度展示 LLM/SQL 调用次数与输入输出 token 用量。
// Args: 无（读取 URL 上的 job_id 作为初始筛选）。
// Returns: 执行明细页面。
export default function ReportRunDetailPage() {
  useLanguage();
  const [searchParams] = useSearchParams();
  const initialJobId = searchParams.get('job_id') || '';
  const [jobId, setJobId] = useState<string>(initialJobId);
  const [status, setStatus] = useState<string>('');
  const [page, setPage] = useState(1);
  const [pageSize, setPageSize] = useState(20);
  const [rows, setRows] = useState<ReportRunDetailRow[]>([]);
  const [total, setTotal] = useState(0);
  const [loading, setLoading] = useState(false);
  const [loadError, setLoadError] = useState('');
  const [jobOptions, setJobOptions] = useState<Array<{ value: string; label: string }>>([]);
  // 详情抽屉：按需拉取单次运行的步骤与单元快照，列表本身不带这些重字段。
  const [detailRunId, setDetailRunId] = useState<string>('');
  const [detailLoading, setDetailLoading] = useState(false);
  const [detailError, setDetailError] = useState('');
  const [detailRun, setDetailRun] = useState<ReportRunDetailRow | null>(null);
  const [detailSteps, setDetailSteps] = useState<ReportRunStep[]>([]);
  const [detailUnits, setDetailUnits] = useState<ReportUnitSnapshot[]>([]);
  const [detailTab, setDetailTab] = useState<string>('units');

  // 方法作用：拉取当前筛选条件下的执行明细页。
  // Args: 无（读取组件内的筛选与分页状态）。
  // Returns: 无返回值。
  const loadRuns = useCallback(async () => {
    setLoading(true);
    setLoadError('');
    try {
      const result = await fetchRunDetails({ job_id: jobId || undefined, status: status || undefined, page, page_size: pageSize });
      setRows(result.runs);
      setTotal(result.total);
    } catch (error) {
      setRows([]);
      setTotal(0);
      setLoadError(error instanceof ApiError ? error.message : t("执行明细加载失败"));
    } finally {
      setLoading(false);
    }
  }, [jobId, status, page, pageSize]);

  // 方法作用：加载报表任务下拉项，供按报表过滤执行记录。
  // Args: 无。
  // Returns: 无返回值。
  const loadJobOptions = useCallback(async () => {
    try {
      const result = await fetchReportList({ page: 1, page_size: 100 });
      setJobOptions(result.reports.map((item) => ({
        value: item.id,
        label: item.title || item.prompt.slice(0, 30),
      })));
    } catch {
      // 下拉项拉取失败不阻塞主表：用户仍可按状态浏览全部执行记录。
      setJobOptions([]);
    }
  }, []);

  useEffect(() => {
    void loadJobOptions();
  }, [loadJobOptions]);

  useEffect(() => {
    void loadRuns();
  }, [loadRuns]);

  // 列表里还有排队/运行中的记录时保持轮询，全部进入终态后自动停止。
  const hasActiveRun = useMemo(
    () => rows.some((row) => isReportActiveStatus(row.status)),
    [rows],
  );
  useEffect(() => {
    if (!hasActiveRun) return undefined;
    const timer = window.setInterval(() => { void loadRuns(); }, POLL_INTERVAL_MS);
    return () => window.clearInterval(timer);
  }, [hasActiveRun, loadRuns]);

  // 方法作用：打开单次运行的执行详情并拉取步骤与单元快照。
  // Args: runId - 报表运行 ID。
  // Returns: 无返回值。
  const openDetail = useCallback(async (runId: string) => {
    setDetailRunId(runId);
    setDetailLoading(true);
    setDetailError('');
    setDetailRun(null);
    setDetailSteps([]);
    setDetailUnits([]);
    try {
      const result = await fetchReportRun(runId);
      setDetailRun(result.run as ReportRunDetailRow);
      setDetailSteps(result.steps || []);
      setDetailUnits(result.units || []);
    } catch (error) {
      setDetailError(error instanceof ApiError ? error.message : t("运行详情加载失败"));
    } finally {
      setDetailLoading(false);
    }
  }, []);

  const columns: TableColumnsType<ReportRunDetailRow> = [
    {
      title: t("报表"),
      dataIndex: 'job_title',
      key: 'job_title',
      ellipsis: true,
      render: (_value, row) => (
        <Tooltip title={row.job_prompt}>
          <Typography.Text strong>{row.job_title || row.job_prompt.slice(0, 24) || row.job_id}</Typography.Text>
        </Tooltip>
      ),
    },
    {
      title: t("状态"),
      dataIndex: 'status',
      key: 'status',
      width: 100,
      render: (value: string) => <Tag color={reportStatusColor(value)}>{reportStatusLabel(value)}</Tag>,
    },
    {
      title: t("开始时间"),
      dataIndex: 'started_at',
      key: 'started_at',
      width: 180,
      render: (value: string | null) => formatReportTime(value),
    },
    {
      title: t("耗时"),
      key: 'duration',
      width: 90,
      render: (_value, row) => runDuration(row.started_at, row.finished_at),
    },
    {
      title: t("LLM 调用"),
      key: 'llm',
      width: 180,
      render: (_value, row) => successFailure(row.stats.llm_success_count, row.stats.llm_failure_count),
    },
    {
      title: t("SQL 执行"),
      key: 'sql',
      width: 180,
      render: (_value, row) => successFailure(row.stats.sql_success_count, row.stats.sql_failure_count),
    },
    {
      title: t("输入 token"),
      key: 'prompt_tokens',
      width: 110,
      align: 'right',
      render: (_value, row) => row.stats.llm_prompt_tokens.toLocaleString(),
    },
    {
      title: t("输出 token"),
      key: 'completion_tokens',
      width: 110,
      align: 'right',
      render: (_value, row) => row.stats.llm_completion_tokens.toLocaleString(),
    },
    {
      title: t("合计 token"),
      key: 'total_tokens',
      width: 110,
      align: 'right',
      render: (_value, row) => row.stats.llm_token_usage.toLocaleString(),
    },
    {
      title: t("操作"),
      key: 'action',
      width: 100,
      render: (_value, row) => (
        <Button type="link" size="small" onClick={() => void openDetail(row.id)}>{t("执行详情")}</Button>
      ),
    },
  ];

  const unitColumns: TableColumnsType<ReportUnitSnapshot> = [
    { title: t("单元"), dataIndex: 'unit_id', key: 'unit_id', width: 120, render: (value: string) => <Typography.Text code>{value}</Typography.Text> },
    {
      title: t("状态"),
      dataIndex: 'status',
      key: 'status',
      width: 90,
      render: (value: string) => <Tag color={reportStatusColor(value)}>{reportStatusLabel(value)}</Tag>,
    },
    { title: t("数据源"), dataIndex: 'datasource', key: 'datasource', width: 140, render: (value: string) => value || '—' },
    { title: t("模型"), dataIndex: 'model_id', key: 'model_id', width: 160, ellipsis: true, render: (value: string) => value || '—' },
    { title: t("返回行数"), dataIndex: 'row_count', key: 'row_count', width: 100, align: 'right' },
    {
      title: t("截断"),
      dataIndex: 'truncated',
      key: 'truncated',
      width: 110,
      render: (value: boolean, row) => (value
        ? <Tooltip title={row.truncated_reason}><Tag color="gold">{t("已截断")}</Tag></Tooltip>
        : <Tag color="green">{t("完整")}</Tag>),
    },
    { title: t("耗时"), dataIndex: 'elapsed_ms', key: 'elapsed_ms', width: 90, render: (value: number) => formatDuration(value) },
    {
      title: t("错误"),
      dataIndex: 'error_message',
      key: 'error_message',
      ellipsis: true,
      render: (value: string) => (value ? <Typography.Text type="danger">{value}</Typography.Text> : '—'),
    },
  ];

  const stepColumns: TableColumnsType<ReportRunStep> = [
    { title: '#', dataIndex: 'seq', key: 'seq', width: 60 },
    { title: t("节点"), dataIndex: 'node', key: 'node', width: 200, render: (value: string) => <Typography.Text code>{value}</Typography.Text> },
    {
      title: t("状态"),
      dataIndex: 'status',
      key: 'status',
      width: 100,
      render: (value: string) => <Tag color={value === 'succeeded' || value === 'success' ? 'green' : value === 'failed' ? 'error' : 'default'}>{value}</Tag>,
    },
    { title: t("开始时间"), dataIndex: 'started_at', key: 'started_at', width: 180, render: (value: string | null) => formatReportTime(value) },
    { title: t("耗时"), key: 'duration', width: 90, render: (_value, row) => formatDuration(row.duration_ms) },
    { title: t("错误"), dataIndex: 'user_error_message', key: 'user_error_message', ellipsis: true, render: (value: string) => value || '—' },
  ];

  const detailStats = detailRun?.stats;

  return (
    <Space direction="vertical" size="middle" style={{ width: '100%' }}>
      <Card
        title={t("报表执行明细")}
        extra={(
          <Button icon={<ReloadOutlined />} onClick={() => void loadRuns()} loading={loading}>{t("刷新")}</Button>
        )}
      >
        <Space wrap size="middle" style={{ marginBottom: 16 }}>
          <Select
            allowClear
            showSearch
            optionFilterProp="label"
            style={{ minWidth: 260 }}
            placeholder={t("按报表筛选")}
            value={jobId || undefined}
            options={jobOptions}
            onChange={(value) => { setJobId(value || ''); setPage(1); }}
          />
          <Select
            allowClear
            style={{ minWidth: 160 }}
            placeholder={t("按状态筛选")}
            value={status || undefined}
            options={STATUS_OPTIONS.map((item) => ({ value: item, label: reportStatusLabel(item) }))}
            onChange={(value) => { setStatus(value || ''); setPage(1); }}
          />
          {hasActiveRun ? (
            <Typography.Text type="secondary"><SyncOutlined spin /> {t("有运行中的任务，正在自动刷新")}</Typography.Text>
          ) : null}
        </Space>
        {loadError ? <Alert type="error" showIcon message={loadError} style={{ marginBottom: 12 }} /> : null}
        <Table
          rowKey="id"
          size="small"
          loading={loading}
          columns={columns}
          dataSource={rows}
          scroll={{ x: 'max-content' }}
          locale={{ emptyText: <Empty description={t("暂无执行记录")} /> }}
          pagination={{
            current: page,
            pageSize,
            total,
            showSizeChanger: true,
            showTotal: (value) => t("共 {total} 条", { total: value }),
            onChange: (nextPage, nextSize) => { setPage(nextPage); setPageSize(nextSize); },
          }}
        />
      </Card>

      <Drawer
        width={960}
        open={Boolean(detailRunId)}
        onClose={() => setDetailRunId('')}
        title={t("执行详情")}
        destroyOnClose
      >
        {detailError ? <Alert type="error" showIcon message={detailError} style={{ marginBottom: 12 }} /> : null}
        <Descriptions size="small" column={2} bordered style={{ marginBottom: 16 }}>
          <Descriptions.Item label={t("运行 ID")} span={2}>
            <Typography.Text copyable code>{detailRunId}</Typography.Text>
          </Descriptions.Item>
          <Descriptions.Item label={t("状态")}>
            {detailRun ? <Tag color={reportStatusColor(detailRun.status)}>{reportStatusLabel(detailRun.status)}</Tag> : '—'}
          </Descriptions.Item>
          <Descriptions.Item label={t("尝试次数")}>{detailRun?.attempt ?? '—'}</Descriptions.Item>
          <Descriptions.Item label={t("开始时间")}>{formatReportTime(detailRun?.started_at)}</Descriptions.Item>
          <Descriptions.Item label={t("结束时间")}>{formatReportTime(detailRun?.finished_at)}</Descriptions.Item>
          <Descriptions.Item label={t("最后节点")}>{detailRun?.last_node || '—'}</Descriptions.Item>
          <Descriptions.Item label={t("耗时")}>{runDuration(detailRun?.started_at ?? null, detailRun?.finished_at ?? null)}</Descriptions.Item>
        </Descriptions>

        <Space size="large" wrap style={{ marginBottom: 16 }}>
          <Statistic title={t("LLM 调用次数")} value={detailStats?.llm_call_count ?? 0} suffix={t("次")} loading={detailLoading} />
          <Statistic title={t("LLM 失败次数")} value={detailStats?.llm_failure_count ?? 0} suffix={t("次")} loading={detailLoading} />
          <Statistic title={t("SQL 执行次数")} value={detailStats?.sql_query_count ?? 0} suffix={t("次")} loading={detailLoading} />
          <Statistic title={t("SQL 失败次数")} value={detailStats?.sql_failure_count ?? 0} suffix={t("次")} loading={detailLoading} />
          <Statistic title={t("输入 token")} value={detailStats?.llm_prompt_tokens ?? 0} loading={detailLoading} />
          <Statistic title={t("输出 token")} value={detailStats?.llm_completion_tokens ?? 0} loading={detailLoading} />
          <Statistic title={t("合计 token")} value={detailStats?.llm_token_usage ?? 0} loading={detailLoading} />
        </Space>

        <Segmented
          value={detailTab}
          onChange={(value) => setDetailTab(String(value))}
          options={[{ value: 'units', label: t("单元执行") }, { value: 'steps', label: t("节点步骤") }]}
          style={{ marginBottom: 12 }}
        />
        {detailTab === 'units' ? (
          <Table
            rowKey="id"
            size="small"
            loading={detailLoading}
            columns={unitColumns}
            dataSource={detailUnits}
            scroll={{ x: 'max-content' }}
            pagination={false}
            locale={{ emptyText: <Empty description={t("暂无单元快照")} /> }}
            expandable={{
              expandedRowRender: (row) => (
                <pre style={{ whiteSpace: 'pre-wrap', wordBreak: 'break-all', margin: 0 }}>{row.sql_text || t("无 SQL")}</pre>
              ),
            }}
          />
        ) : (
          <Table
            rowKey="id"
            size="small"
            loading={detailLoading}
            columns={stepColumns}
            dataSource={detailSteps}
            scroll={{ x: 'max-content' }}
            pagination={false}
            locale={{ emptyText: <Empty description={t("暂无节点日志")} /> }}
          />
        )}
      </Drawer>
    </Space>
  );
}
