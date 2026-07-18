import { useState } from 'react';
import { Card, Tag, Typography, Button, Collapse, Space } from 'antd';
import { DownOutlined, RightOutlined, CheckCircleOutlined } from '@ant-design/icons';
import SqlPanel from './SqlPanel';
import DataTable from './DataTable';
import ChartPanel from './ChartPanel';
import type { SQLStatement } from '../types';

export default function ResultCard({ sql, reasoning, tokens, finalResult, validationErrors, onSendMessage }: {
  sql: string; reasoning: string; tokens: string;
  finalResult: Record<string, unknown> | null; validationErrors: string[];
  onSendMessage?: (msg: string) => void;
}) {
  const [expanded, setExpanded] = useState(true);
  const analysis = finalResult?.analysis as Record<string, unknown> | undefined;
  const errorMessage = finalResult?.error_message as string || '';
  const isError = !finalResult?.success && !!errorMessage;
  const needsTimeRange = finalResult?.needs_time_range as boolean;
  const summary = isError ? errorMessage
    : (analysis?.summary as string || tokens?.slice(0, 200) || '分析完成');
  const insights = (analysis?.insights as string[]) || [];
  const chartConfig = finalResult?.chart as { type: string; option?: Record<string, unknown> } | undefined;
  const data = finalResult?.data as Record<string, unknown>[] | undefined;
  const sqlStatements = (finalResult?.sql_statements as SQLStatement[]) || [];
  const effectiveSql = (finalResult?.sql as string) || sql;
  const sqlContent = sqlStatements.length > 0 ? (
    <div>
      {sqlStatements.map((statement, index) => (
        <div key={`${statement.datasource}-${index}`} style={{ marginBottom: index < sqlStatements.length - 1 ? 16 : 0 }}>
          <Typography.Text strong style={{ display: 'block', marginBottom: 6 }}>
            {statement.datasource} ({statement.dialect})
          </Typography.Text>
          <SqlPanel sqlCode={statement.sql} />
        </div>
      ))}
    </div>
  ) : effectiveSql ? <SqlPanel sqlCode={effectiveSql} /> : null;
  const TIME_OPTIONS = [
    { label: '最近一周', query: '最近一周' },
    { label: '最近一月', query: '最近一个月' },
    { label: '最近一年', query: '最近一年' },
    { label: '最近两年', query: '最近两年' },
    { label: '最近三年', query: '最近三年' },
    { label: '最近五年', query: '最近五年' },
    { label: '全部数据', query: '全部数据' },
  ];

  const collapseItems = [
    ...(sqlContent ? [{
      key: 'sql',
      label: sqlStatements.length > 1 ? `执行 SQL (${sqlStatements.length})` : '执行 SQL',
      children: sqlContent,
    }] : []),
    ...(data ? [{ key: 'data', label: `数据 (${data.length} 行)`, children: <DataTable data={data} /> }] : []),
    ...(chartConfig && chartConfig.type !== 'table' ? [{ key: 'chart', label: `图表 (${chartConfig.type})`, children: <ChartPanel chartConfig={chartConfig} /> }] : []),
    ...(reasoning ? [{ key: 'reasoning', label: '思考过程', children: <div className="thinking-block">{reasoning}</div> }] : []),
    ...(tokens ? [{ key: 'raw', label: '完整响应', children: <div style={{ background: '#f5f5f5', padding: 12, borderRadius: 6, whiteSpace: 'pre-wrap', maxHeight: 300, overflow: 'auto', fontSize: 13 }}>{tokens}</div> }] : []),
  ];

  return (
    <Card size="small"
      title={<Space>
        {isError ? (
          <><span>执行出错</span><Tag color="error">失败</Tag></>
        ) : (
          <><span>分析完成</span><Tag icon={<CheckCircleOutlined />} color="success">成功</Tag></>
        )}
        {validationErrors.length > 0 && <Tag color="warning">{validationErrors.length} 个警告</Tag>}
      </Space>}
      extra={<Button type="text" size="small" icon={expanded ? <DownOutlined /> : <RightOutlined />}
        onClick={() => setExpanded(!expanded)}>{expanded ? '收起' : '展开'}</Button>}>
      <Typography.Paragraph style={{ marginBottom: expanded ? 12 : 0 }} ellipsis={!expanded ? { rows: 2 } : false}>
        {summary}
      </Typography.Paragraph>
      {expanded && needsTimeRange && (
        <div style={{ marginBottom: 12 }}>
          <Typography.Text type="secondary" style={{ display: 'block', marginBottom: 8 }}>选择查询时间范围：</Typography.Text>
          <Space wrap>
            {TIME_OPTIONS.map(opt => (
              <Tag key={opt.label} color="blue" style={{ cursor: 'pointer', padding: '2px 10px' }}
                onClick={() => onSendMessage?.(opt.query)}>
                {opt.label}
              </Tag>
            ))}
          </Space>
        </div>
      )}
      {expanded && insights.length > 0 && (
        <div style={{ marginBottom: 12 }}>
          {insights.map((ins: string, i: number) => (
            <Typography.Paragraph key={i} style={{ marginBottom: 4, paddingLeft: 12 }}>• {ins}</Typography.Paragraph>
          ))}
        </div>
      )}
      {expanded && collapseItems.length > 0 && <Collapse size="small" items={collapseItems} style={{ marginTop: 8 }} />}
    </Card>
  );
}
