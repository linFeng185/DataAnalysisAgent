import { useState, useRef, useCallback, useEffect } from 'react';
import { streamChat, fetchSessionTurns } from '../api/client';
import type { ChatTurnData } from '../types';

const SESSION_KEY = 'chat_turns';
const SESSION_ID_KEY = 'active_session_id';

interface ProgressNode {
  status: 'pending' | 'running' | 'done' | 'error';
  message: string;
}
interface StreamBuffer {
  node: string;
  tokens: string;
}
interface AssistantContent {
  decisionSummary: string;
  tokens: string;
  streamBuffers: Record<string, StreamBuffer>;
  sql: string;
  progressNodes: Record<string, ProgressNode>;
  validationErrors: string[];
  analysisText: string;
}

export interface ChatTurn {
  id: number;
  userQuery: string;
  datasource: string;
  assistant: AssistantContent;
  finalResult: Record<string, unknown> | null;
  status: 'streaming' | 'done' | 'error';
  errorMessage: string;
}

function emptyAssistant(): AssistantContent {
  return {
    decisionSummary: '', tokens: '', sql: '',
    streamBuffers: {},
    progressNodes: {}, validationErrors: [], analysisText: '',
  };
}

/** 从 ChatTurnData 还原为 ChatTurn（历史会话恢复用） */
function turnFromData(d: ChatTurnData, datasource: string): ChatTurn {
  const finalResult = d.final_result && Object.keys(d.final_result).length > 0
    ? d.final_result : null;
  return {
    id: d.turn_id,
    userQuery: d.user_query,
    datasource,
    assistant: {
      ...emptyAssistant(),
      sql: (finalResult?.sql as string) || d.sql,
      decisionSummary: (finalResult?.decision_summary as string) || '',
      tokens: d.assistant_summary,
    },
    finalResult,
    status: 'done',
    errorMessage: '',
  };
}

function loadFromStorage(): { turns: ChatTurn[]; sessionId: string } | null {
  try {
    const raw = sessionStorage.getItem(SESSION_KEY);
    const sid = sessionStorage.getItem(SESSION_ID_KEY);
    if (raw && sid) {
      const turns = JSON.parse(raw) as ChatTurn[];
      return { turns, sessionId: sid };
    }
  } catch { /* ignore */ }
  return null;
}

function saveToStorage(turns: ChatTurn[], sessionId: string) {
  try {
    sessionStorage.setItem(SESSION_KEY, JSON.stringify(turns));
    sessionStorage.setItem(SESSION_ID_KEY, sessionId);
  } catch { /* ignore */ }
}

function clearStorage() {
  try {
    sessionStorage.removeItem(SESSION_KEY);
    sessionStorage.removeItem(SESSION_ID_KEY);
  } catch { /* ignore */ }
}

// 方法作用：管理对话轮次、SSE 事件、会话恢复以及模型和推理偏好透传。
// Args: 无。
// Returns: 对话状态、发送/取消/恢复操作及加载状态。
export function useChat() {
  const saved = useRef(loadFromStorage());
  const [turns, setTurns] = useState<ChatTurn[]>(saved.current?.turns || []);
  const [loading, setLoading] = useState(false);
  const [retryInfo, setRetryInfo] = useState<{current:number;max:number;reason:string}|null>(null);
  const [sessionId, setSessionId] = useState(saved.current?.sessionId || '');
  const [hasMore, setHasMore] = useState(false);
  const [loadingMore, setLoadingMore] = useState(false);
  const nextId = useRef(turns.length > 0 ? Math.max(...turns.map(t => t.id)) + 1 : 1);
  const aborterRef = useRef<AbortController | null>(null);

  // 每次变更持久化到 sessionStorage
  useEffect(() => {
    if (turns.length > 0 && sessionId) {
      saveToStorage(turns, sessionId);
    }
  }, [turns, sessionId]);

  const send = useCallback((
    query: string,
    datasource: string,
    _datasources?: string[],
    _modelId?: string,
    _llmConnectionId?: number,
    enabledSkillIds?: string[],
    reasoningEnabled = false,
    reasoningEffort = '',
  ) => {
    aborterRef.current?.abort();
    setRetryInfo(null);
    setLoading(true);
    const turnId = nextId.current++;
    const newTurn: ChatTurn = {
      id: turnId, userQuery: query, datasource,
      assistant: emptyAssistant(),
      finalResult: null, status: 'streaming', errorMessage: '',
    };
    setTurns(prev => [...prev, newTurn]);

    const updateAssistant = (fn: (a: AssistantContent) => Partial<AssistantContent>) => {
      setTurns(prev => prev.map(t => t.id === turnId ? {
        ...t, assistant: { ...t.assistant, ...fn(t.assistant) },
      } : t));
    };

    const sid = sessionId || `sess_${Date.now()}`;
    if (!sessionId) setSessionId(sid);

    const dss = _datasources && _datasources.length > 1 ? _datasources : undefined;
    const mid = _modelId || undefined;
    const cid = _llmConnectionId;
    aborterRef.current = streamChat(query, datasource, sid,
      (evt: Record<string, unknown>) => {
        const e = evt as Record<string, unknown>;
        switch (e.type) {
          case 'node_start':
            if (e.node) updateAssistant(a => ({
              progressNodes: {
                ...a.progressNodes,
                [e.node as string]: { status: 'running', message: '' },
              },
            }));
            break;
          case 'progress':
            if (e.node) updateAssistant(a => ({
              progressNodes: {
                ...a.progressNodes,
                [e.node as string]: { status: 'running', message: (e.message as string) || '' },
              },
            }));
            break;
          case 'node_end':
            if (e.node) updateAssistant(a => ({
              progressNodes: {
                ...a.progressNodes,
                [e.node as string]: { ...a.progressNodes[e.node as string], status: 'done' },
              },
            }));
            break;
          case 'llm_start':
            if (e.stream_id) updateAssistant(a => {
              const existingBuffers = a.streamBuffers || {};
              return { streamBuffers: {
                ...existingBuffers,
                [e.stream_id as string]: existingBuffers[e.stream_id as string] || {
                  node: (e.node as string) || 'unknown', tokens: '',
                },
              } };
            });
            break;
          case 'thinking':
            // 兼容旧事件但不保存或展示原始模型推理内容。
            break;
          case 'token':
            if (e.content) updateAssistant(a => {
              const streamId = (e.stream_id as string) || `legacy:${(e.node as string) || 'unknown'}`;
              const existingBuffers = a.streamBuffers || {};
              const current = existingBuffers[streamId] || {
                node: (e.node as string) || 'unknown', tokens: '',
              };
              const streamBuffers = {
                ...existingBuffers,
                [streamId]: {
                  ...current,
                  tokens: current.tokens + (e.content as string),
                },
              };
              const tokens = Object.entries(streamBuffers)
                .filter(([, buffer]) => buffer.tokens)
                .map(([id, buffer]) => `【${buffer.node} · ${id.slice(0, 8)}】\n${buffer.tokens}`)
                .join('\n\n');
              return { streamBuffers, tokens };
            });
            break;
          case 'llm_end':
            break;
          case 'sql':
            if (e.sql) updateAssistant(a => ({ sql: e.sql as string }));
            break;
          case 'validation':
            updateAssistant(a => ({
              validationErrors: [
                ...a.validationErrors,
                ...(((e.errors as unknown[]) || []) as string[]),
              ],
            }));
            break;
          case 'analysis':
            if (e.analysis) updateAssistant(a => ({
              analysisText: (e.analysis as Record<string, unknown>).summary as string || '',
            }));
            break;
          case 'decision_summary':
            if (e.summary) updateAssistant(() => ({ decisionSummary: String(e.summary) }));
            break;
          case 'result':
            setTurns(prev => prev.map(t => t.id === turnId ? {
              ...t,
              assistant: {
                ...t.assistant,
                decisionSummary: String((e.decision_summary as string) || t.assistant.decisionSummary || ''),
                sql: (e.sql as string) || t.assistant.sql,
              },
              finalResult: e as unknown as Record<string, unknown>, status: 'done',
            } : t));
            break;
          case 'retry_status':
            setRetryInfo({current: (e.retry as number)||0, max: (e.max as number)||3, reason: (e.reason as string)||''});
            break;
          case 'error':
            setRetryInfo(null);
            setTurns(prev => prev.map(t => t.id === turnId ? {
              ...t, status: 'error', errorMessage: (e.message as string) || '未知错误',
            } : t));
            break;
        }
      },
      () => {
        setLoading(false); setRetryInfo(null);
        setTurns(prev => prev.map(t =>
          t.id === turnId && t.status === 'streaming' ? { ...t, status: 'done' } : t,
        ));
      },
      (err) => {
        setLoading(false); setRetryInfo(null);
        setTurns(prev => prev.map(t =>
          t.id === turnId ? { ...t, status: 'error', errorMessage: err } : t,
        ));
      },
      dss,
      mid,
      cid,
      enabledSkillIds,
      reasoningEnabled,
      reasoningEffort,
    );
  }, [sessionId]);

  const sendMulti = useCallback((query: string, datasourcesList: string[], modelId: string, connectionId?: number) => {
    if (datasourcesList.length > 0) {
      send(query, datasourcesList[0], datasourcesList, modelId, connectionId);
    }
  }, [send]);

  const cancel = useCallback(() => {
    aborterRef.current?.abort();
    aborterRef.current = null;
    setRetryInfo(null);
    setLoading(false);
  }, []);
  const clearSession = useCallback(() => {
    setTurns([]); setSessionId(''); nextId.current = 1;
    setHasMore(false);
    clearStorage();
  }, []);

  /** 恢复历史会话的逐轮结构化记录。 */
  const restoreTurns = useCallback((
    savedTurns: ChatTurnData[], sid: string, datasource: string, hasOlder = false,
  ) => {
    const restored = savedTurns.map(d => turnFromData(d, datasource));
    setTurns(restored);
    setSessionId(sid);
    setHasMore(hasOlder);
    nextId.current = restored.length > 0
      ? Math.max(...restored.map(t => t.id)) + 1 : 1;
    saveToStorage(restored, sid);
  }, []);

  /** 瀑布流加载更早的对话轮次 */
  const loadMoreTurns = useCallback(async (sid: string, datasource: string) => {
    if (loadingMore || !hasMore) return;
    setLoadingMore(true);
    const earliest = turns.length > 0 ? Math.min(...turns.map(t => t.id)) : undefined;
    try {
      const res = await fetchSessionTurns(sid, earliest, 20);
      if (res.turns.length > 0) {
        const older = res.turns.map(d => turnFromData(d, datasource));
        setTurns(prev => {
          const existingIds = new Set(prev.map(t => t.id));
          const newTurns = older.filter(t => !existingIds.has(t.id));
          return [...newTurns, ...prev];
        });
      }
      setHasMore(res.has_more);
    } catch { /* ignore */ }
    finally { setLoadingMore(false); }
  }, [turns, loadingMore, hasMore]);

  /** 检测当前会话是否有更多历史轮次可加载 */
  const checkHasMore = useCallback(async (sid: string) => {
    if (!sid) return;
    try {
      const res = await fetchSessionTurns(sid, undefined, 1);
      setHasMore(res.has_more);
    } catch { setHasMore(false); }
  }, []);

  return { turns, loading, sessionId, send, cancel, clearSession,
           restoreTurns, loadMoreTurns, hasMore, loadingMore, checkHasMore,
           retryInfo };
}
