import { Card } from 'antd';
import { BulbOutlined } from '@ant-design/icons';

export default function ReasoningPanel({ summary }: { summary: string }) {
  if (!summary) return null;
  return (
    <Card size="small" title={<span><BulbOutlined style={{ marginRight: 6 }} />处理摘要</span>} style={{ marginBottom: 8 }}>
      <div className="thinking-block">{summary}</div>
    </Card>
  );
}
