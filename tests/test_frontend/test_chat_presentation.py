"""覆盖 28.1—28.3：公开流的状态行为与数据优先布局。"""

import json
import subprocess
from pathlib import Path

import pytest


class TestChatPresentation:
    """用实际 TypeScript reducer 验证并行、重试与全局开关。"""

    # 执行纯状态变更，确认流式正文不被忽略也不跨流拼接。
    # Args: self - 测试实例。
    # Returns: 无返回值。
    def test_public_reducer_stream_isolation_and_process_gate(self) -> None:
        """过程关闭不存摘要，两个流独立积累，reset 删除重试旧正文。"""
        # Arrange
        script = r"""
const fs = require('fs');
const ts = require('./frontend/node_modules/typescript');
const compiled = ts.transpileModule(fs.readFileSync('frontend/src/hooks/chatPresentation.ts', 'utf8'), { compilerOptions: { module: ts.ModuleKind.CommonJS }}).outputText;
const container = {exports: {}};
new Function('module', 'exports', compiled)(container, container.exports);
const apply = container.exports.applyPublicEvent;
let state = {tokens: '', streamBuffers: {}, activeStreamId: '', processEnabled: false, processSummaries: {}};
state = {...state, ...apply(state, {type: 'process_summary', node: 'analyze_result', stream_id:'p', summary:'隐藏'})};
const hidden = Object.keys(state.processSummaries).length;
state = {...state, ...apply(state, {type: 'answer_reset', node:'analyze_result', stream_id:'a'})};
state = {...state, ...apply(state, {type: 'answer_delta', node:'analyze_result', stream_id:'a', content:'甲'})};
state = {...state, ...apply(state, {type: 'answer_reset', node:'analyze_result', stream_id:'b'})};
state = {...state, ...apply(state, {type: 'answer_delta', node:'analyze_result', stream_id:'b', content:'乙'})};
state = {...state, ...apply(state, {type: 'answer_delta', node:'analyze_result', stream_id:'a', content:'一'})};
const buffers = {...state.streamBuffers};
const visible = state.tokens;
state = {...state, ...apply(state, {type: 'presentation', process_summary_enabled:true})};
state = {...state, ...apply(state, {type: 'process_summary', node:'analyze_result', stream_id:'p', summary:'公开',status:'done',elapsed_ms:10})};
const shown = state.processSummaries.p.summary;
state = {...state, ...apply(state, {type: 'answer_reset', node:'analyze_result', stream_id:'a'})};
state = {...state, ...apply(state, {type: 'presentation', process_summary_enabled:false})};
process.stdout.write(JSON.stringify({hidden, visible, buffers, shown, reset:state.streamBuffers.a.tokens, cleared:Object.keys(state.processSummaries).length}));
"""
        # Act
        result = subprocess.run(
            ["node", "-e", script], check=True, capture_output=True, text=True, encoding="utf-8"
        )
        values = json.loads(result.stdout)
        # Assert
        assert values["hidden"] == 0
        assert values["visible"] == "乙"
        assert values["buffers"]["a"]["tokens"] == "甲一"
        assert values["buffers"]["b"]["tokens"] == "乙"
        assert values["shown"] == "公开"
        assert values["reset"] == ""
        assert values["cleared"] == 0

    # 验证数据优先渲染且公开正文在最终结果前已有组件承载。
    # Args: self - 测试实例。
    # Returns: 无返回值。
    def test_data_first_layout_and_streaming_contract(self) -> None:
        """图表和表格不是收在正文后面的折叠项。"""
        # Arrange
        card = Path("frontend/src/components/ResultCard.tsx").read_text(encoding="utf-8")
        hook = Path("frontend/src/hooks/useChat.ts").read_text(encoding="utf-8")
        # Act / Assert
        assert (
            card.index('data-section="chart"')
            < card.index('data-section="data"')
            < card.index('data-section="analysis"')
        )
        assert "case 'answer_delta':" in hook
        assert "case 'data':" in hook
        assert (
            "status: 'done'"
            not in hook.split("case 'result':", 1)[1].split("case 'retry_status':", 1)[0]
        )

    # 验证正常结束、断流及 UTF-8 分片都通过真实读取器处理。
    # Args: self - 实例；terminal - 结束协议；expected - 回调类型。
    # Returns: 无返回值。
    @pytest.mark.parametrize(
        "terminal,expected", [("complete", "done"), ("missing", "error"), ("error", "error")]
    )
    def test_transport_requires_terminal_event(self, terminal: str, expected: str) -> None:
        """没有 done 就 EOF 不能冒充成功；中文分字节传输不损坏。"""
        # Arrange
        script = r"""
const fs = require('fs');
const ts = require('./frontend/node_modules/typescript');
const compiled = ts.transpileModule(fs.readFileSync('frontend/src/api/client.ts', 'utf8'), {compilerOptions:{module:ts.ModuleKind.CommonJS}}).outputText;
const container = {exports:{}};
new Function('require','module','exports',compiled)(() => ({getLanguage:()=> 'zh-CN', t:text=>text}),container,container.exports);
const terminal = process.argv[1];
let text = 'data: ' + JSON.stringify({type:'answer_delta',content:'增长'}) + '\n\n';
if (terminal !== 'missing') text += 'data: ' + JSON.stringify({type:'done',status:terminal}) + '\n\n';
const bytes = new TextEncoder().encode(text);
global.fetch = async () => new Response(new ReadableStream({start(controller){
  for (let index=0;index<bytes.length;index++) controller.enqueue(bytes.subarray(index,index+1));
  controller.close();
}}), {status:200});
const events=[];
new Promise(resolve => container.exports.streamChat('q','','', event=>events.push(event),
  ()=>resolve({callback:'done',events}), message=>resolve({callback:'error',message,events})))
  .then(result => process.stdout.write(JSON.stringify(result)));
"""
        # Act
        result = subprocess.run(
            ["node", "-e", script, terminal],
            check=True,
            capture_output=True,
            text=True,
            encoding="utf-8",
        )
        values = json.loads(result.stdout)
        # Assert
        assert values["callback"] == expected
        assert values["events"][0]["content"] == "增长"

    # 验证重复坐标失败时前端提供按名称合并求和的显式口径操作。
    # Args: self - 实例。
    # Returns: 无返回值。
    def test_failed_chart_offers_explicit_merge_action(self) -> None:
        """图表失败时允许用户显式选择合并求和口径后重画，并带英文文案。"""
        # Arrange
        panel = Path("frontend/src/components/ChartPanel.tsx").read_text(encoding="utf-8")
        english = Path("frontend/src/i18n/en.ts").read_text(encoding="utf-8")
        # Act / Assert
        assert "status === 'failed'" in panel
        assert "按名称合并求和" in panel
        assert "aggregation: 'sum'" in panel
        assert '"按名称合并求和":' in english

    # 验证类型切换不能把模型自给的聚合口径带进重画请求（合并确认闸门不回退）。
    # Args: self - 实例。
    # Returns: 无返回值。
    def test_adjust_plan_gates_unconfirmed_model_aggregation(self) -> None:
        """未确认合并时请求必须移除模型 sum/avg；确认后必须沿用已确认口径。"""
        # Arrange：以真实 TypeScript 转译并执行 ChartPanel 的公开纯函数。
        script = r"""
const fs = require('fs');
const ts = require('./frontend/node_modules/typescript');
const compiled = ts.transpileModule(
  fs.readFileSync('frontend/src/components/ChartPanel.tsx', 'utf8'),
  {compilerOptions: {module: ts.ModuleKind.CommonJS, jsx: ts.JsxEmit.React}},
).outputText;
const stubs = {
  'react': {lazy: () => () => null, Suspense: null, useEffect: () => {}, useMemo: fn => fn(),
            useRef: value => ({current: value}), useState: value => [value, () => {}]},
  'antd': {Button: null, Dropdown: null, Empty: null, message: {warning(){}, error(){}},
           Space: null, Tooltip: null, Typography: {Text: null, Paragraph: null}},
  '@ant-design/icons': {BarChartOutlined: null, DownOutlined: null},
  '../api/client': {post: async () => ({})},
  './DataTable': {default: null},
  './chatChartLayout': {buildChatChartLayout: () => ({height: 0, option: {}})},
  '../i18n': {getLanguage: () => 'zh-CN', t: text => text, useLanguage: () => 'zh-CN'},
  './EChartsView': {default: null},
};
const container = {exports: {}};
new Function('require', 'module', 'exports', compiled)(
  id => { if (!(id in stubs)) throw new Error('未桩接的依赖: ' + id); return stubs[id]; },
  container, container.exports);
const {buildAdjustPlan, readAdjustError} = container.exports;
const modelSum = {x: 'workshop_name', metrics: ['total_output_qty'], aggregation: 'sum'};
process.stdout.write(JSON.stringify({
  unconfirmed: buildAdjustPlan(modelSum, false),
  confirmed: buildAdjustPlan(modelSum, true),
  emptyUnconfirmed: buildAdjustPlan(undefined, false),
  duplicateError: readAdjustError(new Error('重复坐标缺少明确聚合口径，请增加系列/分面维度或指定聚合')),
  otherError: readAdjustError(new Error('饼图不支持负值，请使用柱状图')),
  emptyError: readAdjustError(null),
}));
"""
        # Act
        result = subprocess.run(
            ["node", "-e", script], check=True, capture_output=True, text=True, encoding="utf-8"
        )
        values = json.loads(result.stdout)
        # Assert：未确认 → 无 aggregation，但角色字段保留；确认 → 保留 sum。
        assert "aggregation" not in values["unconfirmed"]
        assert values["unconfirmed"]["x"] == "workshop_name"
        assert values["confirmed"]["aggregation"] == "sum"
        assert "aggregation" not in values["emptyUnconfirmed"]
        # Assert：重复坐标错误改为面向用户的合并确认指引，其他错误原样透出。
        assert "按名称合并求和" in values["duplicateError"]
        assert "重复坐标" not in values["duplicateError"]
        assert values["otherError"] == "饼图不支持负值，请使用柱状图"
        assert values["emptyError"] == "图表调整失败"
