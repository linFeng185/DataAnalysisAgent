# 23. 报表工作台

本模块对应 `spec/30-report-workbench.md`。2026-09-06 续开发完成持久化调度、实时钻取、页面延期与有界跨源样本聚合。旧 `analysis_schedules` 保留兼容，生产 PostgreSQL 多 Worker 与真实模型验收另行执行。

| # | 功能 | 主要文件/模块 | 描述 | Phase | 状态 | 优先级 |
|---|---|---|---|---|---|---|
| 23.1 | 自然语言计划解析 | `src/reporting/planner.py`、`src/api/routes/report_workbench.py`、`src/llm/prompts.py` | 从文本解析立即/定时、频率、次数、有效期、数据源和分析单元；缺少调度信息默认立即一次。业务语义拆解走 `report.plan` 结构化 LLM（含租户模型绑定；用户书写的编号章节一个章节一个单元），模型不可用或越权时回退规则拆分并在 `warnings` 标注降级。分析单元默认不绑定数据源，模型在授权集合内的猜测会被清空，只有用户原文点名的数据源才保留 | Phase 1 | 单测完成 | P1 |
| 23.2 | 报表任务与运行模型 | `src/reporting/models.py`、`src/reporting/store.py`、`migrations/018_report_workbench.sql` | Job/Run/Step/Version 持久化、幂等、状态机和 RLS；SQLite 测试走应用层模拟 | Phase 1 | 单测完成 | P1 |
| 23.3 | 报表 LangGraph 子图 | `src/graph/subgraphs/report_workbench.py`、`src/graph/contracts.py` | 单数据源多单元拆解、授权 Schema、查询执行、确定性分析、综合洞察和页面发布。执行前由 `report.route` 按各源表清单判定单元命中的数据源，只打命中源；无命中则跳过并说明，不向无关数据源广播 | Phase 1 | 单测完成 | P1 |
| 23.4 | 多业务关系分析 | `src/reporting/relations.py`、`src/graph/subgraphs/report_workbench.py` | 受控列投影、样本限制、类型/匹配率/基数校验、连接膨胀阻断和跨源降级；只发布可验证关系证据，不宣称因果关系 | Phase 2 | 集成测试完成 | P1 |
| 23.5 | 动态报表页面 DSL | `src/reporting/document.py`、`frontend/src/components/report/` | 版本化页面 JSON、响应式布局、图表/表格/洞察/质量块和 ECharts 白名单校验 | Phase 1 | 单测完成 | P1 |
| 23.6 | 交互明细与 Drilldown | `src/reporting/drilldown.py`、`src/api/routes/report_workbench.py` | 私有/分享快照钻取；客户端不能提交任意 SQL，live drilldown 在 Phase 2 | Phase 1 | 单测完成 | P1 |
| 23.7 | 生命周期与分享 | `src/reporting/sharing.py`、`src/api/routes/report_workbench.py` | 默认 7 天、快照分享、指定用户范围、授权码和撤销；延期/永久策略在 Phase 2 | Phase 1 | 单测完成 | P1 |
| 23.8 | 菜单权限与用户配额 | `src/reporting/quota.py`、`src/api/routes/report_workbench.py` | `report.*` 能力、周期次数、并发、查询、Token 和分享限额 | Phase 1 | 单测完成 | P1 |
| 23.9 | 工作台与公开查看器 | `frontend/src/pages/ReportWorkbenchPage.tsx`、`frontend/src/pages/ReportViewerPage.tsx`、`frontend/src/pages/SharedReportPage.tsx` | 自然语言计划预览、任务轮询、节点日志抽屉、动态 DSL 阅读、快照 Drilldown、授权码分享和撤销 | Phase 1 | 单测完成 | P1 |
| 23.10 | 重试、恢复与可观测性 | `src/reporting/runner.py`、`src/reporting/store.py` | 基础节点日志、失败状态、用户/内部错误分离和部分成功；节点重试、恢复扫描在 Phase 2/3 | Phase 1 | 单测完成 | P1 |
| 23.11 | 定时认领与恢复扫描 | `src/reporting/scheduler.py`、Store、应用生命周期 | 日历/时区/次数/截止时间、暂停恢复、SKIP LOCKED 与专属连接锁、心跳及原周期配额恢复；发布前重新校验权限 | Phase 2 | 单测完成 | P1 |
| 23.12 | 登录用户实时 Drilldown | `src/reporting/live_drilldown.py`、私有模板表、API、查看器 | 服务端模板、当前 ACL、字面量过滤、EXPLAIN、限流、配额、脱敏；公开分享不下发 live 定义 | Phase 2 | 集成测试完成 | P1 |
| 23.13 | 页面延期与永久策略管理 | 报表 Store、API、查看器 | 已过期但未清理页面可延期；每次最多 30 天；永久能力独立校验；旧分享不自动续期或恢复 | Phase 2 | 集成测试完成 | P1 |
| 23.14 | 跨源聚合分析完善 | `src/reporting/aggregation.py`、报表子图 | 明确共同粒度、独立来源样本统计、行数/分组/字节预算与安全降级；不冒充全量业务总额 | Phase 2/3 | 集成测试完成 | P2 |

## 依赖关系

```text
11 API / 12 Security / 13 Analytics / 14 Visualization
             ├──────────────┐
             ▼              ▼
       23.1 Planner ──→ 23.2 Job/Run Store
                              │
                              ▼
                      23.3 Report Subgraph
                       ├──→ 23.4 Relations
                       ├──→ 23.5 Document DSL
                       ├──→ 23.6 Drilldown
                       └──→ 23.7 Lifecycle/Sharing
                              │
             21 Platform Admin ──→ 23.8 Quota/Permission
                              │
                              ▼
                      23.9 Frontend / 23.10 Operations
```

## 模块收尾

模块总计 14 项，已完成 14 项，待开发 0 项。这里的完成指当前编号描述的实现与测试状态，不表示真实 PostgreSQL、容灾演练或外部模型的生产验收完成。

| 待开发项 | 不开发原因 | 可开发条件 | 预计开发时机 |
|---|---|---|---|
| 无 | 本轮编号范围内均已实现 | 生产验收见下文，不另伪造开发完成状态 | — |

### 续开发交付与验收边界

- 新增迁移 `020_report_live_queries.sql`、`021_report_dispatch.sql`、`022_report_tenant_boundaries.sql`，本轮未主动执行到用户数据库。022 修复 018 中租户管理员的跨租户布尔分支，并把分享读取与写权限分离；标量 SQL 测试不等同于真实 PostgreSQL RLS 验收。
- 复现并修复私有过期钻取仍返回数据、手动重跑不复核 ACL、重复运行页面 ID 变化且版本始终为 1、已发布页面在状态回写前中断后重复执行等问题。
- 调度恢复使用新运行标识重跑中断任务，不复用未知权限的旧结果；节点级成功单元持久化缓存复用仍不作为本次验收承诺。进程内手动运行与后台扫描共享 PostgreSQL 专属连接锁，连接失效后旧 Worker 无法通过该连接发布。
- 23.14 只处理明确同粒度的授权样本，缺少维度、字段不可见、来源失败、类型冲突或超过预算时分别分析；没有实现无限制全量跨库 Join，也不宣称统计相关性代表因果。
- 验收前按顺序应用新迁移、安装 `vision` 可选依赖并给实际视觉模型声明 `vision=true`；使用真实 PostgreSQL 非 BYPASSRLS 角色演练跨租户、锁连接中断和多 Worker 认领。
- 历史批次记录如下，当前完成状态以上方总表为准。

### 最终本地验证（2026-09-06）

- `RUN_BROWSER_TESTS=1 pytest -m "not live_llm and not local_llm and not live_datasource"`：**1740 passed, 1 deselected, 1 warning**，含真实 Edge 隔离页面测试；不调用真实远程 LLM。
- 本轮所有变更 Python 文件 Ruff 通过；`compileall src tests` 通过；前端 TypeScript/Vite 构建通过。Antd 约 1.18 MB 体积告警保留，不通过提高阈值掩盖。
- 全项目 Ruff 另有 3 处未修改文件的历史导入排序问题：`src/api/routes/knowledge.py`、`src/memory/session_archive.py`、`tests/test_tools/test_db_executor.py`；本轮未夹带格式化。
- 独立内存 Uvicorn + `curl.exe` 验证：过期正文 GET 410、过期私有 Drilldown POST 410、延期 POST 200、再次读取正文 GET 200；完整日志显示 `expired=true` 时未进入数据返回。测试服务已关闭，没有操作用户业务数据。
- 真实 PostgreSQL 多 Worker、非 BYPASSRLS 角色、Vault 和视觉模型仍需独立环境验收。

## 2026-09-06 可靠性整改批次

- 23.2：创建任务、幂等检查和任务配额预占使用同一 PostgreSQL 事务；同身份同幂等键只首次入队，同键不同计划返回 409。手动重跑使用独立运行键，不再复用 `job_id:attempt`。
- 23.8：单次单元/查询/行数限制与月度用量分离；余额读取持久化记录；失败创建返还任务额度；取消释放原统计周期的并发槽。
- 23.8：租户管理员可在“租户管理 → 当前租户用户 → 报表额度”读取与配置能力和自然月额度；后端验证目标用户属于当前租户，策略和能力一起提交，成功后才更新缓存。平台超级管理员不能通过此租户业务端点代配。生产权限以持久化配置为准，跨 Worker 撤权不沿用旧覆盖；权限库异常时失败关闭，不恢复角色默认授权。
- 23.9：修复预览、创建、刷新及查看器重试按钮未调用函数的问题；失败重试沿用幂等键；明确区分每月任务额度和运行额度。新增真实浏览器请求拦截测试，不访问真实业务后端。
- 23.10：取消写入 `cancelled`；只有成功预占的运行才释放并发槽；报表会话以显式租户、用户及 run_id 隔离，避免依赖请求上下文或复用分析单元名。
- 验证：开启隔离浏览器用例、排除真实模型和数据源标记后的完整回归为 `1597 passed, 1 deselected`；前端构建通过，ECharts 图表相关代码由约 1.15 MB 降至约 0.74 MB。PostgreSQL 运行键使用 SQLite 唯一约束验证，不等同于真实 PostgreSQL 多实例验收。相关文件 Ruff 通过；全项目存在 5 处本轮未修改文件的历史导入排序问题，未夹带修改。
- 前序可靠性批次未覆盖 23.11～23.14；这些能力已在本页前述续开发批次实现，当前状态以总表为准。
