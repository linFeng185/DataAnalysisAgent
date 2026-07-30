# 21. 平台管理与强制认证

## 21.1 统一配置

| # | 功能 | 文件 | 描述 | 状态 | 优先级 |
|---|------|------|------|------|--------|
| 21.1.1 | 统一应用配置 | `config/app.yaml`、`src/config.py` | YAML 配置目录，环境变量覆盖，包含全部 Settings 字段 | 单测完成 | P0 |
| 21.1.2 | MCP 配置合并 | `config/app.yaml`、`src/mcp_client/client_manager.py` | 删除独立 mcp_servers.yaml，从统一配置读取 | 单测完成 | P0 |
| 21.1.3 | 可选数据源 YAML | `src/bootstrap.py` | datasources.yaml 不存在时正常启动，页面配置为主要入口 | 单测完成 | P1 |

## 21.2 身份安全

| # | 功能 | 文件 | 描述 | 状态 | 优先级 |
|---|------|------|------|------|--------|
| 21.2.1 | 固定平台超级管理员 | `migrations/006_platform_admin.sql`、`src/bootstrap.py` | users.id=1 唯一 super_admin，启动幂等初始化和冲突阻断 | 单测完成 | P0 |
| 21.2.2 | 全模式强制登录 | `src/api/auth.py`、`src/security/tenant_policy.py` | 登录与 multi_tenant 解耦，业务 API 始终要求 JWT | 单测完成 | P0 |
| 21.2.3 | 登录防爆破 | `src/api/auth.py` | IP+账号限流、原子持久失败计数、临时锁定和统一错误 | 单测完成 | P0 |
| 21.2.4 | 注册开关 | `src/config.py`、`src/api/auth.py` | registration_enabled 默认关闭，公开注册端点固定返回 403 | 单测完成 | P0 |
| 21.2.5 | 租户编码登录与关闭公开注册 | `migrations/013_tenant_identity_llm.sql`、`src/api/auth.py` | tenant_code+大小写敏感用户名登录，编码全局唯一不可变，公开注册固定阻断 | 单测完成 | P0 |

## 21.3 平台管理

| # | 功能 | 文件 | 描述 | 状态 | 优先级 |
|---|------|------|------|------|--------|
| 21.3.1 | 租户管理 API | `src/api/routes/admin.py` | 创建、列表、启停租户并原子创建 tenant_admin | 单测完成 | P0 |
| 21.3.2 | 用户管理 API | `src/api/routes/admin.py` | 用户创建、列表、角色/状态修改、密码重置 | 单测完成 | P0 |
| 21.3.3 | 配置摘要 API | `src/api/routes/admin.py` | 只返回脱敏后的运行配置和功能开关 | 单测完成 | P1 |
| 21.3.4 | 数据源配置持久化 | `datasource_configs`、数据源 Provider | 页面创建的数据源加密持久化并在启动时恢复 | 单测完成 | P1 |
| 21.3.5 | system 资源不可见 | Skills/Knowledge/MCP 路由 | 普通用户不能列出或读取 system 管理内容 | 单测完成 | P0 |
| 21.3.6 | 租户管理员用户自治 | `src/api/routes/admin.py` | tenant_admin 仅管理当前租户用户，可创建同租户 tenant_admin 并保护最后管理员 | 单测完成 | P0 |

## 21.4 管理前端

| # | 功能 | 文件 | 描述 | 状态 | 优先级 |
|---|------|------|------|------|--------|
| 21.4.1 | 租户编码登录入口 | `LoginPage.tsx`、`AuthContext.tsx` | 强制跳转登录，输入 tenant_code+username+password 且不展示注册 | 单测完成 | P0 |
| 21.4.2 | 角色菜单 | `App.tsx` | 按角色显示菜单、账号信息和退出入口 | 单测完成 | P0 |
| 21.4.3 | 平台管理页面 | `AdminPage.tsx` | 租户、用户、安全配置和 system 资源治理入口 | 单测完成 | P0 |

### 模块收尾

模块功能点共 17 项，已完成 17 项，待开发 0 项。

本模块本轮没有待开发功能点。
