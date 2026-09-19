// reportContent 行为测试：真正调用 toTextList / joinTextList 并断言返回值，不做源码字符串断言。
// 运行方式：npm run test:report-content（等价 node --test tests/reportContent.test.mjs）。
// 依赖：Node 24 原生 TypeScript 类型擦除 + node:test，不新增任何依赖。
//
// 背景（真实分享接口取证，非推测）：分享页 22 个 quality 块的 content.limitations 是 string[]，
// 渲染层却用 displayText() 兜底 JSON.stringify，浏览器实际显示 `[限制一,限制二]`——引号、
// 方括号与逗号全部暴露；relationship 的 join_keys / limitations 是同一展示层根因。
// 因此展示层必须有「未知值 → 安全 string[]」的纯函数，且不得把 JSON 标点带进用户可见文本。

import test from 'node:test';
import assert from 'node:assert/strict';

import { joinTextList, toTextList } from '../src/components/report/reportContent.ts';

// JSON 标点集合：这些字符只可能来自 JSON.stringify，绝不允许出现在逐条展示的文本里。
const JSON_PUNCTUATION = ['[', ']', '{', '}', '"'];

test('字符串数组：逐项原样成为独立条目，不带任何 JSON 标点', () => {
  // Arrange：还原真实分享页 quality 块的 content.limitations 形态。
  const limitations = ['样本截断在 5000 行', '部分数据源权限受限'];

  // Act
  const items = toTextList(limitations);

  // Assert
  assert.deepEqual(items, ['样本截断在 5000 行', '部分数据源权限受限']);
  for (const item of items) {
    for (const punctuation of JSON_PUNCTUATION) {
      assert.equal(item.includes(punctuation), false, `条目不得含 JSON 标点 ${punctuation}：${item}`);
    }
  }
});

test('单字符串：包成单元素数组，内容原样保留', () => {
  // Arrange
  const value = '未发现共同连接字段，跨源关系未建立';

  // Act + Assert
  assert.deepEqual(toTextList(value), [value]);
});

test('null / undefined / 空数组 / 空字符串：一律返回空列表，不抛错', () => {
  // Arrange：含「空白字符串」这一等价空值。
  const cases = [null, undefined, [], '', '   ', '\n\t'];

  for (const value of cases) {
    // Act
    const items = toTextList(value);

    // Assert
    assert.deepEqual(items, [], `${JSON.stringify(value)} 必须归一为空列表`);
  }
});

test('数字与布尔：安全转成文本，非有限数值不算内容', () => {
  // Arrange + Act + Assert
  assert.deepEqual(toTextList(0), ['0']);
  assert.deepEqual(toTextList(12.5), ['12.5']);
  assert.deepEqual(toTextList(-3), ['-3']);
  assert.deepEqual(toTextList(true), ['true']);
  assert.deepEqual(toTextList(false), ['false']);
  // NaN / Infinity 经 JSON.stringify 会变成字符串 "null"，那是假内容，必须丢弃。
  assert.deepEqual(toTextList(Number.NaN), []);
  assert.deepEqual(toTextList(Number.POSITIVE_INFINITY), []);
});

test('对象：安全序列化为纯文本，不做 HTML 注入', () => {
  // Arrange
  const value = { field: 'region_code', reason: '全空' };

  // Act
  const items = toTextList(value);

  // Assert
  assert.equal(items.length, 1);
  assert.equal(items[0], '{"field":"region_code","reason":"全空"}');
});

test('数组内混入空值与对象：逐项归一，空值不产生空条目', () => {
  // Arrange：真实载荷里 quality 数组可能混入 null（服务端拼接空串时）。
  const value = ['第一条', null, '', undefined, '   ', 7, { code: 'R1' }];

  // Act
  const items = toTextList(value);

  // Assert
  assert.deepEqual(items, ['第一条', '7', '{"code":"R1"}']);
});

test('不可序列化值（含循环引用）：丢弃该条而不是抛错', () => {
  // Arrange：循环引用会让 JSON.stringify 抛 TypeError。
  const circular = {};
  circular.self = circular;

  // Act + Assert：整体与嵌套两种入口都不能冒泡异常。
  assert.deepEqual(toTextList(circular), []);
  assert.deepEqual(toTextList(['保留这条', circular, '也保留这条']), ['保留这条', '也保留这条']);
});

test('纯函数：入参数组与嵌套对象都不被修改', () => {
  // Arrange
  const nested = { code: 'R1' };
  const value = ['第一条', nested];

  // Act
  toTextList(value);

  // Assert
  assert.deepEqual(value, ['第一条', { code: 'R1' }]);
  assert.equal(Object.keys(nested).length, 1);
});

test('joinTextList：多条目用指定分隔符连接，绝不退回 JSON 展示', () => {
  // Arrange：关系块的 join_keys 用中文顿号，limitations 用分号。
  const joinKeys = ['地区', '年月'];

  // Act + Assert
  assert.equal(joinTextList(joinKeys, '、'), '地区、年月');
  assert.equal(joinTextList(['关系证据不代表因果关系'], '；'), '关系证据不代表因果关系');
  assert.equal(joinTextList(['a', 'b', 'c'], '；'), 'a；b；c');
});

test('joinTextList：无内容时返回破折号，单值不额外加分隔符', () => {
  // Arrange + Act + Assert
  assert.equal(joinTextList(null, '、'), '—');
  assert.equal(joinTextList([], '、'), '—');
  assert.equal(joinTextList('', '、'), '—');
  assert.equal(joinTextList(['唯一值'], '、'), '唯一值');
});
