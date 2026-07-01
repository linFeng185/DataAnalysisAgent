# 电商数据字典 — ClickHouse 生产库

> 数据源: `clickhouse_prod`（ClickHouse @ 10.0.1.100:9000/analytics）
> 环境: 生产，只读
> 方言: ClickHouse

---

## 表结构

### 1. categories — 商品分类

| 字段 | 类型 | 说明 |
|------|------|------|
| `category_id` | UInt32 | 分类 ID |
| `name` | String | 分类名称 |

### 2. products — 商品

| 字段 | 类型 | 说明 |
|------|------|------|
| `product_id` | UInt32 | 商品 ID |
| `category_id` | UInt32 | 所属分类 |
| `name` | String | 商品名称 |
| `price` | Decimal(10,2) | 单价 |

### 3. users — 用户

| 字段 | 类型 | 说明 |
|------|------|------|
| `user_id` | UInt32 | 用户 ID |
| `name` | String | 用户姓名 |
| `vip_level` | UInt8 | 0=普通, 1=银卡, 2=金卡, 3=钻石 |
| `register_date` | Date | 注册日期 |

### 4. orders — 订单

| 字段 | 类型 | 说明 |
|------|------|------|
| `order_id` | UInt32 | 订单 ID |
| `user_id` | UInt32 | 下单用户 |
| `order_date` | Date | 下单日期 |
| `status` | String | completed, cancelled, refunded |

### 5. order_items — 订单明细

| 字段 | 类型 | 说明 |
|------|------|------|
| `item_id` | UInt32 | 明细 ID |
| `order_id` | UInt32 | 所属订单 |
| `product_id` | UInt32 | 商品 |
| `quantity` | UInt8 | 数量 1~5 |
| `unit_price` | Decimal(10,2) | 下单时单价 |

### 6. user_level_log — 等级变更记录

| 字段 | 类型 | 说明 |
|------|------|------|
| `log_id` | UInt32 | 记录 ID |
| `user_id` | UInt32 | 用户 |
| `old_level` | UInt8 | 变更前等级 |
| `new_level` | UInt8 | 变更后等级 |
| `change_date` | Date | 变更日期 |

---

## ClickHouse 方言要点

- 日期截断: `toStartOfMonth(order_date)` / `toStartOfDay(order_date)`
- 日期格式化: `formatDateTime(order_date, '%Y-%m-%d')`
- NULL 处理: `ifNull(col, default)`
- 聚合数组: `groupArray(col)`
- LIMIT: `LIMIT n`（不支持 OFFSET，需用 `LIMIT n OFFSET m` 仅新版本支持）
- **不支持窗口函数**（低版本）或有限支持，排名需用 `arrayEnumerate` + `arraySort`
- 大表查询**必须**带时间范围过滤
- `GROUP BY` 不支持别名
- 金额用 `toDecimal64()` 显式转换

## 注意事项

- 生产库只读，禁止写操作
- 列式存储，**禁止 `SELECT *`**
- 时间过滤用 `toDate()` 转换字符串为 Date 类型
- **无 `created_at` 列**，日期用 `order_date` 或 `register_date`
