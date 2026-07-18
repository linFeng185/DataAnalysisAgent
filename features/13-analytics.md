# 13. 数据分析引擎

## 13. 数据分析引擎 `[P0:6 P1:4 P2:5]`

| # | 功能 | 文件 | 描述 | 状态 |
|---|------|------|------|------|
| 13.1 | compute_statistics() | `src/tools/analyzer.py` | 均值/中位数/标准差/分位数/空值率 | 单测完成 | P0 |
| 13.2 | compute_trend() | 同上 | 环比/方向/移动平均 | 单测完成 | P0 |
| 13.3 | detect_outliers_zscore() | 同上 | Z-Score 异常检测 | 单测完成 | P0 |
| 13.4 | detect_outliers_iqr() | 同上 | IQR 异常检测 | 单测完成 | P0 |
| 13.5 | compute_concentration() | 同上 | Top N 集中度 | 单测完成 | P0 |
| 13.6 | compute_correlation() | 同上 | Pearson 相关系数 | 单测完成 | P0 |
| 13.7 | StructuredAssetAdapter | `src/knowledge/structured_assets.py` | CSV/Excel/Parquet 统一读取、列 profile、时间列/候选主键识别和预览，支持资源上限 | 单测完成 | P1 |
| 13.8 | rolling_backtest / forecast_series | `src/tools/forecasting.py` | naive/线性基线、时间滚动回测、MAE/RMSE/SMAPE、预测区间和模型卡 | 单测完成 | P1 |
| 13.9 | MarketDataProvider / compute_market_metrics | `src/tools/market_analysis.py` | 行情 provider 契约、收益/波动/回撤/Sharpe 指标与复权/as_of/风险声明 | 单测完成 | P2 |
| 13.10 | generate_scenarios | `src/tools/scenario_planning.py` | 变量笛卡尔积、min/max 约束、资源上限和候选方案评分排序 | 单测完成 | P2 |
| 13.11 | JoinContract | `src/tools/join_contract.py` | 跨资产 Join 匹配率、基数、膨胀因子和人工确认状态 | 单测完成 | P1 |
| 13.12 | StructuredQueryEngine | `src/knowledge/structured_query.py` | DuckDB SQL 查询、Excel 多 Sheet 注册、只读校验、结果行数和资源上限 | 单测完成 | P1 |
| 13.13 | TushareMarketDataProvider | `src/market/providers/tushare.py` | A 股日线、分钟线、实时快照 Provider，统一 MarketBar 契约；保留 market/provider 扩展字段 | 单测完成 | P2 |
| 13.14 | MarketDataStore | `src/market/storage.py`、`migrations/002_market_data.sql` | PostgreSQL 批量 upsert、唯一去重、时间索引和查询；请求成功后先持久化 | 单测完成 | P2 |
| 13.15 | ForecastEngine / ForecastModel | `src/tools/forecast_engine.py` | 可注册预测模型、统一 ForecastRequest/Result、滚动回测和模型卡接口 | 单测完成 | P2 |

### 模块收尾

本模块共 15 项，已完成 15 项，待开发 0 项。本批次新增的行情与预测接口已完成单元测试；真实 Tushare 账号、PostgreSQL 压测和交易日历/复权因子属于后续集成环境工作，不阻塞当前契约完成。

---
