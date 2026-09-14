// 报表状态与展示文案的共享映射：创建页、报表信息页与工作台共用同一套口径，
// 避免各页面各写一份导致同一状态出现不同说法。
import { t } from '../i18n';

/** 非终态：这些状态还会继续变化，列表与详情需要保持轮询。 */
export const REPORT_ACTIVE_STATUSES = ['planning', 'queued', 'running'];

/** 终态：到达这些状态后不再轮询，避免无意义的定时请求。 */
export const REPORT_TERMINAL_STATUSES = new Set([
  'succeeded', 'partial', 'failed', 'cancelled', 'expired', 'deleted',
]);

// 方法作用：判断任务状态是否还会变化。
// Args: status - 后端任务状态。
// Returns: 需要继续轮询时为 true。
export function isReportActiveStatus(status: string): boolean {
  return REPORT_ACTIVE_STATUSES.includes(status);
}

// 方法作用：把报表任务状态映射为稳定的视觉标签颜色。
// Args: status - 任务或运行状态。
// Returns: Ant Design Tag 颜色。
export function reportStatusColor(status: string): string {
  if (status === 'succeeded') return 'green';
  if (status === 'partial') return 'gold';
  if (status === 'failed' || status === 'plan_failed') return 'error';
  if (status === 'running') return 'blue';
  if (status === 'queued' || status === 'planning') return 'processing';
  return 'default';
}

// 方法作用：把报表任务状态转换为用户可读文本。
// Args: status - 后端任务状态值。
// Returns: 中文状态文本；planning=计划生成中、draft=报表未生成、plan_failed=计划失败。
// 文案要点：planning 与 draft 都牵扯"计划"和"报表"两件事，必须说清是哪一件——
// 「生成中」看不出在生成什么，「未生成」会被读成"报表没生成出来"。
export function reportStatusLabel(status: string): string {
  const labels: Record<string, string> = {
    planning: t("计划生成中"),
    draft: t("报表未生成"),
    plan_failed: t("计划失败"),
    queued: t("排队中"),
    running: t("运行中"),
    succeeded: t("已完成"),
    partial: t("部分完成"),
    failed: t("失败"),
    cancelled: t("已取消"),
    expired: t("已过期"),
    deleted: t("已删除"),
  };
  return labels[status] || status;
}

// 方法作用：把发散档位转换为用户可读文本。
// Args: level - 发散档位 conservative/standard/deep。
// Returns: 保守/标准/深度；未知取值原样返回。
export function reportDivergenceLabel(level: string | undefined): string {
  const labels: Record<string, string> = {
    conservative: t("保守"),
    standard: t("标准"),
    deep: t("深度"),
  };
  return labels[level || ''] || level || '—';
}

// 方法作用：把执行方式转换为用户可读文本。
// Args: mode - immediate 或 scheduled。
// Returns: 立即一次/定时。
export function reportExecutionModeLabel(mode: string | undefined): string {
  return mode === 'scheduled' ? t("定时") : t("立即一次");
}

// 方法作用：格式化时间并为无值状态提供稳定占位。
// Args: value - ISO 时间文本或空值。
// Returns: 本地化时间文本；空值或无效应返回占位符。
export function formatReportTime(value: string | null | undefined): string {
  if (!value) return '—';
  const date = new Date(value);
  return Number.isNaN(date.getTime()) ? '—' : date.toLocaleString();
}
