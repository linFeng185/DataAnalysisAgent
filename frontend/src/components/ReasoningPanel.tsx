import { Card } from 'antd';
import { BulbOutlined } from '@ant-design/icons';

export default function ReasoningPanel({ reasoning }: { reasoning: string }) {
  if (!reasoning) return null;
  return (
    <Card size="small" title={<span><BulbOutlined style={{ marginRight: 6 }} />思考过程</span>} style={{ marginBottom: 8 }}>
      <div className="thinking-block">{reasoning}</div>
    </Card>
  );
}
