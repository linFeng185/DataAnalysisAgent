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
  | 'deleted';

export interface ReportEstimatedResources {
  query_count: number;
  llm_tokens: number;
  estimated_seconds: number;
}

export interface ReportAnalysisUnit {
  unit_id: string;
  question: string;
  datasource?: string;
  priority: 'primary' | 'secondary' | 'evidence';
  time_range: { kind: 'relative' | 'absolute' | 'all'; value: string };
  dimensions: string[];
  metrics: string[];
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
  | 'callout';

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
