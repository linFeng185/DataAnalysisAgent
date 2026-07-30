# 20. 架构整改

## 20.1 整改功能点

| # | 功能 | 文件 | 状态 | 优先级 |
|---|------|------|------|--------|
| 20.1 | 关键路径异常吞噬清理 | `src/graph/`、`src/memory/`、`src/api/` | 单测完成 | P0 |
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
| 20.14 | 剩余回退异常可见性审计 | `src/` 20 个模块、30 个 fallback handler | 单测完成 | P1 |
| 20.15 | AppContext 与依赖注入 | `src/` 全局工厂、Graph、FastAPI | 单测完成 | P1 |
| 20.16 | 租户策略集中化 | `src/security/`、认证、知识库、Graph | 单测完成 | P0 |
| 20.17 | Settings 单一 Context 来源 | `src/config.py`、`src/main.py`、`src/app_context.py` | 单测完成 | P0 |
| 20.18 | LLM Provider 单一调用链 | `src/llm/` | 单测完成 | P0 |
| 20.19 | 路由显式最小导入 | `src/api/routes/` | 单测完成 | P1 |
| 20.20 | SQL 安全执行统一入口 | `src/security/sql_execution.py`、Graph、Tools | 单测完成 | P0 |
| 20.21 | PostgreSQL 裸连接回归门禁 | `src/`、`tests/` | 单测完成 | P0 |
| 20.22 | 异常处理决策矩阵 | `src/failure_policy.py`、安全与降级边界 | 单测完成 | P0 |
| 20.23 | VectorStore 所有权收口 | `src/memory/`、`src/knowledge/` | 单测完成 | P0 |
| 20.24 | AnalysisState 持久化分层 | `src/graph/`、会话历史 | 单测完成 | P0 |
| 20.25 | 任务计划与统一分析产物 | `src/graph/contracts.py`、`src/graph/artifacts.py`、`src/llm/invocation.py` | 单测完成 | P1 |
| 20.26 | 多数据源请求与结果契约 | `src/graph/contracts.py`、`src/graph/nodes/multi_source.py` | 单测完成 | P1 |
| 20.27 | Skill 两阶段精确激活 | `src/graph/skill_activation.py`、`src/graph/nodes/` | 单测完成 | P1 |
| 20.28 | AnalysisState 上下文分组 | `src/graph/context.py`、`src/graph/state.py`、`src/graph/nodes/` | 单测完成 | P1 |
| 20.29 | 图节点上下文读取收口 | `src/graph/context.py`、`src/graph/nodes/`、`src/graph/workflow.py` | 单测完成 | P1 |

### 模块收尾

模块功能点共 29 项，已完成 29 项，待开发 0 项；第五轮扩展与生产化收口已完成上下文读取收口。

20.5 已通过 `git filter-repo` 清除所有本地 refs 中的 `src/test_data.sql` / `test_data.sql`，本地数据文件继续由 `.gitignore` 排除。远端历史发布属于仓库维护操作，必须在通知协作者后单独 force-push。

第一轮 20.1-20.16、第二轮 20.17-20.24 与第三轮 20.25 均已完成。额外审计同步修复了跨后端 VectorStore 不等值过滤、
pgvector 空过滤全表删除风险、启动预热私有接口泄漏、MCP/Skill/结构化资产跨模块私有访问，以及知识上传授权顺序。
第四轮 20.26-20.28 与第五轮 20.29 已完成。

当前模块无待开发项。
