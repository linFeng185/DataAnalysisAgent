# 20. 架构整改

## 20.1 整改功能点

| # | 功能 | 文件 | 状态 | 优先级 |
|---|------|------|------|--------|
| 20.1 | 异常吞噬清理 | `src/` 多模块 | 单测完成 | P0 |
| 20.2 | 异步 Tool 事件循环修复 | `src/tools/` | 单测完成 | P0 |
| 20.3 | API 路由领域拆包 | `src/api/routes/` | 单测完成 | P1 |
| 20.4 | 依赖单一来源 | `pyproject.toml`、`requirements.txt` | 单测完成 | P0 |
| 20.5 | 大文件 Git 历史清理 | `src/test_data.sql` | 本地执行完成 | P1 |
| 20.6 | PostgreSQL 运行时池化 | `src/memory/pg_pool.py`、运行时存储 | 单测完成 | P0 |
| 20.7 | 目标超长函数拆分 | API 会话/上传、多源合并 | 单测完成 | P1 |
| 20.8 | MCP Agent 节点独立模块 | `src/graph/nodes/mcp_agent.py` | 单测完成 | P1 |
| 20.9 | Connector 注册表 | `src/connectors/registry.py` | 单测完成 | P1 |
| 20.10 | LLM Provider 注册表 | `src/llm/provider_registry.py` | 单测完成 | P1 |
| 20.11 | 工作流节点目录 | `src/graph/node_registry.py` | 单测完成 | P1 |
| 20.12 | 启动编排提取 | `src/bootstrap.py` | 单测完成 | P1 |
| 20.13 | PostgreSQL URL 工具 | `src/db/utils.py` | 单测完成 | P0 |

### 模块收尾

模块功能点共 13 项，已完成 13 项，待开发 0 项。

20.5 已通过 `git filter-repo` 清除所有本地 refs 中的 `src/test_data.sql` / `test_data.sql`，本地数据文件继续由 `.gitignore` 排除。远端历史发布属于仓库维护操作，必须在通知协作者后单独 force-push。
