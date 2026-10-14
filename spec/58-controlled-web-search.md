# 受控联网搜索与研究摘要

对应功能：19.14，实施台账见 features/32-web-search-core.md、features/33-web-search-workflow.md。依赖既有 MCP 权限、聊天请求配额、Prompt 工厂和市场研究子图。

## 1. 审查结论与首期范围

现有市场研究子图委托通用 MCP Agent，可将附件、技能或内部知识引用当成联网证据；搜索工具和查询内容均缺少专用边界。本期改为确定性调用一个已加载且已授权的搜索 MCP 工具，再由模型总结该次搜索结果。复用现有 MCP 服务，不新增 Provider、网络客户端、密钥或抓取正文工具。

默认关闭。管理员配置精确工具名白名单后仍须当前用户明确要求联网。用户当前问题是唯一出站文本，模型不能改写出站请求；历史、数据库结构和结果、知识、附件、Skill 提示均不进入该搜索或摘要路径。普通文件分析和通用 MCP Agent 保持原行为。

## 2. 配置

| Settings 字段 | 默认 | 限制 |
|---|---|---|
| web_search_enabled | false | 总开关，关闭后零搜索、零新增模型调用 |
| web_search_mcp_tools | 空字符串 | 逗号分隔的精确完整工具名，如 public_search__web_search；禁止通配符和服务级全选 |
| web_search_timeout_seconds | 10.0 | 0.1 至 60 秒，单请求搜索总等待上限 |
| web_search_max_results | 8 | 1 至 20 |
| web_search_max_response_chars | 20000 | 1000 至 64000，原始响应超限拒绝解析 |
| web_search_max_query_chars | 256 | 16 至 2000；超限拒绝，不截断用户意图 |

字段写入 config/app.example.yaml 和运行期 LIVE_FIELDS 白名单，不修改实际部署配置。白名单为空或目标未加载时返回不可用，不为搜索请求全量建立 MCP 连接。

## 3. 核心接口与执行顺序

新建 src/search/models.py、src/search/service.py、src/search/__init__.py：

- SearchHit：source_id、url、title、snippet、provider、retrieved_at。source_id 由 URL 稳定摘要生成，时间用 UTC。标题至多 300 字符、摘要至多 2000 字符。URL 仅作引用，不抓取；仅接受无凭证的 HTTP(S) 链接，拒绝本地主机、字面私网地址和危险 scheme，去重。
- SearchResponse：success、hits（SearchHit 列表）、error_code、truncated。错误码固定且脱敏；无正文、SQL 或底层异常泄露。
- is_explicit_web_search(query: str) -> bool：只在问题开头识别有限中英文执行句式，可含直接相邻的「请/请帮我/麻烦/帮我/Please/Can you」等有限请求前缀。只忽略首尾空白，不忽略任意非空白残词；未识别的前缀与句中关键词均不构成授权。明确否定与仅离线要求优先拒绝。
- async search_public_web(query: str) -> SearchResponse：执行一次搜索，不调用 LLM。

执行句式按以下契约识别，使用锚定及词边界的确定性规则，不调用额外模型：

| 形式 | 可接受的执行请求 | 边界 |
|---|---|---|
| 中文动作 | 联网搜索/联网调研/联网查询/联网检索、网上搜索/网上查询、搜索网页/网页搜索等 | 未识别的前置词失败关闭；首尾和内部空白可以保留 |
| 英文动作 | search the web、search online、do/perform a web search | 必须处于请求位置，保留合法主题原文 |
| 英文名词式命令 | 裸 web search / online search，或以 for 引入主题 | 其它余段不能靠排除少量谓语获得授权；web search means / online search refers 等陈述直接拒绝 |

仅提及、解释、能力咨询和设置咨询不构成授权，例如「请解释“联网搜索”是什么功能」「Is web search available?」「How do I disable web search?」「联网搜索如何关闭？」。未知或模糊问法由市场研究子图返回 `needs_input`，请求用户明确联网意图。普通“市场研究/行业研究/竞品研究”同样不构成授权。

明确执行后的合法主题不会因含 how/what/关闭/解释/支持/引用等词被二次判成咨询，例如「联网搜索如何关闭某产品」「Search the web for how to enable web search in a browser.」「请帮我联网搜索“联网搜索”这个术语的定义」。

执行顺序：总开关与明确意图 → 输入长度校验 → 真实认证身份（user_id>0，非 anonymous）→ 已加载的 get_all_tools(tenant_id,user_id) → 精确工具白名单与参数 Schema → 再次检查真实身份一致 → 一次有超时的工具调用 → 有界解析与引用清洗。不得从 state 或模型参数伪造身份。MCP 原工具仍负责调用时授权复核和停用检查。单次失败不改用其他工具，不建立额外连接，取消原样传播。

可用工具的 Schema 必须只含必需 query:string，以及可选 max_results 或 limit:integer；不允许 url、sql、path、headers、body 或任何额外参数。参数由服务端写入，不接受模型参数。Schema 缺失、歧义、不兼容均跳过；只调用配置顺序中第一个兼容且当前可见的工具。

响应支持有界 JSON 文本或等价字典/列表：results 或 items 列表中的 title/url/snippet（兼容 content 作为摘要），或 source_id/locator.url 形式。只有工具实际返回且 URL 合法的条目才产生引用；纯文本或无有效来源返回固定失败码。空结果可正常返回 hits=[]，由研究层说明未检索到证据。不得把任意 source_id 或模型自报引用当成来源。

不做跨身份缓存，不记录问题、片段、完整 URL、凭证或原始异常。日志仅含真实身份、工具名、问题字符数、耗时、条目数和固定错误码；异常处理边界使用脱敏异常及原堆栈，避免重复记录同一底层异常。

## 4. 市场研究接入

market_research_node 先独立检查开关与显式意图；只有通过后调用 search_public_web。配置关闭、匿名或身份不符、无兼容工具、空证据时不调用模型，返回可理解的失败/未检索到证据说明。

摘要模型使用注册 Prompt web.research（通过 src/llm/client.py 工厂和统一 invocation），只接收当前用户问题与 SearchHit 形成的 Evidence。使用现有 render_evidence_context 标注不可信边界，不允许任何知识或探查工具：调用期 use_knowledge_loop(enabled=False)，不使用通用 ReAct Agent，不发送 state/history/schema/attachments/skill_prompt_override。引用完全取自 SearchResponse，不信任模型自报引用。模型缺失、超时或失败时返回确定性的有界来源列表和摘要，不伪造分析。取消不降级。

复用标准非 SQL 响应结构，source=market_research；analysis.citations 包含实际 URL、provider、检索 UTC 时间和 evidence_kind=search_snippet，limitations 明确“仅基于搜索摘要，未抓取网页正文”。不将搜索摘要表述为正文证据；不生成 SQL 或数据库查询。

build_task_plan 的确定性路由补充明确联网关键词并保留现有市场研究路由；既有非联网市场研究问法可得到需明确联网的友好说明。此变化应以实际子图路由和失败回退测试验证。

## 5. 验收和限制

全部单测使用 Fake MCP/Fake LLM；验证关闭零调用、匿名拒绝、跨身份/租户漂移拒绝、工具精确白名单、Schema 拒绝、单次调用预算、超时与取消、超限响应、URL 清洗、注入文本隔离、缓存不串用、模型失败降级、未泄露内部上下文和日志脱敏。集成测试使用实际市场研究子图 + 真实核心服务 + Fake MCP + Fake LLM，不能把全部边界替换成一个成功字典。

本期提供可接已部署搜索 MCP 的接口，不自动安装任何搜索服务。真实远程服务、租户多 Worker 配额、真实网页攻击和浏览器呈现验收单列待开发，不能以离线测试通过替代。聊天总请求配额复用现有入口；每个搜索请求额外固定最多一次 MCP 调用。后续自有搜索 MCP/Provider 与正文抓取均另行设计。
