// 图表分组选项归一的**行为测试**：真正调用 resolveChartGroups 并断言返回值，不做源码字符串断言。
// 运行方式：npm run test:report-groups（等价 node --test tests/chartGroups.test.mjs）。
// 依赖：Node 24 原生 TypeScript 类型擦除 + node:test，不新增任何依赖。
//
// 背景（真实分享接口取证，非推测）：分享页
//   http://localhost:5173/shared/reports/2gwR2fiF5Zh_RnVt6QKhfOBqpQnkenYQ335yPtrvU2s
// 的持久化 document 是 report d5e60e90-7a55-405c-b3ab-1949e169fda0 version 3（60 张图，
// 由修复前的旧逻辑生成）。页面版本按 SPEC 不可变，后端新逻辑不会回写它，因此旧载荷必须在
// **渲染期**收敛。真实取证结论：
//   ① 40 张图带 `__mom__`，其中 13 张的实际 `echarts_option.xAxis.data` 是**分类值**
//      （省市 / missed,summary / RFM 分层 / 账龄桶 / 地区 / 实验组对照组），"环比(%)" 是
//      相邻类目之间的差额百分比，业务语义错误；
//   ② 4 张柱图的类目轴只有一个空标签 `['']`（chart_price_promotion_1/2、
//      chart_procurement_inventory_1/2），数据在旧快照里已聚合丢失，前端无法诚实重建；
//   ③ 月度轴（`2025-01...2025-12`）的 `__mom__` 与年度轴（`2016...2025`）的 `__yoy__`
//      是合法比较项，必须保留。
// 判定依据只能是 `echarts_option.xAxis` 的实际取值，不能信任可能过时的
// `content.chart_type` / `content.x_field`。

import test from 'node:test';
import assert from 'node:assert/strict';

import { resolveChartGroups } from '../src/components/report/chartGroups.ts';

// 方法作用：构造一张旧快照形态的图表块内容（content）与基础 option。
// Args: groupOptions - 分组键到系列数组的映射；groupLabels - 分组键到中文标签；
//       groupField - 服务端声明的默认分组；xAxis - 横轴配置；seriesType - base 系列类型；
//       seriesFields - 该图声明的指标列（用于二阶比较防御）。
// Returns: { content, base } 结构，base 为纯柱 / 纯折线图的最小合法 option。
function snapshot({ groupOptions, groupLabels = {}, groupField = '', xAxis, seriesType = 'bar', seriesFields = [] }) {
  const content = {
    chart_type: seriesType,
    x_field: 'year_month',
    series_fields: seriesFields,
    group_field: groupField,
    group_labels: groupLabels,
    group_options: groupOptions,
  };
  const base = {
    tooltip: { trigger: 'axis' },
    xAxis: xAxis === undefined ? { type: 'category', data: ['华东', '华北'] } : xAxis,
    yAxis: { type: 'value' },
    series: [{ type: seriesType, name: '金额', data: [12, 30] }],
  };
  return { content, base };
}

// 方法作用：构造月度时间轴标签，取值对齐真实快照的 2025-01…2025-12。
// Args: count - 月份数量。
// Returns: 形如 2025-01 的月份字符串数组。
function months(count) {
  return Array.from({ length: count }, (_, index) => `2025-${String(index + 1).padStart(2, '0')}`);
}

// 方法作用：构造年度时间轴标签。
// Args: 无。
// Returns: 形如 2016 的年份字符串数组。
function years() {
  return ['2016', '2017', '2018', '2019', '2020'];
}

// ============================================================================
// ① 真实分类轴：派生比较项被过滤，正常业务分组选项保留
// ============================================================================

// 真实快照里 13 张分类轴图表的逐块取证表（块 id / 分组键 / 声明分组字段 / 实际类目轴标签 / 期望保留的分组键）。
const REAL_CATEGORY_CASES = [
  { id: 'chart_sales_foundation_5', keys: ['__mom__', 'channel_code'], groupField: 'region_code', labels: ['上海市', '北京市', '四川省'], keep: ['channel_code'] },
  { id: 'chart_target_achievement_1', keys: ['__mom__', 'year_month'], groupField: 'year_month', labels: ['missed', 'summary'], keep: ['year_month'] },
  { id: 'chart_target_achievement_2', keys: ['__mom__', 'year_month'], groupField: 'year_month', labels: ['missed', 'summary'], keep: ['year_month'] },
  { id: 'chart_customer_member', keys: ['__mom__', 'ltv_tier'], groupField: 'ltv_tier', labels: ['一般维持', '沉睡高价值', '活跃成长'], keep: ['ltv_tier'] },
  { id: 'chart_customer_member_1', keys: ['__mom__', 'ltv_tier'], groupField: 'ltv_tier', labels: ['一般维持', '沉睡高价值', '活跃成长'], keep: ['ltv_tier'] },
  { id: 'chart_customer_member_2', keys: ['__mom__', 'ltv_tier'], groupField: 'ltv_tier', labels: ['一般维持', '沉睡高价值', '活跃成长'], keep: ['ltv_tier'] },
  { id: 'chart_customer_member_3', keys: ['__mom__', 'ltv_tier'], groupField: 'ltv_tier', labels: ['一般维持', '沉睡高价值', '活跃成长'], keep: ['ltv_tier'] },
  { id: 'chart_refund_aging', keys: ['__mom__', 'metric_group'], groupField: 'metric_group', labels: ['1-30天', '31-60天', '61-90天'], keep: ['metric_group'] },
  { id: 'chart_fulfillment_cost', keys: ['', '__mom__'], groupField: '', labels: ['CN-BJ', 'CN-GD', 'CN-HB'], keep: [''] },
  { id: 'chart_fulfillment_cost_1', keys: ['', '__mom__'], groupField: '', labels: ['CN-BJ', 'CN-GD', 'CN-HB'], keep: [''] },
  { id: 'chart_fulfillment_cost_2', keys: ['', '__mom__'], groupField: '', labels: ['CN-BJ', 'CN-GD', 'CN-HB'], keep: [''] },
  { id: 'chart_experiment_effect', keys: ['', '__mom__'], groupField: '', labels: ['对照组', '实验组'], keep: [''] },
  { id: 'chart_experiment_effect_1', keys: ['', '__mom__'], groupField: '', labels: ['对照组', '实验组'], keep: [''] },
];

test('真实分类轴：__mom__ 被过滤，业务分组选项按原顺序保留', () => {
  // Arrange + Act + Assert：逐块还原真实取证结论，共 13 块
  for (const item of REAL_CATEGORY_CASES) {
    const groupOptions = {};
    for (const key of item.keys) {
      groupOptions[key] = [{ type: 'bar', name: key || '指标值', data: [1, 2, 3] }];
    }
    const { content, base } = snapshot({
      groupOptions,
      groupField: item.groupField,
      xAxis: { type: 'category', data: item.labels },
    });

    // Act
    const result = resolveChartGroups(content, base);

    // Assert
    assert.equal(result.axisIssue, false, `${item.id} 不是空标签轴，不得误报版本重建`);
    assert.deepEqual(result.groups, item.keep, `${item.id} 只应保留业务分组选项`);
    assert.equal('__mom__' in result.options, false, `${item.id} 的环比分组必须被移除`);
    assert.deepEqual(Object.keys(result.options), item.keep, `${item.id} 分组键顺序必须与原始顺序一致`);
  }
});

test('真实分类轴：默认分组落到保留下来的业务分组上，不再停在被过滤的 __mom__', () => {
  // Arrange：chart_sales_foundation_5 快照里声明的 group_field=region_code 并不在 group_options 中，
  // 旧渲染器于是取 keys[0]='__mom__' 当默认视图 —— 用户打开看到的就是"环比"。
  const { content, base } = snapshot({
    groupOptions: {
      __mom__: [{ type: 'bar', name: '环比(%)', data: [1, 2] }],
      channel_code: [{ type: 'bar', name: '渠道', data: [3, 4] }],
    },
    groupField: 'region_code',
    xAxis: { type: 'category', data: ['上海市', '北京市'] },
  });

  // Act
  const result = resolveChartGroups(content, base);

  // Assert
  assert.equal(result.initialGroup, 'channel_code');
  assert.equal(result.activeGroup, 'channel_code');
  assert.deepEqual(result.option.series, [{ type: 'bar', name: '渠道', data: [3, 4] }]);
});

test('分类轴上的 __yoy__ 同样是派生比较项，必须一并过滤', () => {
  // Arrange：分类轴没有"上一期"，同比同样不成立。
  const { content, base } = snapshot({
    groupOptions: {
      __yoy__: [{ type: 'bar', name: '同比(%)', data: [1, 2] }],
      __mom__: [{ type: 'bar', name: '环比(%)', data: [1, 2] }],
      region: [{ type: 'bar', name: '地区', data: [3, 4] }],
    },
    groupField: 'region',
    xAxis: { type: 'category', data: ['华东', '华北'] },
  });

  // Act
  const result = resolveChartGroups(content, base);

  // Assert
  assert.deepEqual(result.groups, ['region']);
  assert.equal('__yoy__' in result.options, false);
  assert.equal('__mom__' in result.options, false);
});

// ============================================================================
// ② 时间轴：可识别时间轴保留合法比较项；纯年度轴移除 __mom__、保留 __yoy__
// ============================================================================

test('月度时间轴：__mom__ 与 __yoy__ 都是合法比较项，必须原样保留', () => {
  // Arrange：真实快照 20+ 张月度折线图，xAxis.data = 2025-01 … 2025-12
  const { content, base } = snapshot({
    groupOptions: {
      __mom__: [{ type: 'line', name: '环比(%)', data: [1, 2] }],
      __yoy__: [{ type: 'line', name: '同比(%)', data: [1, 2] }],
      channel_code: [{ type: 'line', name: '渠道', data: [3, 4] }],
    },
    groupField: 'channel_code',
    xAxis: { type: 'category', data: months(12) },
    seriesType: 'line',
  });

  // Act
  const result = resolveChartGroups(content, base);

  // Assert
  assert.deepEqual(result.groups, ['__mom__', '__yoy__', 'channel_code']);
  assert.equal(result.initialGroup, 'channel_code');
});

test('小时轴与分钟轴：节点级时间轴同样保留合法比较项', () => {
  // Arrange
  const cases = [
    { name: '小时轴', data: ['2025-01-01 08', '2025-01-01 09', '2025-01-01 10'] },
    { name: '分钟轴', data: ['2025-01-01 08:00', '2025-01-01 08:15', '2025-01-01 08:30'] },
    { name: '日轴', data: ['2025-01-01', '2025-01-02', '2025-01-03'] },
  ];

  for (const item of cases) {
    const { content, base } = snapshot({
      groupOptions: { __mom__: [{ type: 'line', name: '环比(%)', data: [1, 2, 3] }] },
      xAxis: { type: 'category', data: item.data },
      seriesType: 'line',
    });

    // Act
    const result = resolveChartGroups(content, base);

    // Assert
    assert.deepEqual(result.groups, ['__mom__'], `${item.name} 的环比必须保留`);
  }
});

test('纯年度轴：移除 __mom__，保留 __yoy__', () => {
  // Arrange：真实 chart_budget_tax_1，xAxis.data = 2016…2025（纯年份）
  const { content, base } = snapshot({
    groupOptions: {
      __mom__: [{ type: 'bar', name: '环比(%)', data: [1, 2] }],
      __yoy__: [{ type: 'bar', name: '同比(%)', data: [1, 2] }],
      '': [{ type: 'bar', name: '指标值', data: [3, 4] }],
    },
    groupField: '',
    xAxis: { type: 'category', data: years() },
  });

  // Act
  const result = resolveChartGroups(content, base);

  // Assert：年度轴没有"上一月"，但逐年同比成立
  assert.deepEqual(result.groups, ['__yoy__', '']);
  assert.equal('__mom__' in result.options, false);
});

// ============================================================================
// ③ 特殊图：base 实际系列非纯 line / 纯 bar 时完全禁用 group_options
// ============================================================================

test('特殊图（pie / scatter / heatmap）与混合类型：完全禁用 group_options，使用 base option', () => {
  // Arrange：旧快照可能给特殊图带上历史分组选项，一旦替换 series 数据形状就不兼容
  const cases = [
    { name: '饼图', series: [{ type: 'pie', name: '占比', data: [1, 2] }] },
    { name: '散点图', series: [{ type: 'scatter', name: '相关性', data: [1, 2] }] },
    { name: '热力图', series: [{ type: 'heatmap', name: '密度', data: [1, 2] }] },
    { name: '混合 bar+line', series: [{ type: 'bar', data: [1] }, { type: 'line', data: [2] }] },
    { name: '空 series', series: [] },
    { name: '非数组 series', series: 'oops' },
  ];

  for (const item of cases) {
    const { content, base } = snapshot({
      groupOptions: { region: [{ type: 'bar', name: '地区', data: [1, 2] }] },
      groupField: 'region',
      xAxis: { type: 'category', data: ['华东', '华北'] },
    });
    base.series = item.series;
    const before = JSON.stringify(base);

    // Act
    const result = resolveChartGroups(content, base);

    // Assert
    assert.deepEqual(result.groups, [], `${item.name} 不得出现分组切换控件`);
    assert.deepEqual(result.options, {}, `${item.name} 不得保留任何历史分组选项`);
    assert.equal(result.initialGroup, '', `${item.name} 不得给出默认分组`);
    assert.deepEqual(result.option, base, `${item.name} 必须原样使用 base option`);
    assert.equal(JSON.stringify(base), before, `${item.name} 不得原地修改 base 入参`);
    assert.equal(JSON.stringify(content.group_options), JSON.stringify({ region: [{ type: 'bar', name: '地区', data: [1, 2] }] }), `${item.name} 不得原地修改分组入参`);
  }
});

// ============================================================================
// ④ 系列类型归一：base 为纯 line / 纯 bar 时按实际类型复制修正
// ============================================================================

test('纯柱状图：旧分组 series 缺失或错误的 type 一律按 base 实际类型修正', () => {
  // Arrange：旧快照可能缺 type（ECharts 会静默丢弃整组）或被硬编码成错误类型
  const { content, base } = snapshot({
    groupOptions: {
      region: [
        { name: '华东', data: [1, 2] },
        { name: '华北', data: [3, 4], type: 'line' },
        { name: '华南', data: [5, 6], type: '' },
      ],
    },
    groupField: 'region',
    xAxis: { type: 'category', data: ['华东', '华北'] },
  });

  // Act
  const result = resolveChartGroups(content, base);

  // Assert
  assert.deepEqual(result.option.series.map(item => item.type), ['bar', 'bar', 'bar']);
  assert.deepEqual(result.option.series.map(item => item.name), ['华东', '华北', '华南']);
});

test('纯折线图：分组 series 被硬编码成 bar 时同样按 base 实际类型修正为 line', () => {
  // Arrange
  const { content, base } = snapshot({
    groupOptions: {
      channel_code: [{ type: 'bar', name: '渠道', data: [1, 2] }],
      '': [{ name: '指标值', data: [3, 4] }],
    },
    groupField: 'channel_code',
    xAxis: { type: 'category', data: months(12) },
    seriesType: 'line',
  });

  // Act
  const result = resolveChartGroups(content, base);

  // Assert：每个分组各 1 条系列，且都被修正为 base 的 line
  assert.deepEqual(result.groups, ['channel_code', '']);
  assert.deepEqual(result.options.channel_code.map(item => item.type), ['line']);
  assert.deepEqual(result.options[''].map(item => item.type), ['line']);
  assert.deepEqual(result.option.series.map(item => item.type), ['line']);
});

test('归一保持纯度：base 与 group_options 的原始对象都不被修改', () => {
  // Arrange
  const groupOptions = {
    region: [{ name: '华东', data: [1, 2] }],
  };
  const { content, base } = snapshot({ groupOptions, groupField: 'region' });
  const contentBefore = JSON.stringify(content);
  const baseBefore = JSON.stringify(base);

  // Act
  const result = resolveChartGroups(content, base);

  // Assert
  assert.equal(JSON.stringify(base), baseBefore, 'base 必须逐字节不变');
  assert.equal(JSON.stringify(content), contentBefore, 'content 必须逐字节不变');
  assert.notEqual(result.option, base, '返回的 option 必须是新对象');
  assert.equal(result.option.series[0].type, 'bar');
  assert.equal('type' in groupOptions.region[0], false, '原始分组 series 不得被写入 type');
});

// ============================================================================
// ⑤ 非法分组选项不得进入 Segmented
// ============================================================================

test('非法分组选项（非数组 / 空数组 / 缺 data / 非对象项）不出现在 groups 与 options', () => {
  // Arrange
  const { content, base } = snapshot({
    groupOptions: {
      not_array: 'oops',
      null_value: null,
      empty_array: [],
      missing_data: [{ type: 'bar', name: '无数据' }],
      non_object_item: [null],
      no_data_field: [{ type: 'bar', name: 'data 不是数组', data: 'oops' }],
      valid_missing_type: [{ name: '有效但缺 type', data: [1, 2] }],
      valid: [{ type: 'bar', name: '正常', data: [3, 4] }],
    },
    groupField: '',
    xAxis: { type: 'category', data: ['华东', '华北'] },
  });

  // Act
  const result = resolveChartGroups(content, base);

  // Assert
  assert.deepEqual(result.groups, ['valid_missing_type', 'valid']);
  assert.deepEqual(Object.keys(result.options), ['valid_missing_type', 'valid']);
  assert.equal(result.initialGroup, 'valid_missing_type');
});

test('全部分组选项非法 / group_options 缺失时，回落到 base option 且不显示控件', () => {
  // Arrange
  const cases = [
    { name: '全非法', groupOptions: { a: [], b: 'oops' } },
    { name: 'group_options 为 null', groupOptions: null },
    { name: 'group_options 缺失', groupOptions: undefined },
  ];

  for (const item of cases) {
    const { content, base } = snapshot({ groupOptions: item.groupOptions, groupField: 'a' });

    // Act
    const result = resolveChartGroups(content, base);

    // Assert
    assert.deepEqual(result.groups, [], `${item.name} 不得出现控件`);
    assert.deepEqual(result.option.series, base.series, `${item.name} 必须使用 base series`);
  }
});

// ============================================================================
// ⑥ 全空类目轴：返回明确的 stale / axis issue
// ============================================================================

test('类目轴 data 非空但全部是 null / 空白字符串：判定为 stale 并给出 axis issue', () => {
  // Arrange：真实 chart_price_promotion_1/2 与 chart_procurement_inventory_1/2 的 xAxis.data 都是 ['']
  const cases = [
    { name: '单个空串', xAxis: { type: 'category', data: [''] } },
    { name: 'null 与空串混排', xAxis: { type: 'category', data: [null, '', '   '] } },
    { name: 'undefined 与空白', xAxis: { type: 'category', data: [undefined, ' \t '] } },
    { name: '无显式 type 但带空白 data', xAxis: { data: [''] } },
  ];

  for (const item of cases) {
    const { content, base } = snapshot({
      groupOptions: { region: [{ type: 'bar', name: '地区', data: [1] }] },
      groupField: 'region',
      xAxis: item.xAxis,
    });

    // Act
    const result = resolveChartGroups(content, base);

    // Assert
    assert.equal(result.axisIssue, true, `${item.name} 必须判定为 stale 类目轴`);
    assert.equal(result.option, null, `${item.name} 不得再产出可绘制 option`);
    assert.deepEqual(result.groups, [], `${item.name} 不得显示分组控件`);
  }
});

test('正常非空类目、数值轴、无显式 data 的合法 option 不得误报 stale', () => {
  // Arrange
  const cases = [
    { name: '正常类目', xAxis: { type: 'category', data: ['华东', '华北'] } },
    { name: '单一合法类目', xAxis: { type: 'category', data: ['2025-01'] } },
    { name: '数值轴', xAxis: { type: 'value' } },
    { name: '带范围的值轴', xAxis: { type: 'value', min: 0, max: 100 } },
    { name: '没有 xAxis', xAxis: null },
    { name: '类目轴无 data 字段', xAxis: { type: 'category' } },
    { name: 'xAxis 为空数组', xAxis: [] },
    { name: 'y 轴全空不影响判定', xAxis: { type: 'category', data: ['华东'] }, yAxis: { type: 'value', data: [''] } },
  ];

  for (const item of cases) {
    const { content, base } = snapshot({ groupOptions: null, xAxis: item.xAxis });
    if (item.yAxis) base.yAxis = item.yAxis;

    // Act
    const result = resolveChartGroups(content, base);

    // Assert
    assert.equal(result.axisIssue, false, `${item.name} 不得误报 stale`);
    assert.notEqual(result.option, null, `${item.name} 必须照常绘制`);
  }
});

test('双轴形态：xAxis 是数组时同样能识别 stale 与正常', () => {
  // Arrange
  const stale = snapshot({
    groupOptions: null,
    xAxis: [{ type: 'category', data: [''] }, { type: 'value' }],
  });
  const fine = snapshot({
    groupOptions: null,
    xAxis: [{ type: 'category', data: ['华东', '华北'] }, { type: 'value' }],
  });

  // Act
  const staleResult = resolveChartGroups(stale.content, stale.base);
  const fineResult = resolveChartGroups(fine.content, fine.base);

  // Assert
  assert.equal(staleResult.axisIssue, true);
  assert.equal(fineResult.axisIssue, false);
});

// ============================================================================
// 判定依据：只能是 xAxis 实际取值
// ============================================================================

test('过时的 content.chart_type / x_field 不参与判定：以 xAxis 实际取值为准', () => {
  // Arrange：content 声称是时间列 year_month，但 xAxis 实际是分类值（真实 chart_target_achievement_1 的形态）
  const staleMeta = snapshot({
    groupOptions: { __mom__: [{ type: 'bar', name: '环比(%)', data: [1, 2] }], year_month: [{ type: 'bar', name: '月份', data: [3, 4] }] },
    groupField: 'year_month',
    xAxis: { type: 'category', data: ['missed', 'summary'] },
  });
  staleMeta.content.x_field = 'year_month';
  staleMeta.content.chart_type = 'line';

  // Act
  const result = resolveChartGroups(staleMeta.content, staleMeta.base);

  // Assert：xAxis 是分类值，__mom__ 必须被移除（旧实现若信任 x_field 会把它当成时间轴）
  assert.deepEqual(result.groups, ['year_month']);
  assert.equal('__mom__' in result.options, false);
});

test('显式指定 activeGroup：合法则生效，非法则回落到默认分组', () => {
  // Arrange
  const { content, base } = snapshot({
    groupOptions: {
      channel_code: [{ type: 'bar', name: '渠道', data: [1, 2] }],
      region_code: [{ type: 'bar', name: '地区', data: [3, 4] }],
    },
    groupField: 'channel_code',
    xAxis: { type: 'category', data: ['华东', '华北'] },
  });

  // Act
  const valid = resolveChartGroups(content, base, 'region_code');
  const invalid = resolveChartGroups(content, base, '已被过滤掉的分组');

  // Assert
  assert.equal(valid.activeGroup, 'region_code');
  assert.deepEqual(valid.option.series, [{ type: 'bar', name: '地区', data: [3, 4] }]);
  assert.equal(invalid.activeGroup, 'channel_code');
});

// ============================================================================
// ⑦ 二阶比较：指标本身已是同比/环比时，旧快照的 __mom__ / __yoy__ 必须过滤
// ============================================================================

// 方法作用：构造一组「时间轴 + 三个分组选项」的旧快照，用于二阶比较防御用例。
// Args: seriesFields - content.series_fields 声明值。
// Returns: { content, base } 结构，横轴为 12 个月的月度时间轴。
function monthlyWithComparisons(seriesFields) {
  return snapshot({
    groupOptions: {
      __mom__: [{ type: 'line', name: '环比(%)', data: [1, 2] }],
      __yoy__: [{ type: 'line', name: '同比(%)', data: [1, 2] }],
      channel_code: [{ type: 'line', name: '渠道', data: [3, 4] }],
    },
    groupField: 'channel_code',
    xAxis: { type: 'category', data: months(12) },
    seriesType: 'line',
    seriesFields,
  });
}

test('时间轴 + series_fields 含同比/环比指标：旧快照的 __mom__ / __yoy__ 必须被过滤', () => {
  // Arrange：真实 chart_sales_foundation_4，series_fields=[net_sales_yoy_pct, net_sales_mom_pct]；
  // 旧快照由修复前的后端生成，指标已是比较值却又挂了自动比较视图——那是"同比的环比"。
  const cases = [
    { name: '单个同比指标', fields: ['net_sales_yoy_pct'] },
    { name: '单个环比指标', fields: ['net_sales_mom_pct'] },
    { name: '真实声明（同比 + 环比）', fields: ['net_sales_yoy_pct', 'net_sales_mom_pct'] },
    { name: '普通金额与比较指标混排', fields: ['net_sales_amount', 'net_sales_yoy_pct'] },
    { name: '中文标签指标', fields: ['净销售额同比'] },
  ];

  for (const item of cases) {
    const { content, base } = monthlyWithComparisons(item.fields);

    // Act
    const result = resolveChartGroups(content, base);

    // Assert：两个派生比较项都被剔除，业务分组照旧保留
    assert.deepEqual(result.groups, ['channel_code'], `${item.name}：二阶比较视图必须被过滤`);
    assert.equal('__mom__' in result.options, false, `${item.name}：__mom__ 必须被移除`);
    assert.equal('__yoy__' in result.options, false, `${item.name}：__yoy__ 必须被移除`);
    assert.equal(result.initialGroup, 'channel_code', `${item.name}：默认分组必须落回业务分组`);
  }
});

test('时间轴 + 普通原始指标：合法比较项继续保留', () => {
  // Arrange：普通金额指标的月度环比/同比是合法比较，不得被二阶比较防御误伤。
  const cases = [
    { name: '净销售额', fields: ['net_sales_amount'] },
    { name: '未声明指标', fields: [] },
    { name: 'series_fields 缺失', fields: undefined },
  ];

  for (const item of cases) {
    const { content, base } = monthlyWithComparisons(item.fields);
    if (item.fields === undefined) delete content.series_fields;

    // Act
    const result = resolveChartGroups(content, base);

    // Assert
    assert.deepEqual(result.groups, ['__mom__', '__yoy__', 'channel_code'], `${item.name}：合法比较项必须保留`);
    assert.equal(result.initialGroup, 'channel_code', `${item.name}：默认分组不变`);
  }
});

test('momentum_score 不得被误判为环比指标', () => {
  // Arrange：momentum_score 只是以 mom 开头，不是环比指标；按 token 边界判定才不误伤。
  const { content, base } = monthlyWithComparisons(['momentum_score']);

  // Act
  const result = resolveChartGroups(content, base);

  // Assert
  assert.deepEqual(result.groups, ['__mom__', '__yoy__', 'channel_code']);
});

test('分类轴 + 比较指标指标：两个比较项仍被剔除，且不误报 axisIssue', () => {
  // Arrange：分类轴本来就剔比较项；叠加比较指标防御后结果必须一致（两条规则不冲突）。
  const { content, base } = snapshot({
    groupOptions: {
      __mom__: [{ type: 'bar', name: '环比(%)', data: [1, 2] }],
      region: [{ type: 'bar', name: '地区', data: [3, 4] }],
    },
    groupField: 'region',
    xAxis: { type: 'category', data: ['华东', '华北'] },
    seriesFields: ['net_sales_yoy_pct'],
  });

  // Act
  const result = resolveChartGroups(content, base);

  // Assert
  assert.equal(result.axisIssue, false);
  assert.deepEqual(result.groups, ['region']);
  assert.equal('__mom__' in result.options, false);
});

test('二阶比较防御保持纯度：base 与 group_options 原始对象不被修改', () => {
  // Arrange
  const { content, base } = monthlyWithComparisons(['net_sales_yoy_pct']);
  const contentBefore = JSON.stringify(content);
  const baseBefore = JSON.stringify(base);

  // Act
  const result = resolveChartGroups(content, base);

  // Assert
  assert.equal(JSON.stringify(content), contentBefore, 'content 必须逐字节不变');
  assert.equal(JSON.stringify(base), baseBefore, 'base 必须逐字节不变');
  assert.notEqual(result.option, base, '返回的 option 必须是新对象');
});
