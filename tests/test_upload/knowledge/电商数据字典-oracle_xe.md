# 电商数据字典 — Oracle XE 测试库

> 数据源: `oracle_xe`（Oracle 21c XE @ 192.168.195.133:1521/XEPDB1）
> 用户: TEST_USER
> 方言: Oracle

---

## 表结构

### 1. categories — 商品分类

| 字段 | 类型 | 说明 |
|------|------|------|
| `category_id` | NUMBER(10) PK | 分类 ID |
| `name` | VARCHAR2(50) | 分类名称 |

### 2. products — 商品

| 字段 | 类型 | 说明 |
|------|------|------|
| `product_id` | NUMBER(10) PK | 商品 ID |
| `category_id` | NUMBER(10) FK | 所属分类 |
| `name` | VARCHAR2(200) | 商品名称 |
| `price` | NUMBER(10,2) | 单价 |

### 3. users — 用户

| 字段 | 类型 | 说明 |
|------|------|------|
| `user_id` | NUMBER(10) PK | 用户 ID |
| `name` | VARCHAR2(100) | 用户姓名 |
| `vip_level` | NUMBER(1) | 0=普通, 1=银卡, 2=金卡, 3=钻石 |
| `register_date` | DATE | 注册日期 |

### 4. orders — 订单

| 字段 | 类型 | 说明 |
|------|------|------|
| `order_id` | NUMBER(10) PK | 订单 ID |
| `user_id` | NUMBER(10) FK | 下单用户 |
| `order_date` | DATE | 下单日期 |
| `status` | VARCHAR2(20) | completed(85%), cancelled(10%), refunded(5%) |

### 5. order_items — 订单明细

| 字段 | 类型 | 说明 |
|------|------|------|
| `item_id` | NUMBER(10) PK | 明细 ID |
| `order_id` | NUMBER(10) FK | 所属订单 |
| `product_id` | NUMBER(10) FK | 商品 |
| `quantity` | NUMBER(3) | 数量 1~5 |
| `unit_price` | NUMBER(10,2) | 下单时单价 |

### 6. user_level_log — 等级变更记录

| 字段 | 类型 | 说明 |
|------|------|------|
| `log_id` | NUMBER(10) PK | 记录 ID |
| `user_id` | NUMBER(10) FK | 用户 |
| `old_level` | NUMBER(1) | 变更前等级 |
| `new_level` | NUMBER(1) | 变更后等级 |
| `change_date` | DATE | 变更日期 |

---

## Oracle 方言要点

- 日期截断: `TRUNC(order_date, 'MM')` / `TRUNC(order_date)`
- 日期格式化: `TO_CHAR(order_date, 'YYYY-MM-DD')`
- NULL 处理: `NVL(col, default)` / `NVL2(col, val, default)`
- 字符串聚合: `LISTAGG(col, ',') WITHIN GROUP (ORDER BY col)`
- 分页: `OFFSET n ROWS FETCH NEXT m ROWS ONLY`（12c+）/ `ROWNUM`（旧版）
- 日期运算: `order_date + 1`（加一天），`ADD_MONTHS(order_date, 1)`
- 字符串比较区分大小写，用 `UPPER()` 统一

## 注意事项

- Oracle 中空字符串 `''` 等价于 `NULL`
- `DATE` 类型同时包含日期和时间（精确到秒）
- 分页可用 `ROWNUM` 或 `ROW_NUMBER() OVER()`
- 金额 `NUMBER(10,2)` 精度可靠
- **无 `created_at` 列**，日期用 `order_date` 或 `register_date`
