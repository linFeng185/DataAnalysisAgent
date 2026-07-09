# 19. 全流程端到端测试计划

## 19.1 测试范围

本计划覆盖 Data Analysis Agent 的全部核心功能链路，使用大规模中文测试数据（22 张表、千万级数据、5 种数据库引擎），重点验证多数据源交叉分析能力。

## 19.2 测试环境

### 19.2.1 数据库连接（5 种引擎）

| 引擎 | 库名 | 版本 | 数据量级 |
|------|------|------|----------|
| ClickHouse | analytics | 24.x | ~1000万行 |
| MySQL | test | 8.0 | ~250万行 |
| PostgreSQL | postgres | 16 | ~250万行 |
| Oracle | XEPDB1 | 21c | ~250万行 |
| SQL Server | master | 2019 | ~250万行 |

### 19.2.2 数据分布策略

- **共享表**（10 张）：每个数据库都有，结构相同但数据不同，用于多数据源交叉分析
- **专属表**（12 张）：每个数据库 2-3 张，验证单库特有查询场景

### 19.2.3 表清单（22 张表）

#### A. 共享表（10 张 — 每库都有，数据独立生成）

| # | 表名 | 中文名 | 单库行数 | 5库总行数 |
|---|------|--------|----------|-----------|
| 1 | customers | 客户 | 20万 | 100万 |
| 2 | products | 商品 | 5万 | 25万 |
| 3 | orders | 订单 | 50万 | 250万 |
| 4 | order_items | 订单明细 | 200万 | 1000万 |
| 5 | categories | 商品分类 | 500 | 2500 |
| 6 | suppliers | 供应商 | 1万 | 5万 |
| 7 | employees | 员工 | 5千 | 2.5万 |
| 8 | stores | 门店 | 2千 | 1万 |
| 9 | inventory | 库存 | 10万 | 50万 |
| 10 | payments | 支付记录 | 50万 | 250万 |

#### B. 专属表（12 张 — 仅特定数据库有）

| # | 表名 | 中文名 | 所属库 | 行数 |
|---|------|--------|--------|------|
| 11 | website_traffic | 网站流量日志 | ClickHouse | 300万 |
| 12 | ad_impressions | 广告曝光记录 | ClickHouse | 200万 |
| 13 | marketing_campaigns | 营销活动 | MySQL | 5千 |
| 14 | product_reviews | 商品评价 | MySQL | 20万 |
| 15 | financial_reports | 财务报表 | PostgreSQL | 5千 |
| 16 | budget_plans | 预算计划 | PostgreSQL | 2千 |
| 17 | hr_records | 人事档案 | Oracle | 1万 |
| 18 | contracts | 合同表 | Oracle | 5千 |
| 19 | shipping_records | 物流记录 | SQL Server | 50万 |
| 20 | customer_tickets | 客服工单 | SQL Server | 10万 |
| 21 | user_behavior_logs | 用户行为日志 | ClickHouse | 500万 |
| 22 | sales_targets | 销售目标 | MySQL | 2万 |

**总数据量**: ~2400万行

## 19.3 测试用例清单

### 测试分组 A：基础能力验证（P0 — 必须全部通过）

| ID | 测试场景 | 数据表 | 验证点 |
|----|----------|--------|--------|
| A1 | 简单查询 | customers | SQL生成 + 执行 + 结果返回 |
| A2 | 聚合查询 | orders + products | 3表JOIN + GROUP BY |
| A3 | 窗口函数 | orders + customers | ROW_NUMBER / RANK |
| A4 | 子查询 | orders + customers | WHERE IN (subquery) |
| A5 | 时间范围 | orders | BETWEEN + 日期函数 |
| A6 | 中文搜索 | products | LIKE + 中文全文 |
| A7 | CASE WHEN | customers + orders | CASE WHEN + GROUP BY |
| A8 | HAVING | order_items | HAVING 子句 |
| A9 | 5表JOIN | 全链路 | orders→items→products→categories→suppliers |
| A10 | UNION ALL | 跨库 | UNION ALL 合并 |

### 测试分组 B：数据分析器验证（P0 — 22 个处理器）

| ID | 处理器 | 测试表 | 验证输出 |
|----|--------|--------|----------|
| B1 | trend | website_traffic | 趋势线数据 |
| B2 | yoy | financial_reports | 同比增长率 |
| B3 | mom | orders | 环比增长率 |
| B4 | distribution | products + orders | 销售分布 |
| B5 | ranking | customers + orders | 客户排名 |
| B6 | proportion | order_items | 品类占比 |
| B7 | anomaly | inventory | 异常库存 |
| B8 | retention | user_behavior_logs | 留存率 |
| B9 | funnel | website_traffic | 转化漏斗 |
| B10 | rfm | customers + orders | RFM分段 |
| B11 | pareto | products + orders | 帕累托分析 |
| B12 | correlation | marketing_campaigns + orders | 相关系数 |
| B13 | growth_rate | financial_reports | 增长率 |
| B14 | seasonal | orders | 季节性分解 |
| B15 | contribution | order_items | 贡献度分析 |
| B16 | cross_pivot | orders + products + customers | 交叉透视 |
| B17 | ab_test | ad_impressions | A/B测试 |
| B18 | budget_variance | budget_plans + financial_reports | 预算偏差 |
| B19 | geo_distribution | customers + stores | 地理分布 |
| B20 | market_basket | order_items | 购物篮分析 |
| B21 | simple_prediction | orders | 简单预测 |
| B22 | aggregation | orders | 通用聚合 |

### 测试分组 C：多数据源交叉分析（P1 — 核心验证）

| ID | 场景 | 使用库 | 验证点 |
|----|------|--------|--------|
| C1 | 两库关联 | MySQL + PostgreSQL | asyncio.gather 并行调度 |
| C2 | 三库交叉 | CK + MySQL + PG | 三源并行 + LLM 合并分析 |
| C3 | 五库全景 | 全部5库 | 5源并行调度 + 合并 |
| C4 | 单库时序 | ClickHouse | 300万行时序查询性能 |
| C5 | 差异对比 | 全部5库 | 跨库对比分析 |

### 测试分组 D：意图分类与路由（P1）

| ID | 场景 | 输入 | 预期路由 |
|----|------|------|----------|
| D1 | 元数据查询 | "mysql_test有哪些表" | llm_direct_answer |
| D2 | 数据分析查询 | "各品类销售额排名" | retrieve_schema→SQL |
| D3 | 多源查询 | "对比mysql和pg的客户数" | multi_source_dispatch |
| D4 | 文件分析 | "分析上传的销售报表" | mcp_agent |
| D5 | 闲聊 | "你好，你能做什么" | llm_direct_answer |

### 测试分组 E：安全与权限（P1）

| ID | 场景 | 验证点 |
|----|------|--------|
| E1 | SQL注入尝试 | `'; DROP TABLE orders; --` 被阻断 |
| E2 | DDL拦截 | `ALTER TABLE` / `TRUNCATE` 被安全阻断 |
| E3 | 未登录访问 | 401 返回 |
| E4 | 跨租户隔离 | 用户A看不到用户B的数据 |
| E5 | 密码加密 | PBKDF2 加密验证 |

### 测试分组 F：会话与历史（P1）

| ID | 场景 | 验证点 |
|----|------|--------|
| F1 | 新会话创建 | session_id 分配 + PG 持久化 |
| F2 | 历史会话列表 | GET /sessions 分页返回 |
| F3 | 会话恢复 | 多轮对话后恢复上下文 |
| F4 | 切页不丢失 | sessionStorage 恢复 |
| F5 | 删除会话 | DELETE /sessions/{id} 成功 |

### 测试分组 G：MCP 文件分析（P1）

| ID | 场景 | 验证点 |
|----|------|--------|
| G1 | 文件上传 | 上传 CSV/Excel 到 /data/uploads |
| G2 | 文件列表 | GET /api/v1/mcp/files |
| G3 | 文件分析 | "分析 data/uploads/销售数据.csv" |
| G4 | 知识库检索 | 上传文档后语义检索 |

### 测试分组 H：性能与压力（P2）

| ID | 场景 | 阈值 |
|----|------|------|
| H1 | 百万级聚合 < 3s | CK 500万行 GROUP BY |
| H2 | 5表JOIN < 5s | order_items 1000万行 JOIN |
| H3 | 10 QPS并发 | 10并发无超时 |
| H4 | 瀑布流加载 | 向上滚动加载历史不卡顿 |
| H5 | SSE流式输出 | 首字节时间 < 2s |

## 19.4 测试数据文件

### 19.4.1 生成脚本

位置：`data/generate_test_data.py`
执行：`python data/generate_test_data.py --db all --scale 1M`

### 19.4.2 数据特征

- 主要数据使用中文（姓名、地址、商品名、分类名等）
- 每库数据独立随机生成，确保多数据源交叉分析时有差异可对比
- 日期范围：2024-01-01 ~ 2025-12-31（2年，支持YoY/MoM）
- 数值范围：销售额 1~100000 元，数量 1~1000

### 19.4.3 知识库测试文件

位置：`tests/fixtures/knowledge/`

| 文件 | 内容 |
|------|------|
| business_rules.txt | 公司业务规则 |
| product_catalog.md | 商品分类与编码规则 |
| sales_policy.txt | 销售政策文档 |
| data_dictionary.csv | 数据字典 |

### 19.4.4 MCP 测试配置

位置：`tests/fixtures/mcp/`

- `sample_sales.csv` — 用于文件分析的样本 CSV
- `sample_inventory.xlsx` — 样本 Excel 报表（CSV 替代）

## 19.5 执行步骤

```
# Step 1: 生成测试数据
cd data && python generate_test_data.py --db all --scale 1000000

# Step 2: 初始化数据库（导入数据）
python -m tests.fixtures.init_databases

# Step 3: 启动服务
python -m src.main

# Step 4: 运行 API 测试
pytest tests/test_api/ -v

# Step 5: 运行工作流测试
pytest tests/test_graph/ -v

# Step 6: 运行集成测试
pytest tests/ -m integration -v

# Step 7: 前端 E2E 测试（可选）
npx playwright test tests/e2e/
```

## 19.6 验收标准

| 级别 | 标准 |
|------|------|
| P0 通过 | A组 + B组 全部通过（32项） |
| P1 通过 | C组 + D组 + E组 + F组 + G组 通过率 ≥ 90% |
| P2 通过 | H组 通过率 ≥ 80% |
| 总体通过 | P0 + P1 + P2 全部达标 |
