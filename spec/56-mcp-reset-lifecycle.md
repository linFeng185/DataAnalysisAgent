# MCP 连接重置生命周期（11.1.12）

日期：2026-10-14。落实 `spec/15-design-review.md` 14.3.2 第 ④ 项：管理员通过 `POST /api/v1/mcp/{name}/reset` 手动恢复 degraded MCP Server。本规范先定义状态机、并发/竞态、owner task 关闭契约与验收标准，再据此实现。

## 1. 背景与待验证风险

- `MCPClientManager` 当前在**调用方任务**里 `AsyncExitStack.enter_async_context()`，而 `remove_server()` / 健康检查重连可能从**另一个任务**关闭同一个栈。MCP SDK 的 `stdio_client`/`sse_client` 内部使用 AnyIO cancel scope，跨任务退出会抛 `RuntimeError: Attempted to exit cancel scope in a different task than it was entered in`，可能造成旧连接无法关闭或残留子进程；本轮必须先用真实本地 MCP 进程复现验证，未证实不得作为已确认故障。
- 重连（`_reconnect`）在指数退避 `sleep` 期间不感知外部的删除/重载；「sleep 结束后无条件重连」会**复活已被删除的服务**。
- 缺少「手动重置」入口：degraded（重连耗尽、工具已摘除）的服务只能等健康检查碰运气。

## 2. 状态机（每个 Server 独立）

```
        ┌──────────────┐  connect 成功   ┌────────┐
        │ configured   │ ─────────────► │ ready  │
        │ (有配置元数据)│ ◄───────────── │(会话+工具)│
        └──────┬───────┘  reset/remove   └───┬────┘
               │ connect 失败                  │ ping 连续失败且重试耗尽
               ▼                               ▼
        ┌──────────────┐   health_check 恢复  ┌──────────┐
        │ unavailable  │ ◄─────────────────── │ degraded │
        │ (无会话无工具)│  或 reset 成功        │(无会话无工具)│
        └──────┬───────┘                      └────┬─────┘
               │ remove / 身份失效                  │ remove
               ▼                                    ▼
        ┌──────────────┐                      ┌──────────┐
        │  removed     │ ◄────────────────────┤ closing  │
        │ (无任何状态)  │      owner 关闭完成   │(关闭中)   │
        └──────────────┘                      └──────────┘
```

| 状态 | 会话 | 工具 | 配置元数据 `_server_configs` | 可被 reset | 可被工具调用 |
|------|------|------|------------------------------|-----------|-------------|
| ready | 有 | 有 | 有 | 是 | 是（每次调用仍经 `authorize_runtime_server`） |
| degraded / unavailable | 无 | 无 | 有（保留合法配置，可重试） | 是 | 否 |
| closing | 无（已从 `sessions` 摘除） | 无 | 移除中 | 否（必须等关闭完成） | 否 |
| removed | 无 | 无 | 无 | 否（404） | 否 |

转移规则：

1. `configured → ready`：`_connect_single` 在 owner task 内建立会话、`initialize`、`list_tools` 全部成功后，会话与工具**原子上线**。任一步失败则回到 unavailable，且不留下任何半初始化对象。
2. `ready → degraded`：`health_check` ping 失败 → `_reconnect` 重试 `_MAX_RECONNECT_RETRIES` 次仍失败。进入 degraded 时**先摘工具、再摘会话**（`langchain_tools` 前缀清理 + `sessions.pop`），不保留旧越权工具。
3. `degraded → ready`：健康检查下一次探测成功后自动回填，或管理员 `reset` 成功。
4. `ready → ready`：`reset` 成功（破坏式重建：先关旧、再建新、最后原子替换）。
5. 任意状态 `→ removed`：`remove_server`（删除 API / 租户清理）关闭会话并清空全部元数据。**removed 是终态**，除非重新创建配置。

## 3. 并发与竞态

### 3.1 按服务串行 / 跨服务并行

- `MCPClientManager` 为每个内部 Server 名维护一把 `asyncio.Lock`（`_ServerGate`）。
- 所有会改变某服务运行时状态的入口都在该服务的锁内执行：`remove_server`、`reset_server`、`_reconnect`（仅重连动作，退避 sleep 在锁外）、`reload_from_db`（逐服务替换段）、`test_connection` 探测后的清理。
- 不同服务使用不同锁，互不阻塞：两个服务的 reset 可真正并行。
- 同一服务的两个并发 reset 严格串行；后一个看到的是前一个完成后的状态，不会出现交错替换。

### 3.2 epoch 防复活

- 每个 Server 名维护单调递增 `epoch`。`remove_server`、`reset_server` 提交、`reload_from_db` 替换提交都会 `epoch += 1`。
- 长时间操作在开始时快照 epoch，提交前复核（锁内）：
  - `_reconnect`：退避 `sleep` 前取 epoch；醒来后若 epoch 变化或配置已被移除 → 放弃重连（**不得复活删除服务**）。
  - `reload_from_db`：查询数据库**之前**整体快照 epoch；逐服务加锁后若该名 epoch 变化 → 跳过该行（防止「读库早于删除、连接晚于删除」的复活）。
  - `reset_server`：加锁后若 `_server_configs` 中已无该服务（已被删除）→ 直接以「不可用」失败，不重建。

### 3.3 顺序契约

「删除 API = 先删数据库行、再 `remove_server`」与「reload 先读库、再连接」并发时，最终状态必须为 **removed**：要么 reload 先提交、remove 后清（终态 removed），要么 remove 先提交、reload 复核 epoch 后跳过。

## 4. owner task 关闭契约（强制）

- 每个连接由一个**专属 owner task**（`src/mcp_client/owner_task.py` 的 `OwnedMCPConnection`）持有：`AsyncExitStack` 的 `enter_async_context` 与 `aclose` **都在 owner task 内**执行，AnyIO cancel scope 与 MCP SDK 的流关闭因此同任务退出。
- 任何其他任务只能通过**协作式关闭**请求 owner task 退出：投递 shutdown 消息 → 等待 owner task 完成 → 超时则 `cancel()` 该 owner task（取消仍在 owner task 内触发栈退出）→ 再等待一个有界时长。
- 工具调用与 ping 通过 owner task 的请求队列转发，保证 SDK 内部 `fail_after` 等 scope 的进入/退出同任务。
- 关闭是幂等的；owner task 启动失败（连接异常）时同样在 owner task 内完成清理后再结束。
- 取消传播：调用方被取消时，owner task 不会被留在无人等待的状态；未完成的请求 future 必须被显式失败，不能悬挂。

## 5. 重置语义

`reset_server(name, *, timeout)` 在一次服务锁内执行：

1. **校验配置可用**：内置 YAML 服务取其配置并检查 `enabled`；数据库受管服务必须持有合法配置（SSE + allowlist + 公网地址，由路由传入的已授权配置或 `_server_configs` 中的最近合法配置），否则以「不可用」失败且**不建立任何连接**。
2. **破坏式拆除**：关闭旧 owner 连接（有界超时）并摘除该服务的会话、工具与运行时元数据；配置元数据保留以便重试。此步保证失败路径上不存在「旧越权工具」。
3. **重建**：在新 owner task 内建立连接并获取工具；失败/超时则由 owner task 自行清理，不留下半初始化对象。
4. **原子替换**：新会话与新工具在一次**无 await** 的关键区内写入 `sessions` / `langchain_tools` / 元数据；观察者只会看到旧集合或新集合，不会看到混合状态。
5. **超时与取消**：整段有总预算 `timeout`（默认 30s，路由可配）；超时抛 `MCPResetTimeoutError`（HTTP 504），取消向上传播；两者都在清理完成后发生。
6. **失败可重试**：失败后该服务处于 unavailable，`_server_configs` 保留合法配置，随后再次 reset 可恢复。

## 6. HTTP API 契约

`POST /api/v1/mcp/{name}/reset?scope=system|tenant|private`（`scope` 可选，用于同名多作用域消歧）

- 身份一律取当前认证上下文（`tenant_id`/`user_id`/`role`），**不接受、不信任请求体或查询串中的租户参数**（请求体不解析任何字段）。

| 状态码 | 场景 | 响应体 |
|--------|------|--------|
| 200 | 重置成功 | `{"status":"ok","name":<公开名>,"scope":<实际作用域>,"tools":<新工具数>}` |
| 400 | `scope` 非法 | `{"detail":"不支持的知识范围: ..."}` |
| 403 | 身份可见但无权管理（角色/作用域）；服务已停用；已有安全阻断 | 受控文案，不含 URL/凭证/内部异常 |
| 404 | 名称未知或对当前身份不可见（含跨租户、已删除） | 受控文案 |
| 500 | 其他内部失败 | `重置 MCP Server 失败`（不泄露内部细节） |
| 503 | 配置存储不可用（授权前查库失败） | `MCP Server 配置存储不可用` |
| 504 | 重置超时 | 受控文案 |

授权与安全规则：

- 解析（`resolve_resettable_server`）复用现有身份可见性：system 全局可见；tenant 需同租户；private 需同租户且同所有者；super_admin 仅可解析 system。
- 管理授权复用 `can_manage_knowledge_resource`：system 需平台超级管理员；tenant 需租户管理员；private 需所有者本人。
- 内置 YAML system 服务（不在数据库）仅平台超级管理员可重置，且 `enabled: false` 时 403 且不建连。
- 数据库受管服务：查库命中但 `enabled=false` 或存在 `mcp_security_blocks.blocked=true` → 403 且**不建立连接**；跨租户名称不可见 → 404。
- 重置使用路由已授权的配置；受管配置在 manager 内再次通过 `_is_managed_remote_config_allowed`（SSE + allowlist + 公网解析）校验，拒绝时同样不建连。
- 每次 MCP 工具调用仍走 `authorize_runtime_server` 二次查库，重置不会把停用/阻断状态绕过。

## 7. 可观测性

- 入口/出口 `debug`/`info` 记录：server 内部名、作用域、耗时、工具数、结果分类。
- 禁止记录 URL 查询串、env、凭证、配置正文；错误只记 `error_type` 与脱敏文案（异常类型名可记，异常消息中的连接信息不进入响应）。
- 竞态放弃（epoch 变化、服务已删除）记 `info` 级「已取消」原因，不报错。

## 8. 验收

| 验收项 | 测试 |
|--------|------|
| 成功重置（会话+工具原子替换，计数正确） | `tests/test_mcp/test_reset_lifecycle.py`、`tests/test_api/test_mcp_reset.py` |
| 同服务并发双 reset 串行、无交错 | 同上 |
| 不同服务 reset 并行 | 同上 |
| 失败后再恢复（配置保留、可重试） | 同上 |
| 超时受控（504，无半初始化） | 同上 |
| 取消（任务取消后无残留会话/工具/进程） | 同上 |
| reset 与 remove 竞态不复活删除服务 | 同上 |
| 工具清理（重置后仅新工具，无旧绑定） | 同上 |
| 身份/角色/scope 消歧/租户隔离（404/403） | `tests/test_api/test_mcp_reset.py` |
| 禁用与安全阻断不建立连接 | 同上 |
| 日志脱敏（无 URL/凭证/正文） | 同上 |
| 真实本地 stdio MCP 跨任务 reset/关闭 | `tests/test_mcp/test_reset_stdio_real.py` |
| owner task 关闭契约（栈同任务退出、幂等、取消） | `tests/test_mcp/test_owner_task.py` |

真实 stdio 验证使用 `tests/fixtures/mcp/stdio_echo_server.py`（FastMCP 本地进程，`sys.executable` 启动），不依赖任何远程服务。

## 10. 本轮执行边界

- 本文件为旧隔离任务设计的重新审查版本，编号改为 56，禁止覆盖现有 Prompt 预算文档 53/54。
- 依赖的 StaticMCPTestServer 和内存数据库 fixture 已在 features/16-testing.md 标记完成；11.1.12 仍为待开发，满足开始条件。
- 不修改 SQL 画像、键列判断或其工具循环。仅实现 MCP 生命周期、reset 路由、必要响应模型与对应测试。
- 先用真实本地 stdio Server 验证跨任务进入/退出风险；无证据不重构。对确需修复的 owner task，测试必须覆盖 reset/remove/health/reload/close 各入口，不能仅 Mock。
- 不为了上线 reset 放宽既有远程 SSE allowlist、SSRF 或角色授权；取消/总超时后不得复活被删除的服务。
- 只运行 MCP、reset API 及安全边界定向回归；记录离线真实进程与生产远程服务验收的差别。
