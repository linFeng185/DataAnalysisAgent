// 报表内容文本归一：把后端 DSL 里的未知值转成可安全展示的纯文本。
//
// 背景（真实分享页取证，非推测）：quality 块的 `content.limitations` 与 relationship 块的
// `join_keys` / `limitations` 都是 string[]，而渲染层原先统一走 `displayText()`——数组会被
// JSON.stringify 成 `[限制一,限制二]` 直接显示，引号、方括号与逗号全部暴露给用户。
// 这里提供「未知值 → 安全 string[]」的唯一归一入口：渲染层按条目渲染，永不出现 JSON 标点。

/** 无内容时展示的破折号，与既有 displayText 的空值语义保持一致。 */
export const EMPTY_TEXT = '—';

// 方法作用：把单个未知值安全转成一行纯文本。
// Args: value - DSL 内容里的单个值（标量 / 对象 / 其它）。
// Returns: 可展示文本；空值、空白字符串与不可序列化值返回 null，表示该条不产出。
function scalarText(value: unknown): string | null {
  if (value === null || value === undefined) return null;
  if (typeof value === 'string') return value.trim() ? value : null;
  // 非有限数值经 JSON.stringify 会变成字符串 "null"，那是假内容，按空值处理。
  if (typeof value === 'number') return Number.isFinite(value) ? String(value) : null;
  if (typeof value === 'boolean') return String(value);
  try {
    const text = JSON.stringify(value);
    // 函数、Symbol 等取不到文本；字面量 "null" 同样视为无内容。
    return text === undefined || text === 'null' ? null : text;
  } catch {
    // 循环引用等不可序列化值：丢弃该条，不让整块渲染抛错。
    return null;
  }
}

// 方法作用：把未知页面值规范成逐条可渲染的安全文本列表。
// Args: value - 数组 / 标量 / 对象等未知值。
// Returns: 字符串数组；空值、空白项与不可序列化项不产生条目，其余保持原顺序。
export function toTextList(value: unknown): string[] {
  if (Array.isArray(value)) {
    return value
      .map(item => scalarText(item))
      .filter((item): item is string => item !== null);
  }
  const single = scalarText(value);
  return single === null ? [] : [single];
}

// 方法作用：把未知页面值规范成单行文本，多条之间用指定分隔符连接。
// Args: value - 数组 / 标量 / 对象等未知值；separator - 条目之间的分隔符（如中文顿号、分号）。
// Returns: 可安全展示的单行文本；无内容时返回破折号。
export function joinTextList(value: unknown, separator: string): string {
  const items = toTextList(value);
  return items.length > 0 ? items.join(separator) : EMPTY_TEXT;
}
