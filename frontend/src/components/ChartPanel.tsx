import { Empty, Typography } from 'antd';
import { BarChartOutlined } from '@ant-design/icons';
import ReactECharts from 'echarts-for-react';

export default function ChartPanel({ chartConfig, height = 360 }: {
  chartConfig: { type: string; option?: Record<string, unknown> } | null | undefined;
  height?: number;
}) {
  if (!chartConfig) {
    return (
      <div style={{ textAlign: 'center', padding: 40 }}>
        <Empty image={<BarChartOutlined style={{ fontSize: 48, color: '#d9d9d9' }} />}
          description={<Typography.Text type="secondary">暂无图表数据</Typography.Text>} />
      </div>
    );
  }

  // 有 type 但无 option：显示提示
  if (!chartConfig.option || Object.keys(chartConfig.option).length === 0) {
    return (
      <div className="chart-container" style={{
        height, display: 'flex', alignItems: 'center', justifyContent: 'center',
        background: '#fafafa', borderRadius: 8,
      }}>
        <div style={{ textAlign: 'center' }}>
          <BarChartOutlined style={{ fontSize: 40, color: '#d9d9d9', marginBottom: 12 }} />
          <Typography.Text type="secondary">
            图表类型: {chartConfig.type}（配置数据未生成）
          </Typography.Text>
        </div>
      </div>
    );
  }

  return (
    <div className="chart-container" style={{ height }}>
      <ReactECharts option={chartConfig.option}
        style={{ height: '100%' }} notMerge opts={{ renderer: 'canvas' }} />
    </div>
  );
}
