import * as echarts from 'echarts/core';
import { BarChart, HeatmapChart, LineChart, PieChart, ScatterChart } from 'echarts/charts';
import {
  DatasetComponent, DataZoomComponent, GridComponent, LegendComponent, MarkAreaComponent,
  MarkLineComponent, MarkPointComponent, TitleComponent, ToolboxComponent, TooltipComponent,
  TransformComponent, VisualMapComponent,
} from 'echarts/components';
import { CanvasRenderer } from 'echarts/renderers';
import ReactEChartsCore from 'echarts-for-react/lib/core';
import type { EChartsReactProps } from 'echarts-for-react/lib/types';

echarts.use([
  BarChart, HeatmapChart, LineChart, PieChart, ScatterChart, CanvasRenderer,
  DatasetComponent, DataZoomComponent, GridComponent, LegendComponent, MarkAreaComponent,
  MarkLineComponent, MarkPointComponent, TitleComponent, ToolboxComponent, TooltipComponent,
  TransformComponent, VisualMapComponent,
]);

// 方法作用：只注册项目支持的图表和组件，避免加载 ECharts 全量实现。
// Args: props - 图表配置、交互事件和渲染尺寸。
// Returns: 可交互的 ECharts 图表。
export default function EChartsView(props: EChartsReactProps) {
  return <ReactEChartsCore {...props} echarts={echarts} />;
}
