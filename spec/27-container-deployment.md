# Linux 前后端分离容器部署

## 1. 部署目标

生产环境使用 Docker Compose 管理独立的前端、后端、PostgreSQL 和 Redis 服务。前端仅负责
React 静态资源与 `/api/` 反向代理，后端不发布宿主机端口，数据库和缓存只加入 Compose
内部网络。

```text
浏览器 / HTTPS 入口
        |
        v
frontend:8080 (Nginx + React SPA)
        | /api/*
        v
backend:8000 (FastAPI + Uvicorn, 1 worker)
        |                    |
        v                    v
postgres:5432           redis:6379
```

## 2. 镜像设计

### 2.1 后端

- `Dockerfile` 使用 `python:3.12-slim` 的 builder/runtime 多阶段构建。
- builder 将项目及可选依赖构造成 wheel，runtime 不保留编译工具链。
- 默认安装 `connectors,documents,structured`，Milvus 本地嵌入模型通过
  `INSTALL_EXTRAS=connectors,documents,structured,embeddings` 显式启用。
- runtime 使用固定 UID/GID `10001` 的 `app` 用户，Uvicorn 固定单 worker 且禁止 reload。
- `/api/v1/health` 同时作为镜像和 Compose 健康检查。

单 worker 是当前约束：登录、注册和查询限流仍含进程内状态，不能通过增加 Uvicorn worker
横向扩展。多实例部署前必须先完成分布式限流和 Chroma 写入协调。

### 2.2 前端

- `frontend/Dockerfile` 使用 Node 22 构建 React，再由非 root Nginx 1.27 提供静态文件。
- `/api/` 转发到内部 `backend:8000`，保持 Cookie 同源，不依赖生产 CORS 放行。
- SSE 路径关闭 `proxy_buffering` 和缓存，读取超时为 600 秒。
- SPA 非静态资源路径回退到 `index.html`。

## 3. 服务与启动顺序

根目录 `docker-compose.example.yml` 是生产编排模板，复制为被 Git 忽略的
`docker-compose.yml` 后使用，包含以下服务：

| 服务 | 职责 | 启动依赖 |
|------|------|----------|
| `postgres` | 状态、会话、知识文件、动态配置和迁移 | 无 |
| `redis` | 数据源共享缓存 | 无 |
| `backend-init` | 创建 bind mount 目录并设置 UID 10001 权限 | 后端镜像 |
| `backend` | FastAPI 和启动迁移 | init 完成、PG/Redis 健康 |
| `frontend` | React/Nginx 入口 | 后端健康 |

ChromaDB 当前通过 `chromadb.PersistentClient` 嵌入后端进程，不部署独立 Chroma Server；其
数据目录挂载到宿主机。Compose 网络固定为 `172.30.0.0/24`，该网段同时是后端可信代理
范围。变更网段时必须同步修改 `config/app.yaml` 的 `trusted_proxy_cidrs`。

## 4. 挂载契约

| 宿主机路径 | 容器路径 | 模式 | 内容 |
|------------|----------|------|------|
| `config/app.yaml` | `/app/config/app.yaml` | 只读 | 应用非密钥配置 |
| `config/datasources.yaml` | `/app/config/datasources.yaml` | 只读 | 固定数据源声明 |
| `data/backend` | `/app/data` | 读写 | Chroma、缓存、受管 Skills、系统知识 |
| `data/model-cache` | `/home/app/.cache` | 读写 | Chroma ONNX 模型缓存 |
| `logs/backend` | `/app/logs` | 读写 | 七天轮转应用日志 |
| `data/postgres` | `/var/lib/postgresql/data` | 读写 | PostgreSQL 数据 |
| `data/redis` | `/data` | 读写 | Redis AOF 数据 |

仓库只维护 `docker-compose.example.yml`、`config/app.example.yaml` 和
`config/datasources.example.yaml`。实际 Compose、配置、`.env`、运行数据和日志均被 Git
忽略，生产密钥由服务器部署环境维护。数据库 URL 中的特殊字符必须 URL encode；推荐使用
`openssl rand -hex` 生成只含 URL 安全字符的密码。

## 5. 网络与 TLS

- Compose 默认只在 `127.0.0.1:${FRONTEND_PORT:-8080}` 发布前端；后端、PostgreSQL、Redis
  均不发布端口。确需直接访问时再显式修改 `FRONTEND_BIND_ADDRESS`。
- Nginx 将真实客户端地址写入 `X-Forwarded-For`，后端仅信任 Compose 固定网段。
- 生产 Cookie 使用 Secure 属性，公网部署必须在宿主机 Caddy/Nginx、云负载均衡或 Ingress
  完成 HTTPS 终止，再转发到前端 8080。
- 业务数据源使用外部地址时，私网主机必须加入 `DATASOURCE_HOST_ALLOWLIST`。

## 6. 备份与升级

- PostgreSQL 使用 `pg_dump` 做逻辑备份，不能只复制运行中的数据目录。
- Redis、Chroma 和受管 Skills 备份前应停止写入或暂停后端服务，再归档对应 bind mount。
- 升级流程为拉取代码、`docker compose build --pull`、备份、`docker compose up -d`，迁移由
  后端启动阶段在 advisory lock 内执行。
- 回滚应用镜像前必须确认新迁移是否向后兼容，不允许直接删除 PostgreSQL 数据目录。
