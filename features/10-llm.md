# 10. LLM 管理层

## 10. LLM 管理层 (llm/) `[P0:10 P1:4 P2:1]`

### 10.1 客户端工厂

| # | 功能 | 文件 | 描述 | 状态 |
|---|------|------|------|------|
| 10.1.1 | ChatOpenAI 工厂 | `src/llm/client.py` | get_openai_llm() — ChatOpenAI 实例 | 单测完成 | P0 |
| 10.1.2 | ChatAnthropic 工厂 | 同上 | get_anthropic_llm() — ChatAnthropic 实例 | 单测完成 | P0 |
| 10.1.3 | LLM 路由器 | 同上 | get_llm() — provider 自动路由 | 单测完成 | P0 |
| 10.1.4 | cheap_llm 工厂 | 同上 | get_cheap_llm() 使用配置的 Provider 与 cheap_llm_model，不硬编码 OpenAI | 单测完成 | P0 |
| 10.1.5 | is_llm_available() | 同上 | API Key 可用性检测 | 单测完成 | P0 |
| 10.1.6 | 节点级模型路由 `[P1]` | 同上 | resolve_llm_task_target() 在 local/remote/none 间确定性选择 | 单测完成 |
| 10.1.7 | 本地模型优先 `[P1]` | 同上 + `src/config.py` | 轻量节点默认 LOCAL_LLM_*，未配置时规则回退，不隐式等待远程 | 单测完成 |
| 10.1.8 | 远程任务授权 `[P1]` | 同上 | LLM_REMOTE_TASKS 默认仅 generate_sql，SQL 强制 reasoning=False | 单测完成 |
| 10.1.9 | 平台厂商与模型目录 | `src/api/routes/llm_admin.py`、`migrations/013_tenant_identity_llm.sql` | super_admin 动态维护厂商协议和模型能力目录，不保存平台 API Key | 单测完成 | P0 |
| 10.1.10 | 租户 LLM 命名连接 | `src/llm/tenant_config.py`、`src/api/routes/llm_admin.py` | 同厂商/跨厂商多连接，地址独立且 API Key 加密持久化 | 单测完成 | P0 |
| 10.1.11 | 租户默认与对话选择 | `src/llm/tenant_config.py`、Chat API | tenant 默认连接/模型，对话可从当前租户启用模型中覆盖 | 单测完成 | P0 |
| 10.1.12 | 租户统一 Provider 解析 | `src/llm/client.py`、`src/llm/invocation.py` | 调用入口按请求租户选择协议 Adapter，跨租户和停用配置失败关闭 | 单测完成 | P0 |
| 10.1.13 | 平台模型目录物理删除 | `src/api/routes/llm_admin.py` | super_admin 可删除未被租户引用的模型和厂商，引用冲突返回 409 | 单测完成 | P0 |
| 10.1.14 | 厂商级模型能力动态表单 | `src/llm/capability_schema.py`、`migrations/014_llm_catalog_reasoning.sql` | 厂商维护字段定义，模型能力按类型、选项和范围验证，不再手写 JSON | 单测完成 | P0 |
| 10.1.15 | 对话推理偏好与 DeepSeek V4 | `src/llm/adapters/deepseek.py`、`tenant_config.py` | Pro/Flash 完整适配 thinking、high/max、reasoning_content 和工具链回传 | 单测完成 | P0 |

### 10.2 Prompt 模板

| # | 功能 | 文件 | 描述 | 状态 |
|---|------|------|------|------|
| 10.2.1 | INTENT_CLASSIFY_PROMPT | `src/llm/prompts.py` | 意图识别 Prompt | 单测完成 | P0 |
| 10.2.2 | SQL_GENERATION_SYSTEM_PROMPT | 同上 | 安全/租户/只读优先于用户和 Skill，SQL 生成 Prompt + 方言速查 | 单测完成 | P0 |
| 10.2.3 | DATA_ANALYSIS_PROMPT | 同上 | 数据分析 Prompt | 单测完成 | P0 |
| 10.2.4 | CHART_RECOMMEND_PROMPT | 同上 | 图表推荐 Prompt | 单测完成 | P0 |
| 10.2.7 | get_dialect_cheatsheet() | 同上 | 3 种方言速查表 | 单测完成 | P0 |
| 10.2.8 | Prompt 版本号管理 | `src/llm/prompts.py`、`src/llm/output_contracts.py` | Prompt ID/版本/变量校验、扩展注册和 Pydantic 输出契约；LangSmith A/B 仍属后续增强 | 单测完成 | P2 |
| 10.2.9 | Prompt 统一总预算 | `src/llm/prompt_budget.py`、各 LLM 调用节点 | System、用户问题、Schema、知识、Skill、示例、结果和历史共享字符预算，按优先级保留与裁剪 | 单测完成 | P1 |
| 10.2.10 | Prompt 多版本回滚与统一调用 | `src/llm/prompts.py`、`src/llm/invocation.py` | 全部业务 LLM 调用走注册表，支持激活版本、回滚、追踪元数据和结构化/流式调用 | 单测完成 | P1 |

### 模块收尾

模块功能点共 23 项，已完成 23 项，待开发 0 项。

本模块本轮没有待开发功能点。

---
