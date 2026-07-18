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
- `sql_reasoning_content`：该轮 SQL 推理文本（存在时）；
- `success`、`error_message`：该轮结束状态。

禁止只为最后一轮补充富数据，也禁止把 `latest_state` 注入所有轮次。`latest_state` 仅保留给旧客户端，
新客户端必须逐轮消费 `turn.final_result`。首次打开长会话返回最新 20 轮，`has_more=true` 时通过
`before=<当前最早 turn_id>` 向前分页。旧记录没有结构化响应时，允许退化为摘要和已有 SQL，但不得
用其他轮次的 SQL、数据或图表补齐。

---
