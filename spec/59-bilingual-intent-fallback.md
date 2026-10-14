# 中英文意图规则回退一致性

对应 19.6 的增量，台账见 features/34-bilingual-intent-fallback.md。依赖既有 4.2 意图分类、TaskPlan、28.4 展示语言及当前授权数据源与 Skill 机制。范围仅为中文/英文请求的规则回退，不新增翻译模型，不修改 SQL 表列标识符。

## 已确认问题

当分类模型未配置、调用失败或返回不可用结果时，英文统计、结构、趋势和归因问题会落入 chat/meta；中文同义请求则进入正确数据能力。主代理对实际 classify_intent_node 的十条离线调用已得到八条失败，证据 ../bilingual-intent-red.log。问题在确定性规则缺少英文表达，不是模型参数、权限或展示语言没有传入。

## 接口与行为

保持 async classify_intent_node(state: AnalysisState) -> dict 的公开契约以及现有 TaskPlan、routing_context 和 Skill 激活结果。state 中的 user_query、历史、权限及数据源不被改写。仅为已有类别补充有限、可解释的英文词组和单词边界匹配：

| 类别 | 英文典型表达 | 既有意图 |
|---|---|---|
| 表结构与字段说明 | list available tables / what columns / column types | metadata |
| 普通统计 | how many / total sales / average order amount | query |
| 时间趋势 | sales trends / trend over time | trend |
| 原因与关联 | why did sales decline / correlation | attribution |
| 排名与分组 | rank products / top products / by category | aggregation |
| 闲聊 | hello / thanks / what can you do | chat |
| 对已有结果追问 | summarize these results / previous result | meta，仅存在历史时 |

更具体的结构、文件、趋势、归因、排名优先于普通统计；历史追问优先于泛化 query，但不得吞掉包含新明确统计目标的请求。文件 ID、预测、研究等既有能力优先级不变。英文匹配用词边界与有限词组，account 不得因 count 误命中、summary 不得因 sum 误命中。不能凭孤立英语字母或宽泛子串把所有请求改成 SQL。未知请求仍沿既有模型/安全回退逻辑处理。

分类模型可用时保留原调用策略与结果契约；本次补齐相当于中文已存在的规则，不新增任何外部调用。模型取消继续向上传播。不得从模型输出构造新权限或绕过 datasource_access。重复调用不新增正常路径 INFO；若修改现有诊断字段，禁止加入用户问题全文或历史正文。

## 验收与限制

pytest 使用真实节点及任务计划、Fake 分类模型，覆盖模型不可用/错误分类回退、中英文同义、大小写/标点/词边界、历史追问、未知问法、文件与预测路由保留、权限与 Skill 状态、取消恢复。不得调用真实远程模型或业务库。已有 28.4 负责正文/图表展示语言，本轮不重复开发。非中英语言词典、所有用户提示本地化和真实模型语义评测仍为 19.6 后续范围，不能把本次规则修复标为 19.6 全部完成。
