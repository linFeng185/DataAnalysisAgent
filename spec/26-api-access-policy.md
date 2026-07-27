# API 访问策略、日志与 IP 控制设计

## 目标

把认证例外、访问日志模式和接口级 IP 黑白名单收敛为统一访问策略。系统采用
`config/app.yaml` 启动基线与 PostgreSQL 动态规则混合模式：数据库不可用时仍保留健康检查、
登录和默认失败关闭能力；平台超级管理员可在线维护受保护接口及 IP 规则。

## 核心约束

1. 未匹配策略的 HTTP 接口默认使用 `jwt + standard`，禁止默认公开。
2. `public` 和 `optional` 只能在 YAML `bootstrap_policies` 中声明，数据库动态策略不能扩大匿名面。
3. 管理策略 API 永远要求固定 `users.id=1` 的 `super_admin`，数据库策略不能覆盖自身认证。
4. `access_log=none` 仅关闭成功请求的访问摘要；IP 拒绝、认证失败、异常和策略变更仍必须记录。
5. 请求 IP 默认使用直接连接地址。只有直接来源命中 `trusted_proxy_cidrs` 时才解析
   `X-Forwarded-For`，并从右向左剥离可信代理。
6. 策略和 IP 规则加载为 `AppContext` 内存快照，请求链路禁止逐请求查询 PostgreSQL。

## YAML 配置

```yaml
api_access:
  default_auth: jwt
  default_access_log: standard
  trusted_proxy_cidrs: []
  emergency_ip_deny: []
  bootstrap_policies:
    - id: health
      path: /api/v1/health
      path_type: exact
      methods: [GET]
      auth: public
      access_log: none
    - id: login
      path: /api/v1/auth/login
      path_type: exact
      methods: [POST]
      auth: public
      access_log: security
    - id: auth_probe
      path: /api/v1/auth/me
      path_type: exact
      methods: [GET]
      auth: optional
      access_log: standard
```

`auth` 可选值为 `public`、`optional`、`jwt`、`jwt_or_admin_key`、`super_admin`。
`access_log` 可选值为 `standard`、`security`、`audit`、`none`。
`path_type` 可选值为 `exact`、`template`，模板使用 FastAPI 路径格式。

## 策略优先级

1. YAML `emergency_ip_deny` 命中时立即返回 403。
2. 管理 API 自保护策略优先，固定为 `super_admin + audit`。
3. YAML 启动策略优先于同路径数据库策略，数据库不能覆盖启动策略。
4. 数据库动态策略按 `priority DESC, id ASC` 匹配。
5. 无匹配时使用 YAML 默认策略。
6. 策略内 IP `deny` 优先；存在启用的 `allow` 时，未命中 `allow` 默认拒绝。

## PostgreSQL 数据结构

```sql
CREATE TABLE api_access_policies (
    id BIGSERIAL PRIMARY KEY,
    policy_key VARCHAR(64) NOT NULL UNIQUE,
    path VARCHAR(512) NOT NULL,
    path_type VARCHAR(16) NOT NULL,
    methods TEXT[] NOT NULL,
    auth_mode VARCHAR(32) NOT NULL,
    access_log_mode VARCHAR(16) NOT NULL,
    priority INT NOT NULL DEFAULT 0,
    enabled BOOLEAN NOT NULL DEFAULT TRUE,
    description TEXT NOT NULL DEFAULT '',
    created_by INT NOT NULL REFERENCES users(id),
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE TABLE api_ip_rules (
    id BIGSERIAL PRIMARY KEY,
    policy_key VARCHAR(64) NOT NULL,
    action VARCHAR(8) NOT NULL,
    cidr CIDR NOT NULL,
    enabled BOOLEAN NOT NULL DEFAULT TRUE,
    description TEXT NOT NULL DEFAULT '',
    created_by INT NOT NULL REFERENCES users(id),
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    UNIQUE (policy_key, action, cidr)
);
```

`api_ip_rules.policy_key` 可以引用 YAML 或数据库策略。写入 API 必须先从当前合并快照验证策略存在。
两张表都启用并强制 PostgreSQL RLS，只有固定超级管理员身份事务可以读写；启动加载使用受信任的
固定超级管理员上下文读取完整快照。

## 中间件与认证边界

新增纯 ASGI `ApiAccessPolicyMiddleware`，位于 `AuthMiddleware` 外层：

1. 解析路径、方法和客户端 IP。
2. 从内存快照解析策略并执行紧急黑名单和接口 IP 规则。
3. 将策略写入 ASGI `scope["state"]`，供 `AuthMiddleware` 执行认证模式。
4. 捕获响应状态和耗时，按 `access_log_mode` 输出一条聚合访问日志。

关闭 `uvicorn.access` 默认传播，由访问策略中间件统一记录，避免重复日志并支持按路由静默。

## 管理 API

以下接口仅固定超级管理员可调用：

| 方法 | 路径 | 作用 |
|------|------|------|
| GET | `/api/v1/admin/access-policies` | 返回 YAML 与数据库合并策略及 IP 规则 |
| POST | `/api/v1/admin/access-policies` | 创建受保护的数据库动态策略 |
| PATCH | `/api/v1/admin/access-policies/{policy_id}` | 修改动态策略 |
| DELETE | `/api/v1/admin/access-policies/{policy_id}` | 删除动态策略及关联 IP 规则 |
| POST | `/api/v1/admin/access-policies/{policy_key}/ip-rules` | 创建接口 CIDR 黑/白名单 |
| PATCH | `/api/v1/admin/access-ip-rules/{rule_id}` | 启停或修改 IP 规则 |
| DELETE | `/api/v1/admin/access-ip-rules/{rule_id}` | 删除 IP 规则 |

动态策略的 `auth_mode` 只允许 `jwt`、`jwt_or_admin_key`、`super_admin`。所有写操作在事务提交后
原子刷新当前进程策略快照；刷新失败时接口返回 500，不使用半更新快照。

创建动态策略请求示例：

```json
{
  "policy_key": "report_export",
  "path": "/api/v1/reports/{report_id}/export",
  "path_type": "template",
  "methods": ["POST"],
  "auth_mode": "super_admin",
  "access_log_mode": "audit",
  "priority": 100,
  "description": "报表导出管理接口"
}
```

响应示例：

```json
{
  "id": 12,
  "policy_key": "report_export",
  "path": "/api/v1/reports/{report_id}/export",
  "path_type": "template",
  "methods": ["POST"],
  "auth_mode": "super_admin",
  "access_log_mode": "audit",
  "priority": 100,
  "enabled": true,
  "description": "报表导出管理接口"
}
```

创建 IP 规则请求示例：

```json
{
  "action": "allow",
  "cidr": "10.20.0.0/16",
  "enabled": true,
  "description": "办公网出口"
}
```

## 响应与审计

- IP 策略拒绝返回 `403 {"detail": "当前 IP 不允许访问此接口"}`。
- 认证失败继续返回现有 401 契约。
- 管理写入冲突返回 409，非法路径、方法、CIDR 或枚举返回 422。
- 管理写入日志仅记录策略编号、动作、CIDR 和操作者编号，不记录 Token、Cookie 或管理密钥。

## 测试范围

- YAML 配置解析、默认失败关闭、模板匹配和方法隔离。
- `public`、`optional`、`jwt_or_admin_key`、`super_admin` 认证模式。
- 直接 IP、可信代理、多级 `X-Forwarded-For`、IPv4/IPv6/CIDR。
- 紧急黑名单、接口 deny 优先、allow 未命中拒绝。
- `standard/security/audit/none` 访问日志与安全拒绝日志。
- 数据库 CRUD、YAML 策略不可修改、动态公开策略拒绝和快照刷新。
- 平台管理页面策略列表、编辑表单和 IP 规则操作。
