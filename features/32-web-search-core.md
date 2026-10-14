# 32. 受控联网搜索服务

设计：spec/58-controlled-web-search.md。Depends on：8.1 MCP 权限与工具调用、11.1 聊天配额、12 请求身份。

| 编号 | 功能 | 优先级 | 状态 | 验收 |
|---|---|---|---|---|
| 32.1 | 搜索配置、显式意图与真实身份门禁 | P2 | 单测完成 | 关闭零调用、长度限制、匿名及身份漂移拒绝 |
| 32.2 | 精确 MCP 工具授权与有界搜索证据 | P2 | 单测完成 | Schema 白名单、一次调用、超时取消、URL 清洗、日志脱敏 |

本模块当前共 2 项，已完成 2 项（单测完成），待开发 0 项。

主代理最终独立验收（2026-10-14）：搜索核心、研究子图、配置、运行期控制与架构约束共 297 项通过。包含追加的六种禁止联网表达、dict/list/JSON 三种响应总预算及摘要异常脱敏；Ruff、Python 语法和差异检查通过。验证使用真实服务逻辑与 Fake MCP/Fake LLM，真实部署边界仍按 33.3 跟踪。

实现与验证（2026-10-14）：

- 代码：`src/search/models.py`（SearchHit / SearchResponse / is_explicit_web_search / is_safe_public_url 守卫）、`src/search/service.py`（async search_public_web 单次调用服务）、`src/search/__init__.py`；配置：`src/config.py`、`src/runtime/settings.py`（LIVE_FIELDS 增加 web_search_ 前缀）、`config/app.example.yaml`。
- 单测：`tests/test_search/test_models.py`、`tests/test_search/test_service.py`、`tests/test_config.py::TestWebSearchSettings`；与 `tests/test_mcp/test_client_manager.py` 联合回归共 104 项通过（含真实 MCPClientManager 的跨租户可见性用例）。
- 市场研究接入（真实子图 + Fake MCP/LLM 集成测试）由 features/33-web-search-workflow.md 跟踪，不在本文件验收范围。

否定授权修复（2026-10-14，独立验收后）：

- 根因：`is_explicit_web_search` 原先只做正向关键词子串匹配，未先处理明确否定，「不要联网搜索，仅分析附件」「Do not search the web; use the attached report only.」被误判为授权并实际调用搜索工具外送。
- 修复：`src/search/models.py` 增加明确否定与仅离线标记表和中英文否定结构判定，判定函数改为否定优先返回 False；`search_public_web` 与 `market_research` 子图复用同一函数，未改动工作流、Prompt、配置与其它批次文件。
- 验收：外部独立验收 `../test_search_boundary_acceptance.py` 11 passed（修复前 9 passed / 2 failed）；仓内 `tests/test_search`（含新增 `test_intent_boundary.py`：否定与仅离线优先、正向保留、服务层零身份读取零 MCP、子图同一判定且不触发模型）129 passed；`tests/test_graph/test_market_research_search.py` 与 `test_capability_subgraphs.py` 合计 49 passed；ruff 与 py_compile 通过。

等价载荷预算修复（2026-10-14，独立验收后）：

- 根因：`_parse_bounded_response` 只对字符串原始响应检查 `web_search_max_response_chars`，dict/list 等价载荷直接进入无界遍历并构造引用；外部独立验收 `../test_search_size_logging_acceptance.py::test_oversize_response_rejected_for_all_supported_shapes` 的 dict/list 两项失败（json 通过）。
- 修复：`src/search/service.py` 新增 `_payload_within_budget`（`json.JSONEncoder(ensure_ascii=False).iterencode` 分块计数，越界即停，不先复制整段序列化文本）；dict/list 分支在结构识别与条目遍历前按与字符串同一预算拒绝超限（`response_too_large`）；非 JSON 值、循环引用等无法纳入有界计数的结构失败关闭（`invalid_response`，异常边界一次脱敏 error 保留原始堆栈）；取消传播与小型合法 dict/list 行为不变。
- 验收：外部 `-k oversize` 3 passed（修复前 dict/list 2 failed）；仓内 `tests/test_search` 169 passed（新增 `TestResponseTotalBudget` 11 项：统一预算含等边界、小载荷不变、巨型多条提前整包拒绝、计数越界即停、不可序列化/循环失败关闭）；`tests/test_config.py`、`tests/test_config_docs.py`、`tests/test_architecture_convergence.py`、`tests/test_architecture_remediation.py`、`tests/test_logging_policy.py`、`tests/test_logging_noise.py` 与搜索测试联合 219 passed；ruff 与 py_compile 通过。

执行请求授权修复（2026-10-14，独立验收后）：

- 根因：任意位置正向关键词匹配把能力咨询和陈述当成外送授权；追加咨询黑名单仍会漏掉同类表述，并误拒绝主题中讨论搜索功能的明确请求。真实服务与 StructuredTool 验收分别复现了误外送和误拒绝。
- 修复：在请求开头识别有限请求前缀及执行动作。删除任意单汉字前缀兼容；英文名词式仅接受裸命令或 `for` 引入主题。保留否定优先、自指咨询拒绝，以及明确执行后的原始合法主题。未知前缀和其它名词陈述默认拒绝，拒绝路径在读取身份及触达工具之前结束。
- 测试契约：`test_models.py` 原先的「空  联网检索  空白」包含非空白前缀，改为真实首尾空白「  联网检索  空白  」；原样例及更多未知前缀移入负例，保留空白容错并验证不能据此扩大授权范围。
- 独立验收：外部五个搜索验收文件共 45 项通过，覆盖原 17 项授权边界、4 项预算/日志、6 项咨询及18项陈述与合法主题。仓内搜索、研究子图、Prompt 与架构相邻回归共 381 项通过；修改文件 Ruff、Python 语法和差异检查通过。测试运行真实服务逻辑、StructuredTool 与 Fake MCP/Fake LLM；不代表远程部署验收。

| 后续待办 | 不开发原因 | 可开发条件 | 预计开发时机 |
|---|---|---|---|
| 真实远程搜索 MCP 部署验收、租户多 Worker 配额、真实网页攻击与浏览器呈现验收 | spec/58 §5 明确单列待开发，不能以离线测试替代 | 已部署的搜索 MCP 服务与多租户多 Worker 环境 | 后续 Phase |
