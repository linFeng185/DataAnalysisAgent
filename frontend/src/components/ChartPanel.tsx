import { Empty, Typography } from 'antd';
import { BarChartOutlined } from '@ant-design/icons';
import ReactECharts from 'echarts-for-react';

export default function ChartPanel({ chartConfig, height = 360 }: {
  chartConfig: { type: string; option?: Record<string, unknown> } | null | undefined;
  height?: number;
}) {
  if (!chartConfig || !chartConfig.option) {
    return (
      <div style={{ textAlign: 'center', padding: 40 }}>
        <Empty image={<BarChartOutlined style={{ fontSize: 48, color: '#d9d9d9' }} />}
          description={<Typography.Text type="secondary">暂无图表数据</Typography.Text>} />
      </div>
    );
  }
  return (
    <div className="chart-container" style={{ height }}>
      <ReactECharts option={chartConfig.option as Record<string, unknown>}
        style={{ height: '100%' }} notMerge opts={{ renderer: 'canvas' }} />
    </div>
  );
}
