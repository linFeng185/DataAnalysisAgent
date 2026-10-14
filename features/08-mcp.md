# 8. MCP 集成

## 8. MCP 集成 (mcp/) `[P0:4 P1:8 P2:9 P3:2]`

### 8.1 MCP Client

| # | 功能 | 文件 | 描述 | 状态 |
|---|------|------|------|------|
| 8.1.1 | MCPClientManager 类 | `src/mcp/client_manager.py` | 管理所有 MCP Client 连接的生命周期 | 开发完成 |
| 8.1.2 | connect_all() | 同上 | 启动时并发连接 config/app.yaml 中声明的 MCP Server | 单测完成 |
| 8.1.3 | _connect_single() | 同上 | 连接单个 MCP Server (支持 stdio + SSE transport) | 开发完成 |
| 8.1.4 | _resolve_env() | 同上 | 解析 MCP 配置中的 ${VAR_NAME} 环境变量占位符 | 开发完成 |
| 8.1.5 | _mcp_to_langchain_tool() | 同上 | 将 MCP Tool 适配为 LangChain StructuredTool (加 namespace 前缀) | 开发完成 |
| 8.1.6 | _build_schema() | 同上 | 从 MCP Tool 的 JSONSchema inputSchema 生成 Pydantic args_schema | 开发完成 |
| 8.1.7 | get_all_tools() | 同上 | 返回所有 MCP 转换来的 LangChain Tool 列表 | 开发完成 |
| 8.1.8 | health_check() | 同上 | 定期 ping 所有 MCP Server，断线自动重连 | 开发完成 |
| 8.1.9 | _reconnect() | 同上 | 单个 MCP Server 的断线重连逻辑 (指数退避) | 开发完成 |
| 8.1.10 | close_all() | 同上 | 关闭所有连接（owner task 内退出 AsyncExitStack，协作式关闭 + 有界取消） | 单测完成 |
| 8.1.11 | _sse_client() | 同上 | SSE transport 的客户端实现 | 开发完成 |
| 8.1.12 | 降级策略 | 同上 | 重连 5 次失败 → 标记 degraded → 从 get_all_tools() 移除 → 健康检查恢复后自动启用 | 开发完成 |
| 8.1.13 | MCP 工具租户隔离 `[P0]` | 同上 | 系统工具全局可见；租户工具按 tenant_id 过滤，无身份仅返回系统工具 | 单测完成 |
| 8.1.14 | MCP 三级作用域隔离 `[P0]` | 同上 | system/tenant/private 工具按 tenant_id + owner_user_id 请求级过滤 | 单测完成 |
| 8.1.15 | 租户与个人 MCP 生命周期 `[P1]` | 同上 + `src/main.py` | DB 配置加载后建立连接；新增、删除和重载同步运行时连接 | 单测完成 |
| 8.1.16 | MCP owner task 连接生命周期 `[P1]` | `src/mcp_client/owner_task.py` + `src/mcp_client/client_manager.py` | 进入/调用/ping/退出同任务，避免跨任务退出 AnyIO cancel scope；协作式关闭、有界取消、未完成请求显式失败；按服务串行、epoch 防复活、手动 reset 破坏式重建（总预算由 `asyncio.timeout` 统一施加，覆盖目标准备/锁等待/拆旧/重建）；`prepare_authorized_server` 只登记单个已授权目标，不整租户加载 | 集成测试完成 |

### 8.2 MCP Server

| # | 功能 | 文件 | 描述 | 状态 |
|---|------|------|------|------|
| 8.2.1 | FastMCP 实例化 | `src/mcp_client/server.py` | FastMCP("data-analysis-agent") 创建 MCP Server | 单测完成 |
| 8.2.2 | query_database Tool | 同上 | 以自然语言查询数据库，返回分析结果与图表；异常响应不泄露内部细节 | 单测完成 |
| 8.2.3 | list_datasources Tool | 同上 | 列出当前所有可用数据源及描述；异常返回稳定协议错误 | 单测完成 |
| 8.2.4 | get_table_schema Tool | 同上 | 获取指定表的完整结构信息，未找到与内部故障使用不同契约 | 单测完成 |
| 8.2.5 | get_metrics Tool | 同上 | 查询业务指标口径定义和计算公式；异常响应脱敏 | 单测完成 |
| 8.2.6 | MCP Server 启动入口 | `src/mcp/__main__.py` | `python -m src.mcp` 启动 MCP Server，并兼容旧 mcp_client 入口 | 单测完成 |
| 8.2.7 | Claude Code 集成配置 | `claude_code_mcp.json` | 配置为 Claude Code 可调用的 MCP Server | 开发完成 |

### 8.3 MCP Agent Node

| # | 功能 | 文件 | 描述 | 状态 |
|---|------|------|------|------|
| 8.3.1 | mcp_agent_node() | `src/graph/nodes/mcp_agent.py` | 使用 create_react_agent 为文件分析和市场研究子图提供受控动态工具调用 | 单测完成 |
| 8.3.2 | route_by_intent() 集成 | `src/graph/workflow.py`、`src/graph/subgraphs/file_analysis.py` | file_analysis 能力路由到独立子图，由子图调用 mcp_agent_node 并回到统一响应出口 | 单测完成 |
| 8.3.3 | MCP Agent system prompt | `src/llm/prompts.py`、`src/llm/invocation.py` | Agent 提示词通过 PromptRegistry 版本化注册并走统一调用边界 | 单测完成 |
| 8.3.4 | MCP Agent 失败契约 `[P1]` | `src/graph/nodes/mcp_agent.py`、`src/graph/subgraphs/file_analysis.py` | 模型不可用时返回标准 mcp_agent 失败响应并经过统一历史出口 | 单测完成 |

### 8.4 MCP 管理与授权

| # | 功能 | 文件 | 描述 | 状态 |
|---|------|------|------|------|
| 8.4.1 | MCP 作用域管理 API `[P0]` | `src/api/routes.py` + `migrations/004_resource_scopes.sql` | 三级权限管理；存储故障可见但创建、删除、连通测试响应不泄露内部异常 | 单测完成 |
| 8.4.2 | 受管 MCP 进程与网络边界 `[P0]` | `src/api/routes.py` + `src/mcp_client/client_manager.py` | 数据库配置仅允许管理员创建 SSE；禁用 stdio/进程参数并按精确主机 allowlist 加载 | 单测完成 |

### 模块收尾

模块功能点共 29 项，已完成 29 项，待开发 0 项。

> 8.1.16 验收边界：真实本地 stdio 子进程（`tests/test_mcp/test_reset_stdio_real.py`）已覆盖跨任务进出、reset/remove 竞态、超时清理与重试；该验收不等于生产远程 SSE 服务验收，远程连通性、allowlist 主机解析与生产网络边界仍需部署环境复核。
> 8.1.16 路由总预算补充（2026-10-14）：`POST /mcp/{name}/reset` 的总 deadline 从目标准备开始计时，剩余预算再传给 `reset_server`，冷目标/既有目标被同名操作持锁时均在预算内以 504 结束；准备阶段失败与重置失败走同一套脱敏状态码映射（404/403/500），不会漏成未处理异常。回归见 `tests/test_api/test_mcp_reset.py::TestMcpResetRouteDeadline`。
> 8.1.16 异常日志单次归属补充（2026-10-14）：owner 边界记录连接异常后打标（`mark_error_logged`/`is_error_logged`，见 `src/mcp_client/owner_task.py`），同一异常跨层传播到 reset 路由时不再重复写同一堆栈；直接调用 owner 的失败仍保留一份脱敏堆栈。回归：`tests/test_mcp/test_owner_task.py`（owner 单次记录与脱敏）与 `tests/test_api/test_mcp_reset.py::TestMcpResetLoggingBoundary`（owner+manager+route 三层联合捕获）。

---
