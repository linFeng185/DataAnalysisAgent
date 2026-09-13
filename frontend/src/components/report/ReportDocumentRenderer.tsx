import { t, useLanguage } from '../../i18n';
import { lazy, Suspense, type CSSProperties, type ReactNode } from 'react';
import { Alert, Button, Card, Empty, List, Space, Statistic, Table, Tag, Typography } from 'antd';
import type { EChartsOption } from 'echarts';
import type { ReportBlock, ReportDocument, ReportDrilldownDefinition } from '../../types/report';

const ReactECharts = lazy(() => import('../EChartsView'));

interface ReportDocumentRendererProps {
  document: ReportDocument;
  dataManifest: Record<string, Record<string, unknown>[]>;
  onDrilldown?: (definition: ReportDrilldownDefinition, filters: Record<string, unknown>) => void;
}
// 方法作用：把未知页面内容转换为不执行 HTML 的可见文本。
// Args: value - DSL 内容中的未知值。
// Returns: 可安全展示的文本。
function displayText(value: unknown): string {
  if (value === null || value === undefined || value === '') return '—';
  if (typeof value === 'string' || typeof value === 'number' || typeof value === 'boolean') return String(value);
  try {
    return JSON.stringify(value);
  } catch {
    return '—';
  }
}

// 方法作用：读取页面块引用的有界脱敏行。
// Args: block - 当前页面块；manifest - 服务端签发的数据清单。
// Returns: 可渲染的数据行数组。
function rowsForBlock(block: ReportBlock, manifest: Record<string, Record<string, unknown>[]>): Record<string, unknown>[] {
  const rows = manifest[block.data_ref];
  if (Array.isArray(rows)) return rows.slice(0, 100);
  const embedded = block.content.rows;
  return Array.isArray(embedded) ? embedded.filter((item): item is Record<string, unknown> => Boolean(item && typeof item === 'object')).slice(0, 100) : [];
}

// 方法作用：根据页面块和清单推导表格列，保持列顺序稳定。
// Args: block - 当前页面块；rows - 当前块数据行。
// Returns: Ant Design 表格列定义。
function tableColumns(block: ReportBlock, rows: Record<string, unknown>[]): Array<{ title: string; dataIndex: string; key: string; ellipsis: boolean; render: (value: unknown) => string }> {
  const contentColumns = Array.isArray(block.content.columns) ? block.content.columns.filter((item): item is string => typeof item === 'string') : [];
  const inferred = rows.flatMap(row => Object.keys(row));
  const names = Array.from(new Set([...contentColumns, ...inferred])).slice(0, 50);
  return names.map(name => ({
    title: name,
    dataIndex: name,
    key: name,
    ellipsis: true,
    render: (value: unknown) => displayText(value),
  }));
}

// 方法作用：把页面块的布局意图映射为 CSS Grid 响应式变量。
// Args: block - 当前页面块。
// Returns: 带自定义 CSS 变量的样式对象。
function blockStyle(block: ReportBlock): CSSProperties {
  return {
    minWidth: 0,
    '--report-desktop-span': String(Math.max(1, Math.min(12, block.layout.desktop.col_span))),
    '--report-tablet-span': String(Math.max(1, Math.min(8, block.layout.tablet.col_span))),
    '--report-mobile-span': String(Math.max(1, Math.min(4, block.layout.mobile.col_span))),
  } as CSSProperties;
}

// 方法作用：从图表点击参数中构造服务端签发的过滤条件。
// Args: definition - 页面中的 Drilldown 定义；params - ECharts 点击参数。
// Returns: 只包含首个已签发字段的过滤字典。
function chartFilters(definition: ReportDrilldownDefinition, params: unknown): Record<string, unknown> {
  const field = definition.allowed_filters[0]?.field;
  if (!field || !params || typeof params !== 'object') return {};
  const name = (params as { name?: unknown }).name;
  return name === undefined || name === null || name === '' ? {} : { [field]: name };
}

// 方法作用：渲染单个报表页面块，并将图表交互限制在服务端定义内。
// Args: block - 页面块；document - 当前报表文档；manifest - 脱敏数据清单；onDrilldown - Drilldown 回调。
// Returns: 页面块 React 节点。
function renderBlock(
  block: ReportBlock,
  document: ReportDocument,
  manifest: Record<string, Record<string, unknown>[]>,
  onDrilldown?: (definition: ReportDrilldownDefinition, filters: Record<string, unknown>) => void,
): ReactNode {
  const rows = rowsForBlock(block, manifest);
  const content = block.content || {};
  const definition = document.drilldowns.find(item => item.drilldown_id === content.drilldown_id);
  const liveDefinition = document.drilldowns.find(item => item.mode === 'live' && item.source_block_id === block.id);
  let body: ReactNode;
  switch (block.type) {
    case 'kpi':
      body = <Statistic value={content.value as string | number | undefined} suffix={typeof content.unit === 'string' ? content.unit : undefined} />;
      break;
    case 'chart':
      body = block.echarts_option ? (
        <Suspense fallback={<Typography.Text type="secondary">{t("图表加载中...")}</Typography.Text>}>
          <ReactECharts
            option={block.echarts_option as EChartsOption}
            notMerge
            style={{ height: 320, width: '100%' }}
            onEvents={definition && onDrilldown ? {
              click: (params: unknown) => onDrilldown(definition, chartFilters(definition, params)),
            } : undefined}
          />
        </Suspense>
      ) : <Empty description={t("暂无图表配置")} />;
      break;
    case 'table': {
      const columns = tableColumns(block, rows);
      body = rows.length > 0 ? (
        <Table<Record<string, unknown>>
          size="small"
          dataSource={rows}
          columns={columns}
          pagination={rows.length > 20 ? { pageSize: 20, showSizeChanger: true } : false}
          scroll={{ x: 'max-content' }}
          rowKey={(_, index) => String(index)}
        />
      ) : <Empty description={t("暂无明细数据")} />;
      break;
    }
    case 'insight_list': {
      const items = Array.isArray(content.items) ? content.items : [];
      body = items.length > 0 ? <List size="small" dataSource={items} renderItem={item => <List.Item>{displayText(item)}</List.Item>} /> : <Empty description={t("暂无洞察")} />;
      break;
    }
    case 'relationship':
      body = <div className="report-relationship-content">
        <Tag color={content.evidence_level === 'verified_relation' ? 'green' : 'gold'}>{displayText(content.evidence_level)}</Tag>
        <Typography.Paragraph>{t("关系类型：")}{displayText(content.relation_type)}</Typography.Paragraph>
        <Typography.Paragraph>{t("连接字段：")}{displayText(content.join_keys)}</Typography.Paragraph>
        <Typography.Paragraph>{t("匹配率：")}{content.match_rate === null || content.match_rate === undefined ? '—' : `${(Number(content.match_rate) * 100).toFixed(1)}%`}</Typography.Paragraph>
        <Typography.Paragraph type="secondary">{t("限制：")}{displayText(content.limitations)}</Typography.Paragraph>
      </div>;
      break;
    case 'quality':
      body = <Alert type="warning" showIcon message={displayText(content.limitations || content.message)} />;
      break;
    case 'callout':
      body = <Alert type={content.status === 'failed' ? 'error' : 'info'} showIcon message={displayText(content.message || content.status)} />;
      break;
    case 'source_citation': {
      const citations = Array.isArray(content.items)
        ? content.items.filter((item): item is Record<string, unknown> => Boolean(item && typeof item === 'object'))
        : [];
      body = citations.length > 0 ? <div className="report-citation-content">
        <Typography.Paragraph type="secondary">{displayText(content.summary)}</Typography.Paragraph>
        <List
          size="small"
          dataSource={citations}
          renderItem={item => <List.Item>
            <Space direction="vertical" size={2}>
              <Typography.Text strong>
                {displayText(item.name || item.code)}{typeof item.version === 'number' ? ` v${item.version}` : ''}
              </Typography.Text>
              {item.expression ? <Typography.Text code>{displayText(item.expression)}</Typography.Text> : null}
            </Space>
          </List.Item>}
        />
      </div> : <Typography.Paragraph type="secondary">{displayText(content.source || content.sources || content.summary)}</Typography.Paragraph>;
      break;
    }
    case 'narrative':
    default:
      body = <Typography.Paragraph style={{ whiteSpace: 'pre-wrap', overflowWrap: 'anywhere' }}>{displayText(content.summary || content.text || content.message)}</Typography.Paragraph>;
      break;
  }
  return <div key={block.id} className={`report-block report-block-${block.type}`} style={blockStyle(block)}>
    <Card size="small" title={block.title || undefined} bordered>
      {body}
      {liveDefinition && onDrilldown && <Button type="link" onClick={() => onDrilldown(liveDefinition, {})}>{t("实时明细（重新查询）")}</Button>}
    </Card>
  </div>;
}

// 方法作用：渲染后端签发的动态报表文档和响应式区段。
// Args: document - 已通过后端 DSL 校验的报表文档；dataManifest - 脱敏数据清单；onDrilldown - 可选 Drilldown 回调。
// Returns: 报表页面 React 节点。
export default function ReportDocumentRenderer({ document, dataManifest, onDrilldown }: ReportDocumentRendererProps) {
  useLanguage();
  return <div className="report-document">
    <header className="report-document-header">
      <Typography.Title level={2}>{document.title}</Typography.Title>
      {document.subtitle && <Typography.Paragraph type="secondary">{document.subtitle}</Typography.Paragraph>}
      <Typography.Text type="secondary">
        {t("生成于")} {new Date(document.generated_at).toLocaleString()} {t("· 版本")} {document.version}
      </Typography.Text>
    </header>
    {document.limitations.length > 0 && <Alert className="report-document-limitations" type="warning" showIcon message={t("报表限制")} description={<List size="small" dataSource={document.limitations} renderItem={item => <List.Item>{item}</List.Item>} />} />}
    {document.sections.map(section => <section key={section.id} id={`report-section-${section.id}`} className="report-section">
      <div className="report-section-heading">
        <Typography.Title level={3}>{section.title}</Typography.Title>
        {section.subtitle && <Typography.Text type="secondary">{section.subtitle}</Typography.Text>}
      </div>
      <div className="report-section-grid">
        {[...section.blocks].sort((left, right) => right.priority - left.priority).map(block => renderBlock(block, document, dataManifest, onDrilldown))}
      </div>
    </section>)}
  </div>;
}
