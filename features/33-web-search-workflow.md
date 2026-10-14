# 33. 联网研究子图与摘要

设计：spec/58-controlled-web-search.md。Depends on：32.1–32.2、10 Prompt 工厂、19.22 市场研究子图。

| 编号 | 功能 | 优先级 | 状态 | 验收 |
|---|---|---|---|---|
| 33.1 | 显式联网路由、证据摘要及模型回退 | P2 | 单测完成 | Fake LLM、禁用知识工具、不发送内部上下文、真实来源引用、异常日志脱敏分级 |
| 33.2 | 子图与搜索服务交互集成 | P2 | 集成测试完成 | 实际子图接真实服务与 Fake MCP；失败关闭及取消传播 |
| 33.3 | 部署搜索服务与真实浏览器验收 | P2 | 待开发 | 专用远程搜索服务、隔离身份及网页攻击样本 |

本模块当前共 3 项：单测完成 1 项（33.1）、集成测试完成 1 项（33.2）、待开发 1 项（33.3）。

已完成证据（2026-10-14）：

- 33.1：`tests/test_graph/test_market_research_search.py`（门禁/证据/隔离/回退/取消/路由直接用例）与 `tests/test_llm/test_web_research_prompt.py`（web.research Prompt 与 WebResearchOutput 契约）通过；默认关闭零调用、身份漂移拒绝、无证据不调用模型、内部哨兵不进入搜索与模型、调用期知识循环禁用并恢复、模型失效确定性来源摘要、取消原样传播、模型自报引用拒绝均有用例守住。
- 33.1（异常日志脱敏与分级补强）：`TestMarketResearchLogging` 覆盖摘要模型、搜索服务、核心导入与身份解析四类异常边界——沿用 `src/tools/sql_rewriter.py` 的安全三元组范式，日志只落固定脱敏文案 + 原始异常类名（`error_type`）+ 净化异常承载的原堆栈；模拟敏感正文不出现、原调用位置可在堆栈回溯、单次失败只打印一次堆栈。正常执行入口「市场研究子图执行开始」与中途「联网搜索返回」降为 debug，INFO 仅保留收尾一条「市场研究子图执行完成」结果摘要，重复成功不累积；取消原样抛出且不写失败堆栈；模型失败后下一轮恢复成功摘要与真实引用。
- 独立验收（外部 `test_search_size_logging_acceptance.py -k summary_exception`，真实市场研究节点 + 真实搜索核心 + StructuredTool）：摘要模型异常敏感正文泄漏红测修复后通过，堆栈保留原调用位置且不含模拟秘密。
- 33.2：`TestMarketResearchSearchIntegration` 使用实际市场研究子图 + 真实 `src/search` 核心 + Fake MCP + Fake LLM：真实来源生成 `source_id/locator.url/provider/UTC 时间/evidence_kind=search_snippet` 引用，工具异常退化为固定错误码 `call_failed` 且不调用模型，取消穿透真实核心原样传播。

| 待开发项 | 不开发原因 | 可开发条件 | 预计开发时机 |
|---|---|---|---|
| 33.3 | 当前无专用真实远程搜索验收环境 | 配置已部署的只读搜索 MCP 及精确工具白名单，并准备隔离身份与网页攻击样本 | 离线功能合入后单独验收 |
