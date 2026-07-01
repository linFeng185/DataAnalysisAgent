# 电商数据字典 — PostgreSQL 主库

> 数据源: `postgres_main`（PostgreSQL 16 @ 192.168.195.133:5432/postgres）
> 方言: PostgreSQL

---

## 表结构

### 1. categories — 商品分类

| 字段 | 类型 | 说明 |
|------|------|------|
| `category_id` | INTEGER PK | 分类 ID |
| `name` | VARCHAR(50) | 分类名称 |

### 2. products — 商品

| 字段 | 类型 | 说明 |
|------|------|------|
| `product_id` | INTEGER PK | 商品 ID |
| `category_id` | INTEGER FK | 所属分类 |
| `name` | VARCHAR(200) | 商品名称 |
| `price` | NUMERIC(10,2) | 单价 |

### 3. users — 用户

| 字段 | 类型 | 说明 |
|------|------|------|
| `user_id` | INTEGER PK | 用户 ID |
| `name` | VARCHAR(100) | 用户姓名 |
| `vip_level` | SMALLINT | 0=普通, 1=银卡, 2=金卡, 3=钻石 |
| `register_date` | DATE | 注册日期 |

### 4. orders — 订单

| 字段 | 类型 | 说明 |
|------|------|------|
| `order_id` | INTEGER PK | 订单 ID |
| `user_id` | INTEGER FK | 下单用户 |
| `order_date` | DATE | 下单日期 |
| `status` | VARCHAR(20) | completed(85%), cancelled(10%), refunded(5%) |

### 5. order_items — 订单明细

| 字段 | 类型 | 说明 |
|------|------|------|
| `item_id` | INTEGER PK | 明细 ID |
| `order_id` | INTEGER FK | 所属订单 |
| `product_id` | INTEGER FK | 商品 |
| `quantity` | INTEGER | 数量 1~5 |
| `unit_price` | NUMERIC(10,2) | 下单时单价 |

### 6. user_level_log — 等级变更记录

| 字段 | 类型 | 说明 |
|------|------|------|
| `log_id` | INTEGER PK | 记录 ID |
| `user_id` | INTEGER FK | 用户 |
| `old_level` | SMALLINT | 变更前等级 |
| `new_level` | SMALLINT | 变更后等级 |
| `change_date` | DATE | 变更日期 |

---

## PostgreSQL 方言要点

- 日期截断: `DATE_TRUNC('month', order_date)`
- 日期格式化: `TO_CHAR(order_date, 'YYYY-MM-DD')`
- NULL 处理: `COALESCE(col, default)`
- 字符串聚合: `STRING_AGG(col, ',')`
- 分页: `LIMIT n OFFSET m`
- 数组展开: `UNNEST(arr)`
- 窗口函数: 完全支持
- 类型转换: `col::INTEGER`

## 注意事项

- 表名和列名在 PG 中默认转为小写（除非双引号包裹）
- **无 `created_at` 列**，日期用 `order_date` 或 `register_date`
- 金额用 `NUMERIC` 类型，精度高，无需额外 ROUND
