# 6. 数据流示例

## 6. 数据流示例

### 场景：用户问"上个月哪些用户消费最高，哪个品类卖得最好？"

```
Step 1 — 意图识别
  LLM: 判定为"聚合排名 + 多维度分析"问题

Step 2 — Schema检索
  搜索"用户消费"→ user_orders 表，"品类"→ product_categories 表
  返回表结构 + JOIN 关系

Step 3 — SQL生成
  LLM 生成:
  SELECT u.user_id, u.user_name, SUM(o.amount) AS total_spent,
         p.category_name, COUNT(*) AS order_count
  FROM user_orders o
  JOIN users u ON o.user_id = u.user_id
  JOIN products p ON o.product_id = p.product_id
  WHERE o.created_at >= '2026-05-01' AND o.created_at < '2026-06-01'
  GROUP BY u.user_id, u.user_name, p.category_name
  ORDER BY total_spent DESC

Step 4 — 安全校验
  校验通过（SELECT 语句，带时间范围过滤）

Step 5 — 执行
  ClickHouse 返回结果 → pandas DataFrame

Step 6 — 分析
  LLM 解读结果:
  - Top 10 消费者排名
  - 各品类销售额分布
  - 计算集中度（Top 10 贡献了 X% 的销售额）
  - 对比上上月发现趋势变化

Step 7 — 可视化
  自动生成: 柱状图（Top10消费）+ 饼图（品类占比）

Step 8 — 归档
  将 SQL 模板和分析结论存入知识库
```

---
