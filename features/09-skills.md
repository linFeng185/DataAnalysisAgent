# 9. Skills 技能系统

## 9. Skills 技能系统 `[P0:4 P1:37 P2:5 P3:3]`

### 9.1 Skill 引擎

| # | 功能 | 文件 | 描述 | 状态 |
|---|------|------|------|------|
| 9.1.1 | Skill dataclass | `src/skill_manager.py` | name / version / description / triggers / depends_on / tools / system_prompt_override / output_schema_extension / source_path / enabled | 开发完成 |
| 9.1.2 | SkillManager 类 | `src/skill_manager.py` | Skill 发现、加载、激活与生命周期管理 | 开发完成 |
| 9.1.3 | discover() | 同上 | 启动时扫描 skills/ 目录，发现所有 SKILL.md | 开发完成 |
| 9.1.4 | _parse_skill_manifest() | 同上 | 解析 SKILL.md 的 YAML frontmatter + Markdown body | 开发完成 |
| 9.1.5 | _check_dependencies() | 同上 | 检查依赖 (mcp_servers / skills / python_packages) 是否满足 | 开发完成 |
| 9.1.6 | match_skills() | 同上 | 根据用户输入匹配激活 Skill: 关键词 + 意图 + 表名三重 OR 匹配 | 开发完成 |
| 9.1.7 | get_active_tools() | 同上 | 动态加载激活 Skill 的 tools.py 模块，获取 BaseTool 列表 | 开发完成 |
| 9.1.8 | build_skill_prompt() | 同上 | 组装激活 Skill 的 system_prompt_override 追加到 System Prompt | 开发完成 |
| 9.1.9 | _load_skill_module() | 同上 | 动态 import Skill 的 tools.py 模块 | 开发完成 |
| 9.1.10 | Skill Manifest v2 与请求授权 | `src/skill_manager.py`、`src/graph/skill_activation.py` | 两阶段激活 Skill，解析 capabilities/accepts/permissions/resources，并在加载与执行前校验资产、网络和请求级工具预算 | 单测完成 | P1 |
| 9.1.11 | Skill 三级作用域隔离 `[P0]` | `src/skill_manager.py` | system/tenant/private 目录发现、复合标识和 tenant_id/user_id 请求级过滤 | 单测完成 |
| 9.1.12 | Skill 隔离执行与资源治理 `[P0]` | `src/skill_runtime.py`、`src/skill_worker.py` | 非系统 Skill 子进程隔离、超时/CPU/内存/网络/文件/输出大小限制 | 单测完成 |
| 9.1.13 | Skill 签名与完整性校验 `[P0]` | `src/skill_security.py`、`src/skill_manager.py` | 包摘要、Ed25519 签名、可信签发者和篡改阻断 | 单测完成 |
| 9.1.14 | Skill 输入输出契约强制 `[P1]` | `src/skill_runtime.py`、工作流 | 工具输入 Pydantic 校验、输出 JSON Schema、脱敏和引用边界 | 单测完成 |

### 9.2 示例 Skill — 数据质量检查

| # | 功能 | 文件 | 描述 | 状态 |
|---|------|------|------|------|
| 9.2.1 | 标准 Skill 包 | `skills/data_quality_check/` | Manifest v2、中文界面配置、严格输入输出契约和固定评测集；仅明确质量关键词触发 | 单测完成 |
| 9.2.2 | check_null_rate Tool | `skills/data_quality_check/tools.py` | 异步检查指定列的空值率，字段缺失按空值处理 | 单测完成 |
| 9.2.3 | check_duplicates Tool | 同上 | 异步检查指定列的非空重复值并返回稳定计数 | 单测完成 |
| 9.2.4 | detect_outliers Tool | 同上 | 异步执行 Z-Score 异常值检测，少于八行时拒绝输出结论 | 单测完成 |
| 9.2.5 | PROMPTS 定义 | `skills/data_quality_check/SKILL.md` | Skill 专属中文执行边界与结果解释约束 | 单测完成 |
| 9.2.6 | 确定性质量报告 `[P1]` | `src/graph/nodes/analyze_result.py` | 激活后真实输出空值率、重复行和异常值，不依赖 LLM 自述 | 单测完成 |

### 9.3 示例 Skill — 自定义报告

| # | 功能 | 文件 | 描述 | 状态 |
|---|------|------|------|------|
| 9.3.1 | 标准 Skill 包 | `skills/custom_report/` | Manifest v2、中文界面配置、严格输入输出契约、固定评测集和模板资产 | 单测完成 |
| 9.3.2 | 周报模板 | `skills/custom_report/templates/weekly_report.jinja2` | Jinja2 模板 — 周度数据报告 | 单测完成 |
| 9.3.3 | 月报模板 | 同上 | Jinja2 模板 — 月度数据报告 | 单测完成 |
| 9.3.4 | 报告渲染工具 | `skills/custom_report/tools.py` | 异步执行 render_report(template, data) 并渲染 Markdown | 单测完成 |
| 9.3.5 | 报告 Skill 工作流闭环 | `src/graph/subgraphs/report.py`、`analyze_result.py` | custom-report 真实执行、结果校验并输出 report Artifact | 单测完成 |

### 9.4 Skill 分发

| # | 功能 | 文件 | 描述 | 状态 |
|---|------|------|------|------|
| 9.4.1 | 本地 Skill 扫描 | `src/skill_manager.py` | 多目录扫描 + 缓存注入 + 手动刷新 | 开发完成 |
| 9.4.2 | Git 子模块支持 | `.gitmodules` | 社区 Skill 用 git submodule 引入 | 待开发 |
| 9.4.3 | Skill Registry 接口 | `src/skill_registry.py`、`src/skill_manager.py` | approved 目录、HTTPS 主机白名单、SHA-256、ZIP 安全检查和三级作用域安装 | 单测完成 |

### 9.5 Skills 管理与 API（新增）

| # | 功能 | 文件 | 描述 | 状态 |
|---|------|------|------|------|
| 9.5.1 | 列表 API | `src/api/routes.py` | GET /skills — is_builtin/triggers/tools/deps | 开发完成 |
| 9.5.2 | 批量上传 | 同上 | POST /skills/upload — 文件夹递归 + YAML 解析 | 开发完成 |
| 9.5.3 | 启用/禁用/删除 | 同上 | PUT toggle + DELETE（内置保护） | 开发完成 |
| 9.5.4 | 刷新 + 内容 | 同上 | POST /refresh + GET /{name}/content | 开发完成 |
| 9.5.5 | 前端管理页 | `SkillsPage.tsx` | 列表/切换/上传/详情 Modal/刷新/删除 | 开发完成 |
| 9.5.6 | Skill 作用域管理授权 `[P0]` | `src/api/routes.py` | system 仅超管、tenant 仅租户管理员、private 仅本人上传/启停/删除 | 单测完成 |

### 9.6 内置底座 Skills

| # | 功能 | 文件 | 描述 | 状态 |
|---|------|------|------|------|
| 9.6.1 | data-profile | `skills/data-profile/` | 对行式数据执行字段画像、缺失率、唯一性、候选键、时间列和重复行检查 | 单测完成 | P1 |
| 9.6.2 | data-preparation | `skills/data-preparation/` | 以非破坏方式执行可审计的去重、空值填充、类型转换、重命名、选列和排序 | 单测完成 | P1 |
| 9.6.3 | schema-linking | `skills/schema-linking/` | 将用户术语确定性映射到表和字段候选，并给出分数、歧义和关联键候选 | 单测完成 | P1 |
| 9.6.4 | cross-source-fusion | `skills/cross-source-fusion/` | 基于 JoinContract 检查跨资产连接的匹配率、基数和结果膨胀风险 | 单测完成 | P1 |
| 9.6.5 | statistical-analysis | `skills/statistical-analysis/` | 为描述统计、趋势、异常、相关和集中度提供统一确定性分析入口 | 单测完成 | P1 |
| 9.6.6 | timeseries-analysis | `skills/timeseries-analysis/` | 校验时间顺序并执行趋势、滚动回测、基线选择和预测区间计算 | 单测完成 | P1 |
| 9.6.7 | document-evidence | `skills/document-evidence/` | 从带来源定位的文档片段提取可追溯证据并检测相互冲突的主张 | 单测完成 | P1 |
| 9.6.8 | artifact-export | `skills/artifact-export/` | 将结构化分析产物确定性导出为 JSON、CSV 或 Markdown 文本 | 单测完成 | P1 |
| 9.6.9 | sql-diagnostics | `skills/sql-diagnostics/` | 对只读 SQL 执行方言校验、规范化改写和静态复杂度诊断 | 单测完成 | P1 |
| 9.6.10 | skill-validator | `skills/skill-validator/` | 离线校验 Skill Manifest v2、工具契约、权限、资源预算和触发冲突 | 单测完成 | P1 |

### 9.7 内置治理 Skills

| # | 功能 | 文件 | 描述 | 状态 |
|---|------|------|------|------|
| 9.7.1 | data-contract | `skills/data-contract/` | 按显式契约校验字段、类型、空值、唯一性、枚举、范围和复合键约束 | 单测完成 | P1 |
| 9.7.2 | schema-drift | `skills/schema-drift/` | 对比两版 Schema 并区分破坏性、兼容性和信息性变化 | 单测完成 | P1 |
| 9.7.3 | dataset-diff | `skills/dataset-diff/` | 按主键确定性比较数据集的新增、删除、修改和字段级差异 | 单测完成 | P1 |
| 9.7.4 | sampling-audit | `skills/sampling-audit/` | 使用固定种子执行随机、系统、分层或时间窗口采样并报告偏差 | 单测完成 | P1 |
| 9.7.5 | distribution-drift | `skills/distribution-drift/` | 比较基准集与当前集的数值分布和类别频率漂移 | 单测完成 | P1 |
| 9.7.6 | semantic-normalization | `skills/semantic-normalization/` | 统一日期、时区、布尔值、空值标记、数值单位和枚举别名 | 单测完成 | P1 |
| 9.7.7 | provenance-audit | `skills/provenance-audit/` | 审核资产指纹、工具参数、代码版本、证据定位和变换链完整性 | 单测完成 | P1 |
| 9.7.8 | result-verification | `skills/result-verification/` | 验证汇总与明细一致性、总计关系、截断状态、空结果和核对规则 | 单测完成 | P1 |
| 9.7.9 | privacy-classifier | `skills/privacy-classifier/` | 确定性识别常见敏感字段和值并给出分类和置信度，不执行脱敏 | 单测完成 | P1 |
| 9.7.10 | skill-evaluator | `skills/skill-evaluator/` | 离线执行固定评测样例并统计契约、确定性、耗时和最低得分门禁 | 单测完成 | P1 |

### 9.8 内置工程治理 Skills

| # | 功能 | 文件 | 描述 | 状态 |
|---|------|------|------|------|
| 9.8.1 | execution-trace-audit | `skills/execution-trace-audit/` | 审计节点执行顺序、耗时、错误、重试和状态写回完整性 | 单测完成 | P1 |
| 9.8.2 | workflow-state-diff | `skills/workflow-state-diff/` | 比较工作流前后状态并识别字段新增、删除、覆盖和持久化差异 | 单测完成 | P1 |
| 9.8.3 | prompt-injection-detector | `skills/prompt-injection-detector/` | 检测不可信文本中的越权指令、角色伪造、工具诱导和数据外传请求 | 单测完成 | P1 |
| 9.8.4 | secret-scanner | `skills/secret-scanner/` | 扫描代码、配置和日志文本中的密钥、令牌、连接串及私钥特征 | 单测完成 | P1 |
| 9.8.5 | reproducibility-audit | `skills/reproducibility-audit/` | 审核输入、代码、依赖、配置、随机种子和重复输出的可复现条件 | 单测完成 | P1 |
| 9.8.6 | api-contract-drift | `skills/api-contract-drift/` | 比较两版 OpenAPI 契约并识别路径、参数、请求体和响应的破坏性变化 | 单测完成 | P1 |
| 9.8.7 | file-ingestion-inspector | `skills/file-ingestion-inspector/` | 检查文件样本的编码、BOM、分隔符、表头、换行和压缩元数据风险 | 单测完成 | P1 |
| 9.8.8 | evidence-coverage-audit | `skills/evidence-coverage-audit/` | 审核结论与引用的映射、证据定位和关键主张覆盖率 | 单测完成 | P1 |
| 9.8.9 | numeric-integrity-audit | `skills/numeric-integrity-audit/` | 检查数值精度、溢出、舍入、非有限值和单位维度一致性 | 单测完成 | P1 |
| 9.8.10 | resilience-policy-audit | `skills/resilience-policy-audit/` | 审核超时、重试、退避、熔断、幂等和失败补偿策略 | 单测完成 | P1 |

### 模块收尾

模块功能点共 64 项，已完成 63 项，待开发 1 项。

| 功能点 | 不开发原因 | 可开发条件 | 预计开发时机 |
|--------|------------|------------|--------------|
| 9.4.2 Git 子模块支持 | 当前优先完成本地受管目录的安全隔离，Git 来源还缺少签名、版本锁定和更新审计 | 建立受信任仓库白名单、commit 锁定和安装审计 | Phase 4，Skill 分发增强 |

---
