# 5. 工具层

## 5. 工具层 (tools/) `[P0:11 P1:2]`

### 5.1 内置工具

| # | 功能 | 文件 | 描述 | 状态 |
|---|------|------|------|------|
| 5.1.1 | SchemaExplorerTool | `src/tools/schema_explorer.py` | 继承 BaseTool，封装 SchemaManager.get_or_fetch_schema() | 开发完成 |
| 5.1.2 | SQLGeneratorTool | `src/tools/sql_generator.py` | 继承 BaseTool，封装 SQL 生成逻辑 | 开发完成 |
| 5.1.3 | SQLglotValidatorTool | `src/tools/sqlglot_validator.py` | 继承 BaseTool，封装 validate_with_sqlglot() | 开发完成 |
| 5.1.4 | DBExecutorTool | `src/tools/db_executor.py` | 继承 BaseTool，封装 SQL 执行逻辑 | 开发完成 |
| 5.1.5 | DBExplainTool | `src/tools/db_executor.py` | 继承 BaseTool，封装 EXPLAIN 空跑逻辑 | 开发完成 |
| 5.1.6 | DataAnalyzerTool | `src/tools/data_analyzer.py` | 继承 BaseTool，封装数据分析逻辑 | 开发完成 |
| 5.1.7 | ChartGeneratorTool | `src/tools/chart_generator.py` | 继承 BaseTool，封装图表生成逻辑 | 开发完成 |

### 5.2 sqlglot 校验工具

| # | 功能 | 文件 | 描述 | 状态 |
|---|------|------|------|------|
| 5.2.1 | validate_with_sqlglot() | `src/tools/sqlglot_validator.py` | 核心校验函数: 语法解析 + 函数白名单 + 方言转译 | 开发完成 |
| 5.2.2 | SUPPORTED_DIALECTS 常量 | 同上 | 20+ 种 sqlglot 支持的方言集合 | 开发完成 |
| 5.2.3 | _get_dialect_functions() | 同上 | 获取指定方言的内置函数白名单 | 开发完成 |
| 5.2.4 | _is_universal_func() | 同上 | 跨数据库通用函数集合 (COUNT/SUM/AVG/COALESCE...) | 开发完成 |
| 5.2.5 | _suggest_correct_function() | 同上 | ClickHouse/PostgreSQL 函数映射建议表 | 开发完成 |

---
