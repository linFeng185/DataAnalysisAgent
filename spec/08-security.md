# 7. 安全设计

## 7. 安全设计

### 7.1 SQL 安全

- 白名单模式：默认只允许 SELECT / SHOW / DESCRIBE / EXPLAIN
- 所有 SQL 执行前经过正则校验 + AST 解析双重检查
- 使用只读数据库账号，权限最小化
- SQL 注入基础防护（LLM 输出的 SQL 已经参数化，不会拼接用户输入）

### 7.2 数据安全

- 查询结果自动脱敏（手机号、身份证号、邮箱等字段）
- 敏感表/字段可配置访问白名单
- 查询日志完整记录（谁、什么时候、查了什么）

### 7.3 用户体系与多租户

#### 7.3.1 核心概念

| 概念 | 说明 |
|------|------|
| **租户 (Tenant)** | 组织/公司，数据隔离的最高边界。多租户模式通过 `MULTI_TENANT` 环境变量控制开关 |
| **用户 (User)** | 属于某一租户。同租户内用户默认隔离，可通过权限配置共享资源 |
| **数据源权限** | 控制"哪个用户能查哪个数据源、能看哪些列/行" |

#### 7.3.2 单租户 vs 多租户

```
MULTI_TENANT=false (单租户，默认):
  所有用户 → default 租户 (tenant_id=1)
  用户A 数据源 ←X→ 用户B 数据源  (用户间默认隔离)

MULTI_TENANT=true (多租户):
  租户A: 用户A1, 用户A2  (共享租户A的数据源池)
  租户B: 用户B1, 用户B2  (完全看不见租户A的任何数据)
  租户A 全部数据 ←=X=→ 租户B 全部数据  (硬隔离)
```

#### 7.3.3 数据源三级可见性

同一租户内，数据源有三种可见级别：

| 级别 | 含义 | 典型场景 |
|------|------|----------|
| `private` | 仅创建者可见（默认） | 个人测试库 |
| `tenant` | 租户内所有用户可见（只读） | 公司共享的生产库 |
| `restricted` | 指定用户可见，可限制列和行 | 财务数据只给财务组 |

#### 7.3.4 列级/行级权限

- **列白名单** (`allowed_columns`): SQL 校验阶段拦截未授权列
- **行过滤** (`row_filter_sql`): 执行时自动追加 WHERE 条件，如 `org_id = user.org_id`
- **敏感列掩码**: PII 字段自动脱敏

#### 7.3.5 请求链路

```
JWT 中间件 → {user_id, tenant_id, role}
  ↓
权限校验: 数据源 ∈ tenant ? 用户有权限 ? 列在白名单 ?
  ↓
SQL 重写: 注入 row_filter_sql
  ↓
执行 → 脱敏 → 审计日志 → 返回
```

#### 7.3.6 存储

```sql
CREATE TABLE tenants (
    id SERIAL PRIMARY KEY, name VARCHAR(128) NOT NULL,
    created_at TIMESTAMPTZ DEFAULT NOW());
INSERT INTO tenants (id, name) VALUES (1, 'default');

CREATE TABLE users (
    id SERIAL PRIMARY KEY, username VARCHAR(64) NOT NULL,
    password_hash VARCHAR(256) NOT NULL,
    role VARCHAR(16) DEFAULT 'analyst',
    tenant_id INT NOT NULL REFERENCES tenants(id) DEFAULT 1,
    created_at TIMESTAMPTZ DEFAULT NOW(),
    UNIQUE(username, tenant_id));

CREATE TABLE datasource_permissions (
    id SERIAL PRIMARY KEY, datasource_name VARCHAR(64) NOT NULL,
    tenant_id INT NOT NULL REFERENCES tenants(id),
    owner_user_id INT NOT NULL REFERENCES users(id),
    visibility VARCHAR(16) DEFAULT 'private',
    access_level VARCHAR(16) DEFAULT 'read',
    allowed_columns TEXT[] DEFAULT '{}',
    row_filter_sql TEXT DEFAULT '',
    created_at TIMESTAMPTZ DEFAULT NOW(),
    UNIQUE(datasource_name, tenant_id));

CREATE TABLE datasource_shared_users (
    permission_id INT REFERENCES datasource_permissions(id) ON DELETE CASCADE,
    user_id INT REFERENCES users(id) ON DELETE CASCADE,
    access_level VARCHAR(16) DEFAULT 'read',
    PRIMARY KEY(permission_id, user_id));

CREATE TABLE query_audit_log (
    id SERIAL PRIMARY KEY, user_id INT NOT NULL, tenant_id INT NOT NULL,
    datasource VARCHAR(64), sql_hash VARCHAR(64),
    row_count INT DEFAULT 0, duration_ms INT DEFAULT 0,
    success BOOLEAN DEFAULT TRUE, error_message TEXT DEFAULT '',
    created_at TIMESTAMPTZ DEFAULT NOW());
```

#### 7.3.7 用户组与个人权限叠加

数据过滤权限分两层，互不冲突，取并集：

```
用户能访问的数据 = 所在组权限 ∪ 个人权限
```

| 层级 | 说明 | 存储 |
|------|------|------|
| **用户组 (Group)** | 批量授权，同一角色/部门的用户继承组权限 | `group_permissions` + `user_groups` |
| **个人 (User)** | 精细化授权，针对单个用户 | `datasource_permissions` (owner_user_id) |

**叠加规则**：
- 用户属于财务组 → 财务组有 `prod_db` 的只读权限 → 用户自动获得
- 同时用户个人被额外授权 `prod_db.orders` 表的写权限 → 两权限合并
- 没有冲突，没有互相覆盖，**取并集**（最宽松原则）

```sql
CREATE TABLE user_groups (
    id SERIAL PRIMARY KEY, tenant_id INT NOT NULL REFERENCES tenants(id),
    name VARCHAR(64) NOT NULL,
    created_at TIMESTAMPTZ DEFAULT NOW());

CREATE TABLE user_group_members (
    group_id INT REFERENCES user_groups(id) ON DELETE CASCADE,
    user_id INT REFERENCES users(id) ON DELETE CASCADE,
    PRIMARY KEY(group_id, user_id));

CREATE TABLE group_permissions (
    id SERIAL PRIMARY KEY, group_id INT REFERENCES user_groups(id) ON DELETE CASCADE,
    datasource_name VARCHAR(64) NOT NULL,
    access_level VARCHAR(16) DEFAULT 'read',
    allowed_columns TEXT[] DEFAULT '{}',
    row_filter_sql TEXT DEFAULT '',
    created_at TIMESTAMPTZ DEFAULT NOW());
```

**校验顺序**：先查个人权限 `datasource_permissions` → 再查所有组的 `group_permissions` → 合并 allowed_columns 和 row_filter_sql → 取并集。

#### 7.3.8 租户超管与角色-菜单分离

**平台超级管理员**：
- `super_admin` 是整个系统的管理员，不隶属于某个租户的管理边界
- 可维护全局系统知识、全局标签和平台级配置
- 可查看并治理所有租户的知识标签，但业务数据访问仍必须审计
- 只有 `super_admin` 可以把个人自定义标签提升为全局标签

**租户超管**：
- 创建租户时自动生成 `tenant_admin` 账号
- 超管拥有租户内**所有权限**（全部数据源 + 全部列/行）
- 超管不走 `datasource_permissions` 校验，直接放行
- 超管可以创建/删除同租户下的其他用户和用户组
- 超管只能管理本租户公共知识，不能管理系统知识、全局标签或其他租户资源

**角色-菜单分离**（角色 ≠ 数据源权限）：

| 角色 | 可见菜单 | 数据源权限 |
|------|---------|-----------|
| `super_admin` | 平台全部菜单（含全局知识和标签治理） | 平台治理权限，业务数据访问必须审计 |
| `tenant_admin` | 全部菜单（对话/数据源/Schema/历史/Skills/知识库/系统设置/用户管理） | 租户内全部数据源，不校验权限表 |
| `analyst` | 对话/数据源/Schema/历史/Skills/知识库 | 按权限表（个人 + 组叠加） |
| `viewer` | 对话（只读）/历史 | 按权限表，强制只读 |

**为什么不把角色和数据源权限绑定**：
- 角色决定"能用哪些功能"（UI 层面），数据源权限决定"能查哪些数据"（数据层面）
- 一个 analyst 今天可能只需要访问 MySQL 测试库，明天可能需要访问生产库——改权限就够，不需要改角色
- 一个 viewer 可能需要看所有数据但只能看不能改——`access_level=read` 控制，不需要单独建一个"超级 viewer"角色

**各角色的菜单配置**：

```python
ROLE_MENUS = {
    "super_admin":  ["chat", "datasource", "schema", "history", "skills", "knowledge", "settings", "users", "platform"],
    "tenant_admin": ["chat", "datasource", "schema", "history", "skills", "knowledge", "settings", "users"],
    "analyst":      ["chat", "datasource", "schema", "history", "skills", "knowledge"],
    "viewer":       ["chat", "history"],
}
```

前端 `App.tsx` 的 sidebar 根据 JWT 中的 `role` 字段动态渲染菜单项。

#### 7.3.9 绝对隔离：五层防线

仅靠应用层 WHERE 过滤不够——任何一个遗漏就造成数据泄露。每一层都必须是防线。

```
用户请求
   │
   ▼
[Layer 1] JWT 中间件 ◀── 唯一信任源
   │  提取 {user_id, tenant_id, role} 写入 RequestContext (ContextVar)
   │  任何代码不得从请求参数中读取用户身份
   │
   ▼
[Layer 2] 路由/业务校验
   │  数据源权限: datasource.tenant_id == current_tenant_id ?
   │  会话隔离:    session.user_id == current_user_id ?
   │  历史隔离:    history.user_id == current_user_id ?
   │  知识库隔离:  knowledge.tenant_id == current_tenant_id ?
   │
   ▼
[Layer 3] 连接层注入 ◀── PostgreSQL 运行时参数
   │  conn.execute("SET app.current_user_id = '5'")
   │  conn.execute("SET app.current_tenant_id = '2'")
   │  连接归还池前 RESET 参数（防止跨请求泄漏）
   │
   ▼
[Layer 4] PostgreSQL RLS ◀── 数据库强制，绕过应用代码也生效
   │  CREATE POLICY tenant_isolation ON sessions
   │      USING (tenant_id = current_setting('app.current_tenant_id')::int);
   │
   ▼
[Layer 5] SQL 重写
   │  row_filter_sql 注入（如 org_id = user.org_id）
   │  列白名单裁剪
   │  敏感列掩码
   │
   ▼
执行 → 脱敏 → 审计日志 → 返回
```

**各存储的隔离方式**：

| 存储 | 隔离列 | 强制方式 |
|------|--------|---------|
| PostgreSQL (sessions, history, permissions) | `tenant_id` + `user_id` | WHERE + RLS |
| PostgreSQL (query_audit_log) | `tenant_id` + `user_id` | RLS 只读，仅超管可查 |
| ChromaDB (知识库, schema 缓存) | metadata.`tenant_id` | `.get(where={"tenant_id": ...})` |
| Redis (限流, 缓存) | 无持久化数据 | key 前缀 `tenant:{id}:user:{id}:` |
| SessionStorage (前端) | 浏览器隔离 | 每个标签页独立，不跨用户共享 |

**连接池防泄漏**：

连接池中的连接会在请求间复用。归还前必须重置 PG 运行时参数：

```python
async def get_db_connection():
    conn = await pool.acquire()
    await conn.execute("SET app.current_user_id = $1", current_user_id())
    await conn.execute("SET app.current_tenant_id = $1", current_tenant_id())
    return conn

async def release_db_connection(conn):
    await conn.execute("RESET app.current_user_id")
    await conn.execute("RESET app.current_tenant_id")
    await pool.release(conn)
```

**ContextVar 防跨请求污染**：

Python async 中不同请求的协程共享同一个线程。用 `ContextVar` 而非 `threading.local`：

```python
from contextvars import ContextVar

_current_user_id: ContextVar[int] = ContextVar("current_user_id")
_current_tenant_id: ContextVar[int] = ContextVar("current_tenant_id")

# 中间件设置
def set_current_user(user_id, tenant_id):
    _current_user_id.set(user_id)
    _current_tenant_id.set(tenant_id)

# 业务代码读取——永远不信任请求参数
def get_current_user_id():
    return _current_user_id.get()
```

#### 7.3.10 知识库与 Skill 的隔离

| 资源 | 隔离粒度 | 原因 |
|------|---------|------|
| 数据源 | 租户 + 用户 + 用户组 + 行/列 | 数据泄露是安全事故 |
| 知识库 | system + tenant + private | 平台、租户和用户知识必须分别授权 |
| Skill | system + tenant + private | 平台能力、组织能力和个人扩展必须分别授权 |
| MCP | system + tenant + private | 外部工具可能携带凭证和数据访问能力，必须同时隔离租户与用户 |

**知识库 (ChromaDB) 隔离**：

无论哪种知识类型（Schema 缓存 / 业务文档 / 指标口径 / 枚举值），统一使用三档知识范围：

| 可见性 | 谁可见 | 典型场景 |
|--------|--------|---------|
| `system` | 所有租户和用户 | SQL 方言手册、分析方法、数据质量规则、产品文档 |
| `tenant` | 当前租户所有人 | 数据源文档、官方数据字典、指标口径、业务规则 |
| `private`（用户上传默认） | 仅创建者 | 个人分析笔记、术语别名、临时补充知识 |

写权限强制为：`system` 仅 `super_admin` 或配置目录扫描任务；`tenant` 仅 `tenant_admin/super_admin`；`private` 为当前登录用户。`tenant_admin` 不得操作系统知识或其他租户知识。

ChromaDB 写入统一注入 `metadata.tenant_id`、`metadata.owner_user_id`、`metadata.visibility`。读取时分别检索 `system`、当前租户 `tenant`、当前用户 `private` 三个范围，再去重和重排；禁止用一次缺少所有者条件的宽查询替代。冲突权重为租户官方知识 > 个人补充 > 系统通用知识，个人知识不能静默覆盖租户指标口径。

**Skill 与 MCP 统一作用域**：

| 作用域 | 可见范围 | Skill 存储 | MCP 来源 |
|--------|----------|------------|----------|
| `system` | 所有租户和用户 | `skills/` 及超级管理员配置的系统目录 | `config/mcp_servers.yaml` 或超级管理员登记的系统服务 |
| `tenant` | 当前租户所有用户 | `data/skills/tenant/{tenant_id}/` | `mcp_servers.scope=tenant` 且匹配 `tenant_id` |
| `private` | 当前租户的创建者 | `data/skills/private/{tenant_id}/{user_id}/` | `mcp_servers.scope=private` 且同时匹配 `tenant_id/owner_user_id` |

写权限强制为：`system` 仅 `super_admin` 或启动扫描任务；`tenant` 仅当前租户的
`tenant_admin/super_admin`；`private` 仅当前登录用户。`tenant_admin` 不得管理系统资源、
其他租户资源或其他用户的私有资源。

运行时读取必须使用来自认证 `ContextVar` 的 `tenant_id/user_id/role`，禁止相信请求体中的所有者字段。
同名资源不允许静默覆盖：内部标识使用 `(scope, tenant_id, owner_user_id, name)`，匹配优先级为
`private > tenant > system`，但只有当前身份可见的候选才能参与优先级计算。

Skill 包的作用域由受信任存储路径决定，不能由上传包的 YAML frontmatter 自行声明。MCP 工具在转换为
LangChain Tool 前保存服务作用域，在每次请求调用 `get_all_tools(tenant_id, user_id)` 时过滤；后台无身份任务
只允许系统工具。启停、删除、测试连接同样执行作用域和角色校验，不能只依赖前端隐藏菜单。

### 7.4 数据库版本与知识库调用策略

#### 7.4.1 数据源 version 字段

`DataSourceConfig` 增加 `version: str` 字段，区分同一 dialect 的不同版本。

```yaml
# config/datasources.yaml
mysql_test:
  dialect: mysql
  version: "8.0"   # ← 新增
  host: ...
```

| 版本差 | 影响 |
|--------|------|
| MySQL 5.7 → 8.0 | 新增窗口函数 (`RANK()`, `ROW_NUMBER()`)、CTE (`WITH`)、`JSON_TABLE()` |
| PostgreSQL 14 → 16 | `ANY_VALUE()`、`MERGE`、并行查询策略变化 |
| ClickHouse 22.x → 24.x | `arrayFold` 替代 `arrayReduce`、`FORMAT` 函数废弃 |

有了 version 字段，知识库检索时可以精准匹配该版本的官方文档 chunk，LLM 不会生成不存在的函数。

#### 7.4.2 知识库按需加载

不每次都调知识库——避免无意义的延迟和 Token 浪费。

| 触发条件 | 行为 |
|----------|------|
| 意图 ∈ {query, aggregation, trend, attribution, metadata} | **加载**——需要业务表结构/函数参考 |
| `retry_count > 0` | **加载**——SQL 执行失败，参考知识库修正 |
| 意图 = `chat` 且 retry=0 | **跳过**——纯闲聊无需 SQL |

#### 7.4.3 当前代码变更清单

| 改动点 | 文件 | 说明 |
|--------|------|------|
| `_load_knowledge_context` | `retrieve_schema.py` | `where` 加入 `tenant_id` + `visibility=tenant` |
| `_upsert_to_cache` | `schema_manager.py` | 写入注 `tenant_id` + `visibility=tenant` |
| `_write_to_chromadb` | `upload_manager.py` | 上传文档注 `tenant_id` + `visibility` |
| `discover()` | `skill_manager.py` | 扫描 `skills/{tenant_id}/` 目录 |
| `match_skills()` | `skill_manager.py` | 跨租户过滤 |

**为什么需要 RLS**：
- 应用层 WHERE 遗漏 → RLS 兜底
- 新同事不知道隔离规则 → RLS 自动生效
- 管理后台直接查库 → RLS 仍然拦截
- RLS 是 PostgreSQL 内核级强制，比任何应用层代码都可靠

#### 7.3.10 API 变更

| 端点 | 变更 |
|------|------|
| `POST /auth/login` | 新增，返回 JWT `{user_id, tenant_id, role}` |
| `GET /datasources` | 按 `tenant_id` + 权限过滤 |
| `POST /datasources` | 绑定 `tenant_id` + `owner_user_id` |
| `GET /sessions` | 按 `user_id` 过滤 |
| `GET /history` | 按 `user_id` 过滤 |
| `POST /chat` | 校验权限 → 注入行过滤 → 审计日志 |

单租户模式（`tenant_id=1`）下现有 API 行为不变。

---
