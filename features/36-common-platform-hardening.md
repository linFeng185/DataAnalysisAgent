# 36. 通用平台加固

设计：spec/61-common-platform-hardening.md。Depends on：12 安全模块、21 平台管理与强制认证、6 知识库管理、17 基础设施与运维（迁移）。

| 编号 | 功能 | 优先级 | 状态 | 验收 |
|---|---|---|---|---|
| 36.1 | 工具输出兼容与安全编码规则 | P2 | 开发完成 | AGENTS.md 持久条款：输出前 JSON 安全编码、U+2028/U+2029 字面转义、仅展示层不改源码/金标；未经原始故障复验不宣称根治 |
| 36.2 | 认证依赖故障 503 边界 | P1 | 集成测试完成 | prod/dev 超时 503 且不清 Cookie；明确无身份/身份变化 401；同 JWT 恢复 200；ContextVar 恢复；取消传播；随机端口 uvicorn+curl.exe 503→200 |
| 36.3 | 依赖异常日志 ContextVar 作用域 | P2 | 单测完成 | 默认关闭、退出恢复 token、仅 opt-in 边界内静默、旧调用零变化 |
| 36.4 | 迁移默认排除 900+ 与 connection_setup | P1 | 集成测试完成 | setup 在 advisory lock 前同一连接恰一次；setup 异常关连接且零 SQL；None 兼容；include_branch 显式布尔才纳入 900+ |
| 36.5 | 知识文件版本存储与精确原文读取 | P1 | 集成测试完成 | 真 PG 沙箱 9 passed（受控槽、未过滤/未 skip）：publication 契约、同版本拒绝、追加顺序、越权拒绝、SHA 不符与真实终止后端故障→同三元组恢复、逐例清理收据硬零与独立复核 (0,0,0) |

共 5 项，已完成 5 项，待开发 0 项；2026-10-14 最新补丁上 Core 209 / Compat 456 / Acceptance 14 / Check 0 / 真 PG 9 全通过；最终提交与终验由主控/Codex 执行。

## 本轮实现说明

- 代码：`src/log_scopes.py`、`src/api/auth.py`、`src/app_context.py`、`src/memory/pg_pool.py`、`src/db/migrations.py`、`src/db/utils.py`、`src/bootstrap.py`、`src/knowledge/file_store.py`；规则：`AGENTS.md` 工具输出兼容条款。
- 受控回归（2026-10-14，隔离环境）：Core 209 passed；Compat 456 passed；Acceptance 14 passed（含 `tests/test_api/test_auth_dependency_http.py` 真回环 uvicorn+curl、`tests/test_db/test_migration_connection_setup.py`）；Check 全绿；原 4 项配置隔离红证保留。
- PG：`tests/test_db/test_knowledge_version_main_postgresql.py` 真 PG 沙箱 9 passed（2026-10-14 受控槽）；真实断连→恢复、逐例清理收据硬零，末例正负对照硬零，独立复核记录 (0,0,0)。

## 模块收尾（完成度 5/5 开发完成）

| 遗留项 | 不开发原因 | 可开发条件 | 预计开发时机 |
|---|---|---|---|
| 工具输出兼容根治声明 | 未经原始故障（Failed to parse item）复验不得宣称根治 | 原始故障复现环境与复验记录 | 复验通过后 |
