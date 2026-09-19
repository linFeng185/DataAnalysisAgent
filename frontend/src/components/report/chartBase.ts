// 图表样式基线：把模板 token 注入后端下发的 ECharts option。
//
// 合并原则是「不覆盖后端意图」——但有三处刻意例外：
// 1. 深色模板下 tooltip/轴颜色强制覆盖：后端是在浅色假设下生成这些颜色的，深色底上会穿帮；
// 2. 纯柱状图的 tooltip 触发语义强制纠偏（见 withTooltipBaseline）：修复前持久化的旧快照
//    把柱图的 trigger 写成 axis，悬停单柱会列出同横轴整批系列，必须按实际 series 纠正；
// 3. 含负值的纯柱图把自动数值轴改成以 0 为对称中心（见 withCenteredZeroAxis）：自动范围在
//    全负数据上退化成 [min, 0]，零线贴到 grid 上沿，柱子从顶部往下垂。修复只改渲染期的
//    轴范围，不动数据、不反转轴，旧快照分享页刷新即生效。

import type { EChartsOption } from 'echarts';
import type { ReportTemplateTokens } from '../../report/templates';

// 方法作用：判断取值是否是可做样式合并的普通对象。
// Args: value - 待判断的值。
// Returns: 是普通对象时返回 true（数组与 null 除外）。
function isPlainObject(value: unknown): value is Record<string, unknown> {
  return Boolean(value) && typeof value === 'object' && !Array.isArray(value);
}

// 方法作用：按背景色亮度判断是否为深色模板。
// Args: surface - 模板的卡片底色。
// Returns: 亮度低于中位时返回 true。
export function isDarkSurface(surface: string): boolean {
  const hex = String(surface || '').replace('#', '');
  if (hex.length !== 6) return false;
  const red = parseInt(hex.slice(0, 2), 16);
  const green = parseInt(hex.slice(2, 4), 16);
  const blue = parseInt(hex.slice(4, 6), 16);
  if ([red, green, blue].some(value => Number.isNaN(value))) return false;
  // ITU-R BT.601 亮度权重
  return (red * 299 + green * 587 + blue * 114) / 1000 < 128;
}

// 方法作用：逐键补齐目标对象缺失的样式，已存在的键默认保持不动。
// Args: target - 后端下发的对象；baseline - 模板基线；force - 是否强制覆盖已存在的键。
// Returns: 合并后的新对象（原对象未被修改）。
function mergeMissing(
  target: Record<string, unknown>,
  baseline: Record<string, unknown>,
  force = false,
): Record<string, unknown> {
  const merged: Record<string, unknown> = { ...target };
  for (const [key, value] of Object.entries(baseline)) {
    if (merged[key] === undefined || force) {
      merged[key] = value;
    } else if (isPlainObject(merged[key]) && isPlainObject(value)) {
      merged[key] = mergeMissing(merged[key] as Record<string, unknown>, value, force);
    }
  }
  return merged;
}

// 图例占位高度：横向图例一行文字约 20px，加默认外边距与网格留白取 40px。
// ECharts 不会自动为图例让出网格空间，不预留就会压在坐标轴与数据上。
const LEGEND_BAND_PX = 40;

// 方法作用：把可用的数值边距取出来，百分比等非数值一律当作「未设置」。
// Args: value - grid 的某个边距取值。
// Returns: 有限数值或 null。
function numericMargin(value: unknown): number | null {
  return typeof value === 'number' && Number.isFinite(value) ? value : null;
}

// 方法作用：按图例的锚定位置为网格预留空间，避免图例压住坐标轴与数据。
// Args: option - 已合并过的 option（就地修改其 grid）。
// Returns: 无返回值。
function reserveLegendSpace(option: Record<string, unknown>): void {
  const legend = option.legend;
  const grid = option.grid;
  if (!isPlainObject(legend) || legend.show === false || !isPlainObject(grid)) return;
  // 纵向图例占的是横向空间，默认落在绘图区右上角。
  if (legend.orient === 'vertical') {
    grid.right = Math.max(numericMargin(grid.right) ?? 0, 160);
    return;
  }
  // 横向图例按锚定方向预留上/下边距。未显式声明位置时按「贴底」处理：
  // 本项目实际运行版本把无位置的横向图例渲染在底部（浏览器实测 getOption() 得到
  // top:null、bottom:15），而基线原先只给 grid.bottom 留 8px，于是图例压在柱子上。
  const anchoredTop = legend.top !== undefined && legend.top !== null && legend.top !== 'auto';
  if (anchoredTop) {
    grid.top = Math.max(numericMargin(grid.top) ?? 0, LEGEND_BAND_PX + 8);
  } else {
    grid.bottom = Math.max(numericMargin(grid.bottom) ?? 0, LEGEND_BAND_PX);
  }
}

// 方法作用：把模板的连续色阶注入 visualMap，兼容单对象与数组写法。
// Args: value - ECharts visualMap 配置；colors - 从低值到高值的模板色阶。
// Returns: 注入 inRange.color 后的新配置，非法项保持原样。
function withSequentialPalette(value: unknown, colors: string[]): unknown {
  if (Array.isArray(value)) {
    return value.map(item => withSequentialPalette(item, colors));
  }
  if (!isPlainObject(value)) return value;
  return mergeMissing(value, { inRange: { color: [...colors] } });
}

// 方法作用：给 line/bar/scatter 系列补齐一致的几何样式，不覆盖后端显式配置。
// Args: value - ECharts series 配置；tokens - 当前报表模板 token。
// Returns: 合并样式后的 series 配置。
function withSeriesBaseline(value: unknown, tokens: ReportTemplateTokens): unknown {
  if (!Array.isArray(value)) return value;
  return value.map(item => {
    if (!isPlainObject(item)) return item;
    const type = String(item.type || '');
    if (type === 'line') {
      return mergeMissing(item, { lineStyle: { width: 2 } });
    }
    if (type === 'bar') {
      return mergeMissing(item, {
        itemStyle: { borderRadius: [4, 4, 0, 0] },
        barGap: '2px',
      });
    }
    if (type === 'scatter') {
      return mergeMissing(item, {
        symbolSize: 8,
        itemStyle: { borderColor: tokens.surface, borderWidth: 2 },
      });
    }
    return item;
  });
}

// 方法作用：依据实际 series 判定是否为「纯柱状图」，用于纠正 tooltip 的触发语义。
// Args: value - option 中实际生效的 series（渲染器已按分组切换替换过）。
// Returns: 非空且每一项都是带非空 type 的对象且 type 严格等于 'bar' 时返回 true，其余一律 false。
function isAllBarSeries(value: unknown): boolean {
  // 空数组、非数组、缺 type、type 为空串、混入非对象项都判不出「是柱图」。
  // 这里宁可返回 false 保持后端原意：猜错类型会静默改变交互语义，比不改更糟。
  if (!Array.isArray(value) || value.length === 0) return false;
  return value.every(item => (
    isPlainObject(item) && typeof item.type === 'string' && item.type === 'bar'
  ));
}

// 数值轴在「含负值纯柱图」下的实际取值范围。
interface BarExtent {
  min: number;
  max: number;
  hasNegative: boolean;
}

// 方法作用：把一个数据点解析成数值，并区分「缺值」与「判不出的结构」。
// Args: point - series.data 的单个元素。
// Returns: 有限数值；null 表示缺值（null/undefined/非有限数）；undefined 表示结构判不出（字符串、数对等）。
function numericPoint(point: unknown): number | null | undefined {
  if (point === null || point === undefined) return null;
  if (typeof point === 'number') return Number.isFinite(point) ? point : null;
  if (!isPlainObject(point)) return undefined;
  const value = point.value;
  if (typeof value === 'number') return Number.isFinite(value) ? value : null;
  if (value === null || value === undefined) return null;
  return undefined;
}

// 方法作用：统计柱系列在数值轴上的真实取值范围，堆叠系列按 ECharts 默认堆叠口径取界。
// Args: series - 已合并样式的 series 数组（只读）。
// Returns: 取值范围的 {min, max, hasNegative}；series 不可读、堆叠策略非默认或取值判不出时返回 null。
function collectBarExtent(series: unknown): BarExtent | null {
  if (!Array.isArray(series) || series.length === 0) return null;
  let min = Number.POSITIVE_INFINITY;
  let max = Number.NEGATIVE_INFINITY;
  let hasNegative = false;
  const stacks = new Map<string, { positive: number[]; negative: number[] }>();
  for (const item of series) {
    if (!isPlainObject(item)) return null;
    // dataset / encode / 缺 data 的系列读不到原值，判不出范围时一律放弃。
    if (!Array.isArray(item.data)) return null;
    const stackName = typeof item.stack === 'string' && item.stack !== '' ? item.stack : '';
    if (stackName && item.stackStrategy !== undefined && item.stackStrategy !== 'samesign') {
      // ECharts 6 的 stackStrategy 默认是 samesign；其余策略（all / positive / negative）的
      // 真实堆叠边界前端推不出来，宁可不动也不能按猜的范围画图。
      return null;
    }
    let bucket = stackName ? stacks.get(stackName) : undefined;
    if (stackName && !bucket) {
      bucket = { positive: [], negative: [] };
      stacks.set(stackName, bucket);
    }
    for (let index = 0; index < item.data.length; index += 1) {
      const value = numericPoint(item.data[index]);
      if (value === undefined) return null;
      // 缺值在图上不落点，也不参与堆叠累计。
      if (value === null) continue;
      if (value < 0) hasNegative = true;
      if (!bucket) {
        // 未声明 stack 的系列按单点取值直接参与取界。
        if (value < min) min = value;
        if (value > max) max = value;
        continue;
      }
      // samesign：正值与负值分别累计，相反符号**不抵消**。
      const side = value < 0 ? bucket.negative : bucket.positive;
      side[index] = (side[index] ?? 0) + value;
    }
  }
  // 堆叠系列真正画出来的是同号逐类目累计后的高度（ECharts 6.1 实测 stackResultDimension
  // 为 [-900, -1800, 900]，负向真实边界是 -1800）。先前按正负代数和取界会低估负向边界，
  // 把第二根负柱截断在轴外（logs/zero-axis-audit-20260919/stacked-bar-audit.json）。
  for (const { positive, negative } of stacks.values()) {
    for (const value of [...positive, ...negative]) {
      if (value === undefined) continue;
      if (value < 0) hasNegative = true;
      if (value < min) min = value;
      if (value > max) max = value;
    }
  }
  if (!Number.isFinite(min) || !Number.isFinite(max)) return null;
  return { min, max, hasNegative };
}

// 方法作用：把正数向上取到 1/2/2.5/5 × 10^k 的整值，保证轴标签与刻度间隔可读。
// Args: value - 数据里最大的绝对取值。
// Returns: 不小于 value 的整值；value 非法时返回 0。
function niceCeil(value: number): number {
  if (!Number.isFinite(value) || value <= 0) return 0;
  const base = 10 ** Math.floor(Math.log10(value));
  const mantissa = value / base;
  // 2.5 档避免把 2400 这类取值粗放扩成 5000，柱子不至于只占半格；刻度仍是 1250 这样的可读数。
  // 浮点误差可能让取整结果略小于原值，此时退一档，避免把柱子截断在轴外。
  if (mantissa <= 1 + 1e-9) return base >= value ? base : 2 * base;
  if (mantissa <= 2 + 1e-9) return 2 * base >= value ? 2 * base : 2.5 * base;
  if (mantissa <= 2.5 + 1e-9) return 2.5 * base >= value ? 2.5 * base : 5 * base;
  if (mantissa <= 5 + 1e-9) return 5 * base >= value ? 5 * base : 10 * base;
  return 10 * base;
}

// 方法作用：确认每个柱系列都绑定在默认（第 0 条）轴上。
// Args: series - series 数组。
// Returns: 未声明轴索引、或显式声明为 0 时返回 true；出现非 0 索引（含非数值）返回 false。
function usesDefaultAxes(series: unknown): boolean {
  if (!Array.isArray(series)) return false;
  for (const item of series) {
    if (!isPlainObject(item)) return false;
    for (const key of ['xAxisIndex', 'yAxisIndex'] as const) {
      const index = item[key];
      if (index === undefined || index === null || index === 0) continue;
      // series 绑在别的轴上时，被改的那条轴未必是它用的那条，判不出就不动。
      return false;
    }
  }
  return true;
}

// 方法作用：在 xAxis / yAxis 中找出唯一一条可以安全改写的数值轴。
// Args: option - 已合并样式的 option。
// Returns: 'xAxis' 或 'yAxis'；形状不是「唯一 value + category」时返回 null（保守不动）。
function resolveValueAxisKey(option: Record<string, unknown>): 'xAxis' | 'yAxis' | null {
  const valueAxes: Array<'xAxis' | 'yAxis'> = [];
  let categoryCount = 0;
  for (const key of ['xAxis', 'yAxis'] as const) {
    const raw = option[key];
    // 多轴数组判不出 series 绑在哪一条上（单元素数组仍可安全改写）。
    if (Array.isArray(raw) && raw.length !== 1) return null;
    const axis = Array.isArray(raw) ? raw[0] : raw;
    if (axis === undefined || axis === null) continue;
    // 轴不是普通对象（字符串等）判不出类型。
    if (!isPlainObject(axis)) return null;
    if (axis.type === 'value') {
      valueAxes.push(key);
      continue;
    }
    if (axis.type === 'category') {
      categoryCount += 1;
      continue;
    }
    // log / time / 未声明 type：与数值轴搭配时判不出 series 用哪条，一律放弃。
    return null;
  }
  // 只认「恰好一条数值轴 + 恰好一条类目轴」这一种能可靠解读的形状。
  // 只看「可改写候选数」是错的：x 是数值轴但显式声明了 min 时，剩下的 y 会被误判成唯一候选。
  if (valueAxes.length !== 1 || categoryCount !== 1) return null;
  const axisKey = valueAxes[0];
  const axis = option[axisKey];
  const target = isPlainObject(axis) ? axis : (axis as unknown[])[0];
  if (!isPlainObject(target)) return null;
  if (target.inverse === true) return null;
  // 后端显式写过的范围与刻度一律尊重：min/max/interval 只要落了一个就不动。
  if (target.min !== undefined && target.min !== null) return null;
  if (target.max !== undefined && target.max !== null) return null;
  if (target.interval !== undefined && target.interval !== null) return null;
  return axisKey;
}

// 方法作用：构造一条锚在数值轴 0 位的静默参考线，让零线比普通网格线略清晰。
// Args: axisKey - 数值轴键名（'xAxis' 或 'yAxis'）；tokens - 当前模板 token。
// Returns: ECharts markLine 配置对象（不显示标签、不参与 tooltip）。
function zeroLine(axisKey: 'xAxis' | 'yAxis', tokens: ReportTemplateTokens): Record<string, unknown> {
  return {
    silent: true,
    symbol: 'none',
    animation: false,
    label: { show: false },
    // 取 textSecondary：比 splitLine 用的 border 明显，又不到正文色那么重，且随主题走。
    lineStyle: { color: tokens.textSecondary, width: 1.5 },
    data: [{ [axisKey]: 0 }],
  };
}

// 方法作用：含负值的纯柱图把自动数值轴改成以 0 为对称中心的 min/max 与刻度间隔，并补 0 参考线。
// Args: option - 已合并样式的 option（只读）；tokens - 当前模板 token。
// Returns: 需要居中时返回新的 option 对象；其余情况原样返回入参。
function withCenteredZeroAxis(
  option: Record<string, unknown>,
  tokens: ReportTemplateTokens,
): Record<string, unknown> {
  if (!isAllBarSeries(option.series)) return option;
  if (!usesDefaultAxes(option.series)) return option;
  const axisKey = resolveValueAxisKey(option);
  if (!axisKey) return option;
  const extent = collectBarExtent(option.series);
  // 全正、全零、无有效数值都不需要居中：自动范围本来就把 0 放在下沿，是正确画法。
  if (!extent || !extent.hasNegative) return option;
  const bound = niceCeil(Math.max(Math.abs(extent.min), Math.abs(extent.max)));
  if (!Number.isFinite(bound) || bound <= 0) return option;
  const raw = option[axisKey];
  const axis = Array.isArray(raw) ? raw[0] : raw;
  if (!isPlainObject(axis)) return option;
  // interval 取上界的一半：刻度固定落在 -bound / -bound÷2 / 0 / bound÷2 / bound。
  const centered: Record<string, unknown> = { ...axis, min: -bound, max: bound, interval: bound / 2 };
  // 对称轴只保证刻度里有一个 0，零线与其它网格线长得一样。给首系列补一条 markLine 才算可见；
  // 首系列若已自带 markLine（后端意图），原样保留，不用零线顶掉它。
  const series = Array.isArray(option.series) ? (option.series as Record<string, unknown>[]) : [];
  const [first, ...rest] = series;
  const withZeroLine = first.markLine === undefined
    ? [{ ...first, markLine: zeroLine(axisKey, tokens) }, ...rest]
    : series;
  return {
    ...option,
    series: withZeroLine,
    [axisKey]: Array.isArray(raw) ? [centered] : centered,
  };
}

// 方法作用：按 tooltip 的触发语义补齐样式，十字准线只加给按轴触发的提示。
// Args: tooltip - 后端下发的 tooltip 配置；tokens - 当前模板 token；
//       force - 是否强制覆盖已存在的键；forceItem - 实际 series 是否判定为纯柱状图。
// Returns: 合并后的 tooltip 配置（原对象未被修改）。
function withTooltipBaseline(
  tooltip: unknown,
  tokens: ReportTemplateTokens,
  force: boolean,
  forceItem: boolean,
): Record<string, unknown> {
  const merged = mergeMissing(
    isPlainObject(tooltip) ? tooltip : {},
    {
      backgroundColor: tokens.surface,
      borderColor: tokens.border,
      textStyle: { color: tokens.textPrimary },
    },
    force,
  );
  if (forceItem) {
    // 旧快照兼容：修复前持久化的柱状图 option 把 trigger 写成 axis（实机取证 20 张纯柱图，
    // 含 8 系列那 3 张），悬停单柱会弹出同横轴整批系列。此处按实际 series 强制回到 item，
    // 并删掉遗留的 axisPointer——十字准线只在 axis 触发下才有意义，留着仍会画出整轴指示线。
    merged.trigger = 'item';
    delete merged.axisPointer;
    return merged;
  }
  // 十字准线只在按轴触发时才有意义：柱状图是 item 触发，无条件补 cross 会让悬停
  // 单柱时弹出整条横轴的全部系列读数（实机取证：8 条系列一起弹出）。
  if (merged.trigger === 'axis') {
    merged.axisPointer = mergeMissing(
      isPlainObject(merged.axisPointer) ? merged.axisPointer : {},
      { type: 'cross' },
      force,
    );
  }
  return merged;
}

// 方法作用：为后端下发的 option 补齐模板样式基线。
// Args: option - 后端校验过的 echarts_option，可能为空；tokens - 当前模板 token。
// Returns: 合并后的 option；入参为空或非对象时返回 undefined。
export function withChartBaseline(
  option: Record<string, unknown> | null | undefined,
  tokens: ReportTemplateTokens,
): EChartsOption | undefined {
  if (!isPlainObject(option)) return undefined;
  // 后端 option 的根键白名单不含 color，因此配色只能在这里统一注入。
  const merged: Record<string, unknown> = { ...option, color: [...tokens.chartPalette] };
  const force = isDarkSurface(tokens.surface);
  merged.backgroundColor = force ? tokens.surface : (merged.backgroundColor ?? tokens.surface);
  merged.textStyle = mergeMissing(
    isPlainObject(merged.textStyle) ? merged.textStyle : {},
    { fontFamily: tokens.fontFamily },
  );
  // 判定依据是**实际生效的 series**，不是内容里的 chart_type：渲染器（ReportDocumentRenderer
  // 的 ChartBlock）先按当前分组把 series 替换掉才调用本函数，而 chart_type 可能来自过时快照。
  const barOnly = isAllBarSeries(merged.series);
  merged.tooltip = withTooltipBaseline(merged.tooltip, tokens, force, barOnly);
  if (merged.visualMap !== undefined) {
    merged.visualMap = withSequentialPalette(merged.visualMap, tokens.sequentialPalette);
  }
  merged.series = withSeriesBaseline(merged.series, tokens);
  // 只在 option 自己声明了图例时才补样式，**绝不凭空造出一个图例**：
  // 后端单系列图表不带 legend（系列名与轴语义重复，图例纯属噪声），而原先的
  // `isPlainObject(legend) ? legend : {}` 会把缺失或 `legend: false` 都变成 `{}`，
  // 等于强行让 ECharts 画出一个图例——这正是图例压住数据的来源。
  if (isPlainObject(merged.legend) && merged.legend.show !== false) {
    merged.legend = mergeMissing(
      merged.legend, { textStyle: { color: tokens.textSecondary } }, force,
    );
  }
  merged.grid = mergeMissing(
    isPlainObject(merged.grid) ? merged.grid : {},
    { containLabel: true, left: 12, right: 16, top: 32, bottom: 8 },
  );
  reserveLegendSpace(merged);
  const axisBaseline = {
    axisLabel: { color: tokens.textSecondary },
    axisLine: { lineStyle: { color: tokens.border } },
    splitLine: { lineStyle: { color: tokens.border } },
  };
  for (const axis of ['xAxis', 'yAxis']) {
    const current = merged[axis];
    if (Array.isArray(current)) {
      merged[axis] = current.map(item => (isPlainObject(item) ? mergeMissing(item, axisBaseline, force) : item));
    } else {
      merged[axis] = mergeMissing(isPlainObject(current) ? current : {}, axisBaseline, force);
    }
  }
  return withCenteredZeroAxis(merged, tokens) as EChartsOption;
}
