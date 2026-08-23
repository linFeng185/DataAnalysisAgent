// 方法作用：清理旧版本曾持久化的敏感聊天轮次和会话标识。
// Args: 无。
// Returns: 无返回值。
export function clearChatStorage(): void {
  try {
    const keys: string[] = [];
    for (let index = 0; index < sessionStorage.length; index += 1) {
      const key = sessionStorage.key(index);
      if (!key) continue;
      const legacyChatKey = key.startsWith('chat_state:')
        || key === 'chat_turns'
        || key === 'active_session_id';
      if (legacyChatKey) {
        keys.push(key);
      }
    }
    keys.forEach(key => sessionStorage.removeItem(key));
  } catch {
    // 存储不可用时无需阻断认证或当前内存会话。
  }
}
