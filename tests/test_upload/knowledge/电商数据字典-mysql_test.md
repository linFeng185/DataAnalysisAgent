# 电商数据字典 — MySQL 测试库

> 数据源: `mysql_test`（MySQL 8.0 @ 192.168.195.133:3306/test）
> 数据范围: 2024-07-01 ~ 2028-06-30
> 方言: MySQL

---

## 表结构

### 1. categories — 商品分类

| 字段 | 类型 | 说明 |
|------|------|------|
| `category_id` | INT PK | 分类 ID |
| `name` | VARCHAR(50) | 分类名称 |

### 2. products — 商品

| 字段 | 类型 | 说明 |
|------|------|------|
| `product_id` | INT PK | 商品 ID |
| `category_id` | INT FK | 所属分类 |
| `name` | VARCHAR(200) | 商品名称 |
| `price` | DECIMAL(10,2) | 单价，10% 高价值品 500-5000，90% 普品 10-500 |

### 3. users — 用户

| 字段 | 类型 | 说明 |
|------|------|------|
| `user_id` | INT PK | 用户 ID |
| `name` | VARCHAR(100) | 用户姓名 |
| `vip_level` | TINYINT | 0=普通(60%)，1=银卡(25%)，2=金卡(10%)，3=钻石(5%) |
| `register_date` | DATE | 注册日期 |

### 4. orders — 订单

| 字段 | 类型 | 说明 |
|------|------|------|
| `order_id` | INT PK | 订单 ID |
| `user_id` | INT FK | 下单用户 |
| `order_date` | DATE | 下单日期 |
| `status` | VARCHAR(20) | completed(85%), cancelled(10%), refunded(5%) |

### 5. order_items — 订单明细

| 字段 | 类型 | 说明 |
|------|------|------|
| `item_id` | INT PK | 明细 ID |
| `order_id` | INT FK | 所属订单 |
| `product_id` | INT FK | 商品 |
| `quantity` | INT | 数量 1~5 |
| `unit_price` | DECIMAL(10,2) | 下单时单价 |

### 6. user_level_log — 等级变更记录

| 字段 | 类型 | 说明 |
|------|------|------|
| `log_id` | INT PK | 记录 ID |
| `user_id` | INT FK | 用户 |
| `old_level` | TINYINT | 变更前等级 |
| `new_level` | TINYINT | 变更后等级（>old_level） |
| `change_date` | DATE | 变更日期 |

---

## MySQL 方言要点

- 日期格式化: `DATE_FORMAT(order_date, '%Y-%m')`
- NULL 处理: `IFNULL(col, default)`
- 字符串聚合: `GROUP_CONCAT(col SEPARATOR ',')`
- 分页: `LIMIT n OFFSET m`
- 窗口函数: MySQL 8.0 支持 `RANK()`, `ROW_NUMBER()`, `DENSE_RANK()`
- **无 `created_at` 列**，日期用 `order_date` 或 `register_date`

## 核心指标

| 指标 | 定义 |
|------|------|
| 销售额 | `SUM(oi.quantity * oi.unit_price)` WHERE `o.status = 'completed'` |
| 客单价 | 销售额 / `COUNT(DISTINCT o.user_id)` |
| 复购率 | 下单≥2次的用户 / 总用户 |
| 退款率 | `COUNT(*) FILTER(status='refunded')` / `COUNT(*)` |

## 注意事项

- **销售额必须过滤 `status = 'completed'`**
- 日期字段为 `DATE` 类型，字符串比较 `>= '2026-06-01' AND < '2026-07-01'`
- 金额列 `DECIMAL(10,2)`，汇总后精度正常
