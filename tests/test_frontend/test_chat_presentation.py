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
