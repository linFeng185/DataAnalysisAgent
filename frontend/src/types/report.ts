/** 报表工作台前端类型契约。 */

export type ReportExecutionMode = 'immediate' | 'scheduled';
export type ReportJobStatus =
  | 'draft'
  | 'planning'
  | 'queued'
  | 'running'
  | 'succeeded'
  | 'partial'
  | 'failed'
  | 'cancelled'
  | 'expired'
  | 'deleted'
  /** 计划后台生成失败：可在「报表信息」里重试计划。 */
  | 'plan_failed';

export interface ReportEstimatedResources {
  query_count: number;
  llm_tokens: number;
  estimated_seconds: number;
}

export type ReportDivergenceLevel = 'conservative' | 'standard' | 'deep';

export interface ReportAnalysisUnit {
  unit_id: string;
  question: string;
  datasource?: string;
  priority: 'primary' | 'secondary' | 'evidence';
  time_range: { kind: 'relative' | 'absolute' | 'all'; value: string };
  dimensions: string[];
  metrics: string[];
  /** 单元来源：用户显式要求或系统发散建议。 */
  origin?: 'user' | 'diverged';
  /** 仅发散单元非空：为什么建议分析这个。 */
  rationale?: string;
  /** 发散单元产生阶段：计划期或运行期。 */
  decided_by?: '' | 'plan' | 'runtime';
}

export interface ReportPlan {
  plan_version: string;
  execution_mode: ReportExecutionMode;
  frequency: 'hourly' | 'daily' | 'weekly' | 'monthly' | null;
  run_at: string | null;
  timezone: string;
  max_runs: number | null;
  schedule_end_at: string | null;
  page_ttl_days: number;
  permanent: boolean;
  requested_datasources: string[];
  analysis_units: ReportAnalysisUnit[];
  /** 发散档位：一次性总授权额度，计划期与运行期共享。 */
  divergence: ReportDivergenceLevel;
  share: {
    enabled: boolean;
    require_code: boolean;
    allow_drilldown: boolean;
    scope: 'private' | 'public' | 'user';
    user_scope: 'none' | 'specified' | 'tenant';
    user_ids: number[];
  };
  warnings: string[];
  needs_confirmation: boolean;
  estimated: ReportEstimatedResources;
}

export interface ReportPlanPreviewResponse {
  plan: ReportPlan;
  plan_hash: string;
  estimated: ReportEstimatedResources;
  job_id: string | null;
}

export interface ReportJob {
  id: string;
  tenant_id: number;
  user_id: number;
  prompt: string;
  plan: ReportPlan;
  status: ReportJobStatus;
  runs_started: number;
  next_run_at?: string | null;
  is_paused?: boolean;
  created_at: string | null;
  updated_at: string | null;
}

export interface ReportRun {
  id: string;
  job_id: string;
  status: 'queued' | 'running' | 'succeeded' | 'partial' | 'failed' | 'cancelled';
  attempt: number;
  last_node: string;
  /** 配额计费口径的估算值，不是实际执行次数。 */
  query_count: number;
  row_count: number;
  /** 真实 token 用量（prompt + completion）。 */
  token_usage: number;
  /** 以下为运行期真实计数，与 query_count 的估算口径相互独立。 */
  llm_call_count: number;
  llm_failure_count: number;
  sql_query_count: number;
  sql_success_count: number;
  sql_failure_count: number;
  partial: boolean;
  error_code: string;
  user_error_message: string;
  trace_id: string;
  started_at: string | null;
  finished_at: string | null;
}

export interface ReportRunStep {
  id: string;
  run_id: string;
  seq: number;
  node: string;
  status: string;
  attempt: number;
  error_code: string;
  user_error_message: string;
  started_at: string | null;
  finished_at: string | null;
  duration_ms?: number | null;
}

/** 单个分析单元最近一次执行的完整调试快照，字段与后端 report_unit_snapshots 表对应。 */
export interface ReportUnitSnapshot {
  id: string;
  run_id: string;
  report_id: string;
  unit_id: string;
  /** 单元执行结果：success / partial / failed。 */
  status: 'success' | 'partial' | 'failed';
  datasource: string;
  dialect: string;
  sql_text: string;
  prompt_text: string;
  prompt_id: string;
  model_id: string;
  params: Record<string, unknown>;
  row_count: number;
  full_count: number;
  truncated: boolean;
  truncated_reason: string;
  elapsed_ms: number;
  error_code: string;
  error_message: string;
  created_at: string;
}

export interface ReportVersionSummary {
  id: string;
  report_id: string;
  job_id: string;
  run_id: string;
  version: number;
  checksum: string;
  expires_at: string | null;
  is_latest: boolean;
}

export interface ReportJobDetailResponse {
  job: ReportJob;
  latest_run: ReportRun | null;
  latest_report: ReportVersionSummary | null;
}

export type ReportBlockType =
  | 'kpi'
  | 'narrative'
  | 'chart'
  | 'table'
  | 'insight_list'
  | 'relationship'
  | 'quality'
  | 'source_citation'
  | 'callout'
  | 'findings';

export interface ReportLayout {
  desktop: { col_span: number; row_span: number };
  tablet: { col_span: number; row_span: number };
  mobile: { col_span: number; row_span: number };
}

export interface ReportBlock {
  id: string;
  section_id: string;
  type: ReportBlockType;
  priority: number;
  data_ref: string;
  evidence_refs: string[];
  layout: ReportLayout;
  visibility: 'private' | 'shared' | 'public';
  title: string;
  content: Record<string, unknown>;
  echarts_option?: Record<string, unknown> | null;
}

export interface ReportSection {
  id: string;
  title: string;
  subtitle: string;
  blocks: ReportBlock[];
}

export interface ReportDrilldownDefinition {
  drilldown_id: string;
  source_block_id: string;
  mode: 'snapshot' | 'live';
  allowed_filters: Array<{ field: string; operator: 'eq' | 'in' | 'between'; value_type: string }>;
  columns: string[];
  max_rows: number;
  data_ref: string;
  expires_at?: string | null;
}

export interface ReportDocument {
  schema_version: 'report.document.v1';
  report_id: string;
  version: number;
  title: string;
  subtitle: string;
  generated_at: string;
  expires_at: string | null;
  theme: Record<string, string>;
  navigation: string[];
  sections: ReportSection[];
  sources: Array<Record<string, string>>;
  limitations: string[];
  permissions: { download: boolean; drilldown: boolean };
  drilldowns: ReportDrilldownDefinition[];
}

export interface PrivateReportResponse {
  report_id: string;
  version: number;
  document: ReportDocument;
  data_manifest: Record<string, Record<string, unknown>[]>;
}

export interface ReportDrilldownResponse {
  drilldown_id: string;
  columns: string[];
  rows: Record<string, unknown>[];
  total: number;
  page: number;
  page_size: number;
}

export interface ReportShareRequest {
  version?: number;
  scope: 'public' | 'user';
  require_code: boolean;
  authorization_code?: string;
  expires_in_days: number;
  allow_drilldown: boolean;
  allow_download: boolean;
  pin_latest: boolean;
  user_ids?: number[];
}

export interface ReportShareResponse {
  share_id: string;
  scope: 'public' | 'user';
  url: string;
  expires_at: string;
  authorization_code_hint: string;
}

export interface ReportShareSummary {
  id: string;
  report_id?: string;
  version?: number;
  scope: 'public' | 'user';
  user_scope?: 'none' | 'specified' | 'tenant';
  user_ids?: number[];
  allow_drilldown: boolean;
  allow_download: boolean;
  pin_latest: boolean;
  expires_at: string | null;
  revoked_at?: string | null;
  view_count?: number;
  created_at?: string | null;
}

export interface ReportSharedResponse extends PrivateReportResponse {}

export interface ReportPermissionResponse {
  capabilities: string[];
  menu_allowed: boolean;
}

export interface ReportQuotaResponse {
  period_start: string;
  period_end: string;
  jobs_remaining: number;
  runs_remaining: number;
  concurrent_remaining: number;
  tokens_remaining: number;
  shares_remaining: number;
}

/** 链接管理页的分享状态筛选值；active 含未到期的有效链接。 */
export type ReportShareLinkStatus = 'active' | 'revoked' | 'expired';

/** 链接管理页的一行分享链接摘要，含访问时长埋点聚合。 */
export interface ReportShareLinkSummary {
  id: string;
  report_id: string;
  report_title: string;
  version: number;
  scope: 'public' | 'user';
  user_scope: 'none' | 'specified' | 'tenant';
  require_code: boolean;
  created_by: number;
  created_at: string | null;
  expires_at: string | null;
  revoked_at: string | null;
  /** 总访问量，等于系统用户访问量与非系统用户访问量之和。 */
  visitor_count: number;
  /** 带登录身份访问的次数（租户未开启身份记录时恒为 0）。 */
  system_visitor_count: number;
  /** 未识别身份（匿名）访问的次数。 */
  anonymous_visitor_count: number;
  total_duration_seconds: number;
  average_duration_seconds: number;
}

/** 链接管理页分页响应。 */
export interface ReportShareLinkListResponse {
  shares: ReportShareLinkSummary[];
  total: number;
  page: number;
  page_size: number;
}

/** 一次分享访问的明细行，用于「最近访问」抽屉。 */
export interface ReportShareVisit {
  /** 匿名访问为 null，界面按非系统用户展示。 */
  user_id: number | null;
  username: string;
  started_at: string | null;
  last_seen_at: string | null;
  duration_seconds: number;
  ip_hash: string;
}

/** 最近访问明细响应。 */
export interface ReportShareVisitListResponse {
  visits: ReportShareVisit[];
  total: number;
}

/** 批量取消分享的结果；skipped 为不可撤销（已撤销或非本人）的条数。 */
export interface ReportShareBatchRevokeResponse {
  revoked: number;
  skipped: number;
}

/** 公开分享页登记一次访问后的埋点标识与访客类型。 */
export interface SharedReportVisitResponse {
  visit_id: string;
  visitor_kind: 'user' | 'anonymous';
}

/** 链接管理页的查询条件；status 与 created_by 省略时由服务端按可见范围返回全部。 */
export interface ReportShareLinkQuery {
  report_id?: string;
  created_by?: number;
  status?: ReportShareLinkStatus;
  page?: number;
  page_size?: number;
}

/** 可按自然月清零已用量的四个额度字段键，取值与后端冻结契约一一对应。 */
export type ReportQuotaResetField = 'jobs_created' | 'runs_started' | 'llm_tokens' | 'share_links';

/** 报表额度已用量重置请求；user_ids 与 all_users 互斥且必居其一，fields 不能为空数组。 */
export interface ReportQuotaUsageResetRequest {
  user_ids?: number[];
  all_users?: boolean;
  fields: ReportQuotaResetField[];
}

/** 报表额度已用量重置结果；reset_users 是实际被清零的用户数，可能小于请求人数。 */
export interface ReportQuotaUsageResetResponse {
  reset_users: number;
  fields: ReportQuotaResetField[];
  period_start: string;
  period_end: string;
}

/** 报表信息列表与详情共用的一条报表记录；列表不返回计划正文，详情才带 plan。 */
export interface ReportListItem {
  id: string;
  /** 模型起的报表标题；为空时列表回退展示 prompt。 */
  title: string;
  prompt: string;
  datasources: string[];
  divergence: ReportDivergenceLevel;
  status: ReportJobStatus;
  runs_started: number;
  next_run_at: string | null;
  created_at: string | null;
  execution_mode: ReportExecutionMode;
  is_paused: boolean;
  latest_report_id: string | null;
  /** 计划失败时的可读原因；后端随记录一并返回。 */
  plan_error?: string | null;
  /** 计划卡死：planning 超过服务端阈值，后台任务多半已随重启丢失，可中断重来。 */
  plan_stuck?: boolean;
  /** 创建者；后端只允许创建者点「生成」，界面据此避免给出必然 403 的按钮。 */
  user_id?: number;
}

/** 报表信息列表分页响应。 */
export interface ReportListResponse {
  reports: ReportListItem[];
  total: number;
  page: number;
  page_size: number;
}

/** 报表信息页的查询条件；user_id 仅租户管理员可用。 */
export interface ReportListQuery {
  user_id?: number;
  status?: ReportJobStatus;
  page?: number;
  page_size?: number;
}

/** 一次运行产生的页面版本摘要；每次成功运行都会新增一条独立可访问的页面。 */
export interface ReportPageSummary {
  id: string;
  version: number;
  expires_at: string | null;
  is_latest: boolean;
  created_at: string | null;
}

/** 单条报表详情：计划、最近运行、页面版本与历史运行。 */
export interface ReportDetailResponse {
  report: ReportListItem;
  plan: ReportPlan | null;
  latest_run: ReportRun | null;
  latest_report: ReportPageSummary | null;
  runs: ReportRun[];
  pages: ReportPageSummary[];
  /** 计划失败时后端给出的可读原因；成功生成计划后为空。 */
  plan_error?: string | null;
}

/** 创建报表请求；创建后进入 planning，计划在后台生成。 */
export interface ReportCreateRequest {
  prompt: string;
  datasources: string[];
  timezone?: string;
  divergence?: ReportDivergenceLevel;
  template_id?: string;
  model_id?: string;
  llm_connection_id?: number | null;
  idempotency_key: string;
}

/** 创建报表响应：只回传标识与初始状态，计划稍后就绪。 */
export interface ReportCreateResponse {
  report_id: string;
  status: ReportJobStatus;
  created_at: string | null;
}

/** 计划与调度更新请求；字段全可选，accepted_unit_ids 只允许删减已有单元。 */
export interface ReportPlanUpdateRequest {
  accepted_unit_ids?: string[];
  execution_mode?: ReportExecutionMode;
  frequency?: 'hourly' | 'daily' | 'weekly' | 'monthly' | null;
  run_at?: string | null;
  max_runs?: number | null;
  schedule_end_at?: string | null;
  page_ttl_days?: number;
  permanent?: boolean;
  timezone?: string;
  template_id?: string;
}

/** 运行报表的响应；此处才预占 jobs_created 额度。 */
export interface ReportRunResponse {
  report_id: string;
  status: ReportJobStatus;
  execution_mode: ReportExecutionMode;
  run_id: string | null;
  quota?: Record<string, unknown> | null;
}
