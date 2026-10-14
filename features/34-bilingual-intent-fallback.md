# 34. 中英文意图规则回退一致性

设计：spec/59-bilingual-intent-fallback.md。Depends on：4.2 意图分类、28.4 展示语言、12 请求权限，均已有实现。本模块为 19.6 的有限增量，不代表所有多语言工作已完成。

| 编号 | 功能 | 优先级 | 状态 | 验收 |
|---|---|---|---|---|
| 34.1 | 中英文规则意图与模型不可用回退对齐 | P2 | 单测完成 | 统计、结构、趋势、原因、分组与词边界 |
| 34.2 | 历史追问和既有权限路由回归 | P2 | 单测完成 | 文件/预测保留、未知问法、授权数据源与 Skill 状态、取消 |

共 2 项，已完成 2 项，待开发 0 项。

## 实现与验证记录（2026-10-14）

- 初版实现范围：仅 src/graph/nodes/classify_intent.py。为既有分支追加有限英文词组，经新增 `_english_phrase_hit` 以 ASCII 词边界匹配（`(?<![a-z0-9_])…(?![a-z0-9_])`），正则按词组缓存；未引入翻译模型、网络调用、新依赖或新的常规日志，`_llm_classify` 与 `_select_authorized_datasource` 未改动，TaskPlan、Skill 激活、授权数据源选择与取消传播路径未改动。
- 初版覆盖词表：结构 14 条（available tables / what columns / column types 等）、相关与异常 7 条（correlation / outlier / anomaly 等）、原因 6 条（why / reason / cause 等）、趋势 4 条（trend / over time 等）、排名分组 9 条（rank / by category / group by 等）、闲聊 7 条（hello / thanks / what can you do 等）、历史追问 8 条（these results / previous result 等，仅存在历史时生效）、普通统计 9 条（how many / total / average / count / sum 等）、统计兜底补充 10 条（quantity / amount / aggregate 等）。
- 初版词边界验证：account 不因 count、summary 不因 sum、user_count 不因 count 命中；中文判定顺序（结构→文件→相关→归因→趋势→排名→闲聊→历史追问→统计）保持不变。
- 初版离线验收：主代理脚本首轮 10 项由 8 failed / 2 passed 变为 10 passed。
- 收尾修复（同日第二轮，依据独立验收脚本扩充为 12 项后的 2 条失败）：历史指代分支（meta）此前先于普通统计分支，且统计兜底只纠正 chat，导致「Ignore the previous results and count orders for June 2026.」「Compared with these results, run a new query for total sales in July 2026.」被误判为 meta / restore_previous_result。修复：新增 `_explicit_new_query_request`（英文 `_NEW_QUERY_PHRASES_EN` 沿用词边界，中文 `_NEW_QUERY_PHRASES_ZH` 仅收忽略之前/忘记上述/新查询/重新查询/重新统计/重新计算等要求重新执行的表达），仅当历史指代与明确新查询信号同时命中时让普通统计分支优先；无信号的「Summarize these results」「Summarize the previous results」「Count in the previous results」「总结一下这些数据」等仅处理已有结果的请求仍判 meta。文件/预测优先级、Skill 激活、授权数据源选择与取消传播未改。
- 收尾日志审计：统计兜底与 `_llm_classify`、`_select_authorized_datasource` 入口的 `query=<问题正文前 80 字符>` 字段统一改为 `query_chars=<长度>`，不再携带问题正文；未新增 INFO、未调整不相关异常策略；新增重复调用日志回归断言问题与历史正文不入日志。
- 测试（实测）：tests/test_graph/test_bilingual_intent.py 66 用例通过（初版 53 + 本轮新增 6 条新查询冲突同类、6 条仅处理已有结果反例、1 条重复调用日志回归）；tests/test_graph/test_classify_intent.py（BUG-15）通过。
- 离线验收（实测）：主代理 12 项独立脚本 ../test_bilingual_intent_acceptance.py 2 failed / 10 passed → 12 passed；定向回归（test_bilingual_intent、test_classify_intent、test_contracts、test_skill_activation、test_node_routing、test_node_registry、test_workflow、test_workflow_remediation、test_failure_visibility、test_processor_routing、test_graph_routing、test_architecture_remediation、test_architecture_convergence 共 13 个文件）283 passed；加跑 tests/test_graph + tests/test_logging_policy + tests/test_logging_noise + tests/test_tools 852 passed、1 skipped（test_live_multi_source 需 RUN_LIVE_DATASOURCE_TESTS=1，非本轮范围）；ruff 与 py_compile 通过。
- 未验收边界：真实远程/本地模型语义质量、非中英语言、全面提示本地化未在本轮验证；真机服务链路未跑，离线 Fake 用例只证明其覆盖范围。

| 待开发项 | 不开发原因 | 可开发条件 | 预计开发时机 |
|---|---|---|---|
| （无） | — | — | — |

19.6 其余的非中英语言兜底、全面提示本地化及真实模型语义验收仍在 features/19-extensions.md 跟踪。
