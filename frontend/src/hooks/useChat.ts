import { useState, useRef, useCallback, useEffect } from 'react';
import { streamChat, fetchSessionTurns } from '../api/client';
import type { ChatTurnData, SessionLatestState } from '../types';

const SESSION_KEY = 'chat_turns';
const SESSION_ID_KEY = 'active_session_id';

interface ProgressNode {
  status: 'pending' | 'running' | 'done' | 'error';
  message: string;
}
interface AssistantContent {
  reasoning: string;
  tokens: string;
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
    reasoning: '', tokens: '', sql: '',
    progressNodes: {}, validationErrors: [], analysisText: '',
  };
}

/** 从 ChatTurnData 还原为 ChatTurn（历史会话恢复用） */
function turnFromData(d: ChatTurnData, datasource: string, isLast: boolean, latest?: SessionLatestState | null): ChatTurn {
  // 最后一轮注入富数据，恢复完整的分析结果 UI
  const finalResult = isLast && latest ? {
    success: latest.success,
    error_message: latest.error_message,
    sql: latest.sql,
    data: latest.data,
    analysis: latest.analysis,
    chart: latest.chart,
  } : null;

  return {
    id: d.turn_id,
    userQuery: d.user_query,
    datasource,
    assistant: {
      ...emptyAssistant(),
      sql: latest?.sql || d.sql,
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

export function useChat() {
  const saved = useRef(loadFromStorage());
  const [turns, setTurns] = useState<ChatTurn[]>(saved.current?.turns || []);
  const [loading, setLoading] = useState(false);
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

  const send = useCallback((query: string, datasource: string, _datasources?: string[], _modelId?: string) => {
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
            break;
          case 'thinking':
            if (e.reasoning_content) updateAssistant(a => ({
              reasoning: a.reasoning + (e.reasoning_content as string),
            }));
            break;
          case 'token':
            if (e.content) updateAssistant(a => ({
              tokens: a.tokens + (e.content as string),
            }));
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
          case 'result':
            setTurns(prev => prev.map(t => t.id === turnId ? {
              ...t, finalResult: e as unknown as Record<string, unknown>, status: 'done',
            } : t));
            break;
          case 'error':
            setTurns(prev => prev.map(t => t.id === turnId ? {
              ...t, status: 'error', errorMessage: (e.message as string) || '未知错误',
            } : t));
            break;
        }
      },
      () => {
        setLoading(false);
        setTurns(prev => prev.map(t =>
          t.id === turnId && t.status === 'streaming' ? { ...t, status: 'done' } : t,
        ));
      },
      (err) => {
        setLoading(false);
        setTurns(prev => prev.map(t =>
          t.id === turnId ? { ...t, status: 'error', errorMessage: err } : t,
        ));
      },
    );
  }, [sessionId]);

  const sendMulti = useCallback((query: string, datasourcesList: string[], modelId: string) => {
    if (datasourcesList.length > 0) {
      send(query, datasourcesList[0], datasourcesList, modelId);
    }
  }, [send]);

  const cancel = useCallback(() => { aborterRef.current?.abort(); setLoading(false); }, []);
  const clearSession = useCallback(() => {
    setTurns([]); setSessionId(''); nextId.current = 1;
    setHasMore(false);
    clearStorage();
  }, []);

  /** 恢复历史会话的对话记录，latest 为最后一轮的富数据 */
  const restoreTurns = useCallback((
    savedTurns: ChatTurnData[], sid: string, datasource: string,
    latest?: SessionLatestState | null,
  ) => {
    const lastIdx = savedTurns.length - 1;
    const restored = savedTurns.map((d, i) => turnFromData(d, datasource, i === lastIdx, latest));
    setTurns(restored);
    setSessionId(sid);
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
        const older = res.turns.map(d => turnFromData(d, datasource, false));
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
           restoreTurns, loadMoreTurns, hasMore, loadingMore, checkHasMore };
}
