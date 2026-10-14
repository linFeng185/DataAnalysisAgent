# 35. 中英文固定公开提示

设计：spec/60-bilingual-public-messages.md。Depends on：28.4 展示语言、4.4 SQL 时间补参与 4.10 统一响应。属于 19.6 增量。

| 编号 | 功能 | 优先级 | 状态 | 验收 |
|---|---|---|---|---|
| 35.1 | SQL 默认时间补参与公开错误双语映射 | P3 | 集成测试完成 | 中文兼容、英文、未知错误与敏感正文隔离 |
| 35.2 | 统一响应出口语言及节点串联回归 | P3 | 集成测试完成 | 语言兜底、具体解释保真、错误状态与历史保持 |

共 2 项，已完成 2 项，待开发 0 项。

## 本轮实现说明

- `src/graph/outcome.py`：`public_error_message`/`public_failure_reason` 增加可选关键字 `language`（默认 `zh-CN`，旧调用零变化）；新增双语常量 `PUBLIC_PROMPT_MESSAGES` 与 `public_prompt_message(kind, language)`；`en`/`en-*` 命中英文，其余语言保留中文默认。
- `src/graph/nodes/generate_sql.py`：结构化时间补参的 `input_explanation`/`time_range_explanation` 使用同一份当前请求语言文案；模型未给出解释时的默认输入提示同语言；已有具体解释原样保留。
- `src/graph/nodes/build_response.py`：展示语言提前到节点入口解析，统一供默认补参兜底、已知错误映射（VALIDATION/EXPLAIN/LLM_UNAVAILABLE/多源失败与部分成功）与失败原因使用；最终 `display_language`、状态、错误码、数据与 `sql_statements` 语义不变；补参日志改为 `explanation_chars` 等元信息。
- 语言匹配边界修复：`_is_english_language` 由字面 `startswith("en")` 收紧为规范化（strip + lower）后仅 `en` 与 `en-*` 命中；`enm` 等共享 en 字面前缀的独立语言代码保持中文默认，调用接口与解析优先级不变。
- 测试：`tests/test_graph/test_bilingual_public_messages.py`（17 项全通过，含真实 `generate_sql → build_response` 串联、界面/历史语言兜底、未知错误 fallback、具体解释保真、敏感正文隔离、高频重复调用无日志、en/en-US/enm 边界）。验收命令：`pytest tests/test_graph/test_bilingual_public_messages.py`；独立验收 `test_bilingual_public_messages_acceptance.py` 8 项全通过，`tests/test_graph/test_outcome.py` 9 项通过，ruff 与 py_compile 通过。

## 模块收尾（完成度 2/2）

模块内无待开发项。以下边界不属于本模块，继续在 19.6 跟踪，不在本轮范围：

| 遗留项 | 不开发原因 | 可开发条件 | 预计开发时机 |
|---|---|---|---|
| 非中英语言（ja/ko/fr/de/es）全面本地化 | spec/60 明确本轮只做中英，其余语言暂保留中文默认 | 需要产品确认各语言固定文案与评测方案 | 19.6 后续增量 |
| 真实模型语言语义质量评测 | 单元测试不能代表真实语言语义质量 | 需要独立的评测集与已配置模型环境 | 19.6 后续增量 |
| 其余 API/联网研究子图/管理员过程摘要文案 | spec/60 明确不扩展，各自调用契约不同需单独评审 | 确定各出口的调用契约与固定文案清单 | 19.6 后续增量 |
