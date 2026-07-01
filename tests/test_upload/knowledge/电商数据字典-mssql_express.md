# 电商数据字典 — SQL Server 测试库

> 数据源: `mssql_express`（SQL Server 2019 Express @ 192.168.195.133:1433/master）
> 用户: sa
> 方言: T-SQL / MSSQL

---

## 表结构

### 1. categories — 商品分类

| 字段 | 类型 | 说明 |
|------|------|------|
| `category_id` | INT PK | 分类 ID |
| `name` | NVARCHAR(50) | 分类名称 |

### 2. products — 商品

| 字段 | 类型 | 说明 |
|------|------|------|
| `product_id` | INT PK | 商品 ID |
| `category_id` | INT FK | 所属分类 |
| `name` | NVARCHAR(200) | 商品名称 |
| `price` | DECIMAL(10,2) | 单价 |

### 3. users — 用户

| 字段 | 类型 | 说明 |
|------|------|------|
| `user_id` | INT PK | 用户 ID |
| `name` | NVARCHAR(100) | 用户姓名 |
| `vip_level` | TINYINT | 0=普通, 1=银卡, 2=金卡, 3=钻石 |
| `register_date` | DATE | 注册日期 |

### 4. orders — 订单

| 字段 | 类型 | 说明 |
|------|------|------|
| `order_id` | INT PK | 订单 ID |
| `user_id` | INT FK | 下单用户 |
| `order_date` | DATE | 下单日期 |
| `status` | NVARCHAR(20) | completed(85%), cancelled(10%), refunded(5%) |

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
| `new_level` | TINYINT | 变更后等级 |
| `change_date` | DATE | 变更日期 |

---

## SQL Server 方言要点

- 日期截断: `CAST(order_date AS DATE)` / `DATEFROMPARTS(YEAR(d), MONTH(d), 1)`
- 日期格式化: `FORMAT(order_date, 'yyyy-MM-dd')` / `CONVERT(VARCHAR, order_date, 23)`
- NULL 处理: `ISNULL(col, default)` / `COALESCE(col, default)`
- 字符串聚合: `STRING_AGG(col, ',')`（2017+）
- 分页: `OFFSET n ROWS FETCH NEXT m ROWS ONLY`（2012+）
- 窗口函数: 完全支持 `RANK()`, `ROW_NUMBER()`, `DENSE_RANK()`
- TOP: `SELECT TOP 1000 * FROM orders`

## 注意事项

- 表名可能使用 `dbo.` schema 前缀
- `NVARCHAR` 列比较时注意 `N'中文'` 前缀
- 金额 `DECIMAL(10,2)` 精度可靠
- `DATE` 类型不含时间部分
- 字符串比较默认不区分大小写（取决于 collation）
- **无 `created_at` 列**，日期用 `order_date` 或 `register_date`
