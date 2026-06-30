# DataAnalysisAgent

基于 LangGraph 的 LLM 数据分析智能体。用自然语言提问，自动完成 **SQL 生成 → 安全校验 → 执行查询 → 数据洞察 → 图表渲染** 全流程。

## 特性

- **自然语言查数** — "本月各产品 GMV 排名？" → 自动 SQL → 执行 → 分析 → 图表
- **多轮对话** — 基于上下文追问，支持 PostgreSQL 持久化会话状态
- **多数据源** — ClickHouse / MySQL / PostgreSQL / SQLite / Oracle / MSSQL
- **流式 SSE** — 实时推送推理链、Token、8 个节点进度
- **安全校验** — sqlglot 语法校验 + DDL/DML/危险函数自动阻断
- **Skills 系统** — 可扩展的技能机制，关键词/意图/表名三重匹配自动激活
- **知识库** — 上传 PDF/Word/TXT/MD，智能分块 + ChromaDB 向量索引，检索增强生成
- **Web UI** — React + Ant Design，6 个管理页面（对话/数据源/表结构/历史/Skills/知识库）

## 快速开始

### 1. 环境要求

- Python 3.10+
- PostgreSQL 16+（可选，用于会话持久化）
- Node.js 18+（前端开发）

### 2. 安装

```bash
git clone https://github.com/<your-username>/DataAnalysisAgent.git
cd DataAnalysisAgent
python -m venv .venv
source .venv/bin/activate  # Linux/Mac
# .venv\Scripts\activate   # Windows
pip install -r requirements.txt
```

### 3. 配置

```bash
cp .env.example .env
# 编辑 .env，至少配置 OPENAI_API_KEY
```

### 4. 启动

```bash
python -m src.main
# 后端: http://localhost:8000
# API 文档: http://localhost:8000/docs
```

### 5. 前端（可选）

```bash
cd frontend
npm install
npm run dev
# 前端: http://localhost:5173
```

## 核心配置

| 变量 | 说明 | 默认值 |
|------|------|--------|
| `LLM_MODEL` | 模型名称 | deepseek-v4-pro |
| `OPENAI_API_KEY` | API Key | — |
| `OPENAI_BASE_URL` | API 地址 | https://api.deepseek.com |
| `DATABASE_URL` | PostgreSQL 会话持久化 | — |
| `SKILLS_DIR` | 内置 Skills 目录 | skills |
| `EXTRA_SKILLS_DIRS` | 额外 Skills 目录（分号分隔） | — |

完整配置见 `.env.example`。

## 项目结构

```
src/
├── api/            # FastAPI 路由 + SSE 流式
├── config.py       # 配置管理 (pydantic-settings)
├── connectors/     # 6 种数据库连接器
├── datasource/     # 数据源注册 + Schema 提取
├── graph/          # LangGraph 工作流（10 个节点）
├── knowledge/      # 知识库（Schema 缓存 + 文档解析 + 上传）
├── llm/            # LLM 客户端工厂 + 适配器
├── mcp/            # MCP 集成
├── memory/         # 会话持久化 + 上下文裁剪 + 历史存储
├── security/       # 数据脱敏 + 限流 + 审计
├── tools/          # LangChain 工具封装
└── skill_manager.py
frontend/           # React + TypeScript 前端
skills/             # 内置 Skills
spec/               # 技术规格文档（分章节）
features/           # 功能清单（按模块）
tests/              # 测试
```

## License

MIT
