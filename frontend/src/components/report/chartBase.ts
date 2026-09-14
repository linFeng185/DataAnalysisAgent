// 图表样式基线：把模板 token 注入后端下发的 ECharts option。
//
// 合并原则是「不覆盖后端意图」——但深色模板下有一处刻意例外：
// 后端是在浅色假设下生成 tooltip/轴颜色的，深色底上会穿帮，必须强制覆盖。

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
  merged.tooltip = mergeMissing(
    isPlainObject(merged.tooltip) ? merged.tooltip : {},
    { backgroundColor: tokens.surface, borderColor: tokens.border, textStyle: { color: tokens.textPrimary } },
    force,
  );
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
  return merged as EChartsOption;
}
