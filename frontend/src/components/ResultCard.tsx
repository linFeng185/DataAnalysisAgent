import { useState } from 'react';
import { Card, Tag, Typography, Button, Collapse, Space } from 'antd';
import {
  CheckCircleOutlined, CheckOutlined, ClockCircleOutlined,
  CloseCircleOutlined, DownOutlined, RightOutlined,
} from '@ant-design/icons';
import SqlPanel from './SqlPanel';
import DataTable from './DataTable';
import ChartPanel from './ChartPanel';
import type { ActionRequestPayload, SQLStatement } from '../types';

// 方法作用：展示统一分析结果、待补参数状态和外部动作人工确认入口。
// Args: sql/tokens/finalResult/validationErrors - 当前轮次输出；onSendMessage - 补参回调；onConfirmAction - 动作确认回调。
// Returns: 分析结果卡片。
export default function ResultCard({
  sql, tokens, finalResult, validationErrors, onSendMessage, onConfirmAction,
}: {
  sql: string; tokens: string;
  finalResult: Record<string, unknown> | null; validationErrors: string[];
  onSendMessage?: (msg: string) => void;
  onConfirmAction?: (request: ActionRequestPayload) => void;
}) {
  const [expanded, setExpanded] = useState(true);
  const analysis = finalResult?.analysis as Record<string, unknown> | undefined;
  const errorMessage = finalResult?.error_message as string || '';
  const status = String(finalResult?.status || (finalResult?.success ? 'success' : 'failed'));
  const needsTimeRange = finalResult?.needs_time_range as boolean;
  const needsInput = status === 'needs_input' || status === 'needs_confirmation' || needsTimeRange;
  const isError = status === 'failed' || (!finalResult?.success && !needsInput);
  const isSuccessful = !isError && !needsInput;
  const summary = isError ? errorMessage
    : (analysis?.summary as string || tokens?.slice(0, 200) || '分析完成');
  const insights = (analysis?.insights as string[]) || [];
  const renderedReport = (analysis?.rendered_report as string) || '';
  const chartConfig = finalResult?.chart as { type: string; option?: Record<string, unknown> } | undefined;
  const rawData = finalResult?.data;
  const data = Array.isArray(rawData) ? rawData as Record<string, unknown>[] : [];
  const actionData = rawData && !Array.isArray(rawData)
    ? rawData as Record<string, unknown> : undefined;
  const actionRequest = actionData?.action_request as ActionRequestPayload | undefined;
  const hasData = data.length > 0;
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
    ...(hasData ? [{ key: 'data', label: `数据 (${data.length} 行)`, children: <DataTable data={data} /> }] : []),
    ...(isSuccessful && hasData && chartConfig && chartConfig.type !== 'table' ? [{ key: 'chart', label: `图表 (${chartConfig.type})`, children: <ChartPanel chartConfig={chartConfig} rows={data} /> }] : []),
    ...(renderedReport ? [{ key: 'report', label: '报告正文', children: <Typography.Paragraph style={{ whiteSpace: 'pre-wrap', marginBottom: 0 }}>{renderedReport}</Typography.Paragraph> }] : []),
    ...(tokens ? [{ key: 'raw', label: '完整响应', children: <div style={{ background: '#f5f5f5', padding: 12, borderRadius: 6, whiteSpace: 'pre-wrap', maxHeight: 300, overflow: 'auto', fontSize: 13 }}>{tokens}</div> }] : []),
  ];

  return (
    <Card size="small"
      title={<Space>
        {isError ? (
          <><span>执行出错</span><Tag icon={<CloseCircleOutlined />} color="error">失败</Tag></>
        ) : needsInput ? (
          <><span>{status === 'needs_confirmation' ? '等待人工确认' : '需要补充信息'}</span><Tag icon={<ClockCircleOutlined />} color="processing">待处理</Tag></>
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
      {expanded && status === 'needs_confirmation' && actionRequest && (
        <Button
          type="primary"
          icon={<CheckOutlined />}
          onClick={() => onConfirmAction?.({ ...actionRequest, confirmed: true })}
          style={{ marginBottom: 12 }}
        >
          确认执行
        </Button>
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
