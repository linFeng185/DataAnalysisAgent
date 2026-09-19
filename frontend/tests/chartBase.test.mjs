// chartBase 行为测试：真正调用 withChartBaseline 并断言返回对象，不做源码字符串断言。
// 运行方式：npm run test:chart（等价 node --test tests/chartBase.test.mjs）。
// 依赖：Node 24 原生 TypeScript 类型擦除 + node:test，不新增任何依赖。
//
// 背景（真实分享接口取证，非推测）：旧快照里 20 张纯柱状图的持久化 option 把
// tooltip.trigger 写成 axis（9 张 1 系列 / 8 张 2 系列 / 3 张 8 系列），
// 悬停单柱会弹出同横轴整批系列的读数。前端必须在渲染期按**实际 series** 纠偏，
// 且不能依赖 content.chart_type——渲染器是先替换分组 series 再调用本函数的。

import test from 'node:test';
import assert from 'node:assert/strict';

import { isDarkSurface, withChartBaseline } from '../src/components/report/chartBase.ts';

// 方法作用：构造浅色模板 token，取值对齐 insight 模板。
// Args: overrides - 需要覆盖的 token 键。
// Returns: 字段完整、可被 withChartBaseline 直接消费的 token 对象。
function lightTokens(overrides = {}) {
  return {
    accent: '#2563eb',
    accentSoft: '#eff6ff',
    bg: '#f7f8fa',
    surface: '#ffffff',
    border: '#e8eaed',
    textPrimary: '#1f2329',
    textSecondary: '#646a73',
    fontFamily: 'sans-serif',
    headingFontFamily: 'sans-serif',
    radius: '10px',
    shadow: 'none',
    spaceUnit: '16px',
    density: 'comfortable',
    chartPalette: ['#2563eb', '#14b8a6'],
    sequentialPalette: ['#cde2fb', '#0d366b'],
    heroBg: '#0f1a2e',
    ...overrides,
  };
}

// 方法作用：构造深色模板 token，取值对齐 ink 模板。
// Args: 无。
// Returns: 深色 token 对象。
function darkTokens() {
  return lightTokens({
    bg: '#0b1220',
    surface: '#111c2e',
    border: '#1e293b',
    textPrimary: '#e2e8f0',
    textSecondary: '#94a3b8',
  });
}

test('旧快照单系列柱状图：axis 纠正为 item，遗留 axisPointer 被删除，其余 tooltip 设置保留', () => {
  // Arrange：还原旧快照形态——纯 bar，但 trigger 是 axis 且带残留 axisPointer。
  const option = {
    tooltip: { trigger: 'axis', axisPointer: { type: 'shadow' }, backgroundColor: '#fffef8', borderColor: '#dddddd' },
    xAxis: { type: 'category', data: ['华东', '华北'] },
    yAxis: { type: 'value' },
    series: [{ type: 'bar', name: '金额', data: [12, 30] }],
  };

  // Act
  const result = withChartBaseline(option, lightTokens());

  // Assert
  assert.equal(result.tooltip.trigger, 'item');
  assert.equal('axisPointer' in result.tooltip, false);
  assert.equal(result.tooltip.backgroundColor, '#fffef8');
  assert.equal(result.tooltip.borderColor, '#dddddd');
});

test('多系列纯柱状图（2 系列 / 8 系列）：同样纠正为 item', () => {
  for (const count of [2, 8]) {
    // Arrange
    const series = Array.from({ length: count }, (_, index) => ({
      type: 'bar',
      name: `系列${index}`,
      data: [index, index + 1],
    }));
    const option = { tooltip: { trigger: 'axis' }, series };

    // Act
    const result = withChartBaseline(option, lightTokens());

    // Assert
    assert.equal(result.tooltip.trigger, 'item', `${count} 系列纯柱图必须按单柱触发`);
    assert.equal('axisPointer' in result.tooltip, false);
  }
});

test('纯折线图：保持 axis 并注入十字准线', () => {
  // Arrange
  const option = { tooltip: { trigger: 'axis' }, series: [{ type: 'line', data: [1, 2, 3] }] };

  // Act
  const result = withChartBaseline(option, lightTokens());

  // Assert
  assert.equal(result.tooltip.trigger, 'axis');
  assert.equal(result.tooltip.axisPointer.type, 'cross');
});

test('混合图（bar + line）：绝不改成 item，按轴触发保留 axis', () => {
  // Arrange
  const option = {
    tooltip: { trigger: 'axis' },
    series: [{ type: 'bar', data: [1, 2] }, { type: 'line', data: [3, 4] }],
  };

  // Act
  const result = withChartBaseline(option, lightTokens());

  // Assert
  assert.equal(result.tooltip.trigger, 'axis');
  assert.equal(result.tooltip.axisPointer.type, 'cross');
});

test('饼图与散点图：保持 item，不注入十字准线', () => {
  for (const type of ['pie', 'scatter']) {
    // Arrange
    const option = { tooltip: { trigger: 'item' }, series: [{ type, data: [1, 2] }] };

    // Act
    const result = withChartBaseline(option, lightTokens());

    // Assert
    assert.equal(result.tooltip.trigger, 'item');
    assert.equal('axisPointer' in result.tooltip, false);
  }
});

test('空 series / 缺 type / 非法 series：不猜类型，维持原 trigger', () => {
  // Arrange：这些都是「判不出是柱图」的形态，不能盲目改成 item。
  const cases = [
    { name: '空数组', series: [] },
    { name: '缺 type', series: [{ name: '金额', data: [1] }] },
    { name: '空字符串 type', series: [{ type: '', data: [1] }] },
    { name: '非数组 series', series: 'oops' },
    { name: '根本没有 series', series: undefined },
    { name: '混入非对象项', series: [{ type: 'bar' }, null] },
  ];

  for (const item of cases) {
    const option = { tooltip: { trigger: 'axis' }, series: item.series };

    // Act
    const result = withChartBaseline(option, lightTokens());

    // Assert
    assert.equal(result.tooltip.trigger, 'axis', `${item.name} 不应被判定为纯柱图`);
  }
});

test('函数保持纯度：传入 option 对象与嵌套 tooltip 都不被修改', () => {
  // Arrange
  const option = {
    tooltip: { trigger: 'axis', axisPointer: { type: 'shadow' } },
    series: [{ type: 'bar', data: [1, 2] }],
    xAxis: { type: 'category', data: ['a', 'b'] },
  };
  const before = JSON.stringify(option);

  // Act
  const result = withChartBaseline(option, lightTokens());

  // Assert
  assert.equal(JSON.stringify(option), before, '原 option 必须逐字节不变');
  assert.notEqual(result, option);
  assert.notEqual(result.tooltip, option.tooltip);
  assert.equal(option.tooltip.trigger, 'axis');
  assert.equal(option.tooltip.axisPointer.type, 'shadow');
});

test('深色模板：强制覆盖颜色之外，柱/线语义与浅色模板完全一致', () => {
  // Arrange
  const tokens = darkTokens();

  // Act：纯柱图（旧快照形态）
  const bar = withChartBaseline({
    tooltip: { trigger: 'axis', axisPointer: { type: 'line' }, backgroundColor: '#ffffff' },
    series: [{ type: 'bar', data: [1] }],
  }, tokens);
  // Act：纯折线
  const line = withChartBaseline({
    tooltip: { trigger: 'axis', backgroundColor: '#ffffff' },
    series: [{ type: 'line', data: [1] }],
  }, tokens);

  // Assert
  assert.equal(isDarkSurface(tokens.surface), true, 'ink 底色必须被判定为深色');
  assert.equal(bar.tooltip.trigger, 'item');
  assert.equal('axisPointer' in bar.tooltip, false);
  assert.equal(bar.tooltip.backgroundColor, '#111c2e');
  assert.equal(line.tooltip.trigger, 'axis');
  assert.equal(line.tooltip.axisPointer.type, 'cross');
  assert.equal(line.tooltip.backgroundColor, '#111c2e');
});

// ---------------------------------------------------------------------------
// 含负值纯柱图的数值轴居中（2026-09-19 增量）
//
// 背景（主代理实机取证，非推测）：「未达成门店缺口金额」与「毛利告警商品分布」各 24 个
// 全负柱值，yAxis.inverse=false，零线 y=38 落在 grid 上沿（grid 上下 38/260），
// 柱子从顶部往下垂。根因是自动范围在「全负」时退化到 [min, 0]，不是符号写错。
// 修复只发生在渲染期：把自动数值轴改成以 0 为对称中心的 min/max，原值与正负号一字不动。
// ---------------------------------------------------------------------------

// 方法作用：构造纯柱状图的 option，默认形态对齐后端 charts.py 的实际输出。
// Args: series - series 数组；overrides - 需要覆盖的根键。
// Returns: 可直接喂给 withChartBaseline 的 option。
function barOption(series, overrides = {}) {
  return {
    xAxis: { type: 'category', data: ['a', 'b'] },
    yAxis: { type: 'value', inverse: false },
    series,
    ...overrides,
  };
}

// 方法作用：判断数值是否为 1/2/5 × 10^k 形式的整值（轴上下界与刻度间隔的可读性依据）。
// Args: value - 待判断的数值。
// Returns: 是整值时返回 true。
function isNiceNumber(value) {
  if (!Number.isFinite(value) || value <= 0) return false;
  const exponent = Math.floor(Math.log10(value));
  const mantissa = value / 10 ** exponent;
  return [1, 2, 2.5, 5, 10].some(step => Math.abs(mantissa - step) < 1e-9);
}

// 方法作用：把结果里的轴配置统一取成对象数组，兼容单对象与数组两种写法。
// Args: axis - 结果中的 xAxis 或 yAxis。
// Returns: 轴对象数组。
function axisList(axis) {
  return Array.isArray(axis) ? axis : [axis];
}

test('全负柱图（实机取证形状：24 个全负值 + inverse:false）：数值轴改为以 0 为对称中心', () => {
  // Arrange：还原「未达成门店缺口金额」的形态——24 个全负柱值，零线原本贴在 grid 上沿。
  const values = Array.from({ length: 24 }, (_, index) => -(index + 1) * 100);
  const option = barOption(
    [{ type: 'bar', name: '缺口金额', data: values }],
    { xAxis: { type: 'category', data: Array.from({ length: 24 }, (_, index) => `门店${index + 1}`) } },
  );
  const before = JSON.stringify(option);

  // Act
  const result = withChartBaseline(option, lightTokens());

  // Assert：0 回到中轴，负值仍向下；上下界取数据最大绝对值 2400 的整值 2500，间隔 1250。
  assert.equal(result.yAxis.min, -2500);
  assert.equal(result.yAxis.max, 2500);
  assert.equal(result.yAxis.interval, 1250);
  assert.equal(result.yAxis.inverse, false, '禁止用反转轴代替居中');
  assert.deepEqual(result.series[0].data, values, '原值与正负号必须逐字保留，不得取绝对值');
  assert.equal(JSON.stringify(option), before, '输入 option 必须逐字节不变');
});

test('正负混合柱图：同样以 0 为对称中轴，正值向上、负值向下', () => {
  // Arrange：含负值即居中，上下界取两侧绝对值较大者。
  const option = barOption([{ type: 'bar', data: [-200, 100, 0] }]);
  const before = JSON.stringify(option);

  // Act
  const result = withChartBaseline(option, lightTokens());

  // Assert
  assert.equal(result.yAxis.min, -200);
  assert.equal(result.yAxis.max, 200);
  assert.equal(result.yAxis.interval, 100);
  assert.deepEqual(result.series[0].data, [-200, 100, 0]);
  assert.equal(JSON.stringify(option), before);
});

test('堆叠柱图按 samesign 正负分别累计取界：相反符号不抵消（审计实机复现形状）', () => {
  // Arrange：与主代理审计同一形状——三个 bar 同 stack，data 分别为 [-900] / [-900] / [900]。
  // ECharts 6.1 实例的 stackResultDimension 实测为 [-900, -1800, 900]：默认 samesign 策略把
  // 正负分别累计，负向真实边界是 -1800。按代数相加只会得到 -900，第二根负柱必然被截断
  // （logs/zero-axis-audit-20260919/stacked-bar-audit.json：extent [-1000,1000]、clipped=true）。
  const series = [
    { type: 'bar', name: 'a', stack: 'total', data: [-900] },
    { type: 'bar', name: 'b', stack: 'total', data: [-900] },
    { type: 'bar', name: 'c', stack: 'total', data: [900] },
  ];

  // Act
  const result = withChartBaseline(
    barOption(series, { xAxis: { type: 'category', data: ['测试类目'] } }),
    lightTokens(),
  );

  // Assert：下界必须覆盖真实的 -1800（而不是代数和的 -900），上界覆盖 +900。
  assert.ok(result.yAxis.min <= -1800, `负向堆叠真实边界 -1800 被截断：min=${result.yAxis.min}`);
  assert.ok(result.yAxis.max >= 900, `正向堆叠边界 900 必须覆盖：max=${result.yAxis.max}`);
  assert.equal(result.yAxis.min, -2000);
  assert.equal(result.yAxis.max, 2000);
});

test('堆叠柱图：同号多系列按类目累计，既不互相抵消也不退回单点取值', () => {
  // Arrange：三个全负系列同 stack → 逐类目累计 -900 / -1800 / -2100。
  const series = [
    { type: 'bar', name: 'a', stack: 'total', data: [-900] },
    { type: 'bar', name: 'b', stack: 'total', data: [-900] },
    { type: 'bar', name: 'c', stack: 'total', data: [-300] },
  ];

  // Act
  const result = withChartBaseline(
    barOption(series, { xAxis: { type: 'category', data: ['测试类目'] } }),
    lightTokens(),
  );

  // Assert：取到 -2100 的累计边界并上取整到 2500，而不是单点的 900。
  assert.ok(result.yAxis.min <= -2100, `同号累计边界被截断：min=${result.yAxis.min}`);
  assert.equal(result.yAxis.min, -2500);
  assert.equal(result.yAxis.max, 2500);
  assert.equal(result.yAxis.interval, 1250);
});

test('堆叠与非堆叠混排：未声明 stack 的系列按单点取值一并计入', () => {
  // Arrange：堆叠对逐类目为 [-1200/+300, -400/+100]，另有独立系列 -500。
  const series = [
    { type: 'bar', name: 'a', stack: 'total', data: [-1200, 100] },
    { type: 'bar', name: 'b', stack: 'total', data: [300, -400] },
    { type: 'bar', name: 'c', data: [-500, 0] },
  ];

  // Act
  const result = withChartBaseline(barOption(series), lightTokens());

  // Assert：负向边界 -1200、正向 +300 都必须落在轴内，独立系列 -500 同样不得越界。
  assert.equal(result.yAxis.min, -2000);
  assert.equal(result.yAxis.max, 2000);
  assert.ok(result.yAxis.min <= -1200 && result.yAxis.min <= -500, '堆叠与独立系列都要覆盖');
  assert.ok(result.yAxis.max >= 300);
});

test('stackStrategy 非默认值：判不出真实堆叠边界时保守不动', () => {
  // Arrange：ECharts 6 默认 samesign，其余策略的真实边界前端无法可靠推断。
  for (const strategy of ['all', 'positive', 'negative']) {
    const series = [
      { type: 'bar', name: 'a', stack: 'total', stackStrategy: strategy, data: [-900] },
      { type: 'bar', name: 'b', stack: 'total', stackStrategy: strategy, data: [-900] },
    ];
    const option = barOption(series, { xAxis: { type: 'category', data: ['测试类目'] } });

    // Act
    const result = withChartBaseline(option, lightTokens());

    // Assert
    assert.equal('min' in result.yAxis, false, `stackStrategy=${strategy} 必须保守不动`);
    assert.equal('max' in result.yAxis, false, `stackStrategy=${strategy} 必须保守不动`);
  }
});

test('对象项按 value 计入，null 缺值跳过，判不出的取值结构保守不动', () => {
  // Arrange：对象项 + 缺值混排。
  const objects = barOption([{ type: 'bar', data: [{ value: -120 }, null, { value: 60 }] }]);

  // Act
  const centered = withChartBaseline(objects, lightTokens());

  // Assert：-120 被计入、null 不参与取界；上界按整值规则把 120 上取到 200。
  assert.ok(centered.yAxis.min <= -120, '对象项的 value 必须参与取界');
  assert.equal(centered.yAxis.min, -200);
  assert.equal(centered.yAxis.max, 200);

  // Arrange：字符串、[x, y] 数对、非数值 value —— 都判不出真实取值。
  const unknownCases = [
    ['-120', 60],
    [[1, -120], [2, 60]],
    [{ value: '-120' }],
  ];
  for (const data of unknownCases) {
    const option = barOption([{ type: 'bar', data }]);
    const before = JSON.stringify(option);

    // Act
    const kept = withChartBaseline(option, lightTokens());

    // Assert：宁可不改，也不能按猜出来的范围画图。
    assert.equal('min' in kept.yAxis, false, `${JSON.stringify(data)} 判不出范围时必须保守不动`);
    assert.equal('max' in kept.yAxis, false);
    assert.equal('interval' in kept.yAxis, false);
    assert.equal(JSON.stringify(option), before);
  }
});

test('全正、全零、空数据与无有效数值：数值轴保持原样', () => {
  // Arrange：这些形态本来就没有「零线贴顶」问题，不得被改动。
  const cases = [
    { name: '全正', data: [1, 2, 3] },
    { name: '全零', data: [0, 0, 0] },
    { name: '空数据', data: [] },
    { name: '全缺值', data: [null, null] },
    { name: '非有限数值', data: [Number.NaN, Number.POSITIVE_INFINITY] },
  ];

  for (const item of cases) {
    const option = barOption([{ type: 'bar', data: item.data }]);
    const before = JSON.stringify(option);

    // Act
    const result = withChartBaseline(option, lightTokens());

    // Assert
    assert.equal('min' in result.yAxis, false, `${item.name} 不应被居中`);
    assert.equal('max' in result.yAxis, false, `${item.name} 不应被居中`);
    assert.equal('interval' in result.yAxis, false, `${item.name} 不应被居中`);
    assert.equal(JSON.stringify(option), before);
  }
});

test('非柱图与混合系列：一律不改数值轴范围', () => {
  // Arrange：柱图以外的类型没有「柱子从顶部下垂」的问题，混合系列则无法保证堆叠语义。
  const cases = [
    { name: '折线', series: [{ type: 'line', data: [-1, -2] }] },
    { name: '饼图', series: [{ type: 'pie', data: [{ value: -1 }, { value: -2 }] }] },
    { name: '散点', series: [{ type: 'scatter', data: [-1, -2] }] },
    { name: 'bar + line 混合', series: [{ type: 'bar', data: [-1, -2] }, { type: 'line', data: [-3, -4] }] },
    { name: 'series 非数组', series: 'oops' },
    { name: 'series 为空', series: [] },
  ];

  for (const item of cases) {
    const option = barOption(item.series, { yAxis: { type: 'value' } });

    // Act
    const result = withChartBaseline(option, lightTokens());

    // Assert
    assert.equal('min' in result.yAxis, false, `${item.name} 不应被居中`);
    assert.equal('max' in result.yAxis, false, `${item.name} 不应被居中`);
  }
});

test('显式范围、反向轴、对数轴与判不出结构的轴：保守不动', () => {
  // Arrange：后端显式声明过意图的轴，前端一律不覆盖。
  const cases = [
    { name: '显式 min', overrides: { yAxis: { type: 'value', min: -5000 } } },
    { name: '显式 max', overrides: { yAxis: { type: 'value', max: 0 } } },
    { name: '显式 interval', overrides: { yAxis: { type: 'value', interval: 500 } } },
    { name: 'min 为字符串表达式', overrides: { yAxis: { type: 'value', min: 'dataMin' } } },
    { name: '反向轴', overrides: { yAxis: { type: 'value', inverse: true } } },
    { name: '对数轴', overrides: { yAxis: { type: 'log' } } },
    { name: '未声明 type', overrides: { yAxis: {} } },
    { name: 'yAxis 数组两个元素', overrides: { yAxis: [{ type: 'value' }, { type: 'value' }] } },
    { name: 'x/y 都是数值轴', overrides: { xAxis: { type: 'value' }, yAxis: { type: 'value' } } },
    // 审查发现：x 是数值轴但显式声明了 min，剩下的 y 曾被误判成「唯一候选」而改轴。
    { name: 'x 数值轴显式 min + y 数值轴', overrides: { xAxis: { type: 'value', min: 0 }, yAxis: { type: 'value' } } },
    { name: 'x 数值轴显式 max + y 数值轴', overrides: { xAxis: { type: 'value', max: 100 }, yAxis: { type: 'value' } } },
    { name: 'log + value 搭配', overrides: { xAxis: { type: 'log' }, yAxis: { type: 'value' } } },
    { name: 'time + value 搭配', overrides: { xAxis: { type: 'time' }, yAxis: { type: 'value' } } },
    { name: '另一条轴缺失', overrides: { xAxis: undefined } },
  ];

  for (const item of cases) {
    const option = barOption([{ type: 'bar', data: [-100, -200] }], item.overrides);
    // 只覆盖 xAxis 的用例没有声明 yAxis，此时期望值就是「什么都没写」。
    const declared = item.overrides.yAxis ?? {};
    const before = JSON.stringify(option);

    // Act
    const result = withChartBaseline(option, lightTokens());

    // Assert：轴上的 min/max/interval 必须与后端写入的一模一样（未写入就仍然没有）。
    for (const axis of axisList(result.yAxis)) {
      assert.equal(axis.min, declared.min, `${item.name} 必须保守不动`);
      assert.equal(axis.max, declared.max, `${item.name} 必须保守不动`);
      assert.equal(axis.interval, declared.interval, `${item.name} 必须保守不动`);
    }
    assert.equal(JSON.stringify(option), before);
  }
});

test('系列显式声明轴索引：非 0 索引判不出绑定关系，保守不动；显式 0 仍生效', () => {
  // Arrange：yAxisIndex 指向第 1 条轴时，series 未必画在被改的那条轴上。
  const stray = barOption([
    { type: 'bar', data: [-100, -200], yAxisIndex: 1 },
  ]);

  // Act
  const strayResult = withChartBaseline(stray, lightTokens());

  // Assert
  assert.equal('min' in strayResult.yAxis, false, 'yAxisIndex=1 必须保守不动');
  assert.equal('max' in strayResult.yAxis, false);

  // Arrange：显式写 0 指向唯一那条轴，语义明确，照常居中。
  const explicitZero = barOption([{ type: 'bar', data: [-100, -200], yAxisIndex: 0 }]);

  // Act
  const zeroResult = withChartBaseline(explicitZero, lightTokens());

  // Assert
  assert.equal(zeroResult.yAxis.min, -200);
  assert.equal(zeroResult.yAxis.max, 200);
});

test('单元素轴数组与水平柱图：同样以 0 为对称中心（水平柱图左右居中）', () => {
  // Arrange：yAxis 写成单元素数组。
  const wrapped = barOption([{ type: 'bar', data: [-40, 10] }], { yAxis: [{ type: 'value' }] });

  // Act
  const centered = withChartBaseline(wrapped, lightTokens());

  // Assert：数组形状保持，元素被居中。
  assert.equal(Array.isArray(centered.yAxis), true);
  assert.equal(centered.yAxis[0].min, -50);
  assert.equal(centered.yAxis[0].max, 50);

  // Arrange：水平柱图——x 是数值轴、y 是类目轴。
  const horizontal = {
    xAxis: { type: 'value' },
    yAxis: { type: 'category', data: ['甲', '乙'] },
    series: [{ type: 'bar', data: [-40, 10] }],
  };

  // Act
  const result = withChartBaseline(horizontal, lightTokens());

  // Assert：居中发生在数值轴上，类目轴不动；零线也跟着变成竖直方向。
  assert.equal(result.xAxis.min, -50);
  assert.equal(result.xAxis.max, 50);
  assert.equal('min' in result.yAxis, false);
  assert.deepEqual(result.series[0].markLine.data, [{ xAxis: 0 }]);
});

test('刻度易读：上下界与间隔都取整值，0 恰好落在刻度上，且不截断数据', () => {
  // Arrange：不同量级的数据，上下界必须都能被 ECharts 分成可读刻度。
  for (const magnitude of [3, 47, 260, 1200, 99800, 0.07]) {
    const option = barOption([{ type: 'bar', data: [-magnitude, magnitude / 2] }]);

    // Act
    const result = withChartBaseline(option, lightTokens());
    const bound = result.yAxis.max;

    // Assert：上界是整值、覆盖全部数据、下界与之对称，0 正好是第 3 个刻度。
    assert.ok(isNiceNumber(bound), `${magnitude} → 上界 ${bound} 必须是 1/2/5 × 10^k 的整值`);
    assert.ok(bound >= magnitude, `${magnitude} → 上界 ${bound} 必须覆盖数据，不得截断`);
    assert.equal(result.yAxis.min, -bound);
    assert.equal(result.yAxis.interval, bound / 2);
    assert.equal(result.yAxis.min + 2 * result.yAxis.interval, 0, '0 必须落在刻度上（零线可见）');
  }
});

test('浅色与深色模板：居中范围只由数据决定，与主题无关', () => {
  // Arrange
  const option = barOption([{ type: 'bar', data: [-200, 100] }]);

  // Act
  const light = withChartBaseline(option, lightTokens());
  const dark = withChartBaseline(option, darkTokens());

  // Assert
  assert.equal(light.yAxis.min, -200);
  assert.equal(dark.yAxis.min, -200);
  assert.equal(light.yAxis.max, 200);
  assert.equal(dark.yAxis.max, 200);
  assert.equal(light.yAxis.interval, 100);
  assert.equal(dark.yAxis.interval, 100);
});

test('分组切换：负值分组切到正值分组后不留 min/max，两次调用互不污染', () => {
  // Arrange：渲染器每次切换分组都会用当前系列的 option 重新调用本函数。
  const negative = barOption([{ type: 'bar', data: [-100, -200] }]);
  const positive = barOption([{ type: 'bar', data: [100, 200] }]);
  const negativeBefore = JSON.stringify(negative);
  const positiveBefore = JSON.stringify(positive);

  // Act
  const first = withChartBaseline(negative, lightTokens());
  const second = withChartBaseline(positive, lightTokens());

  // Assert：负值组居中、正值组保持原样，且不能复用上一次的轴对象。
  assert.equal(first.yAxis.min, -200);
  assert.equal(second.yAxis.min, undefined);
  assert.equal(second.yAxis.max, undefined);
  assert.equal(second.yAxis.interval, undefined);
  assert.notEqual(second.yAxis, first.yAxis, '分组切换不得残留上一次的轴对象');
  assert.equal(JSON.stringify(negative), negativeBefore);
  assert.equal(JSON.stringify(positive), positiveBefore);
});

test('居中后的 option 保持纯度：原始轴对象、series 与 data 都不被就地修改', () => {
  // Arrange
  const yAxis = { type: 'value', name: '缺口金额', inverse: false };
  const data = [-100, -200];
  const option = {
    xAxis: { type: 'category', data: ['a', 'b'] },
    yAxis,
    series: [{ type: 'bar', data }],
  };
  const before = JSON.stringify(option);

  // Act
  const result = withChartBaseline(option, lightTokens());

  // Assert：轴对象是新建的，入参与其嵌套结构逐字节不变。
  assert.notEqual(result.yAxis, yAxis);
  assert.deepEqual(yAxis, { type: 'value', name: '缺口金额', inverse: false });
  assert.deepEqual(data, [-100, -200]);
  assert.equal(JSON.stringify(option), before);
});

// ---------------------------------------------------------------------------
// 0 参考线（审计要求：让零线比普通网格线略清晰，颜色随主题）
//
// 对称轴只保证「刻度里有一个 0」，网格线之间没有区别，零线仍然不显眼。
// 因此居中命中时给首系列补一条 silent 的 markLine，标签关闭、不参与 tooltip，
// 颜色取模板 textSecondary（浅色 #646a73 / 深色 #94a3b8），比 splitLine 的 border 更清晰。
// ---------------------------------------------------------------------------

test('0 参考线：居中命中时给首系列补一条随主题的零线，且只补一条', () => {
  // Arrange：多系列柱图，首系列没有自带 markLine。
  const option = barOption([
    { type: 'bar', name: '甲', data: [-200, 100] },
    { type: 'bar', name: '乙', data: [-50, 20] },
  ]);
  const before = JSON.stringify(option);

  // Act
  const light = withChartBaseline(option, lightTokens());
  const dark = withChartBaseline(option, darkTokens());

  // Assert：只给首系列补线，锚在数值轴的 0 上，且不显示标签、不可交互。
  assert.deepEqual(light.series[0].markLine.data, [{ yAxis: 0 }]);
  assert.equal(light.series[0].markLine.silent, true);
  assert.equal(light.series[0].markLine.symbol, 'none');
  assert.equal(light.series[0].markLine.label.show, false);

  // Assert：颜色随主题走，且都比普通网格线更清晰。
  assert.equal(light.series[0].markLine.lineStyle.color, lightTokens().textSecondary);
  assert.equal(dark.series[0].markLine.lineStyle.color, darkTokens().textSecondary);
  assert.notEqual(lightTokens().textSecondary, lightTokens().border);

  // Assert：其余系列不重复画线；原数据、原 series 字段与入参一字不动。
  assert.equal('markLine' in light.series[1], false);
  assert.deepEqual(light.series[0].data, [-200, 100]);
  assert.equal(JSON.stringify(option), before);
});

test('0 参考线只在居中命中时出现：全正、非柱图与判不出的形状都不补线', () => {
  // Arrange：这些形态本来就没有居中，也不该多出一条线。
  const cases = [
    { name: '全正柱图', series: [{ type: 'bar', data: [10, 20] }], yAxis: { type: 'value' } },
    { name: '折线图', series: [{ type: 'line', data: [-10, -20] }], yAxis: { type: 'value' } },
    { name: '混合系列', series: [{ type: 'bar', data: [-10] }, { type: 'line', data: [-20] }], yAxis: { type: 'value' } },
    { name: '显式范围', series: [{ type: 'bar', data: [-10, -20] }], yAxis: { type: 'value', min: -100 } },
  ];

  for (const item of cases) {
    // Act
    const result = withChartBaseline(barOption(item.series, { yAxis: item.yAxis }), lightTokens());

    // Assert
    assert.equal('markLine' in result.series[0], false, `${item.name} 不应补零线`);
  }
});

test('首系列已自带 markLine：原样保留，不被零线覆盖', () => {
  // Arrange：后端若已声明 markLine，前端不得用零线顶掉它的原意。
  const own = { silent: false, symbol: 'pin', data: [{ yAxis: 500 }] };
  const option = barOption([{ type: 'bar', data: [-200], markLine: own }]);
  const before = JSON.stringify(option);

  // Act
  const result = withChartBaseline(option, lightTokens());

  // Assert：轴照常居中，markLine 逐字保留。
  assert.equal(result.yAxis.min, -200);
  assert.equal(result.yAxis.max, 200);
  assert.deepEqual(result.series[0].markLine, own);
  assert.equal(JSON.stringify(option), before);
});
