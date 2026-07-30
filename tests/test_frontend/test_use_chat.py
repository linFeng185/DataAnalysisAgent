"""前端聊天 Hook 参数透传回归测试。"""

from __future__ import annotations

import json
import logging
import subprocess
from pathlib import Path

logger = logging.getLogger(__name__)


# 验证 useChat 将多数据源和模型参数传给底层流式请求。
# Args: 无
# Returns: 无返回值，断言失败时由 pytest 报告。
def test_use_chat_forwards_multi_datasource_and_model_arguments() -> None:
    """发送多源查询时，streamChat 调用必须包含 dss 和 mid 参数。"""
    logger.debug("test_use_chat_forwards_multi_datasource_and_model_arguments 入口")
    try:
        # Arrange：使用 TypeScript AST 定位 Hook 中唯一的 streamChat 调用。
        source_path = Path("frontend/src/hooks/useChat.ts").resolve()
        inspect_script = r"""
const fs = require('fs');
const ts = require('./frontend/node_modules/typescript');
const sourcePath = process.argv[1];
const sourceText = fs.readFileSync(sourcePath, 'utf8');
const source = ts.createSourceFile(sourcePath, sourceText, ts.ScriptTarget.Latest, true);
const calls = [];
const pending = [source];
while (pending.length > 0) {
  const node = pending.pop();
  if (ts.isCallExpression(node) && node.expression.getText(source) === 'streamChat') {
    calls.push(node);
  }
  for (const child of node.getChildren(source)) {
    pending.push(child);
  }
}
if (calls.length !== 1) {
  throw new Error(`Expected one streamChat call, found ${calls.length}`);
}
const args = calls[0].arguments.map((arg) => arg.getText(source));
process.stdout.write(JSON.stringify({
  argument_count: args.length,
  datasources: args[6] || '',
  model_id: args[7] || '',
  connection_id: args[8] || '',
  skills: args[9] || '',
  reasoning_enabled: args[10] || '',
  reasoning_effort: args[11] || '',
}));
"""

        # Act：解析调用参数，避免依赖源码换行或格式。
        completed = subprocess.run(
            ["node", "-e", inspect_script, str(source_path)],
            check=True,
            capture_output=True,
            text=True,
            encoding="utf-8",
        )
        call = json.loads(completed.stdout)

        # Assert：末尾参数必须分别传入多数据源、模型、连接、Skill 和推理偏好。
        assert call == {
            "argument_count": 12,
            "datasources": "dss",
            "model_id": "mid",
            "connection_id": "cid",
            "skills": "enabledSkillIds",
            "reasoning_enabled": "reasoningEnabled",
            "reasoning_effort": "reasoningEffort",
        }
        logger.info("test_use_chat_forwards_multi_datasource_and_model_arguments 完成", extra=call)
    except Exception as exc:
        logger.error(
            "test_use_chat_forwards_multi_datasource_and_model_arguments 异常: %s",
            exc,
            exc_info=True,
        )
        raise


# 方法作用：验证 SSE 读取异常进入错误回调且不会被当作正常完成。
# Args: 无。
# Returns: 无返回值，断言失败时由 pytest 报告。
def test_stream_chat_read_failure_reports_error_in_catch() -> None:
    """reader.read() 抛错时必须调用 onError，不能继续调用 onDone。"""
    # Arrange：通过 TypeScript AST 检查 streamChat 内部 catch 分支调用。
    source_path = Path("frontend/src/api/client.ts").resolve()
    inspect_script = r"""
const fs = require('fs');
const ts = require('./frontend/node_modules/typescript');
const sourcePath = process.argv[1];
const sourceText = fs.readFileSync(sourcePath, 'utf8');
const source = ts.createSourceFile(sourcePath, sourceText, ts.ScriptTarget.Latest, true);
let streamChat = null;
function find(node) {
  if (ts.isFunctionDeclaration(node) && node.name?.text === 'streamChat') streamChat = node;
  ts.forEachChild(node, find);
}
find(source);
if (!streamChat) throw new Error('streamChat not found');
const catches = [];
function inspect(node) {
  if (ts.isCatchClause(node)) {
    const calls = [];
    function collect(child) {
      if (ts.isCallExpression(child)) calls.push(child.expression.getText(source));
      ts.forEachChild(child, collect);
    }
    collect(node.block);
    catches.push(calls);
  }
  ts.forEachChild(node, inspect);
}
inspect(streamChat);
process.stdout.write(JSON.stringify(catches));
"""

    # Act
    completed = subprocess.run(
        ["node", "-e", inspect_script, str(source_path)],
        check=True,
        capture_output=True,
        text=True,
        encoding="utf-8",
    )
    catch_calls = json.loads(completed.stdout)

    # Assert：至少一个读取 catch 报错，任何 catch 都不能报告正常完成。
    assert any("onError" in calls for calls in catch_calls)
    assert all("onDone" not in calls for calls in catch_calls)


# 方法作用：验证发送新消息前终止旧流，并在所有错误路径清除重试状态。
# Args: 无。
# Returns: 无返回值，断言失败时由 pytest 报告。
def test_use_chat_aborts_previous_stream_and_clears_retry_on_errors() -> None:
    """旧 SSE 回调不得与新消息竞争，错误横幅也不得永久残留。"""
    logger.debug("test_use_chat_aborts_previous_stream_and_clears_retry_on_errors 入口")
    try:
        # Arrange / Act
        source = Path("frontend/src/hooks/useChat.ts").read_text(encoding="utf-8")
        send_start = source.index("const send = useCallback")
        stream_start = source.index("aborterRef.current = streamChat", send_start)
        send_body = source[send_start:stream_start]
        error_body = source[source.index("case 'error':", stream_start):]

        # Assert
        assert "aborterRef.current?.abort()" in send_body
        assert "setRetryInfo(null)" in error_body
        assert source.count("setRetryInfo(null)") >= 2
        logger.info("test_use_chat_aborts_previous_stream_and_clears_retry_on_errors 完成")
    except Exception as exc:
        logger.error(
            "test_use_chat_aborts_previous_stream_and_clears_retry_on_errors 异常: %s",
            exc,
            exc_info=True,
        )
        raise


# 方法作用：验证 SSE JSON 解析失败具有可观测日志且进入错误回调。
# Args: 无。
# Returns: 无返回值，断言失败时由 pytest 报告。
def test_stream_chat_reports_malformed_sse_events() -> None:
    """协议损坏不能静默丢事件后继续宣告完成。"""
    logger.debug("test_stream_chat_reports_malformed_sse_events 入口")
    try:
        # Arrange / Act
        source = Path("frontend/src/api/client.ts").read_text(encoding="utf-8")

        # Assert
        assert "流式事件 JSON 解析失败" in source
        assert "console.error" in source
        assert "throw new Error('流式事件格式无效')" in source
        logger.info("test_stream_chat_reports_malformed_sse_events 完成")
    except Exception as exc:
        logger.error("test_stream_chat_reports_malformed_sse_events 异常: %s", exc, exc_info=True)
        raise


def test_use_chat_does_not_store_model_reasoning() -> None:
    """前端只能展示受控摘要，不得从历史或 SSE 保存模型原始推理。"""
    # Arrange
    source = Path("frontend/src/hooks/useChat.ts").read_text(encoding="utf-8")

    # Act
    forbidden = ("sql_reasoning_content", "reasoning_content", "assistant.reasoning")

    # Assert
    assert all(token not in source for token in forbidden)
