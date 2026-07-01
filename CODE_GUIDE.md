# DataAnalysisAgent — 代码导航指南

## 项目概述

LLM 驱动的数据分析智能体。用自然语言提问，自动完成：意图识别 → 表结构检索 → SQL 生成 → 安全校验 → 执行 → 分析 → 图表生成 → 响应组装。

**技术栈**：FastAPI + LangGraph + LangChain + sqlglot + SQLAlchemy + React + TypeScript。

## 目录结构

```
├── src/
│   ├── api/              ① Web 接口层 — 22 个端点 + SSE 流式
│   ├── graph/            ② 核心流水线 — LangGraph 10 节点 DAG
│   │   └── nodes/           10 个执行节点
│   ├── llm/              ③ LLM 调用层 — 工厂 + 适配器 + Prompt
│   │   └── adapters/         模型适配器
│   ├── datasource/       ④ 数据源管理 — 注册/发现/Schema/凭证加密
│   │   └── providers/        数据源提供者
│   ├── connectors/       ⑤ 数据库连接器 — 6 种方言
│   ├── knowledge/        ⑥ 知识库 — Schema 缓存 + 文档解析 + 上传管理
│   ├── memory/           ⑦ 记忆系统 — 会话持久化 + 上下文裁剪 + 历史
│   ├── tools/            ⑧ 分析工具 — 统计计算
│   ├── security/         ⑨ 安全模块 — 脱敏 + 限流 + 审计
│   ├── mcp/              ⑩ MCP 集成 — 客户端管理 + 工具暴露
│   ├── config.py             配置管理 (pydantic-settings)
│   └── skill_manager.py      技能引擎
├── frontend/                 React SPA (Vite + Ant Design + ECharts)
├── skills/                   内置 Skills
├── spec/                     技术规格（15 个章节）
├── features/                 功能清单（19 个模块）
├── tests/                    测试
└── docs/metrics/             业务指标文档
```

## 核心数据流

```
POST /api/v1/chat {"query": "本月 GMV 排名？", "stream": true}
  │
  ├─ classify_intent     关键词匹配 → 7 种意图 + Skill 激活
  ├─ retrieve_schema     SchemaManager 三级回退 → 表结构 + 知识库上下文
  ├─ generate_sql        LLM 生成 SQL（对话历史注入 + 重试上下文）
  ├─ layer3_validate     sqlglot 语法校验 + DDL/DML 安全拦截
  ├─ layer4_explain      EXPLAIN 预执行（桩）
  ├─ execute_sql         连接池执行（空 SQL 跳过 / 限流 / 审计）
  ├─ analyze_result      统计计算 + LLM 洞察
  ├─ generate_chart      ECharts 配置生成（桩）
  └─ build_response      响应组装 + 对话历史持久化（dict + messages 双写）
```

## 各模块说明

### ① `src/api/` — Web 接口层

| 文件 | 职责 |
|------|------|
| `routes.py` | 22 个端点：chat / history / schema / datasources / skills / knowledge / health |
| `schemas.py` | Pydantic 请求/响应模型 |
| `streaming.py` | SSE 流式（13 种事件类型） |
| `middleware.py` | 异常 → HTTP 状态码映射 |

### ② `src/graph/` — 核心流水线

| 文件 | 职责 |
|------|------|
| `state.py` | `AnalysisState` TypedDict（30+ 字段） |
| `workflow.py` | StateGraph 组装 + 5 个条件路由 + `init_app()` 异步初始化 Checkpointer |
| `nodes/classify_intent.py` | 意图分类 + Skill 激活 |
| `nodes/retrieve_schema.py` | Schema 检索 + 知识库上下文 |
| `nodes/generate_sql.py` | LLM SQL 生成（对话历史注入） |
| `nodes/layer3_validate.py` | 安全校验（14 种危险操作正则） |
| `nodes/execute_sql.py` | SQL 执行（空 SQL 跳过保护） |
| `nodes/analyze_result.py` | 统计 + LLM 分析 |
| `nodes/build_response.py` | 响应组装 + 历史记录（dict + messages 双写） |

### ③ `src/llm/` — LLM 调用层

`client.py`（工厂）+ `adapters/`（DeepSeek/OpenAI/Anthropic 适配器）+ `prompts.py`。

### ④ `src/datasource/` — 数据源管理

`registry.py`（`resolve_or_none` 不抛异常）+ `credential_manager.py`（Fernet + 明文回退）+ `setup.py`（SQLite 演示库）。

### ⑤ `src/connectors/` — 数据库连接器

6 种方言：ClickHouse / MySQL / PostgreSQL / SQLite / Oracle / MSSQL。

### ⑥ `src/knowledge/` — 知识库

| 文件 | 职责 |
|------|------|
| `schema_manager.py` | ChromaDB Schema 缓存（三级回退 + 嵌入模型 + 单例 `get_schema_manager()`） |
| `doc_parser.py` | PDF/Word/TXT/MD 文本提取 + 4 种分块策略（AUTO/HEADING/PARAGRAPH/FIXED） |
| `upload_manager.py` | 异步上传任务（状态追踪 + 后台 ChromaDB 写入 + 进度轮询） |
| `business_rules.py` | 业务规则检索 |
| `doc_loader.py` | Markdown 文档加载与索引 |

### ⑦ `src/memory/` — 记忆系统

| 文件 | 职责 |
|------|------|
| `checkpointer.py` | `AsyncPostgresSaver` + `MemorySaver` 工厂（自动创建 PG 库） |
| `context_builder.py` | 上下文裁剪（热/温/冷三层） |
| `history_store.py` | 内存环形缓冲区（500 条）查询历史 |
| `long_term_store.py` | 长期记忆（ChromaDB + PG 双写） |
| `session_archive.py` | 会话归档 |

### ⑧~⑩ — 工具/安全/MCP

| 模块 | 职责 |
|------|------|
| `tools/analyzer.py` | 描述性统计/趋势/Z-score 异常/集中度/Pearson 相关 |
| `security/data_masker.py` | 数据脱敏 + 频率限制 + 审计日志 |
| `mcp/client_manager.py` | MCP Client 连接管理 |
| `mcp/server.py` | MCP Server（暴露 4 个工具） |

## Skills 系统

`src/skill_manager.py` — 多目录扫描 + 缓存注入 + 手动刷新 + 内置保护。内置 4 个 Skill：
- `data-quality-check` — 空值/重复/异常检测
- `custom-report` — Jinja2 模板报告
- `feature-dev` — 开发流程指南
- `systematic-debugging` — 系统调试协议

## 前端

`frontend/` — React 18 + TypeScript 5 + Ant Design 5 + ECharts。6 页面：对话 / 数据源 / 表结构 / 历史 / Skills / 知识库。

## 快速上手

1. `src/main.py` → `src/api/routes.py` — API 入口
2. `src/graph/workflow.py` — 流水线组装
3. `src/graph/state.py` — 状态定义
4. `src/graph/nodes/generate_sql.py` — 核心 LLM 调用
5. `spec/README.md` — 技术规格索引
6. `features/README.md` — 功能清单索引

## 关键设计模式

- **重试循环**：SQL 语法错误/执行瞬态错误 → generate_sql 重试（最多 3 次）
- **安全阻断**：DDL/DML 在 layer3 被拦截，不进入数据库
- **双重持久化**：`conversation_history`(dict) + `messages`(add_messages) 保证会话跨请求恢复
- **单一路径工作流**：执行路由统一走条件边，避免并行分支状态丢失
- **内置保护**：系统 Skill/知识条目禁止删除（前后端双重校验）
- **优雅降级**：LLM 不可用 → 模板回退；PG 不可用 → MemorySaver；驱动缺失 → 跳过该数据源
