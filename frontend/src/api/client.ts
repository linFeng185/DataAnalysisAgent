const BASE = '/api/v1';

async function request<T>(method: string, path: string, body?: Record<string, unknown>): Promise<T> {
  const opts: RequestInit = {
    method,
    headers: { 'Content-Type': 'application/json' },
  };
  if (body) opts.body = JSON.stringify(body);
  const res = await fetch(`${BASE}${path}`, opts);
  if (!res.ok) {
    const text = await res.text().catch(() => '');
    throw new Error(text || `API error: ${res.status}`);
  }
  return res.json();
}

export function get<T>(path: string): Promise<T> {
  return request<T>('GET', path);
}

export function post<T>(path: string, body: Record<string, unknown>): Promise<T> {
  return request<T>('POST', path, body);
}

export function put<T>(path: string, body: Record<string, unknown>): Promise<T> {
  return request<T>('PUT', path, body);
}

export function del(path: string): Promise<void> {
  return request<void>('DELETE', path);
}

// 会话列表 API
import type { SessionListResponse, SessionDetailResponse, ChatTurnData } from '../types';

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

// SSE 流式聊天
export function streamChat(
  query: string,
  datasource: string,
  sessionId: string,
  onEvent: (evt: Record<string, unknown>) => void,
  onDone: () => void,
  onError: (err: string) => void,
  datasources?: string[],
  modelId?: string,
): AbortController {
  const controller = new AbortController();
  fetch(`${BASE}/chat`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ query, datasource, datasources: datasources || [datasource], model_id: modelId || '', stream: true, session_id: sessionId }),
    signal: controller.signal,
  }).then(async (res) => {
    if (!res.ok) {
      const text = await res.text().catch(() => '');
      onError(text || `HTTP ${res.status}`);
      return;
    }
    const reader = res.body?.getReader();
    if (!reader) { onError('读取流失败'); return; }
    const decoder = new TextDecoder();
    let buf = '';
    try {
      while (true) {
        const { done, value } = await reader.read();
        if (done) break;
        buf += decoder.decode(value, { stream: true });
        const lines = buf.split('\n');
        buf = lines.pop() || '';
        for (const line of lines) {
          if (line.startsWith('data: ')) {
            try { onEvent(JSON.parse(line.slice(6))); } catch { /* skip */ }
          }
        }
      }
    } catch { /* stream interrupted */ }
    onDone();
  }).catch((e) => {
    if (e.name !== 'AbortError') onError(e.message);
  });
  return controller;
}
