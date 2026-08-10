# Phase 4 选定能力设计

本文件定义本轮实现的知识库自进化、Grafana 查询协议与权限适配、一键部署、自动优化建议和 SQL 优化 Skill 的最小可用边界。

## 1. 知识库自进化

`KnowledgeEvolutionService.observe_successful_sql()` 只接受通过 `validate_sql()` 的单条只读 SQL，且调用方必须提供执行成功和验证结果。服务按租户、用户、方言和规范化 SQL 生成稳定指纹，在 VectorStore 中聚合观察次数；达到 `promotion_threshold` 后生成 `MemoryType.LEARNED_PATTERN`，默认 `private` 作用域。失败 SQL、安全拦截 SQL、未执行或未校验 SQL 不得进入观察表。

## 2. Grafana

提供 `/api/v1/grafana/` 下的 SimpleJSON 兼容接口：

- `GET /search` 返回当前身份可见数据源名称；
- `POST /query` 接收 `targets[].rawSql`，逐目标复用租户数据源 ACL、列/行权限和只读有界执行，返回 Grafana table/time series 数据；
- `GET /annotations` 返回空数组并保留协议兼容；
- `GET /tag-keys`、`GET /tag-values` 返回空数组。

接口不接受请求体中的 tenant/user 覆盖值，不执行写 SQL，不把权限错误转换为成功空结果。

## 3. 自动优化建议

`OptimizationAdvisor` 为确定性、只读分析器。输入 SQL、方言及可选 schema/index/statistics，输出 `OptimizationReport`：问题、风险、证据、候选重写 SQL 和候选索引。建议永远标记 `executed=false`，不得执行 DDL 或修改数据源。

## 4. SQL 优化 Skill

`skills/sql-optimization` 只调用 `OptimizationAdvisor`，不连接数据库、不执行 SQL，输出 schema 契约规定的 JSON。它与 `sql-diagnostics` 分工：诊断 Skill 关注语法和方言，优化 Skill 关注性能改进候选。

## 5. 一键部署

`scripts/install.sh` 执行前置依赖检查、显式版本/仓库参数校验、`.env` 初始化、随机密钥生成、`docker compose config` 校验、启动和健康检查。已有 `.env` 默认不覆盖，凭证只能由用户通过环境变量或交互方式提供。
