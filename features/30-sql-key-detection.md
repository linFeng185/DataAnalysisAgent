# 30. 键列判断与对话 SQL 生成接入

设计：spec/55-sql-profiling-and-probing.md。Depends on：10（Prompt/模型工厂）、29.1–29.2（画像服务）。键列判断可先独立开发，接入待画像接口就绪。

| 编号 | 功能 | 优先级 | 状态 | 验收 |
|---|---|---|---|---|
| 30.1 | 有界键列结构判断与确定性回退 | P1 | 单测完成 | Mock 模型、未知列、超时/取消、身份缓存隔离 |
| 30.2 | 键列保护与画像证据注入 | P1 | 集成测试完成 | 超过 32 列仍保留授权键；预算下正确保留 scope |
| 30.3 | 聊天入口启用与报表默认隔离 | P1 | 集成测试完成 | 流式/非流式同源开关，关闭后零新增调用 |

## 实现摘要（2026-10-14 本轮）

- `src/llm/schema_keys.py`（新增）：`detect_schema_keys(context)` 每表固定四键
  `key_columns/display_columns/foreign_keys/profile_columns`；输入只含授权表结构（无问题/样例），
  未知列与伪造表拒绝；无模型/失败/非法输出回退确定性结构候选（主键、索引、关系 join_key、
  `*_id`/`*_code` 命名），不宣称真实唯一；`CancelledError` 原样透传。
  调用走 `src/llm/client.py` 工厂 + 注册 Prompt `schema.keys`（`invoke_structured`），
  输出上限 800 token、超时 3s、调用期以 `use_knowledge_loop(enabled=False)` 禁用知识/探查工具。
  缓存键含 tenant/user/datasource/授权结构指纹/权限指纹（allowed_columns + row_filter_sql）/
  模型指纹/Prompt 版本，TTL 与容量读取配置字段（`sql_profiling_ttl_seconds`、
  `sql_profiling_cache_entries`），字段缺失时取 spec 默认 300s/128。
  `apply_key_hints(tables, hints)` 只复制不改原对象，合法 `key_columns` 写入 `_profile_key_columns`。
- `src/llm/output_contracts.py`：新增 `SchemaKeyTable`、`SchemaKeyDetectionOutput` 契约（越界只裁剪不拒收）。
- `src/llm/prompts.py`：注册 `schema.keys`（task=schema_key_detection，context_budget=16000）。
- `src/graph/nodes/generate_sql.py`：仅当 `RunnableConfig.configurable.enable_sql_intelligence is True`
  且真实认证身份与请求上下文身份一致并从当前 `datasource_access` 取到该数据源授权时启用；
  流程为「原规则选表 → 取选中表完整列做键列判断 → 裁剪保护（`_required_schema_columns` 接受
  `_profile_key_columns`，未授权列名天然不生效）→ 最多 6 表画像 → 重新按预算裁剪 →
  data_profile 注入」。画像模块缺失/按键失败/画像失败均降级为原路径，取消向上传播。
  `_llm_generate` 新增兼容参数 `data_profile=''`/`probe_context=None`；探针作用域只围绕本次
  prepare/stream（ExitStack + finally 复原）；PromptSection `data_profile` priority=95 max=6000，
  规则/键列候选/画像结论前置以抗尾部截断；画像与键列只存本次调用局部变量，
  不进 checkpoint/SSE/历史/业务库。
- `src/api/routes/chat.py`、`src/api/streaming.py`：服务端聊天非流式/流式入口写入
  `configurable.enable_sql_intelligence=True`；`ChatRequest` 无对应字段，请求正文不能覆写；
  报表 runner 的 config 形状不含该标记，行为默认不变。

## 授权边界与物理身份修复（2026-10-14 续做）

- `_authorized_tables` 现按 `allowed_columns` 在**模型输入、结构指纹、回退、缓存与裁剪保护之前**
  过滤列：空白名单表示不额外限制（与画像模块同一语义），非空时精确比较标识符
  （裸列名或 `table.column`）；无授权列的表整体跳过；兼容真实 `TableSchema`/`ColumnInfo`
  与原始字典两种形态。未授权列不再进入结构文本、缓存键与 `_profile_key_columns`。
- 权限与结构指纹保留大小写：`_permission_fingerprint` 不再 `lower()`；结构指纹表名保留原拼写。
  `normalize_model_hints`/`apply_key_hints`/回退候选（主键/索引/关系键）改为
  「精确优先，大小写不敏感回退仅在候选唯一时生效，歧义一律拒绝」，PostgreSQL `Secret`/`secret`
  两列不再互相借权；未知列/伪造表照常拒绝，模型无法借提示扩大授权。
- 生成接入只使用授权过滤后的结构：`_filter_authorized_tables` 在选表之后、键列判断/画像/裁剪
  之前滤掉未授权列；`_profile_columns_for_table` 与 `_required_schema_columns` 的提示保护按
  物理身份精确匹配（歧义提示不保护任何列），大小写变体列无法被提示重新保留。
  未写 `enable_sql_intelligence` 标记的调用（报表/后台/直接节点调用）完全走原路径。

## 显式数据源边界与总开关收口（2026-10-14 再续）

- `_current_datasource_access`：显式 `routing.datasource` 未命中 `datasource_access`
  （或条目非快照字典）时**直接安全拒绝返回 None**，不再落入「唯一授权源回退」借用其他
  数据源的权限快照；未指定数据源且仅有一个非空名授权源时保留原回退。拒绝同时意味着
  零键列模型、零画像、零探针绑定，原 Prompt 路径照常出 SQL；真实身份复核与取消传播不变。
- `_sql_intelligence_enabled`：除服务端 `enable_sql_intelligence` 标记外，同时校验画像预取
  总开关 `sql_profiling_enabled`（字段缺失按开启）；总开关关闭时 `_prepare_sql_intelligence`
  整体不进入，满足 spec/55 §9「禁用开关必须带来零新增模型调用、数据库查询和工具定义」。

## 异常可观测性修复（2026-10-14 最终验收）

- `_prepare_sql_intelligence` 两个回退分支（画像模块缺失、`ProfilingContext` 构造失败）
  按 `src/tools/sql_rewriter.py` 安全三元组补齐堆栈：固定中文文案承载原始 `traceback`、
  保留 `error_type`，导入失败/契约校验正文（可能夹带环境信息或字段值）不回显；
  每个回退只记录一次，日志级别不变，正常启用路径零新增日志。
- 日志回归：`TestIntelligenceFallbackLogObservability`（2 用例）——两个回退各只留一条
  带堆栈事件，模拟秘密不出现在任何日志字段，降级后原 Prompt 路径照常出 SQL。
- 复验（证据 `../sql-observability-final-report.md`）：架构门禁 3 offender 转绿；
  四文件组 88 passed；外部 SQL 验收五文件（含 `test_sql_master_switch_acceptance.py`）
  与上一轮同命令复跑 14 passed。

## 测试

- `tests/test_llm/test_schema_keys.py`（新增，20 用例）：确定性回退、模型失败/超时/取消、
  非法输出与伪造表拒绝、调用契约（工厂/Prompt/800 token/工具禁用复原）、
  缓存身份与权限隔离/TTL/容量、`apply_key_hints` 复制与过滤、结构输入不含样例与问题、
  授权过滤先于模型与回退（真实 `ProfilingContext`/`TableSchema`）、大小写变体与歧义名称拒绝、
  权限/结构指纹大小写隔离缓存。
- `tests/test_graph/test_sql_profiling_integration.py`（新增，最终验收后 28 用例）：
  >32 列键列保护（含无提示被裁剪的反证）、画像与键列进入 Prompt、探针作用域复原、
  当前数据源权限（多源不混用）、最多 6 表画像、画像失败/模块未就绪降级、取消透传、
  关闭标记与身份不符/多源不可定位时零查询且原 Schema 不变、data_profile 段
  优先级 95/上限 6000、两种入口 marker、报表默认关闭、请求不可覆写、
  授权过滤后结构进 Prompt/画像（Fake LLM + 真实 ProfilingContext/TableSchema）、
  变体提示不能恢复未授权列、画像与键列不写回节点结果；
  显式未授权源拒绝（含条目非快照字典）、显式正确源选中、未指定唯一源回退保留、
  未指定多源拒绝、节点层显式未授权零键列模型/画像/探针绑定（记录式替身，真实
  `_llm_generate` 路径）。
- 独立验收 `../test_sql_profiling_acceptance.py`：`-k 'key_detection or key_model'` 2 passed
  （`test_real_profile_context_preserves_key_detection`、`test_key_model_receives_authorized_columns_only`）；
  当时全文件 8 passed（其余 6 项属 29/31 写集）。
- 主代理外部验收集合复验（2026-10-14 再续）：`test_sql_profiling_acceptance.py`（8）+
  `test_sql_master_switch_acceptance.py`（1）+ `test_sql_masking_acceptance.py`（2）+
  `test_sql_policy_intersection_acceptance.py`（2）+
  `test_sql_datasource_selection_acceptance.py`（1）共 **14 passed**（含本轮收口的显式数据源
  边界与总开关两项）。
- 定向回归：`tests/test_graph/test_generate_sql_*.py`、`test_correctness_regressions.py`、
  `tests/test_api/test_streaming*.py`、`test_chat_presentation_stream.py`、
  `test_chat_llm_selection.py`、`test_routes.py`、`test_tenant_propagation.py`、
  `tests/test_llm/test_prompt_registry.py` 等全部通过。

## 模块收尾

共 3 项，已完成 3 项（30.1 单测完成；30.2–30.3 集成测试完成），待开发 0 项。

| 待开发项 | 不开发原因 | 可开发条件 | 预计开发时机 |
|---|---|---|---|
| （无） | — | — | — |

**离线验收边界（不得改写为生产验收）**：画像与探针模块已按 §14 接口落盘并完成接入。
本次验证为离线范围：单元/整合测试使用 Fake LLM、真实 Pydantic 契约
（`ProfilingContext`/`TableSchema`）与内存 SQLite；主代理独立验收
（`../test_sql_profiling_acceptance.py`）在真实内存 SQLite + 真实统一执行器/权限复核上通过
（列/行策略拒绝、身份伪造拒绝、缓存按行列策略分区）。未访问远程模型与业务数据库；
MySQL/ClickHouse/PostgreSQL 实机数据源与远程模型端到端验收按 spec/55 §11 单独执行，
不得以本条离线通过改写为生产验收完成。
