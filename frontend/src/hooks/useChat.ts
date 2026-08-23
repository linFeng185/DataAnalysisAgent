import { useState, useRef, useCallback, useEffect } from 'react';
import { streamChat, fetchSessionTurns } from '../api/client';
import type { ChatAbortController } from '../api/client';
import type { ActionRequestPayload, ChatTurnData } from '../types';
import { useAuth } from './AuthContext';
import { clearChatStorage } from './chatStorage';

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
  status: 'streaming' | 'done' | 'error' | 'cancelled';
  errorMessage: string;
}

// 方法作用：构造不共享引用的空助手流式状态。
// Args: 无。
// Returns: 新的 AssistantContent。
function emptyAssistant(): AssistantContent {
  return {
    decisionSummary: '', tokens: '', sql: '',
    streamBuffers: {},
    progressNodes: {}, validationErrors: [], analysisText: '',
  };
}

// 方法作用：从持久化轮次恢复前端聊天轮次。
// Args: d - 后端轮次数据；datasource - 会话数据源名称。
// Returns: 可直接渲染的 ChatTurn。
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

// 方法作用：管理对话轮次、SSE 事件、会话恢复以及模型和推理偏好透传。
// Args: 无。
// Returns: 对话状态、发送/取消/恢复操作及加载状态。
export function useChat() {
  const { user } = useAuth();
  const identityKey = user ? `${user.tenant_id}:${user.user_id}` : 'anonymous';
  const [turns, setTurns] = useState<ChatTurn[]>([]);
  const [loading, setLoading] = useState(false);
  const [retryInfo, setRetryInfo] = useState<{current:number;max:number;reason:string}|null>(null);
  const [sessionId, setSessionId] = useState('');
  const [hasMore, setHasMore] = useState(false);
  const [loadingMore, setLoadingMore] = useState(false);
  const nextId = useRef(1);
  const aborterRef = useRef<ChatAbortController | null>(null);
  const streamingTurnIdRef = useRef<number | null>(null);

  // 方法作用：认证身份变化时终止旧请求并清空全部内存聊天状态。
  // Args: identityKey - 当前租户与用户组成的身份键。
  // Returns: 无返回值。
  useEffect(() => {
    aborterRef.current?.abort();
    aborterRef.current = null;
    setTurns([]);
    setSessionId('');
    setHasMore(false);
    setLoading(false);
    setRetryInfo(null);
    nextId.current = 1;
    clearChatStorage();
  }, [identityKey]);

  const send = useCallback((
    query: string,
    datasource: string,
    _datasources?: string[],
    _modelId?: string,
    _llmConnectionId?: number,
    enabledSkillIds?: string[],
    reasoningEnabled = false,
    reasoningEffort = '',
    fileIds: number[] = [],
    actionRequest?: ActionRequestPayload,
  ) => {
    aborterRef.current?.abort();
    setRetryInfo(null);
    setLoading(true);
    const turnId = nextId.current++;
    streamingTurnIdRef.current = turnId;
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
            // 中间模型输出仅用于服务端协议续接，前端不保存或展示原始内容。
            void e.stream_id;
            break;
          case 'thinking':
            // 兼容旧事件但不保存或展示原始模型推理内容。
            break;
          case 'token':
            // generate_sql/analyze_result 等中间节点的原始 token 不进入聊天消息。
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
        const isCurrentTurn = streamingTurnIdRef.current === turnId;
        if (isCurrentTurn) {
          setLoading(false); setRetryInfo(null);
          streamingTurnIdRef.current = null;
        }
        setTurns(prev => prev.map(t =>
          t.id === turnId && t.status === 'streaming' ? { ...t, status: 'done' } : t,
        ));
      },
      (err) => {
        const isCurrentTurn = streamingTurnIdRef.current === turnId;
        if (isCurrentTurn) {
          setLoading(false); setRetryInfo(null);
          streamingTurnIdRef.current = null;
        }
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
      fileIds,
      actionRequest,
    );
    const controller = aborterRef.current;
    if (!controller) return;
    controller.onCancelled = () => {
      const isCurrentTurn = streamingTurnIdRef.current === turnId;
      if (isCurrentTurn) {
        setLoading(false); setRetryInfo(null);
        streamingTurnIdRef.current = null;
      }
      setTurns(prev => prev.map(t => t.id === turnId && t.status === 'streaming' ? {
        ...t,
        status: 'cancelled',
        errorMessage: '已取消',
        assistant: {
          ...t.assistant,
          progressNodes: {},
          streamBuffers: {},
          tokens: '',
        },
      } : t));
    };
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
    const turnId = streamingTurnIdRef.current;
    streamingTurnIdRef.current = null;
    if (turnId !== null) {
      setTurns(prev => prev.map(t => t.id === turnId && t.status === 'streaming' ? {
        ...t,
        status: 'cancelled',
        errorMessage: '已取消',
        assistant: {
          ...t.assistant,
          progressNodes: {},
          streamBuffers: {},
          tokens: '',
        },
      } : t));
    }
  }, []);
  const clearSession = useCallback(() => {
    setTurns([]); setSessionId(''); nextId.current = 1;
    setHasMore(false);
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
