# 5. API 设计

## 5. API 设计

### 5.1 核心接口

```
POST /api/v1/chat
  - 发送自然语言查询，返回分析结果

POST /api/v1/chat/stream
  - SSE 流式返回分析过程（SQL生成→执行→分析→图表）

GET  /api/v1/schema/tables
  - 获取所有表列表

GET  /api/v1/schema/tables/{table_name}
  - 获取指定表结构

POST /api/v1/schema/refresh
  - 刷新 Schema 缓存

GET  /api/v1/history?session_id=xxx
  - 获取会话历史

GET  /api/v1/sessions/{session_id}
  - 返回最近 20 轮逐轮结构化响应与 has_more

GET  /api/v1/sessions/{session_id}/turns?before=21&limit=20
  - 向前分页读取更早轮次
```

### 5.2 /api/v1/chat 请求/响应

```json
// 请求
{
  "session_id": "uuid",
  "query": "过去7天各品类的销售额趋势，找出增长最快的3个品类",
  "datasource": "clickhouse_prod"
}

// 响应
{
  "session_id": "uuid",
  "query": "过去7天各品类的销售额趋势...",
  "sql": "SELECT category, toDate(created_at) AS dt, SUM(amount) ...",
  "sql_statements": [
    {
      "datasource": "clickhouse_prod",
      "dialect": "clickhouse",
      "sql": "SELECT category, toDate(created_at) AS dt, SUM(amount) ..."
    }
  ],
  "data": [
    {"category": "电子", "date": "2026-05-28", "sales": 128000}
  ],
  "analysis": {
    "summary": "过去7天销售额排名前三的品类为...",
    "insights": [
      "电子产品以12.8万位居榜首，环比增长23%",
      "家居品类增速最快，环比增长45%"
    ],
    "chart": {
      "type": "line",
      "config": {}
    }
  }
}
```

多数据源请求返回每个来源经过方言重写和权限处理后的最终 SQL。`sql_statements` 是展示与审计的
权威字段；`sql` 仅用于兼容旧客户端。SSE 的 LLM 流事件同时返回 `stream_id`，客户端必须按调用
实例隔离推理和内容 token。

### 5.3 历史会话恢复契约

`GET /sessions/{session_id}` 和 `/sessions/{session_id}/turns` 返回的每个 `turn` 必须包含自己的
`final_result`，字段与聊天最终响应一致，至少包含：

- `sql`、`sql_statements`：该轮处理后的最终 SQL；
- `data`、`row_count`、`truncated`：该轮数据样本与完整行数；
- `analysis`、`chart`：该轮分析与图表；
- `success`、`status`、`source`、`error_code`、`error_message`：该轮统一结束状态。

原始 `reasoning_content`、数据库异常和连接信息不得写入聊天响应、SSE、查询历史或会话恢复结果。

禁止只为最后一轮补充富数据，也禁止把 `latest_state` 注入所有轮次。`latest_state` 仅保留给旧客户端，
新客户端必须逐轮消费 `turn.final_result`。首次打开长会话返回最新 20 轮，`has_more=true` 时通过
`before=<当前最早 turn_id>` 向前分页。旧记录没有结构化响应时，允许退化为摘要和已有 SQL，但不得
用其他轮次的 SQL、数据或图表补齐。

### 5.4 数据源生命周期契约

`POST /api/v1/datasources/test` 使用请求中的临时配置执行方言探针，不注册 Provider、不写入
`datasource_configs`，并在成功或失败后释放连接器。响应固定为 `success/message`，不得返回数据库
异常、连接串或凭证。

`PUT /api/v1/datasources/{name}` 仅允许租户管理员更新本租户数据源，平台超级管理员可跨租户操作。
密码为空时沿用原凭证；更新顺序固定为“临时连接探测 → PostgreSQL 持久化 → 关闭旧连接 → 替换
Provider 配置 → 清除 Registry 缓存”。探测或持久化失败时保留旧配置，避免管理操作中断在线查询。

### 5.5 图表与 Skill 扩展契约

`POST /api/v1/charts/adjust` 只接收最多 500 行已有查询结果和白名单图表指令，复用确定性
`ChartGeneratorTool` 生成新配置，不重新执行 SQL，也不允许客户端直接提交任意 ECharts 脚本。

`ChatRequest.enabled_skill_ids` 使用 Skill 复合资源 ID。后端按认证身份重新校验可见性和启用状态后写入
请求级 `AnalysisState`；空列表表示自动匹配，非空列表表示只激活显式授权项。

`GET /api/v1/skills/registry` 列出审核版本；`POST /api/v1/skills/registry/{name}/install` 安装指定版本。
Registry 未配置时列表返回 `configured=false`，安装返回 503；下载、校验或解压失败不得留下半安装目录。

### 5.6 主动洞察与定时报告契约

| 方法 | 路径 | 说明 |
|------|------|------|
| POST | `/api/v1/automation/schedules` | 创建当前用户的只读 SQL 自动化任务 |
| GET | `/api/v1/automation/schedules` | 列出本人任务；租户管理员可见本租户任务 |
| DELETE | `/api/v1/automation/schedules/{schedule_id}` | 删除当前身份可管理的任务 |
| POST | `/api/v1/automation/schedules/{schedule_id}/run` | 重新授权并立即运行任务 |
| GET | `/api/v1/automation/notifications?limit=50` | 列出当前用户最近的站内通知 |

创建请求固定包含 `name/kind/datasource/sql/frequency/threshold_pct/channels/recipient_email`。
`kind` 仅允许 `insight/report`，频率仅允许 `hourly/daily/weekly/monthly`，渠道仅允许
`in_app/email/feishu/slack`。客户端不得提交租户、用户、数据库方言、SMTP 凭证或 Webhook URL。

创建阶段校验数据源可见性和真实方言下的单条只读 SQL；每次实际运行仍需重新读取当前用户角色、
数据源权限、列白名单和行过滤，并再次通过统一 SQL 安全执行边界。主动洞察首次成功运行只建立基线，
后续按数值列聚合结果与最近成功基线比较；定时报告使用确定性 Markdown 模板，最多展示 50 行脱敏数据。
外发失败只影响对应渠道，站内结果和运行记录必须保留。任务写操作使用 audit 访问日志。

创建示例：

```http
POST /api/v1/automation/schedules
Content-Type: application/json

{
  "name": "销售日报",
  "kind": "report",
  "datasource": "sales",
  "sql": "SELECT day, SUM(amount) AS sales FROM orders GROUP BY day",
  "frequency": "daily",
  "threshold_pct": 10,
  "channels": ["in_app", "email"],
  "recipient_email": "owner@example.com"
}
```

```json
{
  "id": "7e152d59-80c6-4327-bb5f-21ec3b351328",
  "name": "销售日报",
  "kind": "report",
  "datasource": "sales",
  "dialect": "postgres",
  "frequency": "daily",
  "channels": ["in_app", "email"],
  "enabled": true,
  "next_run_at": "2026-07-31T00:00:00Z",
  "last_run_at": null
}
```

---
