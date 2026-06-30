import { CheckCircleOutlined, SyncOutlined, CloseCircleOutlined } from '@ant-design/icons';

interface ProgressNode {
  status: 'pending' | 'running' | 'done' | 'error';
  message: string;
}

const NODES: Record<string, string> = {
  classify_intent: '意图识别', retrieve_schema: '检索表结构',
  generate_sql: '生成 SQL', layer3_validate: 'SQL 校验',
  execute_sql: '执行查询', analyze_result: '分析结果',
  generate_chart: '生成图表', build_response: '组装响应',
};

export default function ProgressBar({ nodes }: { nodes: Record<string, ProgressNode> }) {
  const entries = Object.entries(nodes);
  if (entries.length === 0) return null;

  return (
    <div style={{ display: 'flex', gap: 6, flexWrap: 'wrap', marginBottom: 6 }}>
      {entries.map(([name, n]) => {
        const done = n.status === 'done';
        const running = n.status === 'running';
        const error = n.status === 'error';
        const color = done ? '#52c41a' : error ? '#ff4d4f' : running ? '#1677ff' : '#bbb';
        const bg = done ? '#f6ffed' : error ? '#fff2f0' : running ? '#e6f4ff' : '#fafafa';
        return (
          <span key={name} style={{
            display: 'inline-flex', alignItems: 'center', gap: 4,
            padding: '2px 10px', borderRadius: 10, fontSize: 11,
            background: bg, border: `1px solid ${color}33`, color,
            fontWeight: done ? 500 : 400, transition: 'all 0.3s',
          }}>
            {done ? <CheckCircleOutlined style={{ fontSize: 10 }} />
              : error ? <CloseCircleOutlined style={{ fontSize: 10 }} />
              : <SyncOutlined spin={running} style={{ fontSize: 10 }} />}
            <span style={{ whiteSpace: 'nowrap' }}>{NODES[name] || name}</span>
          </span>
        );
      })}
    </div>
  );
}
