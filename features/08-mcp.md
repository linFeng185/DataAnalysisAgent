# 8. MCP 集成

## 8. MCP 集成 (mcp/) `[P0:4 P1:8 P2:9 P3:2]`

### 8.1 MCP Client

| # | 功能 | 文件 | 描述 | 状态 |
|---|------|------|------|------|
| 8.1.1 | MCPClientManager 类 | `src/mcp/client_manager.py` | 管理所有 MCP Client 连接的生命周期 | 开发完成 |
| 8.1.2 | connect_all() | 同上 | 启动时并发连接 config/mcp_servers.yaml 中所有 MCP Server | 开发完成 |
| 8.1.3 | _connect_single() | 同上 | 连接单个 MCP Server (支持 stdio + SSE transport) | 开发完成 |
| 8.1.4 | _resolve_env() | 同上 | 解析 MCP 配置中的 ${VAR_NAME} 环境变量占位符 | 开发完成 |
| 8.1.5 | _mcp_to_langchain_tool() | 同上 | 将 MCP Tool 适配为 LangChain StructuredTool (加 namespace 前缀) | 开发完成 |
| 8.1.6 | _build_schema() | 同上 | 从 MCP Tool 的 JSONSchema inputSchema 生成 Pydantic args_schema | 开发完成 |
| 8.1.7 | get_all_tools() | 同上 | 返回所有 MCP 转换来的 LangChain Tool 列表 | 开发完成 |
| 8.1.8 | health_check() | 同上 | 定期 ping 所有 MCP Server，断线自动重连 | 开发完成 |
| 8.1.9 | _reconnect() | 同上 | 单个 MCP Server 的断线重连逻辑 (指数退避) | 开发完成 |
| 8.1.10 | close_all() | 同上 | 关闭所有连接 (AsyncExitStack.aclose) | 开发完成 |
| 8.1.11 | _sse_client() | 同上 | SSE transport 的客户端实现 | 开发完成 |
| 8.1.12 | 降级策略 | 同上 | 重连 5 次失败 → 标记 degraded → 从 get_all_tools() 移除 → 健康检查恢复后自动启用 | 开发完成 |
| 8.1.13 | MCP 工具租户隔离 `[P0]` | 同上 | 系统工具全局可见；租户工具按 tenant_id 过滤，无身份仅返回系统工具 | 单测完成 |
| 8.1.14 | MCP 三级作用域隔离 `[P0]` | 同上 | system/tenant/private 工具按 tenant_id + owner_user_id 请求级过滤 | 单测完成 |
| 8.1.15 | 租户与个人 MCP 生命周期 `[P1]` | 同上 + `src/main.py` | DB 配置加载后建立连接；新增、删除和重载同步运行时连接 | 单测完成 |

### 8.2 MCP Server

| # | 功能 | 文件 | 描述 | 状态 |
|---|------|------|------|------|
| 8.2.1 | FastMCP 实例化 | `src/mcp/server.py` | FastMCP("data-analysis-agent") 创建 MCP Server | 开发完成 |
| 8.2.2 | query_database Tool | 同上 | 以自然语言查询数据库，返回分析结果与图表 | 开发完成 |
| 8.2.3 | list_datasources Tool | 同上 | 列出当前所有可用数据源及描述 | 开发完成 |
| 8.2.4 | get_table_schema Tool | 同上 | 获取指定表的完整结构信息 | 开发完成 |
| 8.2.5 | get_metrics Tool | 同上 | 查询业务指标口径定义和计算公式 | 开发完成 |
| 8.2.6 | MCP Server 启动入口 | `src/mcp/__main__.py` | `python -m src.mcp` 启动 MCP Server | 开发完成 |
| 8.2.7 | Claude Code 集成配置 | `claude_code_mcp.json` | 配置为 Claude Code 可调用的 MCP Server | 开发完成 |

### 8.3 MCP Agent Node

| # | 功能 | 文件 | 描述 | 状态 |
|---|------|------|------|------|
| 8.3.1 | mcp_agent_node() | `src/graph/workflow.py` | 使用 create_react_agent 为文件分析场景创建动态工具调用 Node | 开发完成 |
| 8.3.2 | route_by_intent() 集成 | 同上 | intent == "file_analysis" → 路由到 mcp_agent Node | 开发完成 |
| 8.3.3 | MCP Agent system prompt | 同上 | Agent 内联 system prompt | 开发完成 |
| 8.3.4 | MCP Agent 失败契约 `[P1]` | 同上 | 模型不可用时返回标准 mcp_agent 失败响应并经过统一历史出口 | 单测完成 |

### 8.4 MCP 管理与授权

| # | 功能 | 文件 | 描述 | 状态 |
|---|------|------|------|------|
| 8.4.1 | MCP 作用域管理 API `[P0]` | `src/api/routes.py` + `migrations/004_resource_scopes.sql` | system 仅超管、tenant 仅租户管理员、private 仅本人管理 | 单测完成 |
| 8.4.2 | 受管 MCP 进程与网络边界 `[P0]` | `src/api/routes.py` + `src/mcp_client/client_manager.py` | 数据库配置仅允许管理员创建 SSE；禁用 stdio/进程参数并按精确主机 allowlist 加载 | 单测完成 |

### 模块收尾

模块功能点共 28 项，已完成 28 项，待开发 0 项。

---
