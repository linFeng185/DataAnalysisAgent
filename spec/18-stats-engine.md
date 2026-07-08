# 18. 统计引擎重构 — 从 200 行样本到 DB 层精确统计

> 对应缺点 #4: compute_statistics 对前 200 行采样
> 包含 6 轮论证：用户/开发者/架构/数据科学/安全/方言

---

## 18.1 问题量化

### 采样误差表

| 总行数 | 200 行采样率 | 均值误差 | 分位数误差 | min/max |
|--------|------------|---------|-----------|---------|
| 1,000 | 20% | ±3% | ±5% | 基本可靠 |
| 10,000 | 2% | ±7% | ±15% | 不可靠 |
| 100,000 | 0.2% | ±15% | ±30% | 完全不可靠 |
| 1,000,000 | 0.02% | ±30% | 无统计意义 | 无统计意义 |

**核心问题**: 10 万行以上时分位数（q1/q3/median）已不可靠——基于 200 个样本点内插，真实值可能在 ±30%。用户不知道统计来自样本还是全量，据此做的商业决策可能完全错误。

---

## 18.2 六轮论证

### 第 1 轮：用户角度

**用户需要什么**: 统计准确 + 响应快 + **知道可信度**。

| 场景 | 当前 (200 样本) | 改进后 |
|------|----------------|--------|
| 50 万行 "平均客单价" | 均值 ±15% 误差，用户无感知 | 均值精确，标注 "数据库精确统计" |
| 1000 行 "品类占比" | 采样的 200 行可能漏掉长尾品类 | 全量计算，100% 覆盖 |
| 5000 行 "趋势分析" | 趋势方向基本正确 | 相同 |

**用户真正需要的**: "均值 1,280 元"后面加一句"（基于 50 万行全量精确计算）"或"（基于 10% 采样，±5% 误差）"。

### 第 2 轮：开发者角度——方案对比

| 方案 | 复杂度 | 精确度 | 风险 |
|------|--------|--------|------|
| A: 改 `rows[:200]` 为 `rows[:10000]` | 极低 | 中 | 内存爆炸（百万行→OOM） |
| B: 流式计算（滚动 mean/std） | 中 | 低 | **分位数不可流式**（需全量排序） |
| C: DB 内置 PERCENTILE | 高 | 高 | 5 方言 × 5 函数 = 25 适配 |
| **D: 分层混合** | 中高 | 高 | 需决策阈值 |

**选择 D（修正——消除中间层）**:

```
total_rows ≤ 1000      → Python 全量（数据已在 query_result_sample 中）
total_rows > 1000       → DB 二次查询 SELECT PERCENTILE(col) FROM (user_sql)
```

**为什么没有"10K 采样"中间层**: `execute_sql` 只存 `rows[:200]` 到 `query_result_sample`。全量数据在数据库里，不在 Python 内存中。不存在"10K 行采样"的数据源——采了 10K 行说明已经把数据拉到内存了，既然拉了 10K 行为什么不直接做精确统计？

**MySQL 5.7 降级**: 不支持 `PERCENTILE_CONT` → 回退到 `STDDEV_POP + AVG + MIN + MAX + 标注"分位数不可用"`，而不是虚假的采样分位数。

### 第 3 轮：架构角度

**变更范围**:

```
before: execute_sql → rows[:200] → compute_statistics(rows) → result

after:  execute_sql → rows + total_count → compute_statistics(rows, total_rows, dialect, conn) → result
```

- `query_result_sample` 保持不变（前端展调用，仍是 200 行）
- `query_result_full_count` 已有（来自 execute_sql）
- `compute_statistics` 改为 async，内部按策略路由
- `result` 增加 `method` / `confidence` 字段

**无破坏性变更**: 所有现有调用方和前端不受影响。

### 第 4 轮：数据科学角度

**min/max 的致命问题**: 从 100 万行抽 200 行，样本的 min 可能是真实 10 分位，max 可能是真实 90 分位。**min/max 采样完全不可靠。**

**修正**: `total_rows > 1000` 时 min/max 总是从 DB 查（`SELECT MIN(col), MAX(col) FROM (subquery)`），即使其他统计用采样。

### 第 5 轮：安全角度

1. **SQL 注入**: 统计列名只从 `state.relevant_tables`（白名单）取，不从 LLM 输出取
2. **资源**: 统计 SQL 共享 `max_execution_time` 限制
3. **信息泄露**: 统计查询标记为 `sql_type: "stats"` 写入审计日志

### 第 6 轮：方言适配

| 方言 | PERCENTILE | STDDEV | 特殊注意 |
|------|-----------|--------|---------|
| MySQL 8.0 | `PERCENTILE_CONT(0.5) WITHIN GROUP` | `STDDEV_POP` | 5.7 fallback |
| PostgreSQL | 同 | 同 | 全支持 |
| ClickHouse | `quantile(0.5)(col)` | `stddevPop(col)` | 列存加速 100× |
| Oracle | 同 MySQL | 同 | XE 限制 2 CPU |
| MSSQL | 同 MySQL | 同 | 全支持 |

---

## 18.3 实现设计

### 分层策略

```python
async def compute_statistics(rows: list[dict], total_rows: int = 0,
                              dialect: str = "", conn=None, sql: str = "") -> dict:
    if not rows: return empty
    if total_rows <= 1000: return _python_full(rows)                    # method=full
    # 数据不在内存中，必须从 DB 二次查询
    return await _db_stats(sql, dialect, conn) or _python_fallback(rows)  # fallback
```

**为什么不能"10K 采样"**: `query_result_sample` 只存 200 行。全量数据在 DB 里不在 Python 内存中。如果增大存储量（改 `rows[:200]` → `rows[:10000]`），百万行查询会导致 OOM。

### DB 统计 SQL

```sql
SELECT
  AVG(col1) AS col1_mean, STDDEV_POP(col1) AS col1_std,
  MIN(col1) AS col1_min, MAX(col1) AS col1_max,
  PERCENTILE_CONT(0.5) WITHIN GROUP (ORDER BY col1) AS col1_median,
  PERCENTILE_CONT(0.25) WITHIN GROUP (ORDER BY col1) AS col1_q1,
  PERCENTILE_CONT(0.75) WITHIN GROUP (ORDER BY col1) AS col1_q3
FROM (user_sql) AS _stats
```

ClickHouse 替换为 `quantile()` 系列函数。

---

## 18.4 检查清单

- [ ] `src/tools/db_stats.py` — 5 方言统计 SQL 生成器
- [ ] `src/tools/analyzer.py` — compute_statistics 改为 async 分层
- [ ] `src/graph/nodes/analyze_result.py` — 传入 total_rows + dialect + conn
- [ ] MySQL 5.7 fallback 测试
- [ ] 前端标注 method/confidence
