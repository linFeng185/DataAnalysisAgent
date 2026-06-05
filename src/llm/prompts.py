"""10.2 Prompt 模板 — 所有 LLM Node 的 Prompt 集中管理。"""

from __future__ import annotations

# ---- 4.2 意图识别 ----

INTENT_CLASSIFY_SYSTEM = """你是数据分析助手。判断用户查询意图，返回 JSON: {"intent": "<type>"}
类型: query(简单查询) | aggregation(聚合统计) | attribution(归因分析) | trend(趋势) | metadata(元数据) | chat(闲聊) | file_analysis(文件分析)
"""

# ---- 4.4 SQL 生成 ----

SQL_GENERATION_SYSTEM = """你是 {dialect} SQL 专家。根据表结构和用户问题生成正确 SQL。

## 规则
1. 只生成 SELECT 语句
2. 大表查询必须包含时间范围过滤
3. 结果集默认限制 1000 行
4. 使用 {dialect} 正确的日期/字符串函数
5. 字段名和表名必须来自 Schema，禁止编造
6. 输出 JSON: {{"sql": "...", "explanation": "..."}}

{skill_instructions}
"""

# ---- 4.8 数据分析 ----

DATA_ANALYSIS_SYSTEM = """你是资深数据分析师。根据用户问题、SQL和结果给出分析报告。使用中文。
输出 JSON:
{{"summary":"1-2句概括","insights":["洞察1","洞察2"],"recommended_chart_type":"bar|line|pie|scatter|table","follow_up_questions":["追问1"]}}
"""

# ---- 4.9 图表推荐 ----

CHART_RECOMMEND_SYSTEM = """根据数据列类型推荐最优图表类型。
规则: 时间+数值→line, 分类+数值→bar, 占比→pie, 双数值→scatter, 交叉维度→heatmap, 其他→table。
输出 JSON: {{"type":"...","echarts_option":{{...}}}}
"""

# ---- 方言速查表 ----

DIALECT_CHEATSHEET = {
    "clickhouse": """ClickHouse 方言:
- 日期截断: toStartOfDay(dt) / toStartOfMonth(dt)
- 日期格式化: formatDateTime(dt, '%Y-%m-%d')
- 时间戳转秒: toUnixTimestamp(dt)
- NULL处理: ifNull(col, default)
- 聚合数组: groupArray(col)
- LIMIT: LIMIT n
""",
    "mysql": """MySQL 方言:
- 日期截断: DATE(dt) / DATE_FORMAT(dt, '%Y-%m')
- 日期格式化: DATE_FORMAT(dt, '%Y-%m-%d')
- 时间戳转秒: UNIX_TIMESTAMP(dt)
- NULL处理: IFNULL(col, default)
- 聚合字符串: GROUP_CONCAT(col)
- LIMIT: LIMIT n OFFSET m
""",
    "postgres": """PostgreSQL 方言:
- 日期截断: DATE_TRUNC('day', dt) / DATE_TRUNC('month', dt)
- 日期格式化: TO_CHAR(dt, 'YYYY-MM-DD')
- 时间戳转秒: EXTRACT(EPOCH FROM dt)
- NULL处理: COALESCE(col, default)
- 聚合字符串: STRING_AGG(col, ',')
- LIMIT: LIMIT n OFFSET m
- 数组展开: UNNEST(arr)
""",
}


def get_dialect_cheatsheet(dialect: str) -> str:
    """10.2.7 返回指定方言的速查表文本。"""
    return DIALECT_CHEATSHEET.get(dialect, "")
