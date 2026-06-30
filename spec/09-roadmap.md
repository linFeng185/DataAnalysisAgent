# 8. 实现路线图

## 8. 实现路线图

### Phase 1 — MVP（1-2周）

- [ ] 项目骨架搭建（FastAPI + Poetry）
- [ ] 配置体系: settings.py (pydantic-settings) + config/mcp_servers.yaml
- [ ] DataSourceRegistry + DataSourceConfig + EmbeddedProvider
- [ ] ClickHouse 连接器 + DB 内省 (INFORMATION_SCHEMA 查询)
- [ ] SchemaManager: 三级回退机制 (ChromaDB 缓存 → 文档仓库 → DB 自动拉取)
- [ ] KnowledgeEntry 数据结构 + ChromaDB 表级/字段级双粒度索引
- [ ] SchemaSnapshot.to_prompt_text() LLM 注入格式化
- [ ] Markdown 文档加载器 (docs/metrics/ 目录扫描 + 按标题切片)
- [ ] MCPClientManager 基础实现 (stdio transport 至少支持 1 个 MCP Server)
- [ ] SkillManager 基础实现 (skills/ 目录扫描 + SKILL.md 解析)
- [ ] LangChain + LangGraph 初始化（ChatOpenAI/ChatAnthropic 工厂）
- [ ] AnalysisState 定义 + 基础图（classify_intent → generate_sql → layer3_validate → execute_sql → build_response）
- [ ] sqlglot Layer 3 语法校验 Node
- [ ] SQL 安全拦截（正则 DDL/DML 黑名单，Layer 3 之前执行）
- [ ] 结果 JSON 返回
- [ ] MemorySaver 会话持久化（开发环境）
- [ ] LangSmith 接入（可观测性）

### Phase 2 — 增强（2-3周）

- [ ] 多表 JOIN 支持（Schema 关系图谱注入 Prompt）
- [ ] 错误重试边（conditional edge: 执行失败 → 带 error 上下文回到 SQL 生成 Node）
- [ ] 数据分析 Node（pandas 统计 + LLM 洞察）
- [ ] 图表生成 Node（ECharts config 自动生成）
- [ ] 向量知识库（ChromaDB 存储历史 SQL 模板，作为 few-shot 检索）
- [ ] PostgresSaver 替换 MemorySaver（生产级会话持久化）

### Phase 3 — 生产化（2-3周）

- [ ] Web Chat 前端（React）
- [ ] 多数据源支持（MySQL、PostgreSQL）
- [ ] 流式响应 (SSE)
- [ ] 查询缓存（Redis）
- [ ] 限流与并发控制
- [ ] 数据脱敏
- [ ] 监控与日志（Prometheus + Grafana）
- [ ] Docker 容器化部署

### Phase 4 — 进阶（持续迭代）

- [ ] 自动 Insight 发现（定期扫描数据，主动推送异常）
- [ ] 定时报告（按日/周/月自动生成分析报告）
- [ ] 知识库自进化（高质量 SQL 自动积累）
- [ ] 多模态输入（上传 Excel/CSV 直接分析）
- [ ] 权限体系完善（多用户、角色管理）

---
