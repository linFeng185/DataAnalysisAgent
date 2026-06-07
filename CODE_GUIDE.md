# dataAnalysisAgent — 代码导航指南

## 项目概述

这是一个 **自然语言数据分析智能体**。用户用中文描述分析需求（如「过去7天的GMV趋势」），系统自动完成：意图识别 → 表结构检索 → SQL 生成 → 安全校验 → 数据库执行 → 统计分析 → 图表生成 → 响应组装。

技术栈：**FastAPI**（Web 层）+ **LangGraph**（工作流编排）+ **LangChain**（LLM 调用封装）+ **sqlglot**（SQL 方言解析）+ **SQLAlchemy**（数据库连接）。

---

## 目录结构与业务含义

```
D:\work\dataAnalysisAgent\
├── src/                        # 所有业务代码
│   ├── api/                    # ① Web 接口层 — HTTP 请求/响应处理
│   ├── graph/                  # ② 分析流水线 — LangGraph 工作流（核心链路）
│   │   └── nodes/              #     流水线的 9 个执行节点
│   ├── llm/                    # ③ LLM 调用层 — 大模型客户端 + 模型适配
│   │   └── adapters/           #     各模型（DeepSeek/OpenAI/Anthropic）适配器
│   ├── datasource/             # ④ 数据源管理层 — 数据库注册、发现、Schema 提取
│   │   └── providers/          #     数据源提供者（嵌入式 / 外部配置）
│   ├── connectors/             # ⑤ 数据库连接器 — 各方言的 SQL 执行
│   ├── tools/                  # ⑥ 分析工具 — 纯统计计算（趋势/异常/集中度/相关）
│   ├── security/               # ⑦ 安全模块 — 当前仅包占位（未来 SQL 审计）
│   ├── memory/                 # ⑧ 记忆系统 — 当前仅包占位（未来长期记忆）
│   ├── knowledge/              # ⑨ 知识库 — 当前仅包占位（未来业务规则检索）
│   └── mcp/                    # ⑩ MCP 集成 — 当前仅包占位（未来文件分析等）
├── tests/                      # 测试（与 src/ 一一对应）
├── config/                     # 外部配置文件
│   └── datasources.yaml        #     外部数据源连接信息
├── SPEC.md                     # 技术规格说明书
├── FEATURES.md                 # 功能清单与状态跟踪
├── CLAUDE.md                   # 项目开发指南（给 AI 开发助手看）
├── CODE_GUIDE.md               # 本文档 — 给新手开发者看的代码导航
├── pyproject.toml              # Python 项目配置
└── docker-compose.yml          # Docker 编排
```

---

## 核心数据流

一次完整的用户请求处理过程：

```
用户 HTTP POST /api/v1/chat {"query": "...", "stream": true}
         │
    ┌────▼──────────────────────────────────────────────┐
    │  src/api/routes.py   chat()                       │
    │  判断 stream 参数：false→返回 JSON                 │
    │                  true →返回 SSE 事件流              │
    └────┬──────────────────────────────────────────────┘
         │
    ┌────▼──────────────────────────────────────────────┐
    │  src/graph/workflow.py  编译后的 StateGraph       │
    │  按顺序执行 9 个节点，每个节点读/写 AnalysisState   │
    └────┬──────────────────────────────────────────────┘
         │
    ┌────▼──────────────────────────────────────────────┐
    │  ① classify_intent   意图分类（关键词匹配）       │
    │     src/graph/nodes/classify_intent.py             │
    └────┬──────────────────────────────────────────────┘
         │
    ┌────▼──────────────────────────────────────────────┐
    │  ② retrieve_schema   从 Registry 检索表结构        │
    │     src/graph/nodes/retrieve_schema.py              │
    └────┬──────────────────────────────────────────────┘
         │
    ┌────▼──────────────────────────────────────────────┐
    │  ③ generate_sql      调用 LLM 生成 SQL             │
    │     src/graph/nodes/generate_sql.py                │
    │     通过 src/llm/client.py → ChatOpenAI (DeepSeek) │
    └────┬──────────────────────────────────────────────┘
         │
    ┌────▼──────────────────────────────────────────────┐
    │  ④ layer3_validate   SQL 安全校验 + 语法解析       │
    │     src/graph/nodes/layer3_validate.py             │
    │     安全拦截 → 直接返回错误                         │
    │     语法错误 → 回到③重试（最多3次）                  │
    └────┬──────────────────────────────────────────────┘
         │
    ┌────▼──────────────────────────────────────────────┐
    │  ⑤ layer4_explain    EXPLAIN 模拟执行（Phase 2）   │
    │     src/graph/nodes/layer4_explain.py              │
    │     当前为桩：直接通过                              │
    └────┬──────────────────────────────────────────────┘
         │
    ┌────▼──────────────────────────────────────────────┐
    │  ⑥ execute_sql       在目标数据库执行 SQL          │
    │     src/graph/nodes/execute_sql.py                 │
    │     通过 src/connectors/ → SQLAlchemy → DB         │
    │     执行失败（瞬态）→ 回到③重试                      │
    └────┬──────────────────────────────────────────────┘
         │
    ┌────▼──────────────────────────────────────────────┐
    │  ⑦ analyze_result    统计分析 + LLM 洞察          │
    │     src/graph/nodes/analyze_result.py              │
    │     统计：src/tools/analyzer.py（纯 Python）        │
    │     洞察：LLM 生成（可用时）                         │
    └────┬──────────────────────────────────────────────┘
         │
    ┌────▼──────────────────────────────────────────────┐
    │  ⑧ generate_chart    生成 ECharts 配置（Phase 2）  │
    │     src/graph/nodes/generate_chart.py              │
    │     当前为桩：返回空 chart 配置                      │
    └────┬──────────────────────────────────────────────┘
         │
    ┌────▼──────────────────────────────────────────────┐
    │  ⑨ build_response    组装最终 JSON 响应            │
    │     src/graph/nodes/build_response.py              │
    │     success=True → 完整响应（SQL+数据+分析+图表）   │
    │     success=False → 错误响应                        │
    └───────────────────────────────────────────────────┘
```

---

## 各目录详细说明

### ① `src/api/` — Web 接口层

| 文件 | 职责 |
|------|------|
| `routes.py` | 定义 13 个 REST API 端点（`POST /chat`、`GET /health` 等） |
| `schemas.py` | Pydantic 请求/响应模型，用于参数校验和响应序列化 |
| `streaming.py` | SSE（Server-Sent Events）流式输出实现，实时推送执行进度和 LLM token |
| `middleware.py` | 异常处理器注册，将自定义异常映射为 HTTP 状态码 + JSON 错误体 |

**关键概念**：`POST /chat` 端点通过 `stream: true` 参数支持两种模式：
- `stream: false`（默认）→ 等待完整结果，返回 JSON
- `stream: true` → SSE 流式推送，前端实时看到：节点进度 → LLM token → SQL → 验证 → 结果

### ② `src/graph/` — 核心分析流水线

这是项目的**大脑**。LangGraph 的 StateGraph 将 9 个节点按 DAG 组装，通过 `AnalysisState`（一个 TypedDict）在节点间传递数据。

| 文件 | 职责 |
|------|------|
| `state.py` | 定义 `AnalysisState`，包含 30+ 个字段，贯穿整个流水线 |
| `workflow.py` | 组装 StateGraph、定义 5 个条件路由函数、编译成可执行图 |
| `nodes/classify_intent.py` | 关键词匹配 → 7 种意图分类 |
| `nodes/retrieve_schema.py` | 从 DataSourceRegistry 获取表结构 |
| `nodes/generate_sql.py` | 调用 LLM 生成 SQL（含模板回退） |
| `nodes/layer3_validate.py` | SQL 安全检查（防注入）+ sqlglot 语法解析 |
| `nodes/layer4_explain.py` | EXPLAIN 预执行（Phase 2） |
| `nodes/execute_sql.py` | 在目标数据库执行 SQL |
| `nodes/analyze_result.py` | 统计计算 + LLM 数据洞察 |
| `nodes/generate_chart.py` | ECharts 图表配置（Phase 2） |
| `nodes/build_response.py` | 组装最终的 API 响应 |

### ③ `src/llm/` — LLM 调用层

封装所有大模型交互，提供统一的调用接口。

| 文件 | 职责 |
|------|------|
| `client.py` | LLM 工厂：`get_llm()` 返回 LangChain ChatOpenAI 实例，用适配器注入模型参数 |
| `adapters/base.py` | 模型适配器基类：定义 `SupportedFeatures`、响应解析、流式块解析 |
| `adapters/deepseek.py` | DeepSeek 适配器：思考模式（`reasoning_effort`+`extra_body`）、reasoning_content 提取 |
| `adapters/openai_adapter.py` | 标准 OpenAI 适配器 |
| `adapters/registry.py` | 模型名 → 适配器的自动匹配注册表 |
| `prompts.py` | Prompt 模板：SQL 生成提示词、分析提示词、方言速查表 |

### ④ `src/datasource/` — 数据源管理层

管理「这个智能体可以查询哪些数据库」。

| 文件 | 职责 |
|------|------|
| `config.py` | `DataSourceConfig`：连接参数（host/port/db/user/pass/dialect）的统一模型 |
| `registry.py` | `DataSourceRegistry` 单例：统一入口，按名称解析数据源（创建引擎+提取 Schema） |
| `schema_snapshot.py` | `SchemaSnapshot`/`TableSchema`/`ColumnInfo`：表结构的数据模型 |
| `credential_manager.py` | Fernet 加密/解密密码 + `${ENV_VAR}` 占位符解析 |
| `introspection.py` | 多方言数据库内省（查系统表获取列、外键、行数） |
| `setup.py` | 创建内存 SQLite 演示数据源（orders + users 表），零配置体验 |
| `providers/base.py` | `DataSourceProvider` 抽象基类 |
| `providers/embedded.py` | 嵌入式提供者：自动发现 Django/SQLAlchemy ORM 模型 |
| `providers/external.py` | 外部提供者：从 YAML/API 注册远程数据源 |

### ⑤ `src/connectors/` — 数据库连接器

封装各数据库方言的 SQL 执行细节（连接 URL、超时设置、EXPLAIN 语法）。

| 文件 | 职责 |
|------|------|
| `base.py` | `ConnectorBase` 抽象基类 + `create_connector()` 工厂函数 |
| `clickhouse.py` | ClickHouse（`clickhouse+asynch`） |
| `mysql.py` | MySQL（`mysql+aiomysql`） |
| `postgres.py` | PostgreSQL（`postgresql+asyncpg`） |
| `sqlite.py` | SQLite（`aiosqlite`，演示/开发用） |

### ⑥ `src/tools/` — 分析工具

纯 Python 统计分析引擎，不依赖 LLM。

| 文件 | 职责 |
|------|------|
| `analyzer.py` | 描述性统计、趋势检测、Z-score/IQR 异常检测、集中度分析、Pearson 相关 |

### ⑦~⑩ 待开发模块

| 目录 | 未来职责 |
|------|---------|
| `src/security/` | SQL 审计、敏感数据脱敏、访问控制 |
| `src/memory/` | 用户偏好记忆、历史查询关联、长期上下文 |
| `src/knowledge/` | 业务知识库（指标定义、业务规则检索） |
| `src/mcp/` | MCP 协议集成（文件上传分析、外部工具调用） |

---

## 快速上手路径

1. **看入口**：`src/main.py` → `src/api/routes.py` — 了解 API 端点
2. **看核心流水线**：`src/graph/workflow.py` — 理解节点如何组装
3. **看关键节点**：`src/graph/nodes/generate_sql.py` — 理解 LLM 如何生成 SQL
4. **看状态定义**：`src/graph/state.py` — 理解数据如何在节点间流动
5. **看适配器**：`src/llm/adapters/deepseek.py` — 理解如何适配不同模型
6. **看测试**：`tests/` 目录 — 每个模块的测试用例是最好的使用示例

---

## 关键设计模式

- **重试循环**：SQL 语法错误、EXPLAIN 失败、执行瞬态错误都会回到 `generate_sql` 重试（最多 3 次）
- **安全阻断**：DDL/DML 操作（INSERT/DROP/DELETE 等）在 layer3 被直接拦截，不进入数据库
- **优雅降级**：LLM 不可用时，SQL 生成回退到模板，分析回退到纯规则
- **方言抽象**：`introspection.py` 和 `connectors/base.py` 通过字典映射实现多方言支持，新增方言只需加字典项
