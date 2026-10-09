import { t, useLanguage } from '../i18n';
import { Collapse, Space, Tag, Typography } from 'antd';
import { BulbOutlined } from '@ant-design/icons';
import type { ProcessSummary } from '../hooks/chatPresentation';

// 在管理员允许时按业务节点折叠展示受控过程，不渲染原始推理链。
// Args: enabled - 服务端全局开关；summary - 历史处理摘要；entries - 节点过程条目。
// Returns: 默认折叠的过程面板，关闭时不产生界面。
export default function ReasoningPanel({ enabled = false, summary = '', entries = {} }: {
  enabled?: boolean; summary?: string; entries?: Record<string, ProcessSummary>;
}) {
  useLanguage();
  if (!enabled) return null;
  const groups = new Map<string, ProcessSummary[]>();
  for (const entry of Object.values(entries)) {
    groups.set(entry.node, [...(groups.get(entry.node) || []), entry]);
  }
  if (!summary && !groups.size) return null;
  const items = Array.from(groups, ([node, attempts]) => ({
    key: node,
    label: <Space><Typography.Text>{t(attempts[0].summary)}</Typography.Text>
      <Tag color={attempts.some(entry => entry.status === 'running') ? 'processing' : 'success'}>
        {attempts.some(entry => entry.status === 'running') ? t("进行中") : t("完成")}
      </Tag>{attempts.length > 1 && <Tag>{attempts.length} {t("次执行")}</Tag>}</Space>,
    children: attempts.map((entry, index) => <div key={index} style={{ marginBottom: 6 }}>
      <Typography.Text>{t(entry.summary)}</Typography.Text>
      {entry.elapsedMs > 0 && <Typography.Text type="secondary" style={{ marginLeft: 8 }}>
        {(entry.elapsedMs / 1000).toFixed(1)} s
      </Typography.Text>}
    </div>),
  }));
  return <Collapse size="small" style={{ marginTop: 12 }} items={[{
    key: 'process',
    label: <span><BulbOutlined style={{ marginRight: 6 }} />{t("分析过程摘要")}</span>,
    children: <>{summary && <Typography.Paragraph>{summary}</Typography.Paragraph>}
      {items.length > 0 && <Collapse size="small" items={items} />}</>,
  }]} />;
}
