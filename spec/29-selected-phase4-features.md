# Phase 4 选定能力设计

本文件定义知识库自进化、Grafana 查询协议与权限适配、一键部署、自动优化建议和 SQL 优化 Skill 的生产级边界。

## 1. 知识库自进化

`KnowledgeEvolutionService.observe_successful_sql()` 只接受通过 `validate_sql()` 的单条只读 SQL，且调用方必须提供执行成功和验证结果。服务按租户、用户、方言和规范化 SQL 生成稳定指纹，在 PostgreSQL 观察表原子累加；达到 `promotion_threshold` 后生成稳定 ID 的 `MemoryType.LEARNED_PATTERN`，默认 `private` 作用域。失败 SQL、安全拦截 SQL、未执行或未校验 SQL 不得进入观察表。

晋升使用数据库级 Claim 和不可伪造的 Claim Token：多 Worker 只能有一个执行写入，超过 10 分钟的声明允许故障接管，旧 Worker 不能用过期令牌覆盖新 Worker。VectorStore 写入与状态写回之间通过稳定记忆 ID 保证重试幂等；晋升失败释放声明并记录脱敏错误摘要。没有 PostgreSQL 的开发回退路径使用进程内锁保持相同互斥语义。

## 2. Grafana

提供 `/api/v1/grafana/` 下的 SimpleJSON 兼容接口：

- `GET /search` 返回当前身份可见数据源名称；
- `POST /query` 接收 `targets[].rawSql`，对同一请求生成 ACL 快照，逐目标复用租户数据源 ACL、列/行权限和只读有界执行，返回 Grafana table/time series 数据；
- `GET /annotations` 返回空数组并保留协议兼容；
- `GET /tag-keys`、`GET /tag-values` 返回空数组。

接口不接受请求体中的 tenant/user 覆盖值，不执行写 SQL，不把权限错误转换为成功空结果。每次请求最多 20 个目标，以最多 8 路有界并发执行，单目标超时 30 秒；多目标中的执行故障局部隔离并保留其他结果，权限故障仍整体失败，客户端取消必须向下传播。

## 3. 自动优化建议

`OptimizationAdvisor` 为确定性、只读分析器。输入 SQL、方言及可选 schema/index/statistics，输出 `OptimizationReport`：问题、风险、证据来源、证据质量、候选重写 SQL 和候选索引。指定数据源时优先复用已授权 Schema，缺失时通过 `SchemaManager` 自动获取索引与行数估计；获取失败降级为 AST 静态分析并显式返回限制。

SQL、元数据节点数、嵌套深度和字符串体积均受资源预算约束，超限时在解析前拒绝。结果按严重性、代码和证据稳定排序，候选索引必须说明已有索引、Schema 和统计证据。建议永远标记 `executed=false`，不得执行 DDL 或修改数据源。

## 4. SQL 优化 Skill

`skills/sql-optimization` 只调用 `OptimizationAdvisor`，不连接数据库、不执行 SQL，输出 schema 契约规定的 JSON。Skill 显式声明输入资源上限、证据要求和 `executed=false` 安全契约，并用 PostgreSQL、MySQL、ClickHouse 的固定评测样本验证跨方言行为。它与 `sql-diagnostics` 分工：诊断 Skill 关注语法和方言，优化 Skill 关注性能改进候选。

## 5. 一键部署

`scripts/install.sh` 执行前置依赖检查、显式版本/仓库参数校验、`.env` 初始化、随机密钥生成、`docker compose config` 校验、启动和健康检查。已有 `.env` 默认不覆盖，凭证只能由用户通过环境变量或交互方式提供。健康检查失败时输出最近 200 行 Compose 日志并自动停止本次创建或替换的容器，保留持久化卷和用户配置；显式跳过健康检查时不触发回滚。
