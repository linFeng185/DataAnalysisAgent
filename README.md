# DataAnalysisAgent

基于 LangGraph 的 LLM 数据分析智能体。用自然语言提问，自动完成 **意图识别 → Schema 检索 → SQL 生成 → 安全校验 → 查询执行 → 数据洞察 → 图表渲染** 全流程。项目同时提供 FastAPI、SSE 流式协议和 React 管理界面，可从内置 SQLite 演示库起步，再切换到企业数据库与多租户部署。

## 特性

- **自然语言查数** — "本月各产品 GMV 排名？" → 自动 SQL → 执行 → 分析 → 图表
- **多轮对话** — 上下文追问，PostgreSQL 持久化会话状态
- **多数据源** — ClickHouse / MySQL / PostgreSQL / SQLite / Oracle / MSSQL（内置 SQLite 演示库，零配置体验）
- **流式 SSE** — 13 种事件实时推送推理链、Token、8 个节点进度
- **安全校验** — sqlglot 只读 AST 白名单、列权限/行过滤、结果脱敏、SSRF 防护和查询审计
- **Skills 系统** — 可扩展技能，关键词/意图/表名三重匹配自动激活
- **知识库** — 上传 PDF/Word/TXT/MD，智能分块 + ChromaDB 向量索引
- **共享缓存** — 本地 JSON 或 Redis 后端，支持连接级 Schema/枚举缓存及 TTL
- **依赖与租户隔离** — `AppContext` 管理应用资源，`TenantPolicy` 集中执行认证和租户边界
- **Web UI** — React + Ant Design、6 页面：对话 / 数据源 / 表结构 / 历史 / Skills / 知识库

## 快速开始

### 环境要求

- Python 3.12+
- PostgreSQL 16+（可选，用于会话、检查点和审计持久化）
- Redis 7+（可选，用于多实例共享的数据源内容缓存）
- Node.js 18+（前端开发）
- Docker Compose v2（可选，用于启动本地依赖）

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

也可以按开发依赖安装，使 pytest 和 coverage 可用：

```bash
pip install -e ".[dev]"
```

核心依赖包括 `fastapi`、`langgraph`、`langchain`、`sqlalchemy`、`chromadb`、`sqlglot`、`redis` 和 `structlog`。

### 3. 配置

```bash
cp .env.example .env
```

`.env.example` 默认使用 `ENV=dev`，适合本机演示。最少配置（用 DeepSeek API 即可启动）：

```env
OPENAI_API_KEY=sk-your-deepseek-api-key
OPENAI_BASE_URL=https://api.deepseek.com
LLM_MODEL=deepseek-v4-pro
```

开发模式未配置 PostgreSQL 时会使用内存检查点；未配置持久凭证密钥时会生成进程级临时密钥。临时密钥在进程重启后变化，只适合演示环境。

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

## Docker Compose 本地依赖

仓库根目录的 `docker-compose.yml` 编排 MySQL、PostgreSQL、Oracle、SQL Server、ClickHouse、Redis 和 Milvus。应用镜像尚未纳入 Compose，后端与前端仍按上面的命令在宿主机运行。

首次启动前创建 `.env` 并修改所有 `change-me` 密码：

```bash
cp .env.example .env
docker compose --env-file .env config
```

日常开发通常只需要 PostgreSQL 和 Redis：

```bash
docker compose --env-file .env up -d postgres redis
docker compose ps
docker compose exec redis sh -c 'redis-cli --no-auth-warning -a "$REDIS_PASSWORD" ping'
```

预期 Redis 返回 `PONG`。该服务使用 `redis:7.4-alpine`，仅将 `6379` 绑定到宿主机 `127.0.0.1`，启用密码认证和 AOF 持久化，数据保存在 `redis_data` volume。停止和恢复命令：

```bash
docker compose stop postgres redis
docker compose start postgres redis
```

`docker compose down` 只移除容器和网络；只有显式执行 `docker compose down -v` 才会删除 `redis_data`。该操作会清空 Redis 数据，执行前应确认不需要保留缓存。

启动全部数据库和 Milvus 会占用较多内存，并要求 `.env` 中的所有服务密码均已设置：

```bash
docker compose --env-file .env up -d
```

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
| `RUN_MIGRATIONS_ON_STARTUP` | 启动时运行版本化迁移 | true |
| `DATABASE_READONLY_URL` | 执行用户 SQL 的只读连接；生产必填 | — |

Docker 快速启动 PG：

```bash
docker run -d --name postgres -p 5432:5432 \
  -e POSTGRES_PASSWORD=your_password \
  postgres:16-alpine

# 创建数据库
docker exec -it postgres psql -U postgres -c "CREATE DATABASE data_agent;"
```

### Redis 与共享缓存

| 变量 | 说明 | 默认值 |
|------|------|--------|
| `REDIS_URL` | 应用连接 Redis 的 URL，可包含用户名、密码和 DB 编号 | redis://localhost:6379/0 |
| `DATASOURCE_CACHE_BACKEND` | `local` 使用文件缓存，`redis` 使用共享缓存 | local |
| `DATASOURCE_CACHE_DIR` | 本地缓存目录 | ./data/cache/datasource |
| `DATASOURCE_CACHE_TTL_SECONDS` | 数据源内容缓存 TTL（秒） | 604800 |
| `DATASOURCE_CACHE_REDIS_PREFIX` | Redis key 命名空间 | data-agent:datasource-cache |
| `REDIS_PASSWORD` | Compose Redis 密码，不直接被应用读取 | — |

使用 Compose Redis 时，`REDIS_URL` 中的密码必须与 `REDIS_PASSWORD` 一致：

```env
REDIS_PASSWORD=replace-with-a-strong-password
REDIS_URL=redis://:replace-with-a-strong-password@localhost:6379/0
DATASOURCE_CACHE_BACKEND=redis
```

当前 Redis 后端用于数据源内容共享缓存；API 登录、注册和查询限流仍是进程内实现，多 worker 生产部署不能把 Redis 服务存在等同于已启用分布式限流。

### Skills

| 变量 | 说明 | 默认值 |
|------|------|--------|
| `SKILLS_DIR` | 内置 Skills 目录 | skills |
| `EXTRA_SKILLS_DIRS` | 额外目录（分号分隔） | — |

### 认证与多租户

| 变量 | 说明 | 默认值 |
|------|------|--------|
| `MULTI_TENANT` | 开启 JWT 认证和租户隔离 | false |
| `JWT_SECRET` | JWT 签名密钥，生产至少 32 字符 | — |
| `ADMIN_API_KEY` | 平台管理操作密钥，生产至少 32 字符 | — |
| `CORS_ALLOWED_ORIGINS` | 可信前端 origin，多个值用逗号分隔 | 空 |

`dev/test + MULTI_TENANT=false` 允许匿名演示。`prod` 必须启用多租户，并提供 JWT、管理密钥、凭证加密密钥和只读数据库连接，否则应用拒绝启动。

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
| `CREDENTIAL_ENCRYPTION_KEY` | 凭证加密主密钥，生产至少 32 字符 | 无；生产必填 |
| `DATASOURCE_HOST_ALLOWLIST` | 允许访问私网的数据库主机、IP 或 CIDR | 空（仅允许公网地址） |
| `CORS_ALLOWED_ORIGINS` | 允许跨域的前端 origin，逗号分隔 | 空（拒绝跨域） |
| `SECURITY_HSTS_SECONDS` | 生产 HTTPS 的 HSTS 时长 | 31536000 |
| `MAX_QUERIES_PER_HOUR` | 每小时最大查询数 | 100 |
| `MAX_EXECUTION_TIME` | SQL 执行超时（秒） | 30 |
| `ENV` | 运行环境（dev 开启 /docs）| prod |
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
│   ├── api/            # FastAPI 路由、中间件、SSE 和后台任务
│   ├── app_context.py  # 应用级依赖容器与资源生命周期
│   ├── config.py       # pydantic-settings 配置管理
│   ├── connectors/     # 6 种数据库连接器
│   ├── datasource/     # 数据源注册/发现/Schema 提取/凭证加密
│   ├── graph/          # LangGraph 工作流、状态与条件路由
│   │   └── nodes/      # 意图、Schema、SQL、执行、分析和响应节点
│   ├── knowledge/      # ChromaDB/Redis 缓存、文档解析和知识治理
│   ├── llm/            # LLM 工厂 + 模型适配器
│   │   └── adapters/   # DeepSeek / OpenAI / Anthropic 适配器
│   ├── mcp_client/     # MCP 客户端管理 + Server 暴露
│   ├── memory/         # PostgresSaver/MemorySaver + 上下文裁剪 + 历史存储
│   ├── security/       # 租户策略、网络策略、权限校验和数据脱敏
│   ├── tools/          # 统计分析引擎（趋势/异常/集中度/相关）
│   └── skill_manager.py
├── frontend/           # React 18 + TypeScript 5 + Ant Design 5 + ECharts
├── skills/             # 内置 Skills（data-quality-check / custom-report 等）
├── spec/               # 模块化技术规格与整改设计
├── features/           # 模块化功能清单和完成状态
├── tests/              # 测试
├── docs/metrics/       # 业务指标文档（GMV 等）
├── config/             # 外部配置（MCP、数据源）
├── docker-compose.yml  # 本地数据库、Redis 和 Milvus 编排
└── scripts/            # 辅助脚本
```

## 架构

```
POST /api/v1/chat
  │
  ├─ AuthMiddleware       JWT/API Key、租户身份和请求上下文
  ├─ TenantPolicy         认证门禁、数据源/知识范围和写权限
  ├─ prepare_turn         恢复会话状态并准备本轮输入
  ├─ classify_intent      意图识别、Skills 激活和路径选择
  ├─ retrieve_schema      授权 Schema、知识库和枚举值检索
  ├─ decompose_query      复杂问题拆解与依赖规划
  ├─ generate_sql         LLM 生成方言 SQL（含重试上下文）
  ├─ layer3_validate      只读 AST、列权限和行过滤校验
  ├─ layer4_explain       EXPLAIN 预执行与成本检查
  ├─ execute_sql          有界读取、超时、脱敏和审计
  ├─ analyze_result       统计分析与 LLM 洞察
  ├─ generate_chart       ECharts 图表配置
  └─ build_response       响应组装、历史和检查点持久化
```

每个 FastAPI 应用拥有独立 `AppContext`。Registry、缓存、向量存储、PostgreSQL pool、MCP 和 Skill 等资源按需初始化，并在应用关闭时按创建逆序释放。兼容的 `get_*()` 工厂会委托给当前 Context，测试可以直接注入隔离资源。

多租户逻辑集中在 `TenantPolicy`：`tenant_id=0` 表示系统范围，`tenant_id=1` 表示默认租户。业务模块不直接判断 `MULTI_TENANT`，而是通过统一策略获得身份、过滤数据源并限制知识写入范围。

## 开发与测试

日常测试不会调用远程 LLM：

```bash
# 单元测试和可用的本地模型测试
python -m pytest -m "not live_llm"

# 仅本地模型集成测试；服务未启动时默认跳过
python -m pytest -m local_llm

# 显式允许远程模型测试
RUN_LIVE_LLM_TESTS=1 python -m pytest -m live_llm
```

完整质量检查：

```bash
python -m coverage run --branch -m pytest -q -m "not live_llm"
python -m coverage report --fail-under=67
python -m compileall -q src tests

cd frontend
npm run build
```

coverage 配置位于 `pyproject.toml`，当前门禁要求 branch coverage 不低于 67%。

## 生产部署检查

生产环境至少完成以下配置后再启动：

1. 设置 `ENV=prod`、`MULTI_TENANT=true`，配置不少于 32 字符的 `JWT_SECRET` 和 `ADMIN_API_KEY`。
2. 生成独立的 `CREDENTIAL_ENCRYPTION_KEY`，不要复用 `.env.example` 中的示例值。
3. 分别配置状态库 `DATABASE_URL` 和只读查询连接 `DATABASE_READONLY_URL`，后者不得拥有 DDL/DML 权限。
4. 将 `CORS_ALLOWED_ORIGINS` 限制为实际前端域名；HTTPS 部署保留 HSTS 和其他安全响应头。
5. 私网数据源必须显式加入 `DATASOURCE_HOST_ALLOWLIST`，避免开放任意内网目标。
6. Redis 不应直接暴露公网；生产使用强密码、网络 ACL，并根据备份要求管理 AOF/快照。
7. 将日志目录挂载到持久存储。应用日志每日轮转并保留 7 天，审计合规需要更长留存时应由日志平台归档。

生产启动会执行配置门禁；认证、凭证密钥或只读数据库配置不完整时会直接失败，不会降级到匿名模式。

## 常见问题

**启动时 ChromaDB 模型下载慢？**

设置 `EMBEDDING_MODEL_PATH` 指向本地已下载的 `all-MiniLM-L6-v2` 目录。

**PostgresSaver 不可用，降级到 MemorySaver？**

检查 `DATABASE_URL` 是否正确，PG 是否运行，数据库 `data_agent` 是否存在。程序会自动创建，需确保 PG 用户有 CREATE DATABASE 权限。

**Redis 健康检查失败或应用报 `Authentication required`？**

先运行 `docker compose ps redis` 和 `docker compose logs redis`。确认 `.env` 中 `REDIS_PASSWORD` 与 `REDIS_URL` 密码一致；修改密码后需要重建容器：`docker compose up -d --force-recreate redis`。

**配置了 Redis 但缓存仍写入本地目录？**

设置 `DATASOURCE_CACHE_BACKEND=redis` 后重启应用。仅配置 `REDIS_URL` 不会自动切换后端，这是为了让单机开发保留可预测的本地回退。

**数据源执行失败 / 返回空？**

默认使用 `demo` 数据源（内存 SQLite，含 15 条订单）。如果切换到 `mysql_test` 等外部数据源，需确认本地能连接到对应数据库。

**前端页面空白？**

确保后端先启动（8000 端口），且 Vite 代理配置正确（`vite.config.ts` 中 `/api` → `http://localhost:8000`）。

**知识库上传 PDF/Word 显示不全？**

纯文本型 PDF 可正常提取。扫描件（图片型 PDF）的文本提取受限，但在详情弹窗中可通过 iframe 查看原始 PDF。

## 设计与进度文档

- `spec/README.md`：技术规格索引
- `features/README.md`：功能状态索引
- `CODE_GUIDE.md`：代码结构、数据流和扩展入口

## License

MIT
