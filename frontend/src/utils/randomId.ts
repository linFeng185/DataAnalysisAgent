// 方法作用：生成 RFC 4122 的 UUID v4 字符串，兼容非安全上下文（HTTP + 非 localhost 访问）。
// Args: 无。
// Returns: 标准 UUID v4 字符串。
export function randomId(): string {
  // crypto.randomUUID 只在安全上下文（HTTPS / localhost / 127.0.0.1）下暴露；
  // 通过局域网 IP + HTTP 访问时它是 undefined，直接调用会抛 TypeError 中断整个提交流程。
  if (typeof crypto !== 'undefined' && typeof crypto.randomUUID === 'function') {
    return crypto.randomUUID();
  }
  // crypto.getRandomValues 是 Crypto 接口中唯一不受安全上下文限制的方法，随机性强度与
  // randomUUID 一致，这里用它在本地按 RFC 4122 拼装 UUID v4 作为等价降级。
  const bytes = new Uint8Array(16);
  if (typeof crypto !== 'undefined' && typeof crypto.getRandomValues === 'function') {
    crypto.getRandomValues(bytes);
  } else {
    // 极端兜底：运行环境连 getRandomValues 都不存在时才退化到 Math.random。
    // 该值仅用作请求幂等键，不参与任何安全决策，因此此处的弱随机可接受。
    for (let i = 0; i < bytes.length; i += 1) bytes[i] = Math.floor(Math.random() * 256);
  }
  // 按 RFC 4122 写入版本位（固定为 4）与变体位（固定为 10xx）。
  bytes[6] = (bytes[6] & 0x0f) | 0x40;
  bytes[8] = (bytes[8] & 0x3f) | 0x80;
  const hex = Array.from(bytes, byte => byte.toString(16).padStart(2, '0')).join('');
  return `${hex.slice(0, 8)}-${hex.slice(8, 12)}-${hex.slice(12, 16)}-${hex.slice(16, 20)}-${hex.slice(20)}`;
}
