// SSE 事件类型（与后端 streaming.py 中 13 种事件对应）
export type SSEEventType =
  | 'node_start' | 'node_end' | 'progress'
  | 'llm_start' | 'llm_end'
  | 'thinking' | 'token'
  | 'sql' | 'validation'
  | 'result' | 'analysis'
  | 'error' | 'done';

export interface SSEEvent {
  type: SSEEventType;
  node?: string;
  message?: string;
  content?: string;
  reasoning_content?: string;
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

export interface ChatResponse {
  success: boolean;
  user_query: string;
  sql: string;
  sql_reasoning_content?: string;
  data: Record<string, unknown>[];
  analysis: {
    summary: string;
    insights: string[];
    recommended_chart_type: string;
    follow_up_questions: string[];
    analysis_reasoning_content?: string;
    statistics?: Record<string, unknown>;
  };
  chart: { type: string; option?: Record<string, unknown> };
  session_id?: string;
  error_code?: string;
  error_message?: string;
}

export interface DatasourceConfig {
  name: string;
  dialect: string;
  version?: string;
  host: string;
  port: number;
  database: string;
  username: string;
  password?: string;
  description?: string;
  schema?: string;
  tablespace?: string;
  service_name?: string;
  instance?: string;
  file_path?: string;
}

export interface TableInfo {
  name: string;
  description: string;
  columns: ColumnInfo[];
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
  llm_available: boolean;
  uptime: number;
  datasources: number;
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
}

export interface SkillInfo {
  name: string;
  version: string;
  enabled: boolean;
  description: string;
  triggers: string[];
  intents: string[];
  tools: string[];
  dependencies: string[];
  is_builtin: boolean;
}

export interface KnowledgeEntry {
  id: string;
  content: string;
  category: string;
  datasource: string;
  table_name: string;
  source: string;
  source_file: string;
  is_builtin: boolean;
}

export interface KnowledgeDoc {
  name: string;
  size: number;
  modified: number;
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
}

export interface SessionListResponse {
  sessions: SessionInfo[];
  next_cursor: string | null;
  has_more: boolean;
}

/** 会话最新一轮的富数据，用于恢复完整 UI */
export interface SessionLatestState {
  sql: string;
  analysis: Record<string, unknown>;
  chart: Record<string, unknown>;
  data: Record<string, unknown>[];
  success: boolean;
  error_message: string;
}

export interface SessionDetailResponse {
  session: SessionInfo;
  turns: ChatTurnData[];
  latest_state: SessionLatestState | null;
}
