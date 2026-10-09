type ChartOption = Record<string, unknown>;
type OptionItem = Record<string, unknown>;

// 将可单个或数组形式出现的 ECharts 组件转为可映射的对象列表。
// Args: value - 原始组件；fallback - 组件缺失时的默认对象。
// Returns: 组件对象列表，不修改原配置。
function optionItems(value: unknown, fallback: OptionItem[] = []): OptionItem[] {
  if (Array.isArray(value)) return value as OptionItem[];
  return value && typeof value === 'object' ? [value as OptionItem] : fallback;
}

// 按实际容器宽度调整共用图表布局，不修改数据、分类、缺失值或历史选项。
// Args: option - 服务端配置；width - 容器宽度；baseHeight - 最小高度；language - 结果展示语言。
// Returns: 渲染期配置副本及足够容纳各分面的高度。
export function buildChatChartLayout(option: ChartOption, width: number, baseHeight = 360, language = 'zh-CN'): {
  option: ChartOption; height: number;
} {
  const series = optionItems(option.series);
  if (!series.length) return { option, height: baseHeight };
  const actualWidth = Number.isFinite(width) && width > 0 ? width : 800;
  const compact = actualWidth < 600;
  const titles = optionItems(option.title);
  const axes = optionItems(option.xAxis);
  const verticalAxes = optionItems(option.yAxis);
  const pieOnly = series.every(item => item.type === 'pie');
  const panelCount = Math.max(1, optionItems(option.grid).length, titles.length, axes.length, verticalAxes.length,
    pieOnly ? series.length : 0);
  const panelHeight = compact ? 320 : 300;
  const height = Math.max(baseHeight, panelCount * panelHeight + 40);
  let numberFormat: Intl.NumberFormat;
  try {
    numberFormat = new Intl.NumberFormat(language, { notation: 'compact', maximumFractionDigits: 1 });
  } catch {
    numberFormat = new Intl.NumberFormat('zh-CN', { notation: 'compact', maximumFractionDigits: 1 });
  }

  // 窄屏刻度使用结果语言的紧凑数值，提示框及数据表仍保留精确原值。
  // Args: value - 数值轴标签。
  // Returns: 展示用标签，不修改坐标值。
  const formatValue = (value: number | string): string => {
    const numeric = Number(value);
    return Number.isFinite(numeric) ? numberFormat.format(numeric) : String(value);
  };

  // 在共享组件副本上调整轴位置、刻度与重复名称，保持所有坐标及索引。
  // Args: axis - 原始轴；index - 所属轴索引；vertical - 是否为纵轴。
  // Returns: 独立轴配置。
  const layoutAxis = (axis: OptionItem, index: number, vertical: boolean): OptionItem => {
    const title = String(titles[index]?.text || '');
    const name = String(axis.name || '');
    const duplicateName = vertical && name && (title === name || title.endsWith(` · ${name}`));
    const numericAxis = axis.type === 'value' || axis.type === 'log';
    const labels: OptionItem = {
      ...(axis.axisLabel as OptionItem || {}), hideOverlap: true,
      fontSize: compact ? 10 : 12,
    };
    if (compact && numericAxis && !(axis.axisLabel as OptionItem)?.formatter) labels.formatter = formatValue;
    if (compact && vertical && axis.type === 'category') Object.assign(labels, { width: 76, overflow: 'truncate' });
    return {
      ...axis, name: duplicateName ? '' : name, axisLabel: labels,
      nameLocation: vertical ? 'end' : 'middle', nameGap: vertical ? 12 : 30,
      ...(numericAxis ? { splitNumber: compact ? 3 : 5 } : {}),
    };
  };

  const adjusted: ChartOption = {
    ...option,
    legend: {
      ...optionItems(option.legend)[0], type: 'scroll', bottom: 0, left: 8, right: 8,
      textStyle: { ...(optionItems(option.legend)[0]?.textStyle as OptionItem || {}), fontSize: compact ? 10 : 12,
        width: compact ? Math.max(70, actualWidth / 3) : 220, overflow: 'truncate' },
      tooltip: { show: true },
    },
  };
  if (titles.length) adjusted.title = titles.map((title, index) => ({
    ...title, top: index * panelHeight + 8, left: 'center',
    textStyle: { ...(title.textStyle as OptionItem || {}), fontSize: 12,
      width: Math.max(60, actualWidth - 32), overflow: 'truncate' },
  }));
  if (axes.length || verticalAxes.length) {
    const grids = optionItems(option.grid);
    adjusted.grid = Array.from({ length: panelCount }, (_, index) => ({
      ...grids[index], top: index * panelHeight + 45, height: panelHeight - 115,
      left: compact ? 12 : 30, right: compact ? 16 : 30, containLabel: true,
    }));
    const horizontal = axes.map((axis, index) => layoutAxis(axis, index, false));
    const vertical = verticalAxes.map((axis, index) => layoutAxis(axis, index, true));
    if (axes.length) adjusted.xAxis = Array.isArray(option.xAxis) ? horizontal : horizontal[0];
    if (verticalAxes.length) adjusted.yAxis = Array.isArray(option.yAxis) ? vertical : vertical[0];
  }
  if (option.visualMap) {
    const scales = optionItems(option.visualMap).map(scale => ({
      ...scale, bottom: 34,
      ...(compact ? { itemWidth: 10, itemHeight: Math.max(60, Math.min(150, actualWidth - 110)) } : {}),
    }));
    adjusted.visualMap = Array.isArray(option.visualMap) ? scales : scales[0];
  }
  if (pieOnly) {
    const radius = Math.min(actualWidth * 0.29, (panelHeight - 80) * 0.42);
    adjusted.series = series.map((item, index) => ({
      ...item, center: ['50%', index * panelHeight + panelHeight / 2],
      radius: Array.isArray(item.radius) && item.radius[0] !== 0 ? [radius * 0.35, radius] : [0, radius],
      avoidLabelOverlap: true,
      label: { ...(item.label as OptionItem || {}), fontSize: compact ? 10 : 12,
        width: compact ? 62 : 120, overflow: 'truncate' },
    }));
  }
  return { option: adjusted, height };
}
