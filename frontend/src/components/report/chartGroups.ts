// 图表分组选项归一：把后端下发的 `group_options` 收敛成**可直接渲染**的一份结果。
//
// 背景（真实分享接口取证，非推测）：分享页
//   http://localhost:5173/shared/reports/2gwR2fiF5Zh_RnVt6QKhfOBqpQnkenYQ335yPtrvU2s
// 的持久化 document 是 report d5e60e90-7a55-405c-b3ab-1949e169fda0 version 3（60 张图，
// 由修复前的旧逻辑生成）。页面版本按 SPEC 不可变、后端新逻辑不会回写它，因此旧载荷必须在
// **渲染期**收敛。修复前的渲染器只检查「当前选项的 series 有没有 type」，于是：
//   ① 40 张图带 `__mom__`，其中 13 张的实际 `echarts_option.xAxis.data` 是分类值
//      （省市 / missed,summary / RFM 分层 / 账龄桶 / 地区 / 实验组对照组）——"环比(%)" 是
//      相邻类目之间的差额百分比，业务语义错误，且 chart_sales_foundation_5 的默认分组
//      就落在 `__mom__` 上，用户打开看到的第一眼就是错的；
//   ② 4 张柱图的类目轴只剩一个空标签 `['']`（chart_price_promotion_1/2、
//      chart_procurement_inventory_1/2），数据在旧快照里已聚合丢失，继续画空标签柱图
//      会让用户以为 UI 少了内容；
//   ③ 月度轴（`2025-01...2025-12`）的 `__mom__` 与年度轴（`2016...2025`）的 `__yoy__`
//      是合法比较项，必须保留；
//   ④ `chart_sales_foundation_4` 的 `content.series_fields` 是
//      `[net_sales_yoy_pct, net_sales_mom_pct]`，指标本身已是同比/环比，旧逻辑却在它上面
//      又生成了 `__mom__`（=「同比的环比」）。这类二阶比较必须按**声明的指标列名**过滤。
//
// 判定依据只能是 `echarts_option.xAxis` 的**实际取值**，不能信任可能过时的
// `content.chart_type` / `content.x_field`（chart_target_achievement_1 的 chart_type 是 bar、
// x_field 写着 store_id，实际类目却是 missed/summary）；二阶比较的判定依据则是
// `content.series_fields` 的**实际声明值**。
//
// 本模块是纯函数，不依赖 React、不读 DOM、不打日志；渲染层只消费返回值。

/** 服务端算好的某个分组下的系列；`type` 由归一逻辑按 base 实际类型补齐。 */
export interface ChartGroupSeries {
  name?: string;
  type: string;
  data: unknown[];
}

/** 分组归一结果：渲染层只使用这一份。 */
export interface ChartGroupResolution {
  /** 类目轴 data 非空但全部是空白标签：调用方必须显示版本重建提示，不画 canvas、不显示分组控件。 */
  axisIssue: boolean;
  /** 过滤后的分组键（保持原始顺序）；长度 ≤1 时调用方不渲染 Segmented。 */
  groups: string[];
  /** 过滤后的分组标签。 */
  labels: Record<string, string>;
  /** 过滤并归一后的分组选项。 */
  options: Record<string, ChartGroupSeries[]>;
  /** 服务端声明的默认分组；声明值不可用时回落到首个可用分组。 */
  initialGroup: string;
  /** 本次生效的分组键。 */
  activeGroup: string;
  /** 套用当前分组后的最终 option；无 base 或命中 axisIssue 时为 null。 */
  option: Record<string, unknown> | null;
}

// 方法作用：判断取值是否是可做键值遍历的普通对象。
// Args: value - 待判断的值。
// Returns: 是普通对象时返回 true（数组与 null 除外）。
function isPlainObject(value: unknown): value is Record<string, unknown> {
  return Boolean(value) && typeof value === 'object' && !Array.isArray(value);
}

// 方法作用：判断一个类目标签是否「没有可用文字」。
// Args: value - 类目轴上的单个标签。
// Returns: null / undefined / 纯空白字符串返回 true，数值与有文字的字符串返回 false。
function isBlankLabel(value: unknown): boolean {
  if (value === null || value === undefined) return true;
  if (typeof value === 'string') return value.trim() === '';
  return false;
}

// 方法作用：把 option 的某个轴配置统一成数组，兼容单对象与数组两种写法。
// Args: option - 已确认为普通对象的 option；key - 'xAxis' 或 'yAxis'。
// Returns: 轴配置数组；缺失或为 null 时返回空数组。
function axisConfigs(option: Record<string, unknown>, key: string): unknown[] {
  const axis = option[key];
  if (Array.isArray(axis)) return axis;
  return axis === undefined || axis === null ? [] : [axis];
}

// 方法作用：识别单个类目标签的时间粒度。
// Args: value - 横轴上的单个标签。
// Returns: minute / hour / day / month / year；不是可识别时间值时返回 null（规则与后端 _time_grain 同形）。
function labelGrain(value: unknown): string | null {
  // ISO 的 'T' 分隔符归一成空格，其余形状与后端保持一致。
  const text = String(value ?? '').trim().replace('T', ' ');
  if (!text) return null;
  // 纯时刻写法（如 08:30 / 08:30:15）按分钟粒度处理。
  if (/^\d{2}:\d{2}(:\d{2})?$/.test(text)) return 'minute';
  if (text.length >= 16 && text[10] === ' ' && text[13] === ':') return 'minute';
  if (text.length === 13 && text[10] === ' ') return 'hour';
  if (text.length >= 10 && text[4] === '-' && text[7] === '-') return 'day';
  if (text.length === 7 && text[4] === '-') return 'month';
  if (text.length === 4 && /^\d{4}$/.test(text)) return 'year';
  return null;
}

// 方法作用：判定单个轴的类型，未声明 type 但带 data 的轴按 ECharts 语义视作类目轴。
// Args: axis - 单个轴配置。
// Returns: category / time / value / none。
function axisKind(axis: unknown): 'category' | 'time' | 'value' | 'none' {
  if (!isPlainObject(axis)) return 'none';
  const declared = typeof axis.type === 'string' ? axis.type.trim().toLowerCase() : '';
  if (declared === 'time') return 'time';
  if (declared === 'category') return 'category';
  if (declared === 'value' || declared === 'log') return 'value';
  if (!declared && Array.isArray(axis.data)) return 'category';
  return 'none';
}

// 方法作用：判定横轴（xAxis）的语义类型。
// Args: option - 已确认为普通对象的 option。
// Returns: category / time / value / none；双轴时优先认类目轴。
function horizontalAxisKind(option: Record<string, unknown>): 'category' | 'time' | 'value' | 'none' {
  const kinds = axisConfigs(option, 'xAxis').map(axisKind);
  if (kinds.includes('category')) return 'category';
  if (kinds.includes('time')) return 'time';
  if (kinds.includes('value')) return 'value';
  return 'none';
}

// 方法作用：取出横轴上真正用于绘制的类目标签。
// Args: option - 已确认为普通对象的 option。
// Returns: 首个「类目轴且带 data 数组」的标签数组；找不到时返回空数组。
function categoryLabels(option: Record<string, unknown>): unknown[] {
  for (const axis of axisConfigs(option, 'xAxis')) {
    if (axisKind(axis) !== 'category') continue;
    const data = (axis as Record<string, unknown>).data;
    if (Array.isArray(data)) return data;
  }
  return [];
}

// 方法作用：识别「数据已在旧快照里丢失」的空标签类目轴。
// Args: option - 已确认为普通对象的 option。
// Returns: 存在类目轴、其 data 非空且每个标签都是 null / 空白字符串时返回 true。
function isStaleCategoryAxis(option: Record<string, unknown>): boolean {
  return axisConfigs(option, 'xAxis').some(axis => {
    if (axisKind(axis) !== 'category') return false;
    const data = (axis as Record<string, unknown>).data;
    // data 缺失或为空数组不算 stale：那是「没有声明类目」，不是「类目被聚合丢了」。
    if (!Array.isArray(data) || data.length === 0) return false;
    return data.every(item => isBlankLabel(item));
  });
}

// 方法作用：读取 base option 的实际系列类型，用于判定能否套用分组系列。
// Args: option - 已确认为普通对象的 option。
// Returns: 全部系列都是 line 时返回 'line'，都是 bar 时返回 'bar'，其余（pie/scatter/heatmap/
//          混合类型/空/非数组/缺 type）一律返回 null，表示分组选项必须被完全禁用。
function baseSeriesType(option: Record<string, unknown>): 'line' | 'bar' | null {
  const series = option.series;
  if (!Array.isArray(series) || series.length === 0) return null;
  const types = new Set(series.map(item => (isPlainObject(item) && typeof item.type === 'string' ? item.type : '')));
  if (types.size !== 1) return null;
  const [only] = [...types];
  return only === 'line' || only === 'bar' ? only : null;
}

// 方法作用：按横轴语义决定哪些派生比较项必须从分组里剔除。
// Args: kind - 横轴语义类型；labels - 横轴类目标签。
// Returns: 需要剔除的分组键集合；判不出横轴语义时返回空集合（不擅自删除）。
function droppedComparisonKeys(kind: 'category' | 'time' | 'value' | 'none', labels: unknown[]): Set<string> {
  const dropped = new Set<string>();
  // 显式 time 轴本身就是时间轴，比较项一律保留。
  if (kind === 'time') return dropped;
  // 判不出横轴（无轴 / 纯值轴 / 非类目轴）：宁可保留也不猜，猜错会删掉合法比较项。
  if (kind !== 'category') return dropped;
  let seen = false;
  let allTime = true;
  let yearOnly = true;
  for (const label of labels) {
    if (isBlankLabel(label)) continue;
    seen = true;
    const grain = labelGrain(label);
    if (grain === null) {
      allTime = false;
      yearOnly = false;
    } else if (grain !== 'year') {
      yearOnly = false;
    }
  }
  // 一个可用标签都没有：这是 stale 轴的范畴，交给 axisIssue 如实披露，此处不删。
  if (!seen) return dropped;
  if (!allTime) {
    // 真实分类轴：没有「上一期」，环比与同比都不成立。
    dropped.add('__mom__');
    dropped.add('__yoy__');
    return dropped;
  }
  // 纯年度轴：没有「上一月」（移除环比），但逐年同比成立（保留同比）。
  if (yearOnly) dropped.add('__mom__');
  return dropped;
}

// 方法作用：判断声明的指标列名本身是否已经是同比/环比指标。
// Args: field - content.series_fields 里的单个列名。
// Returns: 是同比/环比指标时返回 true。
function isComparisonMetricField(field: unknown): boolean {
  const text = String(field ?? '').trim().toLowerCase();
  if (!text) return false;
  // 中文标签直判：列名本身写着「同比 / 环比」时它已经是比较值。
  if (text.includes('同比') || text.includes('环比')) return true;
  // 拉丁列名按 token 边界判定：net_sales_mom_pct 命中，momentum_score 不命中
  // （子串包含会把 momentum 当成 mom，删掉合法比较项）。
  return text.split(/[^a-z0-9]+/).some(token => token === 'mom' || token === 'yoy');
}

// 方法作用：判断该图声明的指标里是否已经含同比/环比指标。
// Args: seriesFields - content.series_fields 原值。
// Returns: 存在同比/环比指标时返回 true；非数组或空数组返回 false。
function declaresComparisonMetric(seriesFields: unknown): boolean {
  return Array.isArray(seriesFields) && seriesFields.some(item => isComparisonMetricField(item));
}

// 方法作用：过滤非法分组选项并把系列类型按 base 实际类型归一。
// Args: raw - 后端下发的 group_options 原值；baseType - base 实际系列类型；
//       dropped - 需要剔除的分组键。
// Returns: 新的分组选项表；非法项被跳过，入参对象与系列对象都不被修改。
function normalizeGroupOptions(
  raw: unknown,
  baseType: 'line' | 'bar',
  dropped: Set<string>,
): Record<string, ChartGroupSeries[]> {
  const normalized: Record<string, ChartGroupSeries[]> = {};
  if (!isPlainObject(raw)) return normalized;
  for (const [key, value] of Object.entries(raw)) {
    if (dropped.has(key)) continue;
    // 非数组与空数组都不是可用分组：空分组切过去就是「数据清空」。
    if (!Array.isArray(value) || value.length === 0) continue;
    const items: ChartGroupSeries[] = [];
    let legal = true;
    for (const item of value) {
      // 没有 data 数组的系列画不出任何东西；非对象项同理。
      if (!isPlainObject(item) || !Array.isArray(item.data)) {
        legal = false;
        break;
      }
      // 旧快照可能缺 type（ECharts 会静默丢弃整组）或被硬编码成错误类型：
      // 统一按 base 的实际类型复制修正。展开新建对象保留后端设置的其余字段，
      // 不改动原始系列；此处的断言只用于把「已校验的 data 数组 + 已写入的 type」
      // 收敛到 ChartGroupSeries。
      items.push({ ...item, type: baseType, data: item.data } as ChartGroupSeries);
    }
    if (!legal) continue;
    normalized[key] = items;
  }
  return normalized;
}

// 方法作用：把后端下发的分组选项收敛成一份可直接渲染的结果。
// Args: content - 报表块的 content（含 group_options / group_labels / group_field）；
//       baseOption - 该块的 echarts_option；activeGroup - 可选，当前选中的分组键。
// Returns: 归一结果；命中 stale 类目轴时 option 为 null，特殊图时分组被完全禁用。
export function resolveChartGroups(
  content: unknown,
  baseOption: unknown,
  activeGroup?: string,
): ChartGroupResolution {
  const empty: ChartGroupResolution = {
    axisIssue: false,
    groups: [],
    labels: {},
    options: {},
    initialGroup: '',
    activeGroup: '',
    option: null,
  };
  if (!isPlainObject(baseOption)) return empty;
  // 旧快照把类目聚合丢成 ['']：先如实披露，绝不再画误导性 canvas，也不显示分组控件。
  if (isStaleCategoryAxis(baseOption)) return { ...empty, axisIssue: true };
  // pie / scatter / heatmap / 混合类型：分组系列的数据形状与 base 不兼容，完全禁用。
  const baseType = baseSeriesType(baseOption);
  if (baseType === null) return { ...empty, option: { ...baseOption } };

  const payload = isPlainObject(content) ? content : {};
  const dropped = droppedComparisonKeys(horizontalAxisKind(baseOption), categoryLabels(baseOption));
  // 旧快照里还有一类派生项必须按**声明的指标**过滤：指标本身已经是同比/环比时再挂
  // 「环比(%)」就是二阶比较。真实取证 chart_sales_foundation_4 的 series_fields 是
  // [net_sales_yoy_pct, net_sales_mom_pct]，旧逻辑却在它上面又生成了 __mom__。
  // 普通原始指标（net_sales_amount）不受影响，月度轴的合法比较项照旧保留。
  if (declaresComparisonMetric(payload.series_fields)) {
    dropped.add('__mom__');
    dropped.add('__yoy__');
  }
  const options = normalizeGroupOptions(payload.group_options, baseType, dropped);
  const groups = Object.keys(options);
  const declared = typeof payload.group_field === 'string' ? payload.group_field : '';
  const initialGroup = declared && Object.prototype.hasOwnProperty.call(options, declared)
    ? declared
    : (groups[0] ?? '');
  // 选中的分组在本次归一中不可用时（被过滤、组件被复用到另一张图）回落到默认分组。
  const active = typeof activeGroup === 'string' && Object.prototype.hasOwnProperty.call(options, activeGroup)
    ? activeGroup
    : initialGroup;
  const series = Object.prototype.hasOwnProperty.call(options, active) ? options[active] : undefined;
  const labels: Record<string, string> = {};
  const rawLabels = isPlainObject(payload.group_labels) ? payload.group_labels : {};
  for (const key of groups) {
    labels[key] = typeof rawLabels[key] === 'string' ? rawLabels[key] as string : '';
  }
  return {
    axisIssue: false,
    groups,
    labels,
    options,
    initialGroup,
    activeGroup: active,
    option: series ? { ...baseOption, series } : { ...baseOption },
  };
}
