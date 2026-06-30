import { useState, useRef, useCallback } from 'react';
import { streamChat } from '../api/client';

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

export function useChat() {
  const [turns, setTurns] = useState<ChatTurn[]>([]);
  const [loading, setLoading] = useState(false);
  const [sessionId, setSessionId] = useState('');
  const nextId = useRef(1);
  const aborterRef = useRef<AbortController | null>(null);

  const send = useCallback((query: string, datasource: string) => {
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

    // 确保 session_id 在每轮对话中保持一致
    const sid = sessionId || `sess_${Date.now()}`;
    if (!sessionId) setSessionId(sid);

    aborterRef.current = streamChat(query, datasource, sid,
      (evt) => {
        const e = evt as Record<string, unknown>;
        switch (e.type) {
          // 节点生命周期
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

          // LLM 流式
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

          // SQL 相关
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

          // 分析结果
          case 'analysis':
            if (e.analysis) updateAssistant(a => ({
              analysisText: (e.analysis as Record<string, unknown>).summary as string || '',
            }));
            break;

          // 最终结果
          case 'result':
            setTurns(prev => prev.map(t => t.id === turnId ? {
              ...t, finalResult: e as unknown as Record<string, unknown>, status: 'done',
            } : t));
            break;

          // 错误
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

  const cancel = useCallback(() => { aborterRef.current?.abort(); setLoading(false); }, []);
  const clearSession = useCallback(() => {
    setTurns([]); setSessionId(''); nextId.current = 1;
  }, []);

  return { turns, loading, sessionId, send, cancel, clearSession };
}
