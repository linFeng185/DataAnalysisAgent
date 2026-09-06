# 报表续开发接口与运行约束

日期：2026-09-06。对应 `30-report-workbench.md` 的 23.11～23.14，实现说明不代表真实外部环境已经验收。

## 持久化调度

- `ReportScheduler.start/close/run_once/dispatch` 接入 bootstrap；`report_scheduler_enabled=true` 为默认值，数据库未配置时不启用。创建 scheduled 任务要求服务已启动，否则返回 503，不能把进程内 Task 冒充持久化调度。
- 计划预览解析每小时/每天/每周/每月，支持明确星期、24 小时时刻、上午/下午和月内日期。未指定时刻时首期立即执行。首次 UTC 时间、频率、次数、时区必须在前端展示后确认。月末保存 `schedule_day` 锚点，短月夹到月末，下一长月恢复原锚点。
- 到期扫描每实例最多认领两个任务，PostgreSQL 使用 `FOR UPDATE SKIP LOCKED`；获得专属会话级 advisory lock 后推进下次时间。心跳间隔 20 秒，中断扫描阈值 120 秒，单次运行上限 600 秒；停机积压跳过过往日历期，不瞬间补跑全部历史期。
- SQL/图节点执行身份来自当前 `users/tenants`，拒绝停用身份和超管替代业务用户。每次重新解析租户默认模型、报表能力及数据源 ACL；发布前再次比较身份和 ACL。不会把密钥保存在任务计划中。
- PostgreSQL 发布使用持锁连接；连接失联自动释放锁，旧连接无法再提交页面。运行配额预占与 `quota_period_start/quota_held` 同事务提交，正常收尾与恢复共用幂等释放，不重复返还运行次数或 Token。
- 进程中断但页面尚未提交时，终结旧 run 并以新 run_id 重跑；已提交页面但状态未写回时识别已发布版本，不重复执行。仍未承诺节点级成功单元缓存复用。
- 同一任务使用稳定 report_id，重新执行追加版本，不修改旧版本的业务内容。新周期访问历史版本与分享仍按固定版本授权。

请求示例：先 `POST /api/v1/report-workbench/plans`：

```json
{"prompt":"每周一上午9点生成销售报表，运行4次","datasources":["sales"],"timezone":"Asia/Shanghai"}
```

从响应读取 `plan_hash`，确认后 `POST /api/v1/report-workbench/jobs`：

```json
{"prompt":"每周一上午9点生成销售报表，运行4次","plan_hash":"sha256:<预览返回值>","confirm":true,"idempotency_key":"<本次确认唯一键>"}
```

返回 202，含 `job_id/status/execution_mode/estimated/quota`；scheduled 不调用立即执行入队入口。

- `POST /api/v1/report-workbench/jobs/{job_id}/pause` → `{"job_id":"...","is_paused":true}`。
- `POST /api/v1/report-workbench/jobs/{job_id}/resume` → `{"job_id":"...","is_paused":false}`；次数或期限耗尽返回 409。
- `POST /api/v1/report-workbench/jobs/{job_id}/run` 为显式手动运行，不能替代其他用户身份，也不能绕过暂停或 scheduled 次数上限。

## 实时钻取

- 成功 SQL 的只读模板保存在私有 `report_live_queries`，页面只携带随机 `query_ref`；通过 `run_id` 与页面版本绑定。公开响应不包含 SQL、数据源凭证或 live 定义。
- 沿用 `POST /api/v1/report-workbench/reports/{report_id}/drilldowns/{drilldown_id}`。客户端只允许过滤与分页，额外 `sql/datasource` 等字段返回 422。
- `report.live_drilldown` 独立控制。每次按当前租户重验数据源/列白名单/行过滤，受进程查询限流、报表并发与行数预算约束，经过统一 SQL 校验和 EXPLAIN，最后只投影已签发列并脱敏。
- 每页最多 100 行，只允许前 1000 行分页；一次最多读取 page_size+1 行。`has_more` 与 `total_is_exact` 区分有界分页估计和已知总数，不为算总行数执行无界 COUNT。
- 过滤以 SQL AST 字面量构建，覆盖 PostgreSQL/MySQL/ClickHouse 解析测试，不进行客户端 SQL 字符串拼接。

请求：

```json
{"filters":{"region":"华东"},"page":1,"page_size":20}
```

响应示例：

```json
{"drilldown_id":"live-<随机标识>","mode":"live","columns":["region","amount"],"rows":[{"region":"华东","amount":100}],"page":1,"page_size":20,"total":1,"total_is_exact":true,"has_more":false,"masking":{"applied":false,"fields":[]}}
```

## 页面生命周期

- `GET /api/v1/report-workbench/reports/{report_id}/lifecycle` 返回 `expires_at/expired/can_extend/can_permanent`，过期页面不返回正文，但可管理尚未清理的页面。
- `POST .../reports/{report_id}/extend`，请求 `{"days":7}` 或 `{"days":30}`，需要 `report.extend`，每次最多 30 天；从“原有效期与当前时间较晚者”延长。
- `POST .../reports/{report_id}/permanent`，请求 `{}`，需要独立 `report.permanent`；`PUT .../lifecycle` 提供同等受限统一操作。
- 响应示例：`{"report_id":"...","expires_at":null,"permanent":true}`。
- 仅改变生命周期元数据，不重新生成业务分析内容；旧分享不会自动延长或恢复。永久转有限期限会同步收紧超过页面期限的分享。已物理清理或软删除页面返回 404，不能凭延期操作重建。
- 私有正文、私有 Drilldown 和公开分享均检查页面有效期，过期统一返回 410。

## 跨源聚合

`aggregate_cross_source` 仅在各来源声明完全相同的 1～3 个维度且指标全部获授权时执行。分别按来源输出 `sample_sum/sample_mean/sample_count`，不连接原始多对多行，不跨来源相加业务指标。

默认总输入 6000 行、200 个分组、512 KiB 输入及输出预算、每来源最多 8 个数值指标。失败来源、空数据、维度类型不一致、无授权字段、无限数值或预算超限时整体降级为独立分析。输出写入 `cross_source_aggregate` 清单和页面独立表格，明确提示“样本统计，不代表全量业务总额或业务可加性”。

## 部署与验收

1. 新增 019 指标目录、020 私有实时模板、021 调度恢复字段、022 租户 RLS 收紧；没有修改 018 已有迁移的校验和。本轮未主动向用户数据库执行这些迁移；若启用启动自动迁移，重启前应先审查与备份。
2. 在测试 PostgreSQL 上使用非 BYPASSRLS 角色验证跨租户策略、分享只读、两个 Worker 竞争、锁连接断开、预占中断、跨月释放和“页面提交后进程退出”。SQLite 标量测试只证明布尔条件，不代替上述数据库验收。
3. 业务 SQL 仍走统一连接器，单元测试使用 Fake/Mock；真实远程 LLM 不进入默认测试集。
4. 当前剩余职责混用清单见 `features/24-tenant-governance-audit.md`；不能把上述报表实现视为已重构全项目平台/租户权限。
