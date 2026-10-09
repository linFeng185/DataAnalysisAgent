import { getLanguage, t, useLanguage } from '../i18n';
import { lazy, Suspense, useEffect, useMemo, useRef, useState } from 'react';
import { Button, Dropdown, Empty, message, Space, Tooltip, Typography } from 'antd';
import { BarChartOutlined, DownOutlined } from '@ant-design/icons';
import { post } from '../api/client';
import DataTable from './DataTable';
import { buildChatChartLayout } from './chatChartLayout';

const ReactECharts = lazy(() => import('./EChartsView'));

export interface ChartConfig {
  type: string;
  option?: Record<string, unknown>;
  status?: 'success' | 'failed' | 'not_applicable';
  reason?: string;
  data_source?: 'primary' | 'supplemental' | 'attribution';
  chart_plan?: Record<string, unknown>;
  column_labels?: Record<string, string>;
  display_language?: string;
  core_value?: { label: string; value: unknown; column: string };
}

const CHART_TYPES = [
  { key: 'auto', get label() { return t("自动"); } },
  { key: 'line', get label() { return t("折线图"); } },
  { key: 'bar', get label() { return t("柱状图"); } },
  { key: 'pie', get label() { return t("饼图"); } },
  { key: 'scatter', get label() { return t("散点图"); } },
  { key: 'heatmap', get label() { return t("热力图"); } },
  { key: 'table', get label() { return t("表格"); } },
];

// 方法作用：渲染 ECharts 图表并允许基于原始结果切换图表类型。
// Args: chartConfig - 初始配置；rows - 原始查询行；height - 图表高度。
// Returns: 图表、空状态或配置缺失状态。
export default function ChartPanel({ chartConfig, rows = [], height = 360 }: {
  chartConfig: ChartConfig | null | undefined;
  rows?: Record<string, unknown>[];
  height?: number;
}) {
  useLanguage();
  const [current, setCurrent] = useState<ChartConfig | null | undefined>(chartConfig);
  const [adjusting, setAdjusting] = useState(false);
  const chartRef = useRef<HTMLDivElement>(null);
  const [chartWidth, setChartWidth] = useState(0);

  useEffect(() => setCurrent(chartConfig), [chartConfig]);
  useEffect(() => {
    const element = chartRef.current;
    if (!element) return;
    // 观察图表容器而非窗口，侧栏变化、历史回放与分面切换都能重新适配。
    // Args: entries - 容器尺寸通知。
    // Returns: 无返回值，仅在宽度变化时更新状态。
    const updateWidth = (entries: ResizeObserverEntry[]): void => {
      const width = entries[0]?.contentRect.width || element.clientWidth;
      setChartWidth(previous => previous === width ? previous : width);
    };
    setChartWidth(element.clientWidth);
    const observer = new ResizeObserver(updateWidth);
    observer.observe(element);
    return () => observer.disconnect();
  }, [current?.type, Boolean(current?.option)]);
  const displayLanguage = current?.display_language || getLanguage();
  const layout = useMemo(() => buildChatChartLayout(
    current?.option || {}, chartWidth, height, displayLanguage,
  ), [current?.option, displayLanguage, chartWidth, height]);

  // 方法作用：调用后端白名单生成器切换图表类型且复用当前数据。
  // Args: instruction - 菜单选择的图表类型。
  // Returns: 请求结束后无返回值。
  const handleAdjust = async (instruction: string): Promise<void> => {
    if (rows.length === 0) {
      message.warning(t("当前结果没有可用数据"));
      return;
    }
    setAdjusting(true);
    try {
      const adjusted = await post<ChartConfig>('/charts/adjust', {
        rows, instruction, chart_plan: chartConfig?.chart_plan || {},
        column_labels: chartConfig?.column_labels || {},
      });
      setCurrent({ ...adjusted, display_language: chartConfig?.display_language });
    } catch (error) {
      message.error(error instanceof Error ? error.message : t("图表调整失败"));
    } finally {
      setAdjusting(false);
    }
  };

  if (!current) {
    return (
      <div style={{ textAlign: 'center', padding: 40 }}>
        <Empty image={<BarChartOutlined style={{ fontSize: 48, color: '#d9d9d9' }} />}
          description={<Typography.Text type="secondary">{t("暂无图表数据")}</Typography.Text>} />
      </div>
    );
  }

  const toolbar = rows.length > 0 && (
    <div style={{ display: 'flex', justifyContent: 'flex-end', marginBottom: 8 }}>
      <Dropdown
        trigger={['click']}
        menu={{
          items: CHART_TYPES,
          selectedKeys: [current.type],
          onClick: ({ key }) => void handleAdjust(key),
        }}
      >
        <Tooltip title={t("调整图表")}>
          <Button size="small" loading={adjusting} icon={<BarChartOutlined />}>
            <Space size={4}>{CHART_TYPES.find(item => item.key === current.type)?.label || current.type}<DownOutlined /></Space>
          </Button>
        </Tooltip>
      </Dropdown>
    </div>
  );

  if (current.type === 'table') {
    return <div>{toolbar}<DataTable data={rows} columnLabels={current.column_labels} /></div>;
  }
  if (!current.option || Object.keys(current.option).length === 0) {
    return (
      <div>
        {toolbar}
        <div className="chart-container" style={{
          height, display: 'flex', alignItems: 'center', justifyContent: 'center',
          background: '#fafafa', borderRadius: 8,
        }}>
          <div style={{ textAlign: 'center' }}>
            <BarChartOutlined style={{ fontSize: 40, color: '#d9d9d9', marginBottom: 12 }} />
            <Typography.Text type="secondary">
              {current.reason || t("图表类型: {value0}（配置数据未生成）", { value0: current.type })}
            </Typography.Text>
          </div>
        </div>
      </div>
    );
  }

  const chartHeight = layout.height;
  const hasMissing = Array.isArray(current.option.series) && current.option.series.some(
    series => Array.isArray(series?.data) && series.data.some((value: unknown) => value === null),
  );
  return (
    <div>
      {toolbar}
      {current.type === 'line' && hasMissing && <Typography.Paragraph type="secondary" style={{ fontSize: 12 }}>
        {t("缺失值保留为空；折线连接有效数据点，不代表缺失位置为零或已有观测值。")}
      </Typography.Paragraph>}
      <div ref={chartRef} className="chart-container" style={{ height: chartHeight, minWidth: 0 }}>
        <Suspense fallback={(
          <div style={{ height: '100%', display: 'grid', placeItems: 'center' }}>
            <Typography.Text type="secondary">{t("图表加载中...")}</Typography.Text>
          </div>
        )}>
          <ReactECharts option={layout.option}
            style={{ height: '100%' }} notMerge opts={{ renderer: 'canvas' }} />
        </Suspense>
      </div>
    </div>
  );
}
