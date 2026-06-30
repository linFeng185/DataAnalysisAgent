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

---
