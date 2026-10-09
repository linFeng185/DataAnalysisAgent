export interface PublicStreamBuffer { node: string; tokens: string }
export interface ProcessSummary {
  node: string;
  status: 'running' | 'done' | 'error';
  summary: string;
  elapsedMs: number;
}
export interface PublicAssistantState {
  tokens: string;
  streamBuffers: Record<string, PublicStreamBuffer>;
  activeStreamId: string;
  processEnabled: boolean;
  processSummaries: Record<string, ProcessSummary>;
}

// 仅保存受控公开正文与管理员允许的过程摘要，并隔离并行调用。
// Args: state - 当前助手状态；event - SSE 公开事件。
// Returns: 可合并的局部状态，未知或内部事件不产生变化。
export function applyPublicEvent(state: PublicAssistantState, event: Record<string, unknown>): Partial<PublicAssistantState> {
  if (event.type === 'presentation') {
    const enabled = event.process_summary_enabled === true;
    return { processEnabled: enabled, processSummaries: enabled ? state.processSummaries : {} };
  }
  const streamId = String(event.stream_id || '');
  const node = String(event.node || '');
  if (!streamId || !node) return {};
  if (event.type === 'process_summary') {
    if (!state.processEnabled) return {};
    return { processSummaries: { ...state.processSummaries, [streamId]: {
      node,
      status: event.status === 'done' ? 'done' : event.status === 'error' ? 'error' : 'running',
      summary: String(event.summary || ''),
      elapsedMs: Math.max(0, Number(event.elapsed_ms) || 0),
    } } };
  }
  if (!['analyze_result', 'llm_direct_answer'].includes(node)) return {};
  if (event.type === 'answer_reset') {
    return { activeStreamId: streamId, tokens: '', streamBuffers: {
      ...state.streamBuffers, [streamId]: { node, tokens: '' },
    } };
  }
  if (event.type !== 'answer_delta' || typeof event.content !== 'string') return {};
  const tokens = (state.streamBuffers[streamId]?.tokens || '') + event.content;
  return {
    streamBuffers: { ...state.streamBuffers, [streamId]: { node, tokens } },
    ...(state.activeStreamId === streamId || !state.activeStreamId ? { tokens, activeStreamId: streamId } : {}),
  };
}
