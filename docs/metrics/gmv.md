---
category: business_rule
tags: [demo, orders]
tables: [orders]
---

# GMV（总交易额）

## 定义
GMV = SUM(orders.amount) WHERE status != '''cancelled'''

## 说明
GMV 是衡量平台交易规模的核心指标。计算公式：所有非取消订单的金额总和。

## 关联字段
- orders.amount: Float64 - 订单金额
- orders.status: String - 订单状态，取值 pending/paid/shipped/cancelled
