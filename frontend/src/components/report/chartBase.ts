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
  merged.legend = mergeMissing(
    isPlainObject(merged.legend) ? merged.legend : {},
    { textStyle: { color: tokens.textSecondary } },
    force,
  );
  merged.grid = mergeMissing(
    isPlainObject(merged.grid) ? merged.grid : {},
    { containLabel: true, left: 12, right: 16, top: 32, bottom: 8 },
  );
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
