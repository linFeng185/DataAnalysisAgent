# 20. 全流程端到端测试用例

## 20.1 测试准备

### 前置条件

- [x] 5个数据库全部启动且数据已导入
- [ ] 服务已启动：`python -m src.main`
- [ ] 前端可访问：`http://localhost:5173`
- [ ] 数据源已自动注册（启动时加载 `config/datasources.yaml`）

### 数据源清单

| 名称 | 引擎 | 表数 | 行数 |
|------|------|------|------|
| mysql_test | MySQL 8.0 | 13 | 116万 |
| postgres_main | PostgreSQL 16 | 12 | 111万 |
| mssql_express | SQL Server 2019 | 12 | 135万 |
| oracle_xe | Oracle 21c | 12 | 111万 |
| clickhouse_test | ClickHouse 24.8 | 13 | 1111万 |
| **合计** | | **62** | **1584万** |

---

## 20.2 A组 — 基础能力（10题，P0）

### A1: 简单查询

> **提问**："查看前10位客户的姓名、手机号和会员等级"
> **数据源**：mysql_test
> **预期**：返回10行含中文姓名+会员等级，SQL不报错
> **评分**：3=完全正确，2=返回>10行或格式微差，0=报错

### A2: 聚合+JOIN

> **提问**："按商品分类统计销售额，从高到低排序"
> **数据源**：postgres_main
> **预期**：生成3表JOIN SQL，返回分类名+销售额，降序排列
> **评分**：3=SQL含JOIN+GROUP BY+ORDER BY，0=SQL语法错误

### A3: 窗口函数

> **提问**："给客户按累计消费排名，显示前20名"
> **数据源**：mysql_test
> **预期**：SQL含 ROW_NUMBER()/RANK()，返回排名1-20
> **评分**：3=有窗口函数+排序正确，0=无窗口函数

### A4: 子查询

> **提问**："找出消费超过平均消费3倍的客户"
> **数据源**：oracle_xe
> **预期**：SQL含子查询，结果集非空
> **评分**：3=子查询正确，0=语法错误/无子查询

### A5: 时间范围

> **提问**："2025年3月份的订单有多少笔？总金额多少？"
> **数据源**：mssql_express
> **预期**：WHERE order_date BETWEEN，返回计数+求和
> **评分**：3=BETWEEN范围正确，0=日期过滤错误

### A6: 中文LIKE

> **提问**："商品名称里带'空气炸锅'的都有哪些？"
> **数据源**：任意
> **预期**：LIKE '%空气炸锅%'，返回中文商品名
> **评分**：3=中文搜索正常，0=中文乱码/无结果

### A7: CASE WHEN

> **提问**："按累计消费分三层：高(>10万)、中(1-10万)、低(<1万)，统计各层人数"
> **数据源**：postgres_main
> **预期**：SQL含CASE WHEN，返回3行各层人数
> **评分**：3=CASE WHEN+GROUP BY正确，0=表达式错误

### A8: HAVING

> **提问**："找出购买次数超过10次的客户"
> **数据源**：mysql_test
> **预期**：GROUP BY + HAVING count，结果正确
> **评分**：3=HAVING门槛正确，0=无HAVING/语法错

### A9: 5表JOIN

> **提问**："查最近100笔订单完整信息：订单号、客户名、商品名、分类名、供应商名"
> **数据源**：clickhouse_test
> **预期**：5表JOIN(orders/items/products/categories/suppliers)，返回100行
> **评分**：3=5表JOIN全部命中，0=JOIN错误/缺少表

### A10: 跨源汇总

> **提问**："从所有数据库中汇总客户总数"（勾选全部5个数据源）
> **预期**：多源调度触发，各库独立COUNT，合并结果
> **评分**：3=5源并行+结果合并，1=只查了单源，0=报错

---

## 20.3 B组 — 数据分析器（22题，P0）

每个用例测试一个具体处理器。**提问格式为精确输入**，预期命中对应处理器。

| # | 处理器 | 数据源 | 提问 | 预期 |
|---|--------|--------|------|------|
| B1 | trend | clickhouse_test | "分析网站流量的月度趋势" | 返回趋势线数据 chart_type=line |
| B2 | yoy | postgres_main | "对比2024和2025年revenue的同比增长率" | 返回各期YoY百分比 |
| B3 | mom | mysql_test | "分析每月订单金额的环比变化" | 返回月度环比增长率 |
| B4 | distribution | clickhouse_test | "订单金额的分布情况如何" | 自动分桶，返回柱状图 |
| B5 | ranking | mysql_test | "销售额最高的10个商品是哪些" | Top10排名+集中度 |
| B6 | proportion | mssql_express | "各物流公司的运单占比是多少" | 饼图数据 |
| B7 | anomaly | clickhouse_test | "检查库存数据有没有异常值" | 返回异常项列表 |
| B8 | retention | clickhouse_test | "分析用户的7日留存率" | 留存曲线数据 |
| B9 | funnel | clickhouse_test | "从浏览到下单的转化漏斗什么样" | 各环节转化率 |
| B10 | rfm | mysql_test | "对客户做RFM分析" | R/F/M分段结果 |
| B11 | pareto | postgres_main | "按商品做帕累托分析，哪些贡献了80%销售" | 累计占比曲线 |
| B12 | correlation | mysql_test | "营销活动的投入和转化数有相关性吗" | 相关系数 |
| B13 | growth_rate | postgres_main | "季度revenue的增长率是多少" | 各期增长率 |
| B14 | seasonal | clickhouse_test | "订单数据有没有季节性规律" | 趋势+季节+残差 |
| B15 | contribution | mssql_express | "各部门对总销售额的贡献度" | 绝对值+占比 |
| B16 | cross_pivot | clickhouse_test | "按省份和商品分类做交叉分析" | 透视表数据 |
| B17 | ab_test | clickhouse_test | "对比不同广告版本的点击率和转化率" | A/B组对比统计 |
| B18 | budget_variance | postgres_main | "各部门的预算执行偏差有多大" | 预算vs实际 |
| B19 | geo_distribution | mysql_test | "客户在全国各省的分布情况" | 各省客户数 |
| B20 | market_basket | clickhouse_test | "哪些商品经常被一起购买" | 关联规则 |
| B21 | simple_prediction | clickhouse_test | "基于历史数据预测未来30天的订单趋势" | 预测值序列 |
| B22 | aggregation | 任意 | "统计订单总数和平均金额" | 汇总统计 |

---

## 20.4 C组 — 多数据源交叉分析（5题，P1）

### C1: 两库对比

> **操作**：勾选 mysql_test + postgres_main
> **提问**："MySQL和PostgreSQL两个库的客户总数和订单总额有什么不同？"
> **预期**：并行查询两库 → 合并对比 → 给出差异分析

### C2: 三库关联

> **操作**：勾选 clickhouse_test + mysql_test + postgres_main
> **提问**："ClickHouse网站流量的高峰月份和MySQL营销活动的投放月份，以及PostgreSQL对应月份的财务报表收入，三者有什么关联？"
> **预期**：三源并行 → LLM交叉分析 → 指出时间维度上的关联

### C3: 五库全景

> **操作**：勾选全部5个数据源
> **提问**："汇总所有数据库中2024年的总销售额并做对比"
> **预期**：5源并行 → 各库销售额对比表格

### C4: 单库时序大数据

> **操作**：仅选 clickhouse_test
> **提问**："分析过去一年website_traffic的每日UV趋势，找出流量最高的10天"
> **预期**：命中300万行，响应<5s，返回Top10日期+UV

### C5: 跨库差异

> **操作**：全部5源
> **提问**："各数据库customer表的province分布有什么差异？哪个库的广东省客户最多？"
> **预期**：5源并行 → 各省对比 → 指出最大差异

---

## 20.5 D组 — 意图路由（4题，P1）

| # | 操作 | 提问 | 预期路由 |
|---|------|------|----------|
| D1 | 选mysql_test | "mysql_test里有哪些表？" | llm_direct_answer（metadata意图） |
| D2 | 不选数据源 | "你能帮我做哪些数据分析？" | llm_direct_answer（chat意图） |
| D3 | 选mysql_test | "各品类销售额排名" | retrieve_schema→SQL主路径 |
| D4 | 勾选2个以上数据源 | 任意问题 | 自动 multi_source_dispatch |

---

## 20.6 E组 — 安全（4题，P1）

| # | 操作 | 预期结果 |
|---|------|----------|
| E1 | 输入 `"客户的手机号？'; DROP TABLE customers; --"` | 安全阻断，SQL不执行 |
| E2 | 输入 `"帮我 ALTER TABLE customers ADD test VARCHAR"` | 安全阻断，DDL不可执行 |
| E3 | 不带Token访问 `GET /api/v1/sessions` | 单租户：正常返回；多租户：401 |
| E4 | 不带X-Admin-Key访问 `DELETE /api/v1/knowledge/docs/test` | 返回401 "需要X-Admin-Key" |

---

## 20.7 F组 — 会话管理（5题，P1）

| # | 操作步骤 | 预期 |
|---|----------|------|
| F1 | 对话分析页→选mysql_test→输入"你好" | 新session_id分配，GET /sessions可查到 |
| F2 | 基于F1继续→"统计客户总数"→"按省份分组" | 第3问能引用前两问上下文，SQL基于已有结论 |
| F3 | 输入问题→切到"数据源管理"→切回"对话分析" | 对话记录不丢失，session_id不变 |
| F4 | 完成多轮对话→点"历史会话"→选之前的会话 | 列表倒序展示→选择后完整恢复→可继续提问 |
| F5 | 历史会话列表滚动到底部 | 自动加载下一页（瀑布流分页） |

---

## 20.8 G组 — MCP文件分析（3题，P1）

| # | 操作步骤 | 预期 |
|---|----------|------|
| G1 | MCP管理页→上传`tests/fixtures/mcp/sample_sales.csv` | 上传成功，文件列表显示 |
| G2 | 对话分析页输入"分析sample_sales.csv销售数据" | intent=file_analysis→mcp_agent→读取并分析文件 |
| G3 | 上传`business_rules.txt`到知识库→问"公司退货规则是什么？" | 语义检索命中→回复引用文档内容 |

---

## 20.9 H组 — 性能（5题，P2）

| # | 操作 | 预期阈值 |
|---|------|----------|
| H1 | clickhouse_test：`"统计user_behavior_logs每种事件类型的数量"` | 命中500万行，<3s |
| H2 | clickhouse_test：`"orders和order_items按月统计GMV"` | JOIN 70万行，<3s |
| H3 | `ab -n 100 -c 10 http://localhost:8000/api/v1/health` | 全部200，无超时 |
| H4 | 发数据分析请求，观察SSE事件流 | 首字节<2s |
| H5 | 50+轮对话，向上滚动加载历史 | 不卡顿，滚动位置稳定 |

---

## 20.10 评分标准

| 分数 | 含义 |
|------|------|
| 3 | 完全符合预期 |
| 2 | 基本符合，有小瑕疵 |
| 1 | 部分符合，有明显偏差 |
| 0 | 不通过（报错/崩溃/结果完全错） |

### 通过线

| 分组 | 题数 | 满分 | 通过线 |
|------|------|------|--------|
| A 基础能力 | 10 | 30 | ≥24 (80%) |
| B 数据处理器 | 22 | 66 | ≥53 (80%) |
| C 多源交叉 | 5 | 15 | ≥12 (80%) |
| D 意图路由 | 4 | 12 | ≥10 |
| E 安全 | 4 | 12 | =12 (100%) |
| F 会话管理 | 5 | 15 | ≥12 |
| G MCP文件 | 3 | 9 | ≥7 |
| H 性能 | 5 | 15 | ≥12 |
| **总计** | **58** | **174** | **≥142** |

---

## 20.11 执行步骤

```bash
# Step 1: 启动服务
python -m src.main

# Step 2: 前端（另一终端）
cd frontend && npm run dev

# Step 3: 按 A→H 顺序逐题测试，记录得分

# Step 4: 运行快速验证（不依赖LLM）
python tests/test_e2e_quick.py 2>&1 | tee test_results_quick.log

# Step 5: 统计分数
# 将各组得分填入上述表格，对比通过线
```
