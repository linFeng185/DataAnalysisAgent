# 平台管理、强制认证与统一配置设计

## 目标

平台始终要求登录，同时允许通过 `multi_tenant` 选择单租户或多租户数据隔离模式。系统保留
唯一的平台超级管理员，并提供租户、用户、数据源和 system 级资源的完整管理入口。

## 身份与启动约束

1. `users.id=1` 永久保留给唯一 `super_admin`，并归属默认租户 `tenant_id=1`、
   `tenant_code=default`。
2. 启动顺序为：数据库迁移 → 超级管理员校验/初始化 → 其余资源预热。账号缺失、角色冲突或
   初始密码缺失时启动失败，开发环境也不允许匿名降级。
3. `super_admin_username` 与 `super_admin_password` 来自统一配置；密码不得提交到仓库，生产
   必须由环境变量覆盖。初始化仅插入缺失账号，不在每次启动时覆盖已有密码。
4. 所有非公开 API 都要求有效 JWT。`multi_tenant` 只控制租户级过滤和 RLS，不再控制是否登录。
5. 固定账号不能被删除、停用、迁移租户或修改为其他角色。平台级授权同时校验
   `user_id == 1` 和 `role == "super_admin"`。

## 登录与注册安全

- 登录继续使用 HttpOnly Cookie；生产 Cookie 仅通过 HTTPS 发送。所有账号使用
  `tenant_code + username + password` 登录，用户名在租户内区分大小写。
- 登录先执行客户端地址、规范化租户编码与原始用户名组合的滑动窗口限流，再校验数据库账号锁定状态。
- 连续失败达到 `login_lockout_threshold` 后写入 `locked_until`，锁定时长由
  `login_lockout_minutes` 配置；成功登录原子清零失败次数并记录 `last_login_at`。
- 用户不存在时执行固定 bcrypt 校验，避免通过响应耗时枚举账号；所有失败返回统一文案。
- 公开注册永久关闭，`POST /auth/register` 返回 403，前端不展示注册入口。
- 租户和首个租户管理员由平台后台原子创建；后续用户及租户管理员由当前租户管理员自治。

## 统一配置

`config/app.yaml` 是系统配置目录，包含全部 `Settings` 字段及 `mcp_servers`。加载优先级：

1. 显式构造参数；
2. 环境变量；
3. `.env`；
4. `config/app.yaml`；
5. 代码安全默认值。

密钥字段在 YAML 中保持空值，部署环境使用同名大写环境变量注入。`config/mcp_servers.yaml`
删除，MCP Client 从 `config/app.yaml` 的 `mcp_servers` 节读取。`config/datasources.yaml` 可选，
文件不存在时返回空 Provider，不影响启动。

页面创建的数据源写入 `datasource_configs`，密码使用 `CredentialManager` 密文保存；启动时先加载
数据库配置，再补充可选 YAML 配置。同租户数据源名称唯一。

## 平台管理 API

租户生命周期端点仅允许固定超级管理员调用：

| 方法 | 端点 | 作用 |
|------|------|------|
| GET | `/api/v1/admin/tenants` | 分页列出租户及用户数 |
| POST | `/api/v1/admin/tenants` | 原子创建租户和首个 `tenant_admin` |
| PATCH | `/api/v1/admin/tenants/{tenant_id}` | 启停租户，默认租户不可停用 |
| GET | `/api/v1/admin/config` | 返回脱敏后的运行配置摘要 |

用户管理端点仅允许 `tenant_admin` 调用，并强制作用于当前租户：

| 方法 | 端点 | 作用 |
|------|------|------|
| GET | `/api/v1/admin/users` | 列出当前租户用户 |
| POST | `/api/v1/admin/users` | 创建当前租户的 tenant_admin/analyst/viewer |
| PATCH | `/api/v1/admin/users/{user_id}` | 修改当前租户用户角色或状态 |
| POST | `/api/v1/admin/users/{user_id}/reset-password` | 重置当前租户用户密码并清除锁定 |

接口不返回密码、哈希、JWT、API Key、数据库密码或凭证主密钥。

## system 资源隔离

system 级 Skill、知识文件/条目和 MCP 配置属于平台内部资源：

- 只有固定超级管理员能通过管理 API 列出、读取原文、启停、上传或删除；
- `tenant_admin`、`analyst`、`viewer` 的管理列表只包含当前租户资源和本人 private 资源；
- Agent 运行时仍可在受信任内部链路使用启用的 system Skill/知识/MCP，但不得把其原始内容或
  连接配置返回给普通用户；
- 所有限制在后端查询与资源解析层执行，前端菜单隐藏只作为用户体验优化。

## 前端

- 未登录访问任意业务路由时跳转 `/login`；登录成功后恢复目标页。
- 登录页固定输入租户编码、用户名和密码，不展示公开注册表单。
- 侧栏按角色渲染。`super_admin` 增加“平台管理”，包含租户、用户、安全配置三个页签，并提供
  system Skill、知识库和 MCP 的治理入口。
- Header 展示当前账号、角色和退出按钮。普通角色不渲染平台管理路由，直接访问也跳回首页。

## 测试

- 配置优先级、MCP 合并、可选数据源文件；
- 超级管理员首次初始化、幂等、冲突失败和固定身份保护；
- 未登录 401、登录失败锁定、成功清零、注册开关；
- 租户编码登录、大小写敏感用户名与租户用户管理正常/边界/越权路径；
- 普通用户无法列出或读取 system Skill、知识和 MCP；
- 前端登录跳转、角色菜单和平台管理页面生产构建。
