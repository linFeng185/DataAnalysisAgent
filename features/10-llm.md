# 10. LLM 管理层

## 10. LLM 管理层 (llm/) `[P0:10 P1:7]`

### 10.1 客户端工厂

| # | 功能 | 文件 | 描述 | 状态 |
|---|------|------|------|------|
| 10.1.1 | ChatOpenAI 工厂 | `src/llm/client.py` | get_openai_llm() — ChatOpenAI 实例 | 单测完成 | P0 |
| 10.1.2 | ChatAnthropic 工厂 | 同上 | get_anthropic_llm() — ChatAnthropic 实例 | 单测完成 | P0 |
| 10.1.3 | LLM 路由器 | 同上 | get_llm() — provider 自动路由 | 单测完成 | P0 |
| 10.1.4 | cheap_llm 工厂 | 同上 | get_cheap_llm() — gpt-4o-mini | 单测完成 | P0 |
| 10.1.5 | is_llm_available() | 同上 | API Key 可用性检测 | 单测完成 | P0 |
| 10.1.6 | 节点级模型路由 `[P1]` | 同上 | resolve_llm_task_target() 在 local/remote/none 间确定性选择 | 单测完成 |
| 10.1.7 | 本地模型优先 `[P1]` | 同上 + `src/config.py` | 轻量节点默认 LOCAL_LLM_*，未配置时规则回退，不隐式等待远程 | 单测完成 |
| 10.1.8 | 远程任务授权 `[P1]` | 同上 | LLM_REMOTE_TASKS 默认仅 generate_sql，SQL 强制 reasoning=False | 单测完成 |

### 10.2 Prompt 模板

| # | 功能 | 文件 | 描述 | 状态 |
|---|------|------|------|------|
| 10.2.1 | INTENT_CLASSIFY_PROMPT | `src/llm/prompts.py` | 意图识别 Prompt | 单测完成 | P0 |
| 10.2.2 | SQL_GENERATION_SYSTEM_PROMPT | 同上 | 安全/租户/只读优先于用户和 Skill，SQL 生成 Prompt + 方言速查 | 单测完成 | P0 |
| 10.2.3 | DATA_ANALYSIS_PROMPT | 同上 | 数据分析 Prompt | 单测完成 | P0 |
| 10.2.4 | CHART_RECOMMEND_PROMPT | 同上 | 图表推荐 Prompt | 单测完成 | P0 |
| 10.2.7 | get_dialect_cheatsheet() | 同上 | 3 种方言速查表 | 单测完成 | P0 |
| 10.2.8 | Prompt 版本号管理 | 同上 | Phase 3: LangSmith A/B 测试 | 待开发[^6] | P2 |

---
