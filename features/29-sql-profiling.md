# 29. SQL 画像与结构化探查服务

设计：spec/55-sql-profiling-and-probing.md。Depends on：3（连接器）、12（授权）、26（SQL 安全校验），均已有实现。

| 编号 | 功能 | 优先级 | 状态 | 验收 |
|---|---|---|---|---|
| 29.1 | 画像契约与有界多方言采集 | P1 | 集成测试完成 | NULL/空表/常量/重复/唯一、三方言及 SQLite；输出值按原列名脱敏且基数不变 |
| 29.2 | 身份隔离缓存与查询/超时预算 | P1 | 集成测试完成 | TTL/容量/权限变化/同键并发/取消；空权限交集拒绝、行策略 AND 合并 |
| 29.3 | 五种结构化探查与授权工具作用域 | P1 | 集成测试完成 | 非法表列/行权限/参数注入/次数耗尽/样本边界；探查输出脱敏、脱敏值不声称精确匹配 |

## 实现摘要（2026-10-14 本轮）

- `src/datasource/profiling.py`（新增）：`ProfilingContext`/`ProfileSnapshot`/`ProbeRequest`/`ProbeResult`
  契约（`extra="forbid"`，不接受模型侧身份与权限字段）；每表一次有界列投影（`sample_rows + 1` 行、
  行过滤注入 WHERE），Python 侧统计空值/非空/不同值/最高频次/TOP 8；唯一性只在完整可见数据非空无 NULL
  且 `distinct_count == row_count` 时成立，常量列、空表、全 NULL 与样本范围一律不升格为唯一；
  方言渲染走 sqlglot AST（MySQL/PostgreSQL/ClickHouse 断言），Oracle 等未验证方言与同步驱动明确降级；
  进程内缓存按租户/用户/数据源/结构指纹/生效行列策略/列集合/采样配置分区，TTL、容量与同键锁合并并发填充，
  失败不缓存、取消原样传播；`render_profile` 输出 complete/sample 语义并抗尾部截断。
- `src/tools/data_probe.py`（新增）：`use_data_probe_context` 绑定共享调用预算，`build_data_probe_tools`
  仅在 `sql.generate` + 可信上下文 + 总开关开启时返回工具；工具参数经 `ProbeRequest` 校验，
  执行期身份与绑定上下文一致才进入服务，结果契约与失败码脱敏。
- 配置项（spec/55 §9）：`src/config.py` 新增 16 项（`sql_profiling_*` 10 项、`schema_key_detection_*` 3 项、
  `sql_probe_*` 3 项），全部 `Field(ge/le)` 边界并有默认值；`src/runtime/settings.py` 热改白名单新增
  `sql_profiling_`/`sql_probe_`/`schema_key_detection_` 三个前缀；`config/app.example.yaml` 同步模板注释。
  `sql_profiling_full_scan_row_limit` 按 §5 只作为采样行数额外硬上限，不授权无界扫描。

## 安全修复（2026-10-14 独立验收反馈）

- **空权限交集不得等于无限制**：`_effective_allowed_columns` 改为返回 `list | None`，`None` 仅表示
  快照与执行期两侧都未声明列限制；两侧都声明但交集为空时在 `_prepare_target` 整体拒绝
  （`column_not_allowed`），不读缓存、不执行 SQL。`_column_allowed` 对空集合返回拒绝，
  防止空列表再次被当成"不限列"绕过策略。画像、探查与工具三入口共用该复核。
- **画像/探查不得成为结果脱敏旁路**：新增 `_mask_output_value` 复用 `src/security/data_masker.py`
  既有规则（按原列名 + PII 模式），在**统计完成后、对外返回与写缓存前**处理 `top_value`/`top_values[].value`、
  `entity_lookup.value`、`value_domain.value`、`column_repeat.top_value`、`time_range.min/max` 与
  `table_sample` 单元格；`distinct_count`/`uniqueness`/`top_frequency` 仍基于原值统计，不同原值被同一掩码
  合并也不会改变基数；`entity_lookup` 中被打码的值 `exact` 强制为 `false`，画像渲染对脱敏示例标注
  "不可作为精确过滤原值"；未改写 data_masker 规则本身。

## 异常可观测性修复（2026-10-14 最终验收）

- **根因**：本轮新增的宽异常回退分支只写分类结论、未落堆栈，触发架构门禁
  `tests/test_architecture_remediation.py::TestArchitectureRemediation::test_exception_fallbacks_log_traceback`
  3 处 offender（`src/datasource/profiling.py:973` 与
  `src/graph/nodes/generate_sql.py:494/518`，后两者归功能 30）。
- **修复（范式同 `src/tools/sql_rewriter.py` 安全三元组）**：`DataProfiler._run_bounded_query`
  的 `except Exception` 用固定中文文案 `safe_error` 承载 `exc.__traceback__` 记录一次排障堆栈，
  并保留 `error_type` 结构化字段；异常正文可能含 SQL/数据/凭证，原文一律不落盘。
  边界语义收敛：统一执行器已处理并返回 `success=False` 的分支继续只记分类结论、不重复堆栈；
  只有真正漏出执行器的异常才在本边界记录；`CancelledError` 仍原样传播。
- **日志回归**：`TestProfilingFailureObservability`（3 用例）——高频正常查询零日志、
  模拟秘密异常保留堆栈与 `error_type` 且秘密不出现在任何日志字段、单次失败仅一条堆栈
  且后续调用恢复成功。
- **复验**（证据 `../sql-observability-final-report.md`）：架构门禁 3 offender 转绿；
  四文件组 `test_architecture_remediation.py`、`test_architecture_convergence.py`、
  `test_datasource/test_profiling.py`、`test_graph/test_sql_profiling_integration.py`
  共 88 passed；外部 SQL 验收五文件 14 passed。

## 测试

- `tests/test_datasource/test_profiling.py`（新增，最终验收后 42 用例）：契约拒绝未知字段/身份注入、真实 SQLite 完整/样本/
  空表/全 NULL/常量/唯一语义、一表一查与 LIMIT、三方言 AST 渲染、列裁剪、行过滤不泄露策略列、
  执行前授权复核（身份/方言/同步驱动/失败关闭）、查询与总耗时预算、单次超时、缓存 TTL/隔离/副本/
  失败不缓存/同键并发单飞/取消不写缓存、禁用零查询；**本轮新增** `TestProfilingPolicyIntersection`
  （空交集拒绝画像与探查、撤权后缓存不复用、行策略 AND 合并）与 `TestProfilingMasking`
  （画像脱敏且 `distinct_count` 仍为 2、探查 table_sample/value_domain/entity_lookup/time_range 脱敏、
  打码值 `exact=false`）。
- `tests/test_tools/test_data_probe.py`（新增）：工具作用域构建条件、五原语真实 SQLite 契约、
  参数注入与身份伪造拒绝、列白名单、预算与取消；**本轮新增** `TestDataProbePolicyIntersection`
  （空交集在工具路径拒绝且零 SQL）。
- `tests/test_llm/test_data_probe_loop.py`（新增）：统一工具循环挂载、专属预算与知识检索互不消耗。
- `tests/test_graph/test_sql_profiling_integration.py`（新增）：画像段进入 Prompt、键列保护与关闭降级
  （与功能 30 共用，详见 features/30）。
- `tests/test_config.py`（本轮新增 `TestSqlProfilingConfig`）：16 项配置全部进入热改白名单、
  合法覆盖逐一生效、零值/越界预算在合并阶段被拒绝；`config/app.example.yaml` 取值满足模型约束
  （既有参数化用例自动覆盖新键）。
- 独立验收（主代理，真实内存 SQLite + 真实统一执行器/权限复核）：`../test_sql_profiling_acceptance.py`
  8 项中 6 项属本模块；`../test_sql_policy_intersection_acceptance.py` 与 `../test_sql_masking_acceptance.py`
  本轮由红转绿（修复前 4 failed，修复后 4 passed，证据见 `../sql-policy-intersection-review.log`、
  `../sql-masking-review.log` 与本轮复跑日志）。

## 模块收尾

共 3 项，已完成 3 项（29.1–29.3 均集成测试完成），待开发 0 项。

| 待开发项 | 不开发原因 | 可开发条件 | 预计开发时机 |
|---|---|---|---|
| （无） | — | — | — |

**离线验收边界（不得改写为生产验收）**：本轮验证全部离线——单元/集成测试使用 Fake LLM、
真实 Pydantic 契约与内存 SQLite；主代理独立验收在真实内存 SQLite + 真实统一执行器上覆盖
列/行策略拒绝、身份伪造与权限交集、输出脱敏与缓存分区。未访问远程模型与业务数据库；
MySQL/ClickHouse/PostgreSQL 实机数据源与远程模型的端到端验收按 spec/55 §11 单独执行，
不得以本条离线通过改写为业务部署验收完成。
