import { t, useLanguage } from '../i18n';
import { useState } from 'react';
import { Alert, Card, Tag, Typography, Button, Collapse, Space, Statistic, Table } from 'antd';
import {
  CheckCircleOutlined, CheckOutlined, ClockCircleOutlined,
  CloseCircleOutlined, DownOutlined, RightOutlined,
} from '@ant-design/icons';
import SqlPanel from './SqlPanel';
import DataTable from './DataTable';
import type { MaskingMetadata } from '../types';
import ChartPanel from './ChartPanel';
import type {
  ActionRequestPayload, SQLStatement, TouchpointAttributionAnalysis,
} from '../types';

// 方法作用：展示统一分析结果、待补参数状态和外部动作人工确认入口。
// Args: sql/tokens/finalResult/validationErrors - 当前轮次输出；onSendMessage - 补参回调；onConfirmAction - 动作确认回调。
// Returns: 分析结果卡片。
export default function ResultCard({
  sql, tokens, finalResult, validationErrors, onSendMessage, onConfirmAction, streaming = false,
}: {
  sql: string; tokens: string;
  streaming?: boolean;
  finalResult: Record<string, unknown> | null; validationErrors: string[];
  onSendMessage?: (msg: string) => void;
  onConfirmAction?: (request: ActionRequestPayload) => void;
}) {
  useLanguage();
  const [expanded, setExpanded] = useState(true);
  const analysis = finalResult?.analysis as Record<string, unknown> | undefined;
  const masking = finalResult?.masking as MaskingMetadata | undefined;
  const supplementalMasking = finalResult?.supplemental_masking as MaskingMetadata[] | undefined;
  const errorMessage = finalResult?.error_message as string || '';
  const status = String(finalResult?.status || (finalResult?.success ? 'success' : 'failed'));
  const needsTimeRange = finalResult?.needs_time_range as boolean;
  const needsInput = status === 'needs_input' || status === 'needs_confirmation' || needsTimeRange;
  const isError = status === 'failed' || (!finalResult?.success && !needsInput);
  const isSuccessful = !isError && !needsInput;
  const summary = isError ? errorMessage
    : ((streaming ? tokens : '') || analysis?.summary as string || tokens || (streaming ? t("正在生成分析内容...") : t("分析完成")));
  const insights = (analysis?.insights as string[]) || [];
  const indicatorGroups = (analysis?.indicator_groups as Array<{
    theme: string;
    indicators: Array<{ name: string; description: string; table: string; column: string }>;
  }>) || [];
  const attribution = analysis?.attribution as TouchpointAttributionAnalysis | undefined;
  const attributionRows = attribution?.results || [];
  const businessSections = [
    { title: t("关联指标"), items: (analysis?.related_metrics as string[]) || [] },
    { title: t("业务影响"), items: (analysis?.business_impacts as string[]) || [] },
    { title: t("待验证原因"), items: (analysis?.possible_causes as string[]) || [] },
    { title: t("行动建议"), items: (analysis?.recommended_actions as string[]) || [] },
  ].filter(section => section.items.length > 0);
  const renderedReport = (analysis?.rendered_report as string) || '';
  const chartConfig = finalResult?.chart as {
    type: string;
    option?: Record<string, unknown>;
    status?: 'success' | 'failed' | 'not_applicable';
    reason?: string;
    core_value?: { label: string; value: unknown; column: string };
  } | undefined;
  const rawData = finalResult?.data;
  const data = Array.isArray(rawData) ? rawData as Record<string, unknown>[] : [];
  const actionData = rawData && !Array.isArray(rawData)
    ? rawData as Record<string, unknown> : undefined;
  const actionRequest = actionData?.action_request as ActionRequestPayload | undefined;
  const hasData = data.length > 0;
  const chartRows = attributionRows.length > 0
    ? attributionRows as unknown as Record<string, unknown>[]
    : data;
  const hasChartData = chartRows.length > 0;
  const visibleChartTypes = new Set(['line', 'bar', 'pie', 'scatter', 'heatmap']);
  const shouldShowChart = Boolean(
    isSuccessful && hasChartData && chartConfig
    && (visibleChartTypes.has(chartConfig.type) || chartConfig.status === 'failed'),
  );
  const sqlStatements = (finalResult?.sql_statements as SQLStatement[]) || [];
  const effectiveSql = (finalResult?.sql as string) || sql;
  const sqlContent = sqlStatements.length > 0 ? (
    <div>
      {sqlStatements.map((statement, index) => (
        <div key={`${statement.datasource}-${index}`} style={{ marginBottom: index < sqlStatements.length - 1 ? 16 : 0 }}>
          <Typography.Text strong style={{ display: 'block', marginBottom: 6 }}>
            {statement.purpose === 'supplemental' ? t("自主补查") : statement.datasource} ({statement.dialect})
          </Typography.Text>
          {statement.purpose === 'supplemental' && statement.question && (
            <Typography.Text type="secondary" style={{ display: 'block', marginBottom: 6 }}>
              {statement.question}
            </Typography.Text>
          )}
          <SqlPanel sqlCode={statement.sql} />
        </div>
      ))}
    </div>
  ) : effectiveSql ? <SqlPanel sqlCode={effectiveSql} /> : null;
  const TIME_OPTIONS = [
    { label: t("最近一周"), query: '最近一周' },
    { label: t("最近一月"), query: '最近一个月' },
    { label: t("最近一年"), query: '最近一年' },
    { label: t("最近两年"), query: '最近两年' },
    { label: t("最近三年"), query: '最近三年' },
    { label: t("最近五年"), query: '最近五年' },
    { label: t("全部数据"), query: '全部数据' },
  ];

  const collapseItems = [
    ...(sqlContent ? [{
      key: 'sql',
      label: sqlStatements.length > 1 ? t("执行 SQL ({value0})", { value0: sqlStatements.length }) : t("执行 SQL"),
      children: sqlContent,
    }] : []),
    ...(renderedReport ? [{ key: 'report', label: t("报告正文"), children: <Typography.Paragraph style={{ whiteSpace: 'pre-wrap', marginBottom: 0 }}>{renderedReport}</Typography.Paragraph> }] : []),
  ];

  return (
    <Card size="small"
      title={<Space>
        {isError ? (
          <><span>{t("执行出错")}</span><Tag icon={<CloseCircleOutlined />} color="error">{t("失败")}</Tag></>
        ) : needsInput ? (
          <><span>{status === 'needs_confirmation' ? t("等待人工确认") : t("需要补充信息")}</span><Tag icon={<ClockCircleOutlined />} color="processing">{t("待处理")}</Tag></>
        ) : (
          <><span>{streaming ? t("分析中") : t("分析完成")}</span><Tag icon={streaming ? <ClockCircleOutlined /> : <CheckCircleOutlined />} color={streaming ? 'processing' : 'success'}>{streaming ? t("生成中") : t("成功")}</Tag></>
        )}
        {validationErrors.length > 0 && <Tag color="warning">{validationErrors.length} {t("个警告")}</Tag>}
      </Space>}
      extra={<Button type="text" size="small" icon={expanded ? <DownOutlined /> : <RightOutlined />}
        onClick={() => setExpanded(!expanded)}>{expanded ? t("收起") : t("展开")}</Button>}>
      {expanded && (shouldShowChart || chartConfig?.core_value) && <section data-section="chart" style={{ marginBottom: 16 }}>
        {chartConfig?.core_value ? <Statistic title={chartConfig.core_value.label} value={chartConfig.core_value.value as string | number} />
          : <ChartPanel chartConfig={chartConfig} rows={chartRows} />}
      </section>}
      {expanded && hasData && <section data-section="data" style={{ marginBottom: 16 }}>
        <Typography.Text strong style={{ display: 'block', marginBottom: 8 }}>{t("数据详情")} · {data.length} {t("行")}</Typography.Text>
        <DataTable data={data} masking={masking} columnLabels={finalResult?.column_labels as Record<string, string> | undefined} />
        {Boolean(finalResult?.truncated) && <Alert type="info" showIcon message={t("当前展示为截断样本，完整行数：{value0}", { value0: Number(finalResult?.row_count) || data.length })} style={{ marginTop: 8 }} />}
        {supplementalMasking?.some(item => item.applied) && <Tag color="gold">{t("补查数据已脱敏")}</Tag>}
      </section>}
      <section data-section="analysis">
      {expanded && isSuccessful && <Typography.Text strong style={{ display: 'block', marginBottom: 8 }}>{t("分析内容")}</Typography.Text>}
      <Typography.Paragraph style={{ marginBottom: expanded ? 12 : 0, whiteSpace: 'pre-wrap' }} ellipsis={!expanded ? { rows: 2 } : false}>
        {summary}
      </Typography.Paragraph>
      {expanded && needsTimeRange && (
        <div style={{ marginBottom: 12 }}>
          <Typography.Text type="secondary" style={{ display: 'block', marginBottom: 8 }}>{t("选择查询时间范围：")}</Typography.Text>
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
          {t("确认执行")}
        </Button>
      )}
      {expanded && insights.length > 0 && (
        <div style={{ marginBottom: 12 }}>
          {insights.map((ins: string, i: number) => (
            <Typography.Paragraph key={i} style={{ marginBottom: 4, paddingLeft: 12 }}>• {ins}</Typography.Paragraph>
          ))}
        </div>
      )}
      {expanded && chartConfig?.status === 'failed' && chartConfig.reason && (
        <Alert type="warning" showIcon message={t("图表未生成")} description={chartConfig.reason} style={{ marginBottom: 12 }} />
      )}
      {expanded && indicatorGroups.length > 0 && (
        <div style={{ marginBottom: 12 }}>
          {indicatorGroups.map(group => (
            <section key={group.theme} style={{ marginBottom: 16 }}>
              <Space size={8} style={{ marginBottom: 8 }}>
                <Typography.Title level={5} style={{ margin: 0, fontSize: 14 }}>
                  {group.theme}
                </Typography.Title>
                <Tag>{group.indicators.length} {t("项")}</Tag>
              </Space>
              <div style={{
                display: 'grid',
                gridTemplateColumns: 'repeat(auto-fit, minmax(min(100%, 280px), 1fr))',
                gap: 8,
              }}>
                {group.indicators.map(item => (
                  <div key={`${item.table}.${item.column}`} style={{
                    minWidth: 0,
                    padding: '9px 12px',
                    border: '1px solid #e8e8e8',
                    borderLeft: '3px solid #1677ff',
                    borderRadius: 4,
                    background: '#fafafa',
                  }}>
                    <Typography.Text strong style={{ display: 'block', wordBreak: 'break-word' }}>
                      {item.name}
                    </Typography.Text>
                    {item.description && item.description !== item.name && (
                      <Typography.Text type="secondary" style={{ display: 'block', fontSize: 12, marginTop: 2 }}>
                        {item.description}
                      </Typography.Text>
                    )}
                    <Typography.Text code style={{ display: 'block', fontSize: 11, marginTop: 5, wordBreak: 'break-all' }}>
                      {item.table}.{item.column}
                    </Typography.Text>
                  </div>
                ))}
              </div>
            </section>
          ))}
        </div>
      )}
      {expanded && attribution && (
        <section style={{ marginBottom: 16, borderTop: '1px solid #f0f0f0', paddingTop: 12 }}>
          <Space size={8} wrap style={{ marginBottom: 8 }}>
            <Typography.Text strong>{t("触点归因")}</Typography.Text>
            <Tag color={attribution.status === 'applied' ? 'green' : 'warning'}>
              {attribution.model_display_name}
            </Tag>
            <Tag>{({
              user_explicit: t("用户指定"),
              llm_recommended: t("模型推荐"),
              rule_default: t("规则默认"),
            } as const)[attribution.selection_source]}</Tag>
            <Tag>{attribution.dataset_source === 'supplemental' ? t("补查数据") : t("主查询数据")}</Tag>
          </Space>
          <Typography.Paragraph type="secondary" style={{ marginBottom: 8 }}>
            {attribution.selection_reason}
          </Typography.Paragraph>
          {attribution.status === 'insufficient' ? (
            <Alert type="warning" showIcon message={t("归因证据不足")}
              description={(attribution.eligibility?.missing_requirements || []).join('；')} />
          ) : (
            <>
              <Space size={[8, 8]} wrap style={{ marginBottom: 10 }}>
                <Tag>{t("路径")} {attribution.total_paths || 0}</Tag>
                <Tag color="green">{t("已转化")} {attribution.converted_paths || 0}</Tag>
                <Tag>{t("未转化")} {attribution.unconverted_paths || 0}</Tag>
                {attribution.half_life_days != null && (
                  <Tag>{t("半衰期")} {String(attribution.half_life_days)} {t("天")}</Tag>
                )}
              </Space>
              <Table rowKey="touchpoint" size="small" pagination={false}
                dataSource={attributionRows}
                columns={[
                  { title: t("触点"), dataIndex: 'touchpoint' },
                  { title: t("归因价值"), dataIndex: 'attributed_value', align: 'right' },
                  { title: t("归因占比"), dataIndex: 'attribution_pct', align: 'right',
                    render: value => `${value}%` },
                  { title: t("转化信用"), dataIndex: 'conversion_credit', align: 'right' },
                ]} />
            </>
          )}
        </section>
      )}
      {expanded && businessSections.length > 0 && (
        <div style={{
          display: 'grid',
          gridTemplateColumns: 'repeat(auto-fit, minmax(min(100%, 320px), 1fr))',
          gap: 12,
          marginBottom: 12,
        }}>
          {businessSections.map(section => (
            <section key={section.title} style={{ minWidth: 0 }}>
              <Typography.Text strong style={{ display: 'block', marginBottom: 6 }}>
                {section.title}
              </Typography.Text>
              {section.items.map((item, index) => (
                <Typography.Paragraph key={index} style={{ marginBottom: 5, paddingLeft: 12 }}>
                  • {item}
                </Typography.Paragraph>
              ))}
            </section>
          ))}
        </div>
      )}
      {expanded && collapseItems.length > 0 && <Collapse size="small" items={collapseItems} style={{ marginTop: 8 }} />}
      </section>
    </Card>
  );
}
