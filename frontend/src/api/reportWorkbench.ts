import { del, get, post } from './client';
import type {
  PrivateReportResponse,
  ReportDrilldownResponse,
  ReportJob,
  ReportJobDetailResponse,
  ReportPermissionResponse,
  ReportPlan,
  ReportPlanPreviewResponse,
  ReportQuotaResponse,
  ReportRun,
  ReportRunStep,
  ReportShareRequest,
  ReportShareResponse,
  ReportShareSummary,
  ReportSharedResponse,
} from '../types/report';

const REPORT_BASE = '/report-workbench';

// 方法作用：暂停或恢复已有报表调度，不改变运行次数上限。
// Args: jobId - 任务标识；paused - 目标暂停状态。
// Returns: 调度状态。
export function setReportJobPaused(jobId: string, paused: boolean): Promise<Record<string, unknown>> {
  return post<Record<string, unknown>>(`${REPORT_BASE}/jobs/${encodeURIComponent(jobId)}/${paused ? 'pause' : 'resume'}`, {});
}

export interface PageLifecycle {
  report_id: string;
  expires_at: string | null;
  expired: boolean;
  can_extend: boolean;
  can_permanent: boolean;
}

// 方法作用：读取含过期页面在内的生命周期管理权限，不读取页面正文。
// Args: reportId - 页面标识。
// Returns: 有效期与可执行操作。
export function fetchPageLifecycle(reportId: string): Promise<PageLifecycle> {
  return get<PageLifecycle>(`${REPORT_BASE}/reports/${encodeURIComponent(reportId)}/lifecycle`);
}

// 方法作用：按已授权能力延期或永久保留页面，不自动延长分享。
// Args: reportId - 页面标识；days - 延期天数，null 表示永久。
// Returns: 操作结果。
export function updatePageLifecycle(reportId: string, days: number | null): Promise<Record<string, unknown>> {
  const action = days === null ? 'permanent' : 'extend';
  return post<Record<string, unknown>>(`${REPORT_BASE}/reports/${encodeURIComponent(reportId)}/${action}`, days === null ? {} : { days });
}

// 方法作用：解析报表工作台计划预览请求。
// Args: prompt - 自然语言报表描述；datasources - 当前用户选择的数据源；timezone - 租户时区。
// Returns: 结构化计划、哈希和资源估算。
export function previewReportPlan(
  prompt: string,
  datasources: string[],
  timezone = 'Asia/Shanghai',
): Promise<ReportPlanPreviewResponse> {
  return post<ReportPlanPreviewResponse>(`${REPORT_BASE}/plans`, { prompt, datasources, timezone });
}

// 方法作用：按用户确认的计划创建并排队报表任务。
// Args: prompt - 原始描述；planHash - 服务端计划哈希；plan - 可选已编辑计划；confirm - 是否确认；idempotencyKey - 幂等键。
// Returns: 新建任务的排队摘要。
export function createReportJob(
  prompt: string,
  planHash: string,
  plan: ReportPlan | null,
  confirm: boolean,
  idempotencyKey: string,
): Promise<Record<string, unknown>> {
  return post<Record<string, unknown>>(`${REPORT_BASE}/jobs`, {
    prompt,
    plan_hash: planHash,
    plan_override: plan,
    confirm,
    idempotency_key: idempotencyKey,
  });
}

// 方法作用：读取当前身份可见的报表任务列表。
// Args: limit - 返回任务数量上限。
// Returns: 任务数组和总数。
export function fetchReportJobs(limit = 50): Promise<{ jobs: ReportJob[]; total: number }> {
  return get<{ jobs: ReportJob[]; total: number }>(`${REPORT_BASE}/jobs?limit=${limit}`);
}

// 方法作用：读取单个报表任务、最近运行和最新页面版本。
// Args: jobId - 报表任务 ID。
// Returns: 任务详情。
export function fetchReportJob(jobId: string): Promise<ReportJobDetailResponse> {
  return get<ReportJobDetailResponse>(`${REPORT_BASE}/jobs/${encodeURIComponent(jobId)}`);
}

// 方法作用：读取任务的最近运行记录。
// Args: jobId - 报表任务 ID；limit - 返回数量上限。
// Returns: 运行数组和总数。
export function fetchReportRuns(jobId: string, limit = 20): Promise<{ runs: ReportRun[]; total: number }> {
  return get<{ runs: ReportRun[]; total: number }>(`${REPORT_BASE}/jobs/${encodeURIComponent(jobId)}/runs?limit=${limit}`);
}

// 方法作用：读取单次运行及节点步骤日志。
// Args: runId - 报表运行 ID。
// Returns: 运行摘要和步骤数组。
export function fetchReportRun(runId: string): Promise<{ run: ReportRun; steps: ReportRunStep[] }> {
  return get<{ run: ReportRun; steps: ReportRunStep[] }>(`${REPORT_BASE}/runs/${encodeURIComponent(runId)}`);
}

// 方法作用：读取当前用户的不可变私有报表页面和脱敏快照。
// Args: reportId - 报表 ID。
// Returns: 页面文档和数据清单。
export function fetchPrivateReport(reportId: string): Promise<PrivateReportResponse> {
  return get<PrivateReportResponse>(`${REPORT_BASE}/reports/${encodeURIComponent(reportId)}`);
}

// 方法作用：提交私有快照 Drilldown 过滤条件。
// Args: reportId - 报表 ID；drilldownId - 服务端签发的 Drilldown ID；filters - 已签发字段过滤；page/pageSize - 分页。
// Returns: 有界脱敏明细。
export function fetchPrivateDrilldown(
  reportId: string,
  drilldownId: string,
  filters: Record<string, unknown>,
  page = 1,
  pageSize = 50,
): Promise<ReportDrilldownResponse> {
  return post<ReportDrilldownResponse>(
    `${REPORT_BASE}/reports/${encodeURIComponent(reportId)}/drilldowns/${encodeURIComponent(drilldownId)}`,
    { filters, page, page_size: pageSize },
  );
}

// 方法作用：创建固定版本或最新指针的报表分享链接。
// Args: reportId - 报表 ID；request - 分享范围、授权码和交互权限。
// Returns: 只显示一次的分享 URL 和摘要。
export function createReportShare(reportId: string, request: ReportShareRequest): Promise<ReportShareResponse> {
  return post<ReportShareResponse>(
    `${REPORT_BASE}/reports/${encodeURIComponent(reportId)}/shares`,
    request as unknown as Record<string, unknown>,
  );
}

// 方法作用：读取当前用户可管理的分享链接摘要，不返回 token 或授权码。
// Args: reportId - 报表 ID。
// Returns: 分享摘要列表。
export function fetchReportShares(reportId: string): Promise<{ shares: ReportShareSummary[]; total: number }> {
  return get<{ shares: ReportShareSummary[]; total: number }>(`${REPORT_BASE}/reports/${encodeURIComponent(reportId)}/shares`);
}

// 方法作用：撤销当前用户可管理的分享链接。
// Args: shareId - 分享 ID。
// Returns: 撤销状态。
export function revokeReportShare(shareId: string): Promise<{ status: string }> {
  return del<{ status: string }>(`${REPORT_BASE}/shares/${encodeURIComponent(shareId)}`);
}

// 方法作用：读取当前身份的报表能力，控制菜单和页面入口。
// Args: 无。
// Returns: report.* 能力集合和菜单可见标记。
export function fetchReportPermissions(): Promise<ReportPermissionResponse> {
  return get<ReportPermissionResponse>(`${REPORT_BASE}/permissions/me`);
}

// 方法作用：读取当前身份剩余的报表周期配额。
// Args: 无。
// Returns: 周期起止时间和剩余资源摘要。
export function fetchReportQuota(): Promise<ReportQuotaResponse> {
  return get<ReportQuotaResponse>(`${REPORT_BASE}/quotas/me`);
}

// 方法作用：解析公开报表接口的错误响应，不触发全局登录重定向。
// Args: response - 公开接口响应对象。
// Returns: 服务端错误消息。
async function publicError(response: Response): Promise<Error> {
  const text = await response.text().catch(() => '');
  try {
    const payload = JSON.parse(text) as { detail?: unknown };
    if (typeof payload.detail === 'string' && payload.detail.trim()) return new Error(payload.detail);
  } catch {
    // 非 JSON 响应使用状态码回退。
  }
  return new Error(text.trim() || `HTTP ${response.status}`);
}

// 方法作用：访问公开报表快照并携带短时授权会话。
// Args: token - URL 分享 token；sessionToken - 可选授权码解锁会话。
// Returns: 公开脱敏页面和允许的快照数据。
export async function fetchSharedReport(token: string, sessionToken = ''): Promise<ReportSharedResponse> {
  const headers: HeadersInit = {};
  if (sessionToken) headers['X-Report-Share-Session'] = sessionToken;
  const response = await fetch(`/api/v1${REPORT_BASE}/shared/reports/${encodeURIComponent(token)}`, {
    credentials: 'include',
    headers,
  });
  if (!response.ok) throw await publicError(response);
  return response.json() as Promise<ReportSharedResponse>;
}

// 方法作用：提交公开分享授权码并获取短时会话令牌。
// Args: token - URL 分享 token；authorizationCode - 用户输入的授权码。
// Returns: 短时会话令牌和过期秒数。
export async function unlockSharedReport(token: string, authorizationCode: string): Promise<{ session_token: string; expires_in_seconds: number }> {
  const response = await fetch(`/api/v1${REPORT_BASE}/shared/reports/${encodeURIComponent(token)}/unlock`, {
    method: 'POST',
    credentials: 'include',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ authorization_code: authorizationCode }),
  });
  if (!response.ok) throw await publicError(response);
  return response.json() as Promise<{ session_token: string; expires_in_seconds: number }>;
}

// 方法作用：访问公开分享的快照 Drilldown，并将授权会话限制在当前请求。
// Args: token - URL 分享 token；drilldownId - 服务端签发 ID；sessionToken - 可选授权会话；filters/page/pageSize - 筛选和分页。
// Returns: 有界脱敏明细。
export async function fetchSharedDrilldown(
  token: string,
  drilldownId: string,
  sessionToken: string,
  filters: Record<string, unknown>,
  page = 1,
  pageSize = 50,
): Promise<ReportDrilldownResponse> {
  const response = await fetch(
    `/api/v1${REPORT_BASE}/shared/reports/${encodeURIComponent(token)}/drilldowns/${encodeURIComponent(drilldownId)}`,
    {
      method: 'POST',
      credentials: 'include',
      headers: { 'Content-Type': 'application/json', ...(sessionToken ? { 'X-Report-Share-Session': sessionToken } : {}) },
      body: JSON.stringify({ filters, page, page_size: pageSize }),
    },
  );
  if (!response.ok) throw await publicError(response);
  return response.json() as Promise<ReportDrilldownResponse>;
}
