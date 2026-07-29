import { useEffect, useState } from 'react';
import { Button, Dropdown, Empty, message, Space, Tooltip, Typography } from 'antd';
import { BarChartOutlined, DownOutlined } from '@ant-design/icons';
import ReactECharts from 'echarts-for-react';
import { post } from '../api/client';

export interface ChartConfig {
  type: string;
  option?: Record<string, unknown>;
}

const CHART_TYPES = [
  { key: 'auto', label: '自动' },
  { key: 'line', label: '折线图' },
  { key: 'bar', label: '柱状图' },
  { key: 'pie', label: '饼图' },
  { key: 'scatter', label: '散点图' },
  { key: 'heatmap', label: '热力图' },
  { key: 'table', label: '表格' },
];

// 方法作用：渲染 ECharts 图表并允许基于原始结果切换图表类型。
// Args: chartConfig - 初始配置；rows - 原始查询行；height - 图表高度。
// Returns: 图表、空状态或配置缺失状态。
export default function ChartPanel({ chartConfig, rows = [], height = 360 }: {
  chartConfig: ChartConfig | null | undefined;
  rows?: Record<string, unknown>[];
  height?: number;
}) {
  const [current, setCurrent] = useState<ChartConfig | null | undefined>(chartConfig);
  const [adjusting, setAdjusting] = useState(false);

  useEffect(() => setCurrent(chartConfig), [chartConfig]);

  // 方法作用：调用后端白名单生成器切换图表类型且复用当前数据。
  // Args: instruction - 菜单选择的图表类型。
  // Returns: 请求结束后无返回值。
  const handleAdjust = async (instruction: string): Promise<void> => {
    if (rows.length === 0) {
      message.warning('当前结果没有可用数据');
      return;
    }
    setAdjusting(true);
    try {
      const adjusted = await post<ChartConfig>('/charts/adjust', { rows, instruction });
      setCurrent(adjusted);
    } catch (error) {
      message.error(error instanceof Error ? error.message : '图表调整失败');
    } finally {
      setAdjusting(false);
    }
  };

  if (!current) {
    return (
      <div style={{ textAlign: 'center', padding: 40 }}>
        <Empty image={<BarChartOutlined style={{ fontSize: 48, color: '#d9d9d9' }} />}
          description={<Typography.Text type="secondary">暂无图表数据</Typography.Text>} />
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
        <Tooltip title="调整图表">
          <Button size="small" loading={adjusting} icon={<BarChartOutlined />}>
            <Space size={4}>{CHART_TYPES.find(item => item.key === current.type)?.label || current.type}<DownOutlined /></Space>
          </Button>
        </Tooltip>
      </Dropdown>
    </div>
  );

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
              图表类型: {current.type}（配置数据未生成）
            </Typography.Text>
          </div>
        </div>
      </div>
    );
  }

  return (
    <div>
      {toolbar}
      <div className="chart-container" style={{ height }}>
        <ReactECharts option={current.option}
          style={{ height: '100%' }} notMerge opts={{ renderer: 'canvas' }} />
      </div>
    </div>
  );
}
