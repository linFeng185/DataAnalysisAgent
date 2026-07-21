"""前端聊天 Hook 参数透传回归测试。"""

from __future__ import annotations

import json
import logging
from pathlib import Path
import subprocess


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

        # Assert：最后两个参数必须分别传入多数据源列表和模型标识。
        assert call == {
            "argument_count": 8,
            "datasources": "dss",
            "model_id": "mid",
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
