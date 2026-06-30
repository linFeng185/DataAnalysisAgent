import { useEffect, useRef } from 'react';
import { Button } from 'antd';
import { CopyOutlined } from '@ant-design/icons';
import hljs from 'highlight.js/lib/core';
import sql from 'highlight.js/lib/languages/sql';
import 'highlight.js/styles/github-dark.css';

hljs.registerLanguage('sql', sql);

function highlightSQL(code: string): string {
  try { return hljs.highlight(code, { language: 'sql' }).value; } catch { return code; }
}

export default function SqlPanel({ sqlCode }: { sqlCode: string }) {
  const codeRef = useRef<HTMLElement>(null);

  useEffect(() => {
    if (codeRef.current) codeRef.current.innerHTML = highlightSQL(sqlCode);
  }, [sqlCode]);

  return (
    <div style={{ position: 'relative' }}>
      <div style={{ position: 'absolute', top: 4, right: 4, zIndex: 1 }}>
        <Button size="small" icon={<CopyOutlined />}
          onClick={() => navigator.clipboard.writeText(sqlCode)}>复制</Button>
      </div>
      <pre className="sql-block" style={{ margin: 0, paddingTop: 40 }}>
        <code ref={codeRef}>{sqlCode}</code>
      </pre>
    </div>
  );
}
