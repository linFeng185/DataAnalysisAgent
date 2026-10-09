# 自主知识检索工具与统一 LLM 工具循环

日期：2026-09-19。设计与审核：Codex；开发执行：Claude Code。

## 1. 目标与现状

- 所有业务 LLM 调用均可按需调用 search_knowledge，尤其 SQL 生成/重试、异常分析、归因、报表规划和路由；不是只向 MCP 加工具。
- 保留 retrieve_schema 的预检索，不替换 Schema 获取、SQL layer1/2/3、安全检查或模型工厂。
- 复用 retrieval.search_knowledge 的向量/关键词召回、权限过滤、去重、确定性重排，不另建 RAG 或检索专用 LLM。
- 工具不调用 LLM，但工具结果返回后同一任务模型需要继续生成；无工具请求仍只有原有一次模型调用。
- 冷启动、嵌入和分范围扫描影响耗时，不承诺毫秒级；未经实测不宣称 layer3 失败率或 SQL 重试数已下降。
- 依赖 6.8.2/6.8.3/6.8.7/6.8.8/6.9.4/10.2.9/10.2.10 已完成，本次新增模块27，不重新开发既有功能。

## 2. 只读工具与安全边界

新增 src/tools/knowledge_search.py，定义 KnowledgeSearchArgs（Pydantic，extra=forbid）与 build_knowledge_tools(...) -> list[StructuredTool]，工具名 search_knowledge。

| 模型参数 | 约束 | 含义 |
|---|---|---|
| query | 去空白后1～512字符 | 具体字段、枚举、口径或错误线索 |
| datasource | 默认空，最多128字符 | 空值使用服务端默认数据源；显式值仍需授权 |
| category | 默认空，最多64字符 | 可选知识类别，不改变权限 |
| top_k | 整数1～8，默认5 | 拒绝非法值 |

身份、默认数据源、可选请求白名单、store、超时和预算只由服务端提供。模型不得传 tenant_id/user_id/role/visibility/owner_user_id/filters。

返回完整 JSON：status、evidence、citations、truncated；状态区分 ok/empty/invalid_arguments/forbidden/timeout/unavailable/budget_exhausted。仅导出受限正文、安全locator、source_id、version、必要分数和来源类别，不透传metadata、内部路径、连接串或堆栈。引用只能来自实际召回。复用 render_trusted_knowledge_context：知识不能覆盖系统安全、租户权限、只读或工具授权。

安全规则：
1. 构建时捕获 get_current_identity()，执行时再次比较；匿名/身份漂移失败关闭，不从模型、可编辑state或metadata恢复身份。
2. 保持 system + 本租户tenant + 本人private；super_admin仍只读system（spec/44）。
3. 指定数据源先通过Registry摘要和 resolve_datasource_access 授权，不调用Registry.resolve打开业务连接；请求白名单存在时还需是其子集。授权故障不退回无过滤检索。
4. 无默认数据源且参数为空，仅查明确未绑定数据源知识，不得全源扫描。允许给过滤器和search_knowledge新增仅关键字参数 unbound_only=False：True下推精确 datasource=""，与非空datasource互斥，不改变旧调用默认行为。缺少metadata的历史条目失败关闭。
5. 向量与关键词过滤一致，兼容Chroma/Milvus；单租户也禁止匿名。工具不写库、不执行SQL、不调用其他工具或模型。
6. 后台绑定已有可信身份并finally/reset恢复；无可信身份只禁用检索，不采用默认租户/平台身份。

实施补充：`search_knowledge` 增加仅关键字参数 `fail_on_backend_error=False`；原预检索维持兼容，新工具启用后端可用性检查，全部范围后端失败抛 `KnowledgeBackendUnavailable`，不得伪装为空命中。新上传未绑定知识明确写入 `datasource=""`；不自动修改历史条目。

## 3. 通用工具循环

新增 src/llm/knowledge_loop.py，提供 invoke_with_knowledge(prepared, messages=None)、stream_with_knowledge(prepared, messages=None) 或等价接口，流式与非流式共用协议逻辑。

- invoke_structured/invoke_text/stream_prompt 默认接入，工具回合结束才解析JSON/Pydantic。
- prepare_invocation继续负责注册Prompt、预算、模型、trace；不在get_task_llm隐藏Agent，不monkeypatch模型。
- bind_tools保留原任务模型、config/callbacks/tags/metadata、用量统计、租户模型选择；验证with_config返回RunnableBinding时工具/配置有效。
- 无tool_call不查向量库、不强制检索，不先增加一次模型调用决定是否检索。
- 每个tool_call都有匹配ToolMessage ID，包含未知/非法/拒绝/超额请求；不执行未知工具，不留下孤立调用。
- 支持同回合多个调用；最多3个工具回合、6次实际检索，再最多1次禁用工具的最终回答；未知/非法/重复请求也受回合上限控制。
- 本次执行内成功/empty结果按身份、datasource、query、category、top_k缓存；不缓存故障/拒绝，不跨请求。同一prepared多次执行和并行节点不共享预算/缓存。
- 明确不支持bind_tools时回退原一次调用；不把认证错误、超时、5xx、解析错误伪装成能力不支持。
- 取消向下传播，尊重FailureDomain.KNOWLEDGE策略；不可用不伪装empty，权限始终失败关闭。

默认预算：单次检索（含初始化/授权）3秒，累计检索时间8秒；单条正文1600字符、单次结果6000字符。总消息含system/human、工具schema、AI tool_calls、ToolMessage均纳入已有的 PromptDefinition.context_budget（由 get_prompt_definition 读取；项目不存在 llm_context_budget 配置项）。预留工具空间，不在已填满Prompt后无限追加；空间不足裁减证据/停止工具，不削弱系统安全、核心问题与SQL Schema优先级。保持完整JSON，不能硬截断序列化字符串，locator/source_id同样受限。预算耗尽明确上下文不足，不编造口径。

日志只含状态/计数/耗时和非敏感标识，不含query/正文/prompt/凭证。复用七天轮转、不新增后端、不逐回合INFO；触及的检索路径不得保留正文DEBUG泄露。

## 4. SQL流式与MCP

### SQL与SSE

generate_sql流式和首块超时非流式回退均走共用循环；完整合并tool_call_chunks再执行，中间正文不能污染SQL JSON。保留reasoning、原首块回退条件和final/done。

LangGraph callbacks独立产生on_chat_model_stream，仅generator不yield不能隐藏中间回合；需要可靠标记与SSE过滤或经测试验证的等价方案。工具参数/结果不能作为最终答案增量，最终答案仅出现一次，请求正常结束。工具交互期间不强求逐token展示，可保留节点进度。

### MCP单循环

工具加入附件/Skill/MCP同级列表，复用Agent及总限额，不套第二个通用循环；知识工具自身仍有次数/超时预算。外部MCP故障而本地工具可用时可继续；市场研究的外部工具护栏不得因新增知识工具而放宽。

真实知识引用接入_extract_tool_citations；附件白名单不能误删知识引用，也不能信任模型伪造引用。回归现有契约、工具额度、错误路径。

## 5. 调用点覆盖

| 调用点 | 要求 |
|---|---|
| classify_intent选源/规划 | 通用结构化入口，未选源仅未绑定知识，指定源需授权 |
| decompose_query、llm_answer、multi_source总结 | 通用入口 |
| generate_sql与重试 | 流式助手及首块回退 |
| analyze_result润色/分析/JSON重试 | 通用/显式助手，保留整体timeout |
| graph.attribution、indicator_catalog、node_routing | 通用入口，不改计算/路由护栏 |
| reporting.planner/charts/sample_guard/runner、report_workbench | 通用入口，保留身份、统计、预算 |
| plan_worker标题 | 可信身份下可用，不强制检索 |
| knowledge.image_assets | 显式助手，保留图片消息块和timeout |
| mcp_agent | 工具列表接入，单循环 |
| provider传输、测试注入planner Fake、tool/graph.ainvoke | 非业务裸调用，不另套循环 |

完成后搜索src下.ainvoke/.astream，逐条说明覆盖或排除理由，不能只靠修改公共入口推断无旁路。

## 6. 实施与验收

顺序：先工具/安全TDD，再共用循环TDD，再统一和裸调用接入、SQL/SSE、MCP、后台身份、跨方言与真实验收。修改范围为上述新模块及必要的invocation/retrieval/SQL/分析/MCP/图片/后台/SSE边界和对应tests，不夹带无关重构。更新features/27及CODE_GUIDE的新模块/调用链/失败边界；不改根SPEC.md/FEATURES.md，不commit/push、不新增重依赖、不更改运行时配置或重启用户服务。

| 编号 | 必须证明 |
|---|---|
| A1 | 参数无身份/权限字段；空白/超长/非法/未知字段拒绝，空召回为empty |
| A2 | 两租户/用户、三范围、超管、匿名、身份漂移、源越权、后台reset；向量/关键词同过滤 |
| A3 | 无调用=一次模型零检索，ID闭合、最后JSON有效，多调用/重复/错误/超额有界 |
| A4 | query/正文/敏感metadata不进日志，高频正常不加INFO，脱敏异常堆栈保留，异常不误降级 |
| A5 | SQL工具分片重建，中间内容不污染SQL/SSE，最终事件一次，首块回退和reasoning兼容 |
| A6 | prompt+schema+消息硬预算，完整JSON、长locator、超时/取消、并发缓存和额度隔离 |
| A7 | MCP本地检索不依赖外部，无双循环，市场研究护栏保留，真实附件/知识引用并存 |
| A8 | Fake模型请求orders.status合法值→实际工具返回证据→续轮生成正确条件→原SQL校验；覆盖ClickHouse/MySQL/PostgreSQL |
| A9 | 分析/归因/报表/路由/图片/后台可达入口，契约/统计/身份回归通过 |
| A10 | 记录真实服务/可用本地模型/只读检索步骤结果与缺口，离线通过不等于真实链路通过 |

单元测试Fake/Mock不调远程模型；local_llm仅本地服务可用执行，远程需RUN_LIVE_LLM_TESTS=1，本轮默认不启用。新增方法中文作用/Args/Returns注释和公开docstring；pytest按模块分组，覆盖正常/边界/错误/回退和Arrange/Act/Assert。

## 7. 审核结论（审核者填写）

2026-09-19，Codex 独立审核结论：**代码实现与本次离线验收通过；真实业务服务链路未验收，不视为生产上线放行。**

### 7.1 执行与返修

- 通过本机 Claude Code 执行实现，Codex 负责设计、独立失败用例、返修要求和复验；未提交或推送 Git，未重启既有服务，未调用真实远程业务模型。
- 审核发现并复验了默认源绕过白名单、原始 locator 重新进入正文、初始化不在超时内、失败不计预算、首轮工具正文泄漏、初始 schema 预算、后端故障伪装 empty、坏 JSON 调用 ID、供应商 reasoning 字段、MCP 故障阻断本地工具等边界。
- 大 Schema 专门验证“真实 evidence 进入下一轮”，不是仅检查工具名存在。最终预留包含实际工具定义、单次结果空间与协议开销，不增加原 Prompt 总预算。
- 读操作的阻塞 SDK 移至工作线程；Milvus 只读取消与写锁保护策略分开。在途只读线程由 SDK 有限超时结束并消费结果/异常，不强杀线程。

### 7.2 独立验证记录

| 检查 | 结果 | 范围与证据 |
|---|---|---|
| 定向合并回归 | 1022 passed，54.73秒，退出码0 | llm/tools/knowledge/memory，聊天与SSE，SQL生成/分析/MCP，报表后台/统计/权限；`logs/knowledge-independent-final-pytest.log` |
| 真实隔离 Chroma | 三方言均通过，已包含在1022项中 | `tests/test_graph/test_knowledge_chroma_review.py`：真实内存Chroma + 真实工具/检索/重排；其他租户与其他用户私有文档不进入模型消息；模型使用Fake |
| SQL 校验 | ClickHouse/MySQL/PostgreSQL 均通过 | 验证枚举证据、ToolMessage、引用、SQL条件、sqlglot及Schema校验；不是实际连接这三类业务数据库 |
| Python语法 | 本次36个变更/新增Python文件编译通过 | `compileall` 退出码0 |
| Ruff | 仅剩一处已存在的F401 | `src/memory/vector_store_milvus.py:12` 未使用logging导入；已用 `git show HEAD:...` 验证基线同样报错，不夹带修复；其余本轮静态问题已处理 |
| 差异检查 | `git diff --check` 通过 | 未修改根SPEC.md/FEATURES.md |
| 真实服务 | 未验收 | 本机8000端口可响应，但无授权会话的探测返回401；未配置本地模型，未擅自开启远程模型测试 |

### 7.3 适用限制与后续验收

1. 所有业务调用入口统一接入，不等于所有模型/节点每次都实际挂工具：不支持工具、匿名、禁用或剩余预算不足时沿原调用路径返回；部分既有小预算Prompt会触发此回退。是否需要调整这些任务预算，作为后续配置/模型验收事项，不擅自提高总预算。
2. 工具调用回合的SSE先缓冲，确认是最终回答才释放，避免中间正文和参数污染SQL或答案；未强求工具回合逐token可见。
3. 原有单/多租户模式语义保持不变；单租户模式不额外启用数据源权限表隔离。多租户隔离的离线验证显式使用多租户策略，不能把它等同于任意部署配置都自动强隔离。
4. 历史知识缺少datasource元数据时，不纳入新的unbound_only精确查询；已有预检索语义不变。需要复用这类历史全局知识时，应经授权补齐元数据或重新摄取，不由只读工具自动迁移。
5. 真实验收需要本机可用授权会话及允许测试的模型；覆盖SQL失败后的定向补检索、异常结果/归因、报表后台、多源隔离、并发请求与失败恢复，并记录冷启动/实际SDK延迟。没有这些证据，不宣称layer3失败率或SQL重试次数已经下降。
