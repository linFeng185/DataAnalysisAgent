# 1. 项目基础设施

## 1. 项目基础设施 `[P0:10 P1:4]`

### 1.1 项目骨架

| # | 功能 | 文件 | 描述 | 状态 |
|---|------|------|------|------|
| 1.1.1 | Poetry 项目初始化 | `pyproject.toml` | 创建 Poetry 管理的 Python 项目，定义依赖 | 单测完成 | P0 |
| 1.1.2 | FastAPI 应用入口 | `src/main.py` | FastAPI 实例化、生命周期管理、路由挂载 | 单测完成 | P0 |
| 1.1.3 | 配置管理 | `src/config.py` | 基于 pydantic-settings 的 Settings 类，从 .env / 环境变量 / YAML 加载配置 | 单测完成 | P0 |
| 1.1.4 | Docker Compose 本地依赖 | `docker-compose.yml`（Git 忽略） | 开发者可选维护本地数据库/中间件；生产模板见 17.4.2 | 开发完成 | P0 |
| 1.1.5 | requirements.txt | `requirements.txt` | 生产环境 pip 依赖固定版本 | 单测完成 | P0 |
| 1.1.6 | 日志配置 | `src/logging_config.py` | structlog 结构化日志，支持 JSON/Console 双格式，区分开发/生产 | 单测完成 | P0 |
| 1.1.7 | 异常体系 | `src/exceptions.py` | DataSourceNotFoundError、SQLValidationError、ExecutionError、RateLimitError 等自定义异常 | 单测完成 | P0 |
| 1.1.8 | Ruff 静态检查 | `pyproject.toml`、`.github/workflows/test.yml` | Python 3.14.0 规则、导入与高风险错误检查，本地和 CI 使用同一配置 | 单测完成 | P1 |

### 1.2 配置体系

| # | 功能 | 文件 | 描述 | 状态 |
|---|------|------|------|------|
| 1.2.1 | Settings 类 | `src/config.py` | LLM API Key / DB 连接串 / Redis URL / ChromaDB 路径 / 限流参数 / 日志级别 | 单测完成 | P0 |
| 1.2.2 | .env 模板 | `.env.example` | 所有可配置环境变量的模板文件 | 单测完成 | P0 |
| 1.2.3 | MCP Server 注册表 | `config/app.yaml` | 在统一配置中声明外部 MCP Server | 单测完成 | P0 |
| 1.2.4 | 数据源配置文件 | `config/datasources.yaml` | 外挂模式数据源声明 (dialect/host/port/database/凭证引用) | 开发完成 | P0 |

### 1.3 异常与错误处理

| # | 功能 | 文件 | 描述 | 状态 |
|---|------|------|------|------|
| 1.3.1 | DataSourceNotFoundError | `src/exceptions.py` | 数据源未找到异常 | 单测完成 | P0 |
| 1.3.2 | SQLValidationError | `src/exceptions.py` | SQL 校验失败异常，携带 errors/warnings 列表 | 单测完成 | P0 |
| 1.3.3 | SQLSecurityError | `src/exceptions.py` | SQL 安全拦截异常（含拦截原因和违规操作） | 单测完成 | P0 |
| 1.3.4 | ExecutionError | `src/exceptions.py` | SQL 执行失败异常，携带原始错误信息和 retry_count | 单测完成 | P0 |
| 1.3.5 | RateLimitError | `src/exceptions.py` | 请求频率超限异常 | 单测完成 | P0 |
| 1.3.6 | KnowledgeNotFoundError | `src/exceptions.py` | 知识库未找到相关知识异常 | 单测完成 | P0 |
| 1.3.7 | 全局异常处理中间件 | `src/api/middleware.py` | 7 种异常 → HTTP 响应映射 | 单测完成 | P0 |

### 模块收尾

模块功能点共 19 项，已完成 19 项，待开发 0 项。当前模块无待开发项。

---
