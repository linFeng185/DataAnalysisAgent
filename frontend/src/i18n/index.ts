import { useSyncExternalStore } from 'react';
import en from './en';

export type Language = 'zh-CN' | 'en-US';
const storageKey = 'data-agent.language';
const listeners = new Set<() => void>();

// 方法作用：读取浏览器偏好，受限存储或未知语言始终回退中文。
// Args: 无。
// Returns: 已支持的语言代码。
function initialLanguage(): Language {
  try { return localStorage.getItem(storageKey) === 'en-US' ? 'en-US' : 'zh-CN'; }
  catch { return 'zh-CN'; }
}
let language = initialLanguage();

// 方法作用：读取静态界面词典，动态参数保持原样，不翻译用户业务内容。
// Args: source - 中文界面键；values - 命名插值。
// Returns: 当前语言的界面文本。
export function t(source: string, values: Record<string, string | number | undefined | null> = {}): string {
  const template = language === 'en-US' ? en[source] ?? source : source;
  return template.replace(/\{(\w+)\}/g, (match, key: string) => key in values ? String(values[key] ?? '') : match);
}

// 方法作用：保存语言并通知 React，存储禁用不妨碍本次会话切换。
// Args: next - 已支持语言。
// Returns: 无返回值。
export function setLanguage(next: Language): void {
  if (!['zh-CN', 'en-US'].includes(next) || next === language) return;
  language = next;
  try { localStorage.setItem(storageKey, next); } catch { /* 浏览器禁用持久化时只保持会话内偏好。 */ }
  document.documentElement.lang = next;
  listeners.forEach(listener => listener());
}

// 方法作用：订阅界面语言变化并返回注销方法。
// Args: listener - React 重绘通知。
// Returns: 取消订阅函数。
function subscribe(listener: () => void): () => void {
  listeners.add(listener);
  return () => { listeners.delete(listener); };
}

// 方法作用：读取可订阅的语言快照。
// Args: 无。
// Returns: 当前语言。
function snapshot(): Language { return language; }

// 方法作用：为界面组件订阅语言，不要求重新启动服务或浏览器页面。
// Args: 无。
// Returns: 当前支持语言。
export function useLanguage(): Language { return useSyncExternalStore(subscribe, snapshot, snapshot); }

// 方法作用：跨标签页同步同源语言设置。
// Args: event - 存储变化。
// Returns: 无返回值。
function syncStorage(event: StorageEvent): void {
  if (event.key === storageKey) setLanguage(event.newValue === 'en-US' ? 'en-US' : 'zh-CN');
}
window.addEventListener('storage', syncStorage);
document.documentElement.lang = language;
