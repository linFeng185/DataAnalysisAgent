import { useEffect, useRef } from 'react';
import { Button } from 'antd';
import { CopyOutlined } from '@ant-design/icons';
import { format } from 'sql-formatter';
import hljs from 'highlight.js/lib/core';
import sql from 'highlight.js/lib/languages/sql';
import 'highlight.js/styles/github-dark.css';

hljs.registerLanguage('sql', sql);

/** 格式化 SQL 并高亮 */
function formatAndHighlight(code: string): string {
  try {
    const formatted = format(code, { language: 'sql', keywordCase: 'upper' });
    return hljs.highlight(formatted, { language: 'sql' }).value;
  } catch {
    try { return hljs.highlight(code, { language: 'sql' }).value; } catch { return code; }
  }
}

export default function SqlPanel({ sqlCode }: { sqlCode: string }) {
  const codeRef = useRef<HTMLElement>(null);

  useEffect(() => {
    if (codeRef.current) codeRef.current.innerHTML = formatAndHighlight(sqlCode);
  }, [sqlCode]);

  return (
    <div style={{ position: 'relative' }}>
      <div style={{ position: 'absolute', top: 4, right: 4, zIndex: 1 }}>
        <Button size="small" icon={<CopyOutlined />}
          onClick={() => navigator.clipboard.writeText(sqlCode)}>复制</Button>
      </div>
      <pre className="sql-block" style={{ margin: 0, paddingTop: 40, overflow: 'auto' }}>
        <code ref={codeRef} />
      </pre>
    </div>
  );
}
