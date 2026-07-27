# 22. API 访问策略与 IP 控制

> Depends on：21.1.1 统一应用配置、21.3.1 平台管理 API、12.4.12 纯 ASGI 身份上下文、17.3.4 结构化日志。前置功能均已完成。

## 22.1 配置与策略模型

| # | 功能 | 文件 | 描述 | 状态 | 优先级 |
|---|------|------|------|------|--------|
| 22.1.1 | YAML 启动策略 | `config/app.yaml`、`src/config.py` | 公开、可选认证、管理 Key 和访问日志模式结构化配置 | 集成测试完成 | P0 |
| 22.1.2 | 策略匹配与默认失败关闭 | `src/security/api_access_policy.py` | 方法、精确路径和模板匹配，未知路由默认 JWT | 集成测试完成 | P0 |

## 22.2 IP 与访问日志

| # | 功能 | 文件 | 描述 | 状态 | 优先级 |
|---|------|------|------|------|--------|
| 22.2.1 | 可信代理客户端 IP | `src/security/api_access_policy.py` | 仅可信代理可提供转发地址，支持 IPv4/IPv6/CIDR | 集成测试完成 | P0 |
| 22.2.2 | 接口 IP 黑白名单 | `src/security/api_access_policy.py` | 紧急 deny、接口 deny 优先、allow 默认拒绝 | 集成测试完成 | P0 |
| 22.2.3 | 分级访问日志 | `src/api/access_policy.py`、`src/logging_config.py` | standard/security/audit/none 聚合日志，安全事件始终保留 | 集成测试完成 | P1 |

## 22.3 动态管理

| # | 功能 | 文件 | 描述 | 状态 | 优先级 |
|---|------|------|------|------|--------|
| 22.3.1 | 策略与 IP 规则迁移 | `migrations/007_api_access_policy.sql` | 动态策略、CIDR 规则和平台管理强制 RLS | 集成测试完成 | P0 |
| 22.3.2 | 策略内存快照 | `src/security/api_access_policy.py`、`src/bootstrap.py` | 启动加载、原子刷新、请求零数据库查询 | 集成测试完成 | P0 |
| 22.3.3 | 平台管理 API | `src/api/routes/access_policy.py` | 动态策略与 IP 规则 CRUD，YAML 基线只读 | 集成测试完成 | P1 |
| 22.3.4 | 平台管理前端 | `frontend/src/pages/AdminPage.tsx` | 策略列表、编辑和接口 IP 规则维护 | 集成测试完成 | P1 |

### 模块收尾

模块功能点共 9 项，已完成 9 项，待开发 0 项。
