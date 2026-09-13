# 电商数据字典 — Oracle XE 测试库

> 数据源: `oracle_xe`（Oracle 21c XE @ 192.168.195.133:1521/XEPDB1）
> 用户: TEST_USER
> 方言: Oracle
> 适用范围: 零售业务域 12 张小写表（orders / products / customers / stores / payments 等）

---

## ⚠️ 关键：本库有两套同名表，先读这一节

Oracle 库同时存在**两套**电商表，名称大小写不同、结构完全不同，**不要混用**：

| | **小写新表（业务分析请用这套）** | 大写老表（历史样例，数据量小） |
|---|---|---|
| 表名 | `orders`、`products`、`customers`、`stores`、`payments`… | `ORDERS`、`PRODUCTS`、`USERS`、`CATEGORIES`… |
| 订单主键 | `id` | `ORDER_ID` |
| 订单金额 | `total_amount`、`discount_amount` | 无金额列 |
| 订单状态 | `order_status` | `status` |
| 日期列类型 | **VARCHAR2 文本 `'YYYY-MM-DD'`** | 真正的 DATE |

**写 SQL 一律用小写表名**（如 `orders`）。Oracle 对未加引号的标识符统一转大写，
`FROM orders` 会命中 `ORDERS` 老表，而老表没有 `total_amount` 等列。

### 日期列陷阱（必须遵守）

小写表的日期列是 **VARCHAR2 文本**（不是 DATE），**且各列格式不统一**，禁止直接套日期函数：

| 列 | 实际存储格式 | 示例 |
|---|---|---|
| `orders.order_date` | `YYYY-MM-DD` | `'2026-09-13'` |
| `orders.order_time` | `HH24:MI:SS`（**纯时间，无日期部分**） | `'14:30:00'` |
| `payments.payment_time` | `YYYY-MM-DD HH24:MI:SS`（**带时间，19 位**） | `'2026-09-13 14:30:00'` |
| `customers.register_date` / `customers.birth_date` | `YYYY-MM-DD` | `'2026-09-13'` |
| `stores.open_date` | `YYYY-MM-DD` | `'2016-01-03'` |

**推荐写法（对所有格式都安全）**：利用 ISO 格式"字典序 = 时间序"的特性，用字符串比较与截取，
完全绕开 `TO_DATE` 格式串风险：

| 场景 | 推荐写法 |
|---|---|
| 最近 90 天 | `o.order_date >= TO_CHAR(SYSDATE - 90, 'YYYY-MM-DD')` |
| 按月分组 | `SUBSTR(o.order_date, 1, 7) AS year_month` |
| 跨月对齐（含时间列） | `SUBSTR(p.payment_time, 1, 7) AS year_month` |

**若必须用 TO_DATE，格式串必须与值严格匹配**，否则报错：
- `TO_DATE(payment_time, 'YYYY-MM-DD')` → **ORA-01830**（格式串比输入短，输入还有剩余）
- `TO_DATE(order_date, 'YYYY-MM-DD HH24:MI:SS')` → **ORA-01840**（格式串比输入长）
- `TO_DATE(order_time, ...)` 对纯时间列**无法转为日期**，只能按字符串使用

其余错误写法：
- `TRUNC(o.order_date, 'MM')`（VARCHAR2 直用日期函数）→ **ORA-01722**
- `TO_CHAR(o.order_date, 'YYYY-MM')` → format 被静默忽略，返回原字符串（结果错误但不报错）

---

## 小写新表（业务分析主数据）

### orders — 订单

| 字段 | 类型 | 说明 |
|------|------|------|
| `id` | NUMBER | 订单 ID |
| `customer_id` | NUMBER | 客户 ID（关联 customers.id） |
| `store_id` | NUMBER | 门店 ID（关联 stores.id） |
| `order_date` | VARCHAR2 | 下单日期，格式 `YYYY-MM-DD` ⚠️ 文本 |
| `order_time` | VARCHAR2 | 下单时间，格式 `HH24:MI:SS`（纯时间）⚠️ 文本 |
| `order_status` | VARCHAR2 | 订单状态（如 completed / cancelled / returned） |
| `payment_method` | VARCHAR2 | 支付方式 |
| `total_amount` | NUMBER | 订单总额 |
| `discount_amount` | NUMBER | 折扣金额 |
| `notes` | VARCHAR2 | 备注 |

### customers — 客户（新表没有 users 表）

| 字段 | 类型 | 说明 |
|------|------|------|
| `id` | NUMBER | 客户 ID |
| `name` | VARCHAR2 | 姓名 |
| `gender` | VARCHAR2 | 性别 |
| `birth_date` | VARCHAR2 | 出生日期，格式 `YYYY-MM-DD` ⚠️ 文本 |
| `register_date` | VARCHAR2 | 注册日期，格式 `YYYY-MM-DD` ⚠️ 文本 |
| `customer_level` | VARCHAR2 | 客户等级 |
| `province` / `city` | VARCHAR2 | 省 / 市 |
| `total_spent` | NUMBER | 累计消费 |

### stores — 门店

| 字段 | 类型 | 说明 |
|------|------|------|
| `id` | NUMBER | 门店 ID |
| `store_name` | VARCHAR2 | 门店名称 |
| `store_type` | VARCHAR2 | 门店类型 |
| `province` / `city` / `address` | VARCHAR2 | 地区信息 |
| `open_date` | VARCHAR2 | 开业日期，格式 `YYYY-MM-DD` ⚠️ 文本 |
| `area_sqm` / `staff_count` | NUMBER | 面积 / 员工数 |

### products — 商品

| 字段 | 类型 | 说明 |
|------|------|------|
| `id` | NUMBER | 商品 ID |
| `product_name` | VARCHAR2 | 商品名称 |
| `brand` / `specification` | VARCHAR2 | 品牌 / 规格 |
| `category_id` | NUMBER | 分类 ID |
| `supplier_id` | NUMBER | 供应商 ID |
| `unit_price` / `cost_price` | NUMBER | 售价 / 成本价 |
| `weight_kg` | NUMBER | 重量 |
| `is_active` | NUMBER | 是否在售（1/0） |

### categories — 商品分类

| 字段 | 类型 | 说明 |
|------|------|------|
| `id` | NUMBER | 分类 ID |
| `category_name` | VARCHAR2 | 分类名称 |
| `parent_id` | NUMBER | 父分类 ID（自关联） |
| `sort_order` | NUMBER | 排序 |
| `description` | VARCHAR2 | 描述 |

### order_items — 订单明细

| 字段 | 类型 | 说明 |
|------|------|------|
| `id` | NUMBER | 明细 ID |
| `order_id` | NUMBER | 订单 ID |
| `product_id` | NUMBER | 商品 ID |
| `warehouse_id` | NUMBER | 仓库 ID |
| `quantity` | NUMBER | 数量 |
| `unit_price` | NUMBER | 下单时单价 |
| `discount` | NUMBER | 折扣 |
| `subtotal` | NUMBER | 小计 |

### payments — 支付

| 字段 | 类型 | 说明 |
|------|------|------|
| `id` | NUMBER | 支付 ID |
| `order_id` | NUMBER | 订单 ID |
| `transaction_id` | VARCHAR2 | 交易流水号 |
| `amount` | NUMBER | 支付金额 |
| `payment_method` | VARCHAR2 | 支付方式 |
| `payment_time` | VARCHAR2 | 支付时间，格式 `YYYY-MM-DD HH24:MI:SS`（含时间）⚠️ 文本 |
| `status` | VARCHAR2 | 支付状态 |
| `failure_reason` | VARCHAR2 | 失败原因 |

> 其余小写表：`contracts`、`employees`、`hr_records`、`inventory`、`suppliers`。

---

## 大写老表（历史样例，一般不用）

| 表 | 主键 | 关键列 |
|----|------|--------|
| `CATEGORIES` | CATEGORY_ID | NAME |
| `PRODUCTS` | PRODUCT_ID | NAME、PRICE、CATEGORY_ID |
| `USERS` | USER_ID | NAME、VIP_LEVEL、REGISTER_DATE（DATE） |
| `ORDERS` | ORDER_ID | USER_ID、ORDER_DATE（DATE）、STATUS |
| `ORDER_ITEMS` | ITEM_ID | ORDER_ID、PRODUCT_ID、QUANTITY、UNIT_PRICE |
| `USER_LEVEL_LOG` | LOG_ID | USER_ID、OLD_LEVEL、NEW_LEVEL、CHANGE_DATE（DATE） |

> 大写老表的日期列是真正的 DATE 类型，可以直接 `TRUNC(ORDER_DATE, 'MM')`。
> 与 小写新表区分：老表用大写主键（`ORDER_ID`），新表用 `id`。

---

## Oracle 方言要点

- 日期截断: `TRUNC(dt, 'MM')` / `TRUNC(dt)`
- 日期格式化: `TO_CHAR(dt, 'YYYY-MM-DD')`
- **文本日期列（VARCHAR2）必须先 `TO_DATE(col, 'YYYY-MM-DD')` 再套日期函数**
- NULL 处理: `NVL(col, default)` / `NVL2(col, val, default)`
- 字符串聚合: `LISTAGG(col, ',') WITHIN GROUP (ORDER BY col)`
- 分页: `OFFSET n ROWS FETCH NEXT m ROWS ONLY`（12c+）
- 日期运算: `dt + 1`（加一天），`ADD_MONTHS(dt, 1)`
- 安全除法: `num / NULLIF(den, 0)`

## 注意事项

- Oracle 中空字符串 `''` 等价于 `NULL`
- `DATE` 类型同时包含日期和时间（精确到秒）
- **禁止跨两套表混用列名**：`ORDERS.STATUS` ≠ `orders.order_status`，
  同一条 SQL 里必须只使用一套表的列名
- 金额 `NUMBER` 精度可靠，展示时可用 `ROUND(x, 2)`
- 其它业务域（`workforce_*` 等）存在 NVARCHAR2 列，与 VARCHAR2 混用做 UNION 或比较
  会报 ORA-12704，需 `CAST(col AS VARCHAR2(n))` 或 `TO_CHAR(col)` 统一字符集
- 跨域按月份对齐时：`workforce_*` 的 `year_month` 是 NVARCHAR2 存 `'YYYY-MM'`，
  与零售表的文本日期列做 UNION 前必须 `CAST(列 AS VARCHAR2(20))` 统一字符集
