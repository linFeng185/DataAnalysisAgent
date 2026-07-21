# DataAnalysisAgent

基于 LangGraph 的 LLM 数据分析智能体。用自然语言提问，自动完成 **SQL 生成 → 安全校验 → 执行查询 → 数据洞察 → 图表渲染** 全流程。

## 特性

- **自然语言查数** — "本月各产品 GMV 排名？" → 自动 SQL → 执行 → 分析 → 图表
- **多轮对话** — 上下文追问，PostgreSQL 持久化会话状态
- **多数据源** — ClickHouse / MySQL / PostgreSQL / SQLite / Oracle / MSSQL（内置 SQLite 演示库，零配置体验）
- **流式 SSE** — 13 种事件实时推送推理链、Token、8 个节点进度
- **安全校验** — sqlglot 语法校验 + DDL/DML/危险函数自动阻断
- **Skills 系统** — 可扩展技能，关键词/意图/表名三重匹配自动激活
- **知识库** — 上传 PDF/Word/TXT/MD，智能分块 + ChromaDB 向量索引
- **Web UI** — React + Ant Design、6 页面：对话 / 数据源 / 表结构 / 历史 / Skills / 知识库

## 快速开始

### 环境要求

- Python 3.12+
- PostgreSQL 16+（可选，会话持久化）
- Node.js 18+（前端开发）

### 1. 克隆项目

```bash
git clone https://github.com/linFeng185/DataAnalysisAgent.git
cd DataAnalysisAgent
```

### 2. 安装 Python 依赖

```bash
python -m venv .venv
source .venv/bin/activate   # Linux/Mac
# .venv\Scripts\activate    # Windows

pip install -r requirements.txt
```

核心依赖：`fastapi` `langgraph` `langchain` `sqlalchemy` `chromadb` `sqlglot` `pyyaml` `python-docx` `PyPDF2` `jinja2`。

### 3. 配置

```bash
cp .env.example .env
```

最少配置（用 DeepSeek API 即可启动）：

```env
OPENAI_API_KEY=sk-your-deepseek-api-key
OPENAI_BASE_URL=https://api.deepseek.com
LLM_MODEL=deepseek-v4-pro
```

### 4. 启动后端

```bash
python -m src.main
```

输出 `Application startup complete.` 表示成功。访问：
- 后端：http://localhost:8000
- API 文档：http://localhost:8000/docs
- 健康检查：http://localhost:8000/api/v1/health

### 5. 启动前端（可选）

```bash
cd frontend
npm install
npm run dev
```

前端运行在 http://localhost:5173 ，Vite 代理将 `/api` 请求转发到后端 8000 端口。

### 6. 首次查询

打开前端 → 选择 `demo` 数据源 → 输入"本月各产品的销售额排名是怎样的？" → 发送。

系统会自动生成 SQL、执行查询、分析结果并展示图表。

## 配置项全表

### LLM

| 变量 | 说明 | 默认值 |
|------|------|--------|
| `LLM_PROVIDER` | 供应商（openai/anthropic） | openai |
| `LLM_MODEL` | 模型名称 | deepseek-v4-pro |
| `OPENAI_API_KEY` | API Key | — |
| `OPENAI_BASE_URL` | API 地址 | https://api.deepseek.com |
| `LLM_TEMPERATURE` | 生成温度 | 0.0 |
| `LLM_MAX_TOKENS` | 最大 Token 数 | 4096 |
| `LLM_TIMEOUT` | 超时（秒） | 60 |
| `CHEAP_LLM_MODEL` | 摘要/低成本任务模型 | deepseek-v4-pro |

### 嵌入模型

| 变量 | 说明 | 默认值 |
|------|------|--------|
| `EMBEDDING_MODEL_PATH` | 本地 `all-MiniLM-L6-v2` 路径 | 留空自动从 HuggingFace 下载（~80MB）|
| `CHROMA_PERSIST_DIR` | ChromaDB 数据目录 | ./chroma_data |
| `CHROMA_COLLECTION_NAME` | ChromaDB collection 名 | data_agent_knowledge |

### 会话持久化

| 变量 | 说明 | 默认值 |
|------|------|--------|
| `DATABASE_URL` | PG 连接串（`postgresql+asyncpg://`）| —（留空用内存） |

Docker 快速启动 PG：

```bash
docker run -d --name postgres -p 5432:5432 \
  -e POSTGRES_PASSWORD=your_password \
  postgres:16-alpine

# 创建数据库
docker exec -it postgres psql -U postgres -c "CREATE DATABASE data_agent;"
```

### Skills

| 变量 | 说明 | 默认值 |
|------|------|--------|
| `SKILLS_DIR` | 内置 Skills 目录 | skills |
| `EXTRA_SKILLS_DIRS` | 额外目录（分号分隔） | — |

### 数据源密码

| 变量 | 说明 |
|------|------|
| `MYSQL_PASSWORD` | MySQL 密码 |
| `PG_PASSWORD` | PostgreSQL 密码 |
| `ORACLE_PASSWORD` | Oracle 密码 |
| `MSSQL_PASSWORD` | MSSQL 密码 |

### 其他

| 变量 | 说明 | 默认值 |
|------|------|--------|
| `CREDENTIAL_ENCRYPTION_KEY` | Fernet 密钥（base64）| 开发默认 |
| `MAX_QUERIES_PER_HOUR` | 每小时最大查询数 | 100 |
| `MAX_EXECUTION_TIME` | SQL 执行超时（秒） | 30 |
| `ENV` | 运行环境（dev 开启 /docs）| dev |
| `LOG_LEVEL` | 日志级别 | INFO |

### 可选数据库驱动

```bash
# MySQL 数据源需要
pip install aiomysql>=0.2.0

# PostgreSQL 数据源需要
pip install asyncpg>=0.30.0

# ClickHouse 数据源需要
pip install clickhouse-connect>=0.8.0
```

## API 概览

### 对话
| 方法 | 路径 | 说明 |
|------|------|------|
| POST | `/api/v1/chat` | 主端点（`stream=true` 返回 SSE） |

### Skills
| 方法 | 路径 | 说明 |
|------|------|------|
| GET | `/api/v1/skills` | 列出所有 Skill |
| POST | `/api/v1/skills/upload` | 上传 SKILL.md |
| POST | `/api/v1/skills/refresh` | 重新扫描 |
| PUT | `/api/v1/skills/{n}/toggle` | 启用/禁用 |
| DELETE | `/api/v1/skills/{n}` | 删除（内置禁止） |

### 知识库
| 方法 | 路径 | 说明 |
|------|------|------|
| GET | `/api/v1/knowledge` | 列出条目（?category=&search=） |
| GET | `/api/v1/knowledge/docs` | 列出已索引文档 |
| POST | `/api/v1/knowledge/docs/upload` | 上传文档（?strategy=&chunk_size=） |
| GET | `/api/v1/knowledge/upload/status` | 异步任务进度 |
| GET | `/api/v1/knowledge/docs/{n}/content` | 文档内容（PDF/Word/TXT） |
| GET | `/api/v1/knowledge/docs/{n}/raw` | 原始文件（PDF iframe） |
| DELETE | `/api/v1/knowledge/{id}` | 删除条目 |
| DELETE | `/api/v1/knowledge/docs/{n}` | 删除文档 |

### 表结构 / 数据源 / 历史 / 健康
| 方法 | 路径 | 说明 |
|------|------|------|
| GET | `/api/v1/schema/tables` | 表列表（?datasource=&search=） |
| GET | `/api/v1/schema/tables/{n}` | 表详情 |
| POST | `/api/v1/schema/refresh` | 刷新 Schema |
| GET/POST/DELETE | `/api/v1/datasources` | 数据源 CRUD |
| GET | `/api/v1/history` | 查询历史（?datasource=&search=） |
| GET | `/api/v1/health` | 健康检查 |

## 项目结构

```
DataAnalysisAgent/
├── src/
│   ├── api/            # FastAPI 路由（22 端点）+ SSE 流式
│   ├── config.py       # pydantic-settings 配置管理
│   ├── connectors/     # 6 种数据库连接器
│   ├── datasource/     # 数据源注册/发现/Schema 提取/凭证加密
│   ├── graph/          # LangGraph 10 节点 DAG + 5 条件路由
│   │   └── nodes/      # 10 个执行节点
│   ├── knowledge/      # ChromaDB Schema 缓存 + 文档解析（PDF/Word/TXT）+ 上传管理
│   ├── llm/            # LLM 工厂 + 模型适配器
│   │   └── adapters/   # DeepSeek / OpenAI / Anthropic 适配器
│   ├── mcp/            # MCP 客户端管理 + Server 暴露
│   ├── memory/         # PostgresSaver/MemorySaver + 上下文裁剪 + 历史存储
│   ├── security/       # 数据脱敏 + 频率限制 + 审计日志
│   ├── tools/          # 统计分析引擎（趋势/异常/集中度/相关）
│   └── skill_manager.py
├── frontend/           # React 18 + TypeScript 5 + Ant Design 5 + ECharts
├── skills/             # 内置 Skills（data-quality-check / custom-report 等）
├── spec/               # 技术规格（15 个章节）
├── features/           # 功能清单（19 个模块 + 附录）
├── tests/              # 测试
├── docs/metrics/       # 业务指标文档（GMV 等）
├── config/             # 外部配置（MCP、数据源）
└── scripts/            # 辅助脚本
```

## 架构

```
POST /api/v1/chat
  │
  ├─ classify_intent      意图识别 + Skill 激活
  ├─ retrieve_schema      表结构检索 + 知识库上下文
  ├─ generate_sql         LLM 生成 SQL（对话历史 + 重试）
  ├─ layer3_validate      安全校验（DDL/DML 阻断）
  ├─ layer4_explain       EXPLAIN 预执行
  ├─ execute_sql          数据库执行（限流/审计）
  ├─ analyze_result       统计分析 + LLM 洞察
  ├─ generate_chart       ECharts 图表配置
  └─ build_response       响应组装 + 对话持久化
```

## 常见问题

**启动时 ChromaDB 模型下载慢？**

设置 `EMBEDDING_MODEL_PATH` 指向本地已下载的 `all-MiniLM-L6-v2` 目录。

**PostgresSaver 不可用，降级到 MemorySaver？**

检查 `DATABASE_URL` 是否正确，PG 是否运行，数据库 `data_agent` 是否存在。程序会自动创建，需确保 PG 用户有 CREATE DATABASE 权限。

**数据源执行失败 / 返回空？**

默认使用 `demo` 数据源（内存 SQLite，含 15 条订单）。如果切换到 `mysql_test` 等外部数据源，需确认本地能连接到对应数据库。

**前端页面空白？**

确保后端先启动（8000 端口），且 Vite 代理配置正确（`vite.config.ts` 中 `/api` → `http://localhost:8000`）。

**知识库上传 PDF/Word 显示不全？**

纯文本型 PDF 可正常提取。扫描件（图片型 PDF）的文本提取受限，但在详情弹窗中可通过 iframe 查看原始 PDF。

## License

MIT
