import { del, get, patch, post } from './client';
import type {
  PrivateReportResponse,
  ReportCreateRequest,
  ReportCreateResponse,
  ReportDetailResponse,
  ReportDivergenceLevel,
  ReportDrilldownResponse,
  ReportJob,
  ReportJobDetailResponse,
  ReportJobStatus,
  ReportListQuery,
  ReportListResponse,
  ReportPageSummary,
  ReportPermissionResponse,
  ReportPlan,
  ReportPlanPreviewResponse,
  ReportPlanUpdateRequest,
  ReportQuotaResponse,
  ReportRunResponse,
  ReportQuotaUsageResetRequest,
  ReportQuotaUsageResetResponse,
  ReportRun,
  ReportRunListQuery,
  ReportRunListResponse,
  ReportRunStats,
  ReportRunStep,
  ReportUnitSnapshot,
  ReportShareRequest,
  ReportShareResponse,
  ReportShareSummary,
  ReportSharedResponse,
  ReportShareBatchRevokeResponse,
  ReportShareLinkListResponse,
  ReportShareLinkQuery,
  ReportShareLinkStatus,
  ReportShareVisitListResponse,
  SharedReportVisitResponse,
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

/** 逐页生命周期操作结果；permanent 为 true 时 expires_at 为 null。 */
export interface ReportPageLifecycleResult {
  report_id: string;
  expires_at: string | null;
  permanent: boolean;
}

// 方法作用：按页面 id 延长有效期，服务端约束单次最多 30 天。
// Args: reportId - 页面版本 id；days - 延期天数（7 或 30）。
// Returns: 更新后的有效期与永久标记。
export function extendReportPage(reportId: string, days: number): Promise<ReportPageLifecycleResult> {
  return post<ReportPageLifecycleResult>(`${REPORT_BASE}/reports/${encodeURIComponent(reportId)}/extend`, { days });
}

// 方法作用：按页面 id 设为永久保留；超出租户永久报表上限时服务端返回 409，原因需原样展示。
// Args: reportId - 页面版本 id。
// Returns: 更新后的有效期（null 表示永久）与永久标记。
export function makeReportPagePermanent(reportId: string): Promise<ReportPageLifecycleResult> {
  return post<ReportPageLifecycleResult>(`${REPORT_BASE}/reports/${encodeURIComponent(reportId)}/permanent`, {});
}

// 方法作用：解析报表工作台计划预览请求。
// Args: prompt - 自然语言报表描述；datasources - 当前用户选择的数据源；timezone - 租户时区；modelId - 显式选择的模型；llmConnectionId - 模型所属命名连接。
// Returns: 结构化计划、哈希和资源估算。
export function previewReportPlan(
  prompt: string,
  datasources: string[],
  timezone = 'Asia/Shanghai',
  modelId = '',
  llmConnectionId?: number,
  divergence: ReportDivergenceLevel = 'standard',
): Promise<ReportPlanPreviewResponse> {
  // 显式带上命名连接与模型，后端才能用该连接做结构化规划；都不传时回退租户默认连接。
  return post<ReportPlanPreviewResponse>(`${REPORT_BASE}/plans`, {
    prompt,
    datasources,
    timezone,
    model_id: modelId,
    llm_connection_id: llmConnectionId ?? null,
    divergence,
  });
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
  acceptedUnitIds?: string[] | null,
  templateId?: string,
): Promise<Record<string, unknown>> {
  return post<Record<string, unknown>>(`${REPORT_BASE}/jobs`, {
    prompt,
    plan_hash: planHash,
    plan_override: plan,
    confirm,
    idempotency_key: idempotencyKey,
    // 不传表示全部接受；传数组表示只保留列表中的单元。空数组由服务端拒绝。
    accepted_unit_ids: acceptedUnitIds ?? null,
    // 模板不进计划哈希：改配色不需要重新预览与重新确认。
    template_id: templateId || null,
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

// 方法作用：读取单次运行及节点步骤日志和单元快照。
// Args: runId - 报表运行 ID。
// Returns: 运行摘要、步骤数组和单元快照数组。
export function fetchReportRun(
  runId: string,
): Promise<{ run: ReportRun & { stats?: ReportRunStats }; steps: ReportRunStep[]; units?: ReportUnitSnapshot[] }> {
  return get<{ run: ReportRun & { stats?: ReportRunStats }; steps: ReportRunStep[]; units?: ReportUnitSnapshot[] }>(
    `${REPORT_BASE}/runs/${encodeURIComponent(runId)}`,
  );
}

// 方法作用：分页读取执行明细，含 LLM/SQL 调用次数与输入输出 token。
// Args: query - 任务过滤、状态过滤和分页参数。
// Returns: 运行明细数组、总数和分页信息。
export function fetchRunDetails(query: ReportRunListQuery = {}): Promise<ReportRunListResponse> {
  const params = new URLSearchParams();
  if (query.job_id) params.set('job_id', query.job_id);
  if (query.status) params.set('status', query.status);
  params.set('page', String(query.page ?? 1));
  params.set('page_size', String(query.page_size ?? 20));
  return get<ReportRunListResponse>(`${REPORT_BASE}/runs?${params.toString()}`);
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

// 方法作用：读取链接管理页的分享链接平铺列表及各链接的访问量与停留时长聚合。
// Args: query - 报表、创建人、状态筛选与分页条件。
// Returns: 分享链接摘要、总数和分页信息。
export function fetchReportShareLinks(query: ReportShareLinkQuery = {}): Promise<ReportShareLinkListResponse> {
  const params = new URLSearchParams();
  if (query.report_id) params.set('report_id', query.report_id);
  if (query.created_by) params.set('created_by', String(query.created_by));
  if (query.status) params.set('status', query.status);
  params.set('page', String(query.page ?? 1));
  params.set('page_size', String(query.page_size ?? 20));
  return get<ReportShareLinkListResponse>(`${REPORT_BASE}/shares?${params.toString()}`);
}

// 方法作用：读取单条分享链接的最近访问明细，只返回系统用户及其停留时长。
// Args: shareId - 分享链接 ID。
// Returns: 访问明细数组和总数。
export function fetchReportShareVisits(shareId: string): Promise<ReportShareVisitListResponse> {
  return get<ReportShareVisitListResponse>(`${REPORT_BASE}/shares/${encodeURIComponent(shareId)}/visits`);
}

// 方法作用：批量取消分享链接，服务端逐条判定可撤销性并回传跳过数量。
// Args: shareIds - 要取消的分享链接 ID 列表。
// Returns: 已取消与已跳过的条数。
export function batchRevokeReportShares(shareIds: string[]): Promise<ReportShareBatchRevokeResponse> {
  return post<ReportShareBatchRevokeResponse>(`${REPORT_BASE}/shares/batch-revoke`, { share_ids: shareIds });
}

// 方法作用：公开分享页成功加载快照后登记一次访问，取得后续上报停留时长用的访问 ID。
// Args: token - URL 分享 token；unlocked - 本次加载是否已通过授权码解锁。
// Returns: 访问 ID 与访客类型（系统用户或匿名）。
export async function startSharedReportVisit(token: string, unlocked: boolean): Promise<SharedReportVisitResponse> {
  const response = await fetch(`/api/v1${REPORT_BASE}/shared/reports/${encodeURIComponent(token)}/visits`, {
    method: 'POST',
    credentials: 'include',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ unlocked }),
  });
  if (!response.ok) throw await publicError(response);
  return response.json() as Promise<SharedReportVisitResponse>;
}

// 方法作用：以累积值幂等覆盖的方式上报公开分享页停留时长，重试或重复上报不会重复计数。
// Args: token - URL 分享 token；visitId - 访问登记返回的 ID；durationSeconds - 累积停留秒数；unlocked - 是否已通过授权码解锁；keepalive - 页面卸载时为 true。
// Returns: 服务端确认保留的累积秒数。
export async function reportSharedReportVisitDuration(
  token: string,
  visitId: string,
  durationSeconds: number,
  unlocked = false,
  keepalive = false,
): Promise<{ visit_id: string; duration_seconds: number }> {
  // 卸载路径必须用 fetch keepalive：navigator.sendBeacon 只能发 POST，无法表达 PATCH 的幂等覆盖语义。
  const response = await fetch(
    `/api/v1${REPORT_BASE}/shared/reports/${encodeURIComponent(token)}/visits/${encodeURIComponent(visitId)}`,
    {
      method: 'PATCH',
      credentials: 'include',
      keepalive,
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ duration_seconds: Math.round(durationSeconds), unlocked }),
    },
  );
  if (!response.ok) throw await publicError(response);
  return response.json() as Promise<{ visit_id: string; duration_seconds: number }>;
}

// 方法作用：由租户管理员把指定用户或全租户的本月额度已用量清零，不修改额度上限。
// Args: request - 目标范围（user_ids 与 all_users 互斥二选一）与要清零的字段，fields 不能为空。
// Returns: 实际清零的用户数、实际字段与当前周期起止。
export function resetReportQuotaUsage(request: ReportQuotaUsageResetRequest): Promise<ReportQuotaUsageResetResponse> {
  // user_ids 与 all_users 互斥：两个都发会被后端判为冲突，因此按范围只发其中一个。
  const body: Record<string, unknown> = { fields: request.fields };
  if (request.all_users) body.all_users = true;
  else body.user_ids = request.user_ids || [];
  return post<ReportQuotaUsageResetResponse>(`${REPORT_BASE}/admin/quota-usage/reset`, body);
}

// 方法作用：创建报表记录并触发后台计划生成，不预占额度。
// Args: request - 提示词、数据源、时区、发散档位、模板与模型选择、幂等键。
// Returns: 新建报表的标识与初始状态（planning）。
export function createReport(request: ReportCreateRequest): Promise<ReportCreateResponse> {
  return post<ReportCreateResponse>(`${REPORT_BASE}/reports`, {
    prompt: request.prompt,
    datasources: request.datasources,
    timezone: request.timezone || 'Asia/Shanghai',
    // 发散档位与模型选择都必须显式下发：后端据此生成计划，不依赖服务端默认值。
    divergence: request.divergence || 'standard',
    template_id: request.template_id || '',
    model_id: request.model_id || '',
    llm_connection_id: request.llm_connection_id ?? null,
    idempotency_key: request.idempotency_key,
  });
}

// 方法作用：读取当前身份可见的报表信息列表，支持状态与用户筛选与分页。
// Args: query - user_id（仅租户管理员）、status、page、page_size。
// Returns: 报表摘要数组、总数与分页信息。
export function fetchReportList(query: ReportListQuery = {}): Promise<ReportListResponse> {
  const params = new URLSearchParams();
  // 只有租户管理员可以按用户筛选：其他角色传 user_id 会被服务端 403。
  if (query.user_id) params.set('user_id', String(query.user_id));
  if (query.status) params.set('status', query.status);
  params.set('page', String(query.page ?? 1));
  params.set('page_size', String(query.page_size ?? 20));
  return get<ReportListResponse>(`${REPORT_BASE}/reports?${params.toString()}`);
}

/** 报表用户筛选的下拉项。 */
export interface ReportUserOption {
  value: number;
  label: string;
}

// 方法作用：按用户名搜索租户内用户，供报表列表的创建者筛选下拉使用。
// Args: search - 可选用户名关键字；pageSize - 最多取多少条。
// Returns: 下拉选项数组；接口不可用时返回空数组，筛选框退化为可输入。
export async function fetchReportUsers(search = '', pageSize = 50): Promise<ReportUserOption[]> {
  const params = new URLSearchParams({ page: '1', page_size: String(pageSize) });
  if (search.trim()) params.set('search', search.trim());
  const result = await get<{ users?: Array<{ id: number; username?: string }> }>(`/admin/users?${params.toString()}`);
  return (result.users || []).map(item => ({ value: item.id, label: item.username || String(item.id) }));
}

// 方法作用：读取单条报表的计划、最近运行、页面版本与历史运行。
// Args: reportId - 报表标识。
// Returns: 报表详情。
export function fetchReportDetail(reportId: string): Promise<ReportDetailResponse> {
  return get<ReportDetailResponse>(`${REPORT_BASE}/reports/${encodeURIComponent(reportId)}`);
}

// 方法作用：更新未生成报表的计划与调度（仅 draft 可改，其他状态服务端返回 409）。
// Args: reportId - 报表标识；request - 要修改的字段，accepted_unit_ids 只允许删减。
// Returns: 更新后的报表详情。
export function updateReportPlan(reportId: string, request: ReportPlanUpdateRequest): Promise<ReportDetailResponse> {
  return patch<ReportDetailResponse>(
    `${REPORT_BASE}/reports/${encodeURIComponent(reportId)}`,
    request as unknown as Record<string, unknown>,
  );
}

// 方法作用：把未生成的报表投入执行，此时才预占 jobs_created 额度。
// Args: reportId - 报表标识。
// Returns: 排队结果与剩余额度；额度不足时服务端返回 429。
export function runReport(reportId: string): Promise<ReportRunResponse> {
  return post<ReportRunResponse>(`${REPORT_BASE}/reports/${encodeURIComponent(reportId)}/run`, {});
}

/** 单单元重跑结果：覆盖该单元最近一次快照，不重生成页面。 */
export interface ReportUnitRerunResponse {
  unit_id: string;
  status: string;
  snapshot_id: string;
}

// 方法作用：重跑报表中的单个分析单元，只覆盖该单元的执行快照。
// Args: reportId - 报表标识；unitId - 分析单元标识。
// Returns: 单元标识、执行状态与最新快照 ID。
export function rerunReportUnit(reportId: string, unitId: string): Promise<ReportUnitRerunResponse> {
  return post<ReportUnitRerunResponse>(
    `${REPORT_BASE}/reports/${encodeURIComponent(reportId)}/units/${encodeURIComponent(unitId)}/rerun`,
    {},
  );
}

// 方法作用：读取单个分析单元最近一次执行的快照，没有执行记录时 snapshot 为 null。
// Args: reportId - 报表标识；unitId - 分析单元标识。
// Returns: 快照对象或 null。
export function fetchReportUnitSnapshot(reportId: string, unitId: string): Promise<{ snapshot: ReportUnitSnapshot | null }> {
  return get<{ snapshot: ReportUnitSnapshot | null }>(
    `${REPORT_BASE}/reports/${encodeURIComponent(reportId)}/units/${encodeURIComponent(unitId)}/snapshot`,
  );
}

// 方法作用：对计划生成失败的报表重试后台规划（仅 plan_failed 可重试）。
// Args: reportId - 报表标识。
// Returns: 复位后的状态（planning），详情需要重新读取。
export function retryReportPlan(reportId: string): Promise<{ report_id: string; status: ReportJobStatus }> {
  return post<{ report_id: string; status: ReportJobStatus }>(`${REPORT_BASE}/reports/${encodeURIComponent(reportId)}/retry-plan`, {});
}

// 方法作用：删除报表记录；服务端只允许删除未生成或计划失败的报表。
// Args: reportId - 报表标识。
// Returns: 删除状态。
export function deleteReport(reportId: string): Promise<{ status: string }> {
  return del<{ status: string }>(`${REPORT_BASE}/reports/${encodeURIComponent(reportId)}`);
}

// 方法作用：读取报表的页面版本摘要，由服务端按版本倒序返回。
// Args: reportId - 报表标识。
// Returns: 页面版本摘要数组。
export function fetchReportPages(reportId: string): Promise<{ pages: ReportPageSummary[] }> {
  return get<{ pages: ReportPageSummary[] }>(`${REPORT_BASE}/reports/${encodeURIComponent(reportId)}/pages`);
}
