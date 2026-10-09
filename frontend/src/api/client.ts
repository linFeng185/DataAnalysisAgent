import { getLanguage, t } from '../i18n';

const BASE = '/api/v1';
const pendingGetRequests = new Map<string, Promise<unknown>>();

export class ApiError extends Error {
  readonly status: number;

  // 保存 HTTP 状态供认证边界区分未登录与服务不可用。
  // Args: message - 展示消息；status - HTTP 状态码。
  // Returns: API 错误实例。
  constructor(message: string, status: number) {
    super(message);
    this.name = 'ApiError';
    this.status = status;
  }
}

// 方法作用：认证失效时统一返回登录页，避免页面停留在不可用状态。
// Args: 无。
// Returns: 无返回值。
function redirectToLogin(): void {
  if (window.location.pathname !== '/login') window.location.assign('/login');
}

// 方法作用：发送统一 JSON API 请求并处理认证失效和错误响应。
// Args: method - HTTP 方法；path - API 相对路径；body - 可选 JSON 请求体。
// Returns: 解析后的响应对象。
async function request<T>(method: string, path: string, body?: Record<string, unknown>): Promise<T> {
  const opts: RequestInit = {
    method,
    headers: { 'Content-Type': 'application/json' },
    credentials: 'include',
  };
  if (body) opts.body = JSON.stringify(body);
  return timedRequest<T>(path, opts, response => response.json());
}

// 方法作用：为非流式 API 的连接和响应读取设置期限，超时取消网络请求。
// Args: path - 相对路径；options - 请求选项；read - 响应读取函数。
// Returns: 解析结果；失败时释放定时器并抛出可显示的错误。
async function timedRequest<T>(path: string, options: RequestInit, read: (response: Response) => Promise<T>): Promise<T> {
  const controller = new AbortController();
  const timer = window.setTimeout(() => controller.abort(), path.startsWith('/auth/') ? 10000 : 30000);
  try {
    const response = await fetch(`${BASE}${path}`, { ...options, credentials: 'include', signal: controller.signal });
    if (response.status === 401 && path !== '/auth/me' && path !== '/auth/login') redirectToLogin();
    if (!response.ok) {
      throw new ApiError(formatApiError(await response.text(), `API error: ${response.status}`), response.status);
    }
    return await read(response);
  } catch (error) {
    if (controller.signal.aborted) {
      console.warn('API 请求超时', { method: options.method || 'GET', path: path.split('?')[0] });
      throw new Error(t("请求超时，请检查服务状态后重试"));
    }
    throw error;
  } finally {
    window.clearTimeout(timer);
  }
}

// 方法作用：通过统一超时和认证边界上传 JSON 配置文件。
// Args: path - API 路径；file - 用户选择的文件。
// Returns: 服务端导入结果。
export function uploadConfig<T>(path: string, file: File): Promise<T> {
  const body = new FormData();
  body.append('file', file);
  return timedRequest(path, { method: 'POST', body }, response => response.json());
}

// 方法作用：通过带认证和超时控制的请求下载配置文件。
// Args: path - API 路径。
// Returns: 文件内容。
export function downloadConfig(path: string): Promise<Blob> {
  return timedRequest(path, { method: 'GET' }, response => response.blob());
}

// 方法作用：将 FastAPI 错误响应转换为用户可读的简短消息。
// Args: text - HTTP 响应正文；fallback - 正文为空或无法解析时的回退消息。
// Returns: 优先返回 JSON detail 字段，否则返回原文或回退消息。
function formatApiError(text: string, fallback: string): string {
  const raw = String(text || '').trim();
  if (!raw) return fallback;
  try {
    const payload = JSON.parse(raw) as { detail?: unknown };
    if (typeof payload.detail === 'string' && payload.detail.trim()) return payload.detail;
    if (payload.detail !== undefined && payload.detail !== null) return String(payload.detail);
  } catch {
    // 非 JSON 响应继续展示服务端原文。
  }
  return raw;
}

// 方法作用：合并同一路径的并发 GET 请求，并在请求结束后清理进行中状态。
// Args: path - API 相对路径。
// Returns: 解析后的响应对象 Promise。
export function get<T>(path: string): Promise<T> {
  const existing = pendingGetRequests.get(path);
  if (existing) return existing as Promise<T>;

  const pending = request<T>('GET', path).finally(() => {
    if (pendingGetRequests.get(path) === pending) {
      pendingGetRequests.delete(path);
    }
  });
  pendingGetRequests.set(path, pending);
  return pending;
}

export function post<T>(path: string, body: Record<string, unknown>): Promise<T> {
  return request<T>('POST', path, body);
}

export function put<T>(path: string, body: Record<string, unknown>): Promise<T> {
  return request<T>('PUT', path, body);
}

export function patch<T>(path: string, body: Record<string, unknown>): Promise<T> {
  return request<T>('PATCH', path, body);
}

export function del<T = void>(path: string, body?: Record<string, unknown>): Promise<T> {
  return request<T>('DELETE', path, body);
}

// 会话列表 API
import type {
  ActionRequestPayload, ChatTurnData, ChatUploadedFile,
  SessionDetailResponse, SessionListResponse,
} from '../types';

export interface ChatAbortController extends AbortController {
  onCancelled?: () => void;
}

export async function fetchSessions(cursor?: string | null, limit = 20): Promise<SessionListResponse> {
  const params = new URLSearchParams();
  if (cursor) params.set('cursor', cursor);
  params.set('limit', String(limit));
  const qs = params.toString();
  return get<SessionListResponse>(`/sessions${qs ? '?' + qs : ''}`);
}

export async function fetchSession(id: string): Promise<SessionDetailResponse> {
  return get<SessionDetailResponse>(`/sessions/${id}`);
}

export async function fetchSessionTurns(
  id: string, before?: number, limit = 20,
): Promise<{ turns: ChatTurnData[]; has_more: boolean }> {
  const params = new URLSearchParams();
  if (before) params.set('before', String(before));
  params.set('limit', String(limit));
  return get(`/sessions/${id}/turns?${params.toString()}`);
}

export async function deleteSession(id: string): Promise<void> {
  return del(`/sessions/${id}`);
}

// 方法作用：把聊天附件以 multipart/form-data 上传到当前用户私有文件存储。
// Args: files - 用户当前选择的附件列表。
// Returns: 后续 ChatRequest.file_ids 使用的文件摘要。
export async function uploadChatFiles(files: File[]): Promise<ChatUploadedFile[]> {
  const form = new FormData();
  files.forEach(file => form.append('files', file));
  const response = await fetch(`${BASE}/chat/files`, {
    method: 'POST',
    credentials: 'include',
    body: form,
  });
  if (response.status === 401) redirectToLogin();
  if (!response.ok) {
    const payload = await response.json().catch(() => null) as { detail?: string } | null;
    throw new Error(payload?.detail || `附件上传失败: HTTP ${response.status}`);
  }
  const payload = await response.json() as { files?: ChatUploadedFile[] };
  return payload.files || [];
}

// 方法作用：发送带租户 LLM 连接和模型选择的 SSE 聊天请求并分发事件。
// Args: query - 用户问题；datasource - 主数据源；sessionId - 会话；onEvent/onDone/onError - 事件、完成和错误回调；datasources - 多数据源；modelId - 模型标识；llmConnectionId - 命名连接；enabledSkillIds - Skill 资源 ID；reasoningEnabled/reasoningEffort - 推理偏好；fileIds - 请求级附件；actionRequest - 结构化动作请求。
// Returns: 可取消当前请求的 AbortController。
export function streamChat(
  query: string,
  datasource: string,
  sessionId: string,
  onEvent: (evt: Record<string, unknown>) => void,
  onDone: () => void,
  onError: (err: string) => void,
  datasources?: string[],
  modelId?: string,
  llmConnectionId?: number,
  enabledSkillIds?: string[],
  reasoningEnabled = false,
  reasoningEffort = '',
  fileIds: number[] = [],
  actionRequest?: ActionRequestPayload,
): ChatAbortController {
  const controller = new AbortController() as ChatAbortController;
  let cancellationNotified = false;
  const notifyCancelled = (): void => {
    if (cancellationNotified) return;
    cancellationNotified = true;
    controller.onCancelled?.();
  };
  controller.signal.addEventListener('abort', notifyCancelled, { once: true });
  fetch(`${BASE}/chat`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    credentials: 'include',
    body: JSON.stringify({
      query,
      datasource,
      datasources: datasources || [datasource],
      model_id: modelId || '',
      llm_connection_id: llmConnectionId ?? null,
      enabled_skill_ids: enabledSkillIds || [],
      reasoning_enabled: reasoningEnabled,
      reasoning_effort: reasoningEffort,
      file_ids: fileIds,
      action_request: actionRequest || null,
      display_language: getLanguage(),
      stream: true,
      session_id: sessionId,
    }),
    signal: controller.signal,
  }).then(async (res) => {
    if (res.status === 401) redirectToLogin();
    if (!res.ok) {
      const text = await res.text().catch(() => '');
      onError(formatApiError(text, `HTTP ${res.status}`));
      return;
    }
    const reader = res.body?.getReader();
    if (!reader) { onError('读取流失败'); return; }
    const decoder = new TextDecoder();
    let buf = '';
    let terminalStatus = '';
    let reportedError = false;
    // 解析完整 SSE 行并记录结束协议，断流不能被误报为成功。
    // Args: line - 已按 UTF-8 解码的完整事件行。
    // Returns: 无返回值，非法协议抛出错误进入统一读取边界。
    const consumeLine = (line: string): void => {
      if (!line.startsWith('data:') || controller.signal.aborted) return;
      try {
        const event = JSON.parse(line.slice(5).trim()) as Record<string, unknown>;
        if (event.type === 'error') reportedError = true;
        if (event.type === 'done') terminalStatus = String(event.status || '');
        onEvent(event);
      } catch (error) {
        console.error('流式事件 JSON 解析失败', error);
        throw new Error('流式事件格式无效');
      }
    };
    try {
      while (true) {
        const { done, value } = await reader.read();
        if (done) {
          buf += decoder.decode();
          if (buf.trim()) consumeLine(buf);
          break;
        }
        buf += decoder.decode(value, { stream: true });
        const lines = buf.split('\n');
        buf = lines.pop() || '';
        for (const line of lines) consumeLine(line);
      }
    } catch (error) {
      if (controller.signal.aborted) notifyCancelled();
      else onError(error instanceof Error ? error.message : '流式读取失败');
      return;
    }
    if (controller.signal.aborted) {
      notifyCancelled();
      return;
    }
    if (reportedError || terminalStatus !== 'complete') {
      onError(terminalStatus === 'error' || reportedError ? t("流式处理失败，请稍后重试") : t("连接中断，分析未完成，请重试"));
      return;
    }
    onDone();
  }).catch((e) => {
    if (e.name === 'AbortError' || controller.signal.aborted) notifyCancelled();
    else onError(e.message);
  });
  return controller;
}
