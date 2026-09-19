import { t, useLanguage } from '../../i18n';
import { lazy, Suspense, useMemo, useState, type CSSProperties, type ReactNode } from 'react';
import { Alert, Button, Empty, List, Segmented, Space, Statistic, Table, Tag, Typography } from 'antd';
import type { EChartsOption } from 'echarts';
import type { ReportBlock, ReportDocument, ReportDrilldownDefinition } from '../../types/report';
import { densityMetrics, resolveTemplate, type ReportTemplateTokens } from '../../report/templates';
import { withChartBaseline } from './chartBase';
import { resolveChartGroups } from './chartGroups';
import { EMPTY_TEXT, joinTextList, toTextList } from './reportContent';
import { useReportTemplate } from '../../hooks/useReportTemplate';
import ReportBlockPair from './ReportBlockPair';
import ReportHero, { HERO_KPI_LIMIT } from './ReportHero';

const ReactECharts = lazy(() => import('../EChartsView'));

interface ReportDocumentRendererProps {
  document: ReportDocument;
  dataManifest: Record<string, Record<string, unknown>[]>;
  onDrilldown?: (definition: ReportDrilldownDefinition, filters: Record<string, unknown>) => void;
  /** 可选：由阅读页控制的模板 id。不传时使用文档记录的模板。 */
  templateId?: string;
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
  // 后端按「列名（备注或字段说明）」下发显示标题；拿不到时退回原始列名。
  const labels = block.content.column_labels && typeof block.content.column_labels === 'object' && !Array.isArray(block.content.column_labels)
    ? block.content.column_labels as Record<string, unknown>
    : {};
  return names.map(name => ({
    title: typeof labels[name] === 'string' && labels[name] ? labels[name] as string : name,
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

// 方法作用：渲染图表块，并在服务端给出候选列时提供维度/指标切换。
// Args: block - 图表块；rows - 该块引用的脱敏行；tokens - 模板样式令牌；
//       definition - 受控钻取定义；onDrilldown - 钻取回调。
// Returns: 图表块 React 节点。
function ChartBlock({
  block,
  rows,
  tokens,
  definition,
  onDrilldown,
}: {
  block: ReportBlock;
  rows: Record<string, unknown>[];
  tokens: ReportTemplateTokens;
  definition: ReportDrilldownDefinition | undefined;
  onDrilldown: ((definition: ReportDrilldownDefinition, filters: Record<string, unknown>) => void) | undefined;
}): ReactNode {
  // 服务端把每种分组的系列都算好了：切换只换 series，不重算、不请求后端，
  // 因此不会再出现"点了标签数据清空"——数据一直都在页面里。
  // 分组选项的过滤与归一（分类轴上的派生比较项、按 base 实际类型修正系列、剔除非法分组、
  // 识别全空类目轴）全部由纯函数 chartGroups.resolveChartGroups 承担，组件只消费结果。
  const [selected, setSelected] = useState<string | null>(null);
  const resolved = useMemo(
    () => resolveChartGroups(block.content, block.echarts_option, selected ?? undefined),
    [block.content, block.echarts_option, selected],
  );
  if (resolved.axisIssue) {
    // 旧快照把类目聚合丢成了 ['']（chart_price_promotion_1/2、chart_procurement_inventory_1/2）。
    // 继续画空标签柱图会让用户以为 UI 少了内容，这里如实告知只能重新生成报表版本。
    return <Alert type="warning" showIcon message={t("图表分类轴缺少可用标签，请重新生成报表版本")} />;
  }
  const option = resolved.option as EChartsOption | null;
  if (!option) return <Empty description={t("暂无图表配置")} />;
  return (
    <Space direction="vertical" style={{ width: '100%' }} size={4}>
      {resolved.groups.length > 1 && (
        // 分组标签是完整业务名（如「订单数 - 地区：华南」），Segmented 的 min-content
        // 宽度会把整页撑宽：实机 390px 下 documentElement.scrollWidth=646 > clientWidth=390，
        // 最大溢出源就是这个控件。wrapper 让它在自己容器内横向滚动，控件本身仍完整
        // 可见可点（不隐藏、不截断），整页宽度不受其内容长度影响。
        <div className="report-chart-group-switch">
          <Segmented
            size="small"
            value={resolved.activeGroup}
            options={resolved.groups.map(field => ({ label: resolved.labels[field] || field, value: field }))}
            onChange={value => setSelected(String(value))}
          />
        </div>
      )}
      <Suspense fallback={<Typography.Text type="secondary">{t("图表加载中...")}</Typography.Text>}>
        <ReactECharts
          option={withChartBaseline(option, tokens) as EChartsOption}
          notMerge
          style={{ height: densityMetrics(tokens).chartHeight, width: '100%' }}
          onEvents={definition && onDrilldown ? {
            click: (params: unknown) => onDrilldown(definition, chartFilters(definition, params)),
          } : undefined}
        />
      </Suspense>
    </Space>
  );
}

// 方法作用：渲染单个报表页面块，并将图表交互限制在服务端定义内。
// Args: block - 页面块；document - 当前报表文档；manifest - 脱敏数据清单；onDrilldown - Drilldown 回调。
// Returns: 页面块 React 节点。
function renderBlock(
  block: ReportBlock,
  document: ReportDocument,
  manifest: Record<string, Record<string, unknown>[]>,
  onDrilldown: ((definition: ReportDrilldownDefinition, filters: Record<string, unknown>) => void) | undefined,
  tokens: ReportTemplateTokens,
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
      body = (
        <ChartBlock
          block={block}
          rows={rows}
          tokens={tokens}
          definition={definition}
          onDrilldown={onDrilldown}
        />
      );
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
        {content.cross_source ? <Tag color="blue">{t("跨源分布级")}</Tag> : null}
        <Typography.Paragraph>{t("关系类型：")}{displayText(content.relation_type)}</Typography.Paragraph>
        <Typography.Paragraph>{t("连接字段：")}{joinTextList(content.join_keys, '、')}</Typography.Paragraph>
        <Typography.Paragraph>{t("匹配率：")}{content.match_rate === null || content.match_rate === undefined ? '—' : `${(Number(content.match_rate) * 100).toFixed(1)}%`}</Typography.Paragraph>
        <Typography.Paragraph>{t("方向与强度：")}{displayText(content.direction)} / {displayText(content.strength)}</Typography.Paragraph>
        <Typography.Paragraph type="secondary">{t("限制：")}{joinTextList(content.limitations, '；')}</Typography.Paragraph>
      </div>;
      break;
    case 'quality': {
      // 真实分享页取证：content.limitations 是 string[]，原先经 displayText 兜底会被
      // JSON.stringify 成 `[限制一,限制二]` 整段显示。现按逐条列表渲染；条目本身仍是
      // 纯文本 React 节点，不引入 HTML 注入面。数组为空时退回 content.message。
      const limitationItems = toTextList(content.limitations);
      const items = limitationItems.length > 0 ? limitationItems : toTextList(content.message);
      if (items.length === 0) {
        body = <Alert type="warning" showIcon message={EMPTY_TEXT} />;
      } else if (items.length === 1) {
        body = <Alert type="warning" showIcon message={items[0]} />;
      } else {
        body = <Alert
          type="warning"
          showIcon
          description={<List size="small" dataSource={items} renderItem={item => <List.Item>{item}</List.Item>} />}
        />;
      }
      break;
    }
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
    case 'findings': {
      const items = Array.isArray(content.items)
        ? content.items.filter((item): item is Record<string, unknown> => Boolean(item && typeof item === 'object'))
        : [];
      body = items.length > 0 ? <div className="report-findings-content">
        <List
          size="small"
          dataSource={items}
          renderItem={item => <List.Item>
            <Space direction="vertical" size={2}>
              <Typography.Text>
                {displayText(item.label)}
                {item.value === null || item.value === undefined ? '' : `：${displayText(item.value)}`}
                {item.change_ratio === null || item.change_ratio === undefined ? '' : `（${t("变化")} ${displayText(item.change_ratio)}%）`}
              </Typography.Text>
              {item.baseline ? <Typography.Text type="secondary">{displayText(item.baseline)}</Typography.Text> : null}
              {Array.isArray(item.caveats) && item.caveats.length > 0
                ? <Typography.Text type="warning">{item.caveats.map(value => displayText(value)).join('；')}</Typography.Text>
                : null}
            </Space>
          </List.Item>}
        />
        <Typography.Paragraph type="secondary" className="report-findings-method" style={{ marginTop: 8, marginBottom: 0 }}>
          {t("计算方法：")}{displayText(content.method)}
        </Typography.Paragraph>
        {Array.isArray(content.limitations) && content.limitations.length > 0
          ? <Typography.Paragraph type="warning" style={{ marginBottom: 0 }}>{content.limitations.map(value => displayText(value)).join('；')}</Typography.Paragraph>
          : null}
      </div> : <Empty description={t("暂无深挖发现")} />;
      break;
    }
    case 'narrative':
    default:
      body = <Typography.Paragraph style={{ whiteSpace: 'pre-wrap', overflowWrap: 'anywhere' }}>{displayText(content.summary || content.text || content.message)}</Typography.Paragraph>;
      break;
  }
  return <div key={block.id} className={`report-block report-block-${block.type}`} style={blockStyle(block)}>
    {block.title ? <Typography.Title className="report-block-title" level={4}>{block.title}</Typography.Title> : null}
    <div className="report-block-content">
      {body}
      {liveDefinition && onDrilldown && <Button type="link" onClick={() => onDrilldown(liveDefinition, {})}>{t("实时明细（重新查询）")}</Button>}
    </div>
  </div>;
}

// 方法作用：渲染一个区段并在首张图与叙事并存时应用显式图文配对。
// Args: blocks - 已移除 hero KPI 的区段块；document - 当前文档；manifest - 数据清单；
//       onDrilldown - 受控钻取回调；tokens - 模板令牌。
// Returns: 保持优先级顺序且不重复渲染的区段节点列表。
function renderSectionBlocks(
  blocks: ReportBlock[],
  document: ReportDocument,
  manifest: Record<string, Record<string, unknown>[]>,
  onDrilldown: ((definition: ReportDrilldownDefinition, filters: Record<string, unknown>) => void) | undefined,
  tokens: ReportTemplateTokens,
): ReactNode[] {
  const sorted = [...blocks].sort((left, right) => right.priority - left.priority);
  const chart = sorted.find(block => block.type === 'chart');
  const narrative = sorted.find(block => block.type === 'narrative');
  if (!chart || !narrative) {
    return sorted.map(block => renderBlock(block, document, manifest, onDrilldown, tokens));
  }
  const pairedIds = new Set([chart.id, narrative.id]);
  const pair = <ReportBlockPair
    key={`pair-${chart.id}-${narrative.id}`}
    chartType={typeof chart.content.chart_type === 'string' ? chart.content.chart_type : ''}
    chart={renderBlock(chart, document, manifest, onDrilldown, tokens)}
    narrative={renderBlock(narrative, document, manifest, onDrilldown, tokens)}
  />;
  const firstPairIndex = Math.min(sorted.indexOf(chart), sorted.indexOf(narrative));
  const rendered: ReactNode[] = [];
  sorted.forEach((block, index) => {
    if (index === firstPairIndex) rendered.push(pair);
    if (!pairedIds.has(block.id)) rendered.push(renderBlock(block, document, manifest, onDrilldown, tokens));
  });
  return rendered;
}

// 方法作用：渲染后端签发的动态报表文档和响应式区段。
// Args: document - 已通过后端 DSL 校验的报表文档；dataManifest - 脱敏数据清单；onDrilldown - 可选 Drilldown 回调。
// Returns: 报表页面 React 节点，需由 ReportPageShell 包裹以获得整页底色与模板主题。
export default function ReportDocumentRenderer({ document, dataManifest, onDrilldown, templateId }: ReportDocumentRendererProps) {
  useLanguage();
  const [recordedTemplateId] = useReportTemplate(document);
  const activeTemplateId = templateId || recordedTemplateId;
  const tokens = resolveTemplate(activeTemplateId).tokens;
  const heroKpis = useMemo(
    () => document.sections.flatMap(section => section.blocks).filter(block => block.type === 'kpi').slice(0, HERO_KPI_LIMIT),
    [document.sections],
  );
  const promotedKpiIds = useMemo(() => new Set(heroKpis.map(block => block.id)), [heroKpis]);
  // 页面级背景、限宽与模板作用域统一由 ReportPageShell 承担，这里只负责文档本身的内容与样式。
  return <div className="report-document">
    <ReportHero document={document} kpis={heroKpis} tokens={tokens} />
    {document.limitations.length > 0 && <Alert className="report-document-limitations" type="warning" showIcon message={t("报表限制")} description={<List size="small" dataSource={document.limitations} renderItem={item => <List.Item>{item}</List.Item>} />} />}
    {document.sections.map((section, index) => <section key={section.id} id={`report-section-${section.id}`} className="report-section">
      <div className="report-section-heading">
        <Typography.Text className="report-section-index">{String(index + 1).padStart(2, '0')}</Typography.Text>
        <div>
          <Typography.Title level={3}>{section.title}</Typography.Title>
          {section.subtitle && <Typography.Text type="secondary">{section.subtitle}</Typography.Text>}
        </div>
      </div>
      <div className="report-section-grid">
        {renderSectionBlocks(
          section.blocks.filter(block => !promotedKpiIds.has(block.id)),
          document,
          dataManifest,
          onDrilldown,
          tokens,
        )}
      </div>
    </section>)}
  </div>;
}
