// 图表分组切换的布局契约测试（node:test，无新增依赖）。
//
// 事实依据（Playwright 对真实分享页的 390px 实测）：
//   http://localhost:5173/shared/reports/2gwR2fiF5Zh_RnVt6QKhfOBqpQnkenYQ335yPtrvU2s
//   修复前 documentElement.scrollWidth=646 > clientWidth=390，最大溢出元素是
//   .ant-segmented（宽 636）——分组标签是完整业务名，控件的 min-content 宽度把整页撑开。
//   修复后 scrollWidth=390 / overflow=0，控件在自己的容器内横向滚动（scrollWidth 449 >
//   clientWidth 370，滚动后最后一个选项可见）。
//
// 本文件把「滚动发生在控件容器内」这一契约钉在源码上：CSS 规则被删或被改回
// overflow: visible / hidden，整页溢出就会复发或被截断到不可访问。

import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { test } from 'node:test';

const STYLES = readFileSync(new URL('../src/index.css', import.meta.url), 'utf8');
const RENDERER = readFileSync(new URL('../src/components/report/ReportDocumentRenderer.tsx', import.meta.url), 'utf8');

// 方法作用：从 CSS 文本中抽出指定选择器的声明块并解析成键值对。
// Args: selector - 选择器文本；支持逗号分组的选择器列表（如 `.a,\n.b { ... }`）。
// Returns: 声明名到值的映射；选择器不存在时返回 null。
function ruleOf(selector) {
  // 注释里可能带花括号或冒号，先剥掉再解析，避免把注释当成声明。
  const css = STYLES.replace(/\/\*[\s\S]*?\*\//g, '');
  for (const block of css.split('}')) {
    const brace = block.indexOf('{');
    if (brace === -1) continue;
    const selectors = block.slice(0, brace).split(',').map(item => item.trim());
    if (!selectors.includes(selector)) continue;
    const declarations = {};
    for (const part of block.slice(brace + 1).split(';')) {
      const [name, ...rest] = part.split(':');
      if (!name || rest.length === 0) continue;
      declarations[name.trim()] = rest.join(':').trim();
    }
    return declarations;
  }
  return null;
}

test('分组切换容器让滚动发生在控件内部而不是整页', () => {
  // Arrange / Act
  const rule = ruleOf('.report-chart-group-switch');

  // Assert
  assert.ok(rule, 'index.css 缺少 .report-chart-group-switch 规则');
  assert.equal(rule['overflow-x'], 'auto', '容器必须自己横向滚动');
  assert.equal(rule['max-width'], '100%', '容器不得超过父级宽度');
  assert.equal(rule['min-width'], '0', 'flex 子项必须允许收缩，否则仍会撑宽父级');
});

test('容器不隐藏也不截断控件：横向滚动条是唯一可达手段', () => {
  // Arrange / Act
  const rule = ruleOf('.report-chart-group-switch');

  // Assert：hidden / clip 会让窄屏下的切换项彻底不可达
  assert.notEqual(rule['overflow-x'], 'hidden');
  assert.notEqual(rule['overflow-x'], 'clip');
  assert.notEqual(rule['display'], 'none');
});

test('容器内保持内容宽度，选项不被压缩到不可读', () => {
  // Arrange / Act
  const rule = ruleOf('.report-chart-group-switch > .ant-segmented');

  // Assert：宽度被反向压回容器宽时横向滚动无从触发，长标签会被挤成省略号
  assert.ok(rule, '缺少容器内 Segmented 的宽度规则');
  assert.equal(rule.width, 'max-content');
});

test('渲染器把分组切换包进该容器，且仍用 Segmented 做模式切换', () => {
  // Arrange
  const wrapperIndex = RENDERER.indexOf('<div className="report-chart-group-switch">');

  // Act
  const segmentedIndex = RENDERER.indexOf('<Segmented', wrapperIndex);

  // Assert：wrapper 必须紧邻包裹 Segmented，控件本身不被替换或移除
  assert.notEqual(wrapperIndex, -1, '渲染器未给分组切换加明确容器');
  assert.notEqual(segmentedIndex, -1, '容器内必须是 Segmented 切换控件');
  assert.ok(segmentedIndex > wrapperIndex, 'Segmented 必须在容器内部');
  assert.match(RENDERER.slice(wrapperIndex, segmentedIndex + 400), /onChange=/, '切换回调不得丢失');
});

test('图表块的 flex 链路允许收缩，避免子项把页面撑开', () => {
  // Arrange / Act
  const spaceRule = ruleOf('.report-block-content .ant-space');
  const itemRule = ruleOf('.report-block-content .ant-space-item');

  // Assert
  assert.ok(spaceRule, '缺少 .report-block-content .ant-space 规则');
  assert.equal(spaceRule['min-width'], '0');
  assert.ok(itemRule, '缺少 .report-block-content .ant-space-item 规则');
  assert.equal(itemRule['max-width'], '100%');
});
