import type { ReactNode } from 'react';

interface ReportBlockPairProps {
  chartType: string;
  chart: ReactNode;
  narrative: ReactNode;
}

// 方法作用：显式选择图文横向配对或热力图满宽的上下配对。
// Args: chartType - 当前图表类型；chart - 图表节点；narrative - 研判文本节点。
// Returns: 占满区段整行的配对容器。
export default function ReportBlockPair({ chartType, chart, narrative }: ReportBlockPairProps) {
  const stacked = chartType === 'heatmap';
  return <div className={`report-block-pair ${stacked ? 'report-block-pair-stacked' : 'report-block-pair-split'}`}>
    <div className="report-block-pair-chart">{chart}</div>
    <aside className="report-block-pair-narrative">{narrative}</aside>
  </div>;
}
