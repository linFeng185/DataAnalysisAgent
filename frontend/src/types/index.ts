// SSE 事件类型（与后端 streaming.py 中 13 种事件对应）
export type SSEEventType =
  | 'node_start' | 'node_end' | 'progress'
  | 'llm_start' | 'llm_end'
  | 'token'
  | 'sql' | 'validation'
  | 'result' | 'analysis' | 'decision_summary'
  | 'error' | 'done';

export interface SSEEvent {
  type: SSEEventType;
  node?: string;
  stream_id?: string;
  message?: string;
  content?: string;
  sql?: string;
  valid?: boolean;
  errors?: unknown[];
  warnings?: unknown[];
  success?: boolean;
  analysis?: Record<string, unknown>;
  data?: Record<string, unknown>[];
  chart?: { type: string; option?: Record<string, unknown> };
  [key: string]: unknown;
}

export interface SQLStatement {
  datasource: string;
  dialect: string;
  sql: string;
  purpose?: 'primary' | 'supplemental';
  question?: string;
}

export interface TouchpointAttributionItem {
  touchpoint: string;
  attributed_value: number | string;
  attribution_pct: number | string;
  conversion_credit: number | string;
}

export interface TouchpointAttributionAnalysis {
  status: 'applied' | 'insufficient';
  selected_model: 'first_touch' | 'last_touch' | 'linear' | 'time_decay' | 'position_based';
  model_display_name: string;
  selection_source: 'user_explicit' | 'llm_recommended' | 'rule_default';
  selection_reason: string;
  dataset_source: 'primary' | 'supplemental';
  total_attributed_value?: number | string;
  total_paths?: number;
  converted_paths?: number;
  unconverted_paths?: number;
  half_life_days?: number | string | null;
  eligibility: {
    eligible: boolean;
    row_count: number;
    total_paths: number;
    converted_paths: number;
    unconverted_paths: number;
    missing_requirements: string[];
    warnings: string[];
  };
  results: TouchpointAttributionItem[];
}

export interface MaskingMetadata {
  applied: boolean;
  fields: Array<{ column: string; categories: string[]; masked_count: number }>;
}

export interface ChatResponse {
  success: boolean;
  status?: string;
  source?: string;
  user_query: string;
  sql: string;
  sql_statements: SQLStatement[];
  data: Record<string, unknown>[] | Record<string, unknown>;
  row_count: number;
  truncated: boolean;
  masking?: MaskingMetadata;
  supplemental_masking?: MaskingMetadata[];
  analysis: {
    summary: string;
    insights: string[];
    recommended_chart_type: string;
    follow_up_questions: string[];
    statistics?: Record<string, unknown>;
    rendered_report?: string;
    related_metrics?: string[];
    business_impacts?: string[];
    possible_causes?: string[];
    recommended_actions?: string[];
    limitations?: string[];
    indicator_groups?: Array<{
      theme: string;
      indicators: Array<{
        name: string;
        description: string;
        table: string;
        column: string;
      }>;
    }>;
    attribution?: TouchpointAttributionAnalysis;
  };
  chart: {
    type: string;
    option?: Record<string, unknown>;
    status?: 'success' | 'failed' | 'not_applicable';
    reason?: string;
    data_source?: 'primary' | 'supplemental' | 'attribution';
    core_value?: { label: string; value: unknown; column: string };
  };
  decision_summary?: string;
  artifact?: Record<string, unknown>;
  session_id?: string;
  error_code?: string;
  error_message?: string;
  activated_skills?: string[];
  activated_knowledge?: Array<{
    source_id: string;
    category: string;
    source: string;
    source_file?: string;
    table_name?: string;
    locator?: Record<string, unknown>;
    relevance?: number;
    trust_level?: string;
    match_count?: number;
  }> | string;
}

export interface SchemaSyncStatus {
  status: 'idle' | 'running' | 'cancelling' | 'succeeded' | 'cancelled' | 'failed' | 'unknown';
  phase?: string;
  error?: string;
  table_count?: number;
  finished_at?: string;
  run_id?: string;
}

export interface DatasourceConfig {
  schema_sync?: SchemaSyncStatus;
  name: string;
  dialect: string;
  version?: string;
  host: string;
  port: number;
  database: string;
  username: string;
  description?: string;
  schema?: string;
  tablespace?: string;
  service_name?: string;
  instance?: string;
  file_path?: string;
  extra_params?: Record<string, unknown>;
  updated_at?: string;
}

export interface DatasourceCreatePayload {
  name: string;
  dialect: string;
  version?: string;
  host?: string;
  port?: number;
  database?: string;
  username?: string;
  password?: string;
  description?: string;
  schema?: string;
  tablespace?: string;
  service_name?: string;
  instance?: string;
  file_path?: string;
}

export interface DatasourceConnectionResult {
  success: boolean;
  message: string;
}

export interface TableInfo {
  name: string;
  description: string;
  columns: ColumnInfo[];
  column_count?: number;
  row_count_estimate?: number;
  relations?: { target_table: string; join_key: string }[];
}

export interface ColumnInfo {
  name: string;
  type: string;
  comment: string;
  is_nullable?: boolean;
  is_primary_key?: boolean;
}

export interface HealthResponse {
  status: string;
  version: string;
  uptime_seconds: number;
}

export interface ActionRequestPayload {
  action_name: string;
  payload: Record<string, unknown>;
  idempotency_key: string;
  confirmed: boolean;
  metadata?: Record<string, unknown>;
}

export interface ChatUploadedFile {
  file_id: number;
  filename: string;
  size: number;
}

export interface HistoryItem {
  id: string;
  query: string;
  sql: string;
  time: string;
  success: boolean;
  datasource?: string;
  session_id?: string;
  row_count?: number;
  error_message?: string;
  final_result?: Record<string, unknown>;
}

export interface SkillInfo {
  resource_id: string;
  name: string;
  version: string;
  enabled: boolean;
  description: string;
  triggers: string[];
  intents: string[];
  tools: string[];
  dependencies: string[];
  is_builtin: boolean;
  scope: KnowledgeScope;
  tenant_id: number;
  owner_user_id: number;
}

export interface RegistrySkillInfo {
  name: string;
  version: string;
  description: string;
  api_version: string;
  sha256: string;
  installed: boolean;
}

export type AutomationKind = 'insight' | 'report';
export type AutomationFrequency = 'hourly' | 'daily' | 'weekly' | 'monthly';
export type AutomationChannel = 'in_app' | 'email' | 'feishu' | 'slack';

export interface AutomationSchedule {
  id: string;
  name: string;
  kind: AutomationKind;
  datasource: string;
  sql: string;
  dialect: string;
  frequency: AutomationFrequency;
  threshold_pct: number;
  channels: AutomationChannel[];
  recipient_email: string;
  enabled: boolean;
  next_run_at: string;
  last_run_at: string | null;
}

export interface AutomationRun {
  id: string;
  status: 'success' | 'failed';
  error_message: string;
  result_payload: Record<string, unknown>;
  started_at: string;
  finished_at: string | null;
}

export interface AutomationNotification {
  id: string;
  schedule_id: string;
  kind: AutomationKind;
  title: string;
  body: string;
  is_read: boolean;
  created_at: string;
}

export type KnowledgeScope = 'system' | 'tenant' | 'private';

export interface KnowledgeTag {
  id: number;
  name: string;
  slug: string;
  tag_group: string;
  aliases: string[];
  description: string;
  scope: 'global' | 'private';
  tenant_id: number | null;
  owner_user_id: number | null;
  is_active: boolean;
  is_seed: boolean;
}

export interface KnowledgeEntry {
  id: string;
  content: string;
  category: string;
  datasource: string;
  table_name: string;
  source: string;
  source_file: string;
  scope: KnowledgeScope;
  tag_ids: number[];
  tags: string[];
  tenant_id?: number | null;
  owner_user_id?: number | null;
  owner_username?: string;
  can_delete: boolean;
  is_builtin: boolean;
}

export interface KnowledgeDoc {
  id?: number;
  name: string;
  size: number;
  modified: number;
  scope: KnowledgeScope;
  datasource?: string;
  tag_ids: number[];
  tenant_id?: number | null;
  owner_user_id?: number | null;
  owner_username?: string;
  can_delete: boolean;
  is_builtin: boolean;
}

export interface SessionInfo {
  session_id: string;
  title: string;
  datasource: string;
  turn_count: number;
  created_at: string;
  last_active_at: string;
}

export interface ChatTurnData {
  turn_id: number;
  user_query: string;
  assistant_summary: string;
  sql: string;
  timestamp: string;
  final_result: Record<string, unknown>;
}

export interface SessionListResponse {
  sessions: SessionInfo[];
  next_cursor: string | null;
  has_more: boolean;
}

/** 会话最新一轮的富数据，用于恢复完整 UI */
export interface SessionLatestState {
  sql: string;
  sql_statements?: SQLStatement[];
  analysis: Record<string, unknown>;
  chart: Record<string, unknown>;
  data: Record<string, unknown>[];
  row_count: number;
  truncated: boolean;
  success: boolean;
  error_message: string;
  decision_summary?: string;
  artifact?: Record<string, unknown>;
}

export interface SessionDetailResponse {
  session: SessionInfo;
  turns: ChatTurnData[];
  latest_state: SessionLatestState | null;
  has_more: boolean;
}
