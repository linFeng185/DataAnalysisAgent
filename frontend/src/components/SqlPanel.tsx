import { t, useLanguage } from '../i18n';
import { useEffect, useRef } from 'react';
import { Button } from 'antd';
import { CopyOutlined } from '@ant-design/icons';
import { format } from 'sql-formatter';
import hljs from 'highlight.js/lib/core';
import sql from 'highlight.js/lib/languages/sql';
import 'highlight.js/styles/github-dark.css';

hljs.registerLanguage('sql', sql);

const HTML_ESCAPES: Record<string, string> = {
  '&': '&amp;',
  '<': '&lt;',
  '>': '&gt;',
  '"': '&quot;',
  "'": '&#39;',
};

// 方法作用：把用户可控 SQL 转换为只能作为文本渲染的 HTML。
// Args: value - 未经信任的 SQL 原文。
// Returns: 完整转义后的 HTML 文本。
function escapeHtml(value: string): string {
  return value.replace(/[&<>"']/g, character => HTML_ESCAPES[character]);
}

// 方法作用：格式化并高亮 SQL，高亮器异常时返回安全转义文本。
// Args: code - 待展示的 SQL 原文。
// Returns: 仅含 Highlight.js 标记或 HTML 实体的安全片段。
function formatAndHighlight(code: string): string {
  try {
    const formatted = format(code, { language: 'sql', keywordCase: 'upper' });
    return hljs.highlight(formatted, { language: 'sql' }).value;
  } catch {
    try {
      return hljs.highlight(code, { language: 'sql' }).value;
    } catch {
      return escapeHtml(code);
    }
  }
}

// 方法作用：展示带复制操作的安全 SQL 高亮面板。
// Args: sqlCode - 待展示和复制的 SQL。
// Returns: SQL 面板 React 节点。
export default function SqlPanel({ sqlCode }: { sqlCode: string }) {
  useLanguage();
  const codeRef = useRef<HTMLElement>(null);

  useEffect(() => {
    if (codeRef.current) codeRef.current.innerHTML = formatAndHighlight(sqlCode);
  }, [sqlCode]);

  return (
    <div style={{ position: 'relative' }}>
      <div style={{ position: 'absolute', top: 4, right: 4, zIndex: 1 }}>
        <Button size="small" icon={<CopyOutlined />}
          onClick={() => navigator.clipboard.writeText(sqlCode)}>{t("复制")}</Button>
      </div>
      <pre className="sql-block" style={{ margin: 0, paddingTop: 40, overflow: 'auto' }}>
        <code ref={codeRef} className="hljs" />
      </pre>
    </div>
  );
}
