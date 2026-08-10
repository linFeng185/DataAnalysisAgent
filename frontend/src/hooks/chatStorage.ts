// 方法作用：根据认证身份生成聊天缓存分区键，阻止不同租户或用户复用会话。
// Args: tenantId - 租户 ID；userId - 用户 ID。
// Returns: 稳定的浏览器存储命名空间。
export function chatStorageNamespace(tenantId?: number, userId?: number): string {
  return tenantId && userId ? `chat_state:${tenantId}:${userId}` : 'chat_state:anonymous';
}

// 方法作用：读取指定身份分区中的聊天轮次和会话 ID。
// Args: namespace - 聊天缓存命名空间。
// Returns: 可恢复状态；缓存缺失或损坏时返回 null。
export function loadChatStorage(namespace: string): { turns: unknown[]; sessionId: string } | null {
  try {
    const raw = sessionStorage.getItem(`${namespace}:turns`);
    const sessionId = sessionStorage.getItem(`${namespace}:session`) || '';
    if (!raw || !sessionId) return null;
    const turns = JSON.parse(raw) as unknown;
    if (!Array.isArray(turns)) return null;
    return { turns, sessionId };
  } catch {
    return null;
  }
}

// 方法作用：持久化指定身份分区中的聊天状态。
// Args: namespace - 聊天缓存命名空间；turns - 轮次列表；sessionId - 会话 ID。
// Returns: 无返回值。
export function saveChatStorage(namespace: string, turns: unknown[], sessionId: string): void {
  try {
    sessionStorage.setItem(`${namespace}:turns`, JSON.stringify(turns));
    sessionStorage.setItem(`${namespace}:session`, sessionId);
  } catch {
    // 浏览器存储配额不足时不影响当前内存会话。
  }
}

// 方法作用：清理所有聊天分区，确保退出登录后下一身份没有残留对话。
// Args: 无。
// Returns: 无返回值。
export function clearChatStorage(namespace?: string): void {
  try {
    const keys: string[] = [];
    for (let index = 0; index < sessionStorage.length; index += 1) {
      const key = sessionStorage.key(index);
      if (!key) continue;
      const scoped = namespace
        ? key === `${namespace}:turns` || key === `${namespace}:session`
        : key.startsWith('chat_state:') || key === 'chat_turns' || key === 'active_session_id';
      if (key && scoped) {
        keys.push(key);
      }
    }
    keys.forEach(key => sessionStorage.removeItem(key));
  } catch {
    // 存储不可用时无需阻断退出登录。
  }
}
