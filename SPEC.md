# 大数据分析智能体 (Data Analysis Agent) — 技术规格文档

## 1. 概述

### 1.1 项目目标

构建一个 LLM 驱动的数据分析智能体，接收用户自然语言输入，自动完成 SQL 生成、查询执行、结果处理、数据分析与可视化展示的全流程。

### 1.2 核心能力

- **NL2SQL**：将自然语言问题转化为可执行的 SQL 语句
- **自主查询**：自动连接数据源执行 SQL，处理查询错误并重试
- **数据分析**：对查询结果进行统计、归因、趋势分析
- **可视化与报告**：将分析结果以图表和文字报告形式呈现

---

## 2. 系统架构

```
┌─────────────────────────────────────────────────────────┐
│                      用户界面层                          │
│  ┌──────────┐  ┌──────────┐  ┌──────────────────────┐   │
│  │ Web Chat │  │  API     │  │ 集成平台 (飞书/Slack) │   │
│  └────┬─────┘  └────┬─────┘  └──────────┬───────────┘   │
└───────┼──────────────┼───────────────────┼───────────────┘
        │              │                   │
        └──────────────┼───────────────────┘
                       │
┌──────────────────────┼───────────────────────────────────┐
│              智能体核心 (Agent Core)                      │
│                                                          │
│  ┌──────────────────────────────────────────────────┐   │
│  │        LangGraph 状态图 (StateGraph)               │   │
│  │  ┌────────┐ ┌────────┐ ┌────────┐ ┌────────┐     │   │
│  │  │意图识别│→│Schema  │→│SQL 生成│→│安全校验│     │   │
│  │  │  Node  │ │检索Node│ │  Node  │ │  Node  │     │   │
│  │  └────────┘ └────────┘ └────────┘ └────┬───┘     │   │
│  │                                         │         │   │
│  │  ┌────────┐ ┌────────┐ ┌────────┐      │         │   │
│  │  │反思归档│←│结果分析│←│SQL 执行│←─────┘         │   │
│  │  │  Node  │ │  Node  │ │  Node  │                │   │
│  │  └────────┘ └────────┘ └────────┘                │   │
│  │              ↑ 失败时回退到 SQL 生成 Node          │   │
│  └──────────────────────┬───────────────────────────┘   │
│                         │                                │
│  ┌──────────────────────┼───────────────────────────┐   │
│  │          LangChain 工具层 (BaseTool)               │   │
│  │                                                    │   │
│  │  ┌──────────┐ ┌──────────┐ ┌──────────┐ ┌──────┐  │   │
│  │  │ Schema   │ │ SQL Gen  │ │ sqlglot  │ │ DB   │  │   │
│  │  │ Explorer │ │ + Dialect│ │ Validator│ │ Exec │  │   │
│  │  │ Tool     │ │ Tool     │ │ Tool     │ │ Tool │  │   │
│  │  └──────────┘ └──────────┘ └──────────┘ └──────┘  │   │
│  │  ┌──────────┐ ┌──────────┐ ┌──────────────────┐   │   │
│  │  │ Data     │ │ Chart    │ │ Memory           │   │   │
│  │  │ Analyzer │ │ Generator│ │ Manager          │   │   │
│  │  │ Tool     │ │ Tool     │ │                  │   │   │
│  │  └──────────┘ └──────────┘ └──────────────────┘   │   │
│  └──────────────────────────────────────────────────┘   │
└──────────────────────┬───────────────────────────────────┘
                       │
┌──────────────────────┼───────────────────────────────────┐
│              数据层 (Data Layer)                          │
│  ┌──────────┐  ┌──────────┐  ┌──────────────────────┐   │
│  │ ClickHouse│  │MySQL/PG  │  │ 缓存 (Redis)         │   │
│  │ (OLAP)   │  │ (OLTP)   │  │                      │   │
│  └──────────┘  └──────────┘  └──────────────────────┘   │
└──────────────────────────────────────────────────────────┘
```

---

## 3. 核心组件详细设计

### 3.1 LangGraph 编排引擎

使用 LangGraph 的 `StateGraph` 实现完整的分析流水线，将每个处理阶段定义为独立的 Node，通过条件边实现错误重试和动态路由。

**AgentState 定义**（在图中流转的共享状态）：

```python
from typing import TypedDict, Annotated, Sequence
from langgraph.graph.message import add_messages
from langchain_core.messages import BaseMessage


class AnalysisState(TypedDict):
    messages: Annotated[Sequence[BaseMessage], add_messages]
    user_query: str                    # 用户原始问题
    datasource: str                    # 目标数据源名称
    intent: str                        # 意图分类结果
    relevant_tables: list[dict]        # Schema检索到的相关表
    generated_sql: str                 # LLM 生成的 SQL
    sql_valid: bool                    # 安全校验是否通过
    validation_error: str              # 校验失败原因
    query_result: list[dict]           # SQL 执行结果
    execution_error: str               # 执行错误信息
    retry_count: int                   # 当前重试次数
    analysis_result: dict              # 分析结论
    chart_config: dict                 # 图表配置
    final_response: dict               # 最终返回给用户的完整响应
```

**LangGraph 节点与边设计**：

```
                  ┌─────────────┐
                  │   START     │
                  └──────┬──────┘
                         │
                  ┌──────▼──────┐
                  │ 意图识别     │  classify_intent()
                  │ Node         │  → 判断问题类型: query / metadata / chat
                  └──────┬──────┘
                         │
                  ┌──────▼──────┐
                  │ Schema 检索  │  retrieve_schema()
                  │ Node         │  → 向量检索 + 关键词匹配相关表
                  └──────┬──────┘
                         │
                  ┌──────▼──────┐
                  │ SQL 生成     │  generate_sql()
                  │ Node         │  → ChatOpenAI + schema + few-shot → SQL
                  └──────┬──────┘
                         │
              ┌──────────▼──────────┐
              │                     │
    ┌─────────▼─────────┐  ┌────────▼────────┐
    │ sqlglot 语法校验   │  │ sqlglot 方言    │
    │ (parse + validate) │  │ 转译 (transpile)│  layer3_validate()
    │ Node               │  │ Node            │  → 零成本本地校验
    └─────────┬─────────┘  └────────┬────────┘
              │                     │
              └──────────┬──────────┘
                         │
                    ┌────▼────┐
                    │ 通过?    │  conditional edge
                    └────┬────┘
                         │
                ┌────────┼────────┐
                │ NO              │ YES
          ┌─────▼─────┐    ┌──────▼──────┐
          │ error msg │    │ EXPLAIN 空跑 │  db_explain_check()
          │ → LLM修正 │    │ Node         │  → 不查数据只验语义
          │ → 回到SQL │    └──────┬──────┘
          │ 生成Node  │           │
          └───────────┘      ┌────▼────┐
                             │ 通过?    │  conditional edge
                             └────┬────┘
                                  │
                         ┌────────┼────────┐
                         │ NO              │ YES
                   ┌─────▼─────┐    ┌──────▼──────┐
                   │ error msg │    │ SQL 执行     │  execute_sql()
                   │ → LLM修正 │    │ Node         │  → 真正查询
                   │ → 回到SQL │    └──────┬──────┘
                   │ 生成Node  │           │
                   └───────────┘      ┌────▼────┐
                                     │ 执行成功? │
                                     └────┬────┘
                                 │
                         ┌───────┼───────┐
                         │ NO            │ YES
                         │               │
                   ┌─────▼─────┐   ┌─────▼─────┐
                   │ 重试次数   │   │ 数据分析   │  analyze_result()
                   │ < 3?      │   │ Node       │  → pandas 统计 + LLM 洞察
                   └─────┬─────┘   └─────┬─────┘
                         │               │
                   ┌─────┼─────┐   ┌─────▼─────┐
                   │ YES │ NO  │   │ 可视化图表 │  generate_chart()
                   │     │     │   │ Node       │  → 自动选图 → ECharts config
                   └──┬──┘ └───┘   └─────┬─────┘
                      │        │         │
                      │   ┌────▼────┐    │
                      │   │ error → │    │
                      │   │ 返回用户│    │
                      │   └────────┘    │
                      │                 │
                      │   ┌─────────────┘
                      │   │
                 ┌────▼───▼────┐
                 │ 回到 SQL    │
                 │ 生成 Node   │  (retry path: 附带错误信息)
                 └─────────────┘
                                      │
                               ┌──────▼──────┐
                               │ 构建最终响应 │  build_response()
                               │ Node         │  → 组装 analysis + chart + data
                               └──────┬──────┘
                                      │
                               ┌──────▼──────┐
                               │    END      │
                               └─────────────┘
```

**条件路由逻辑**：

```python
from langgraph.graph import StateGraph, END


def should_retry(state: AnalysisState) -> str:
    """SQL 执行失败时判断是否重试"""
    if state.get("execution_error") and state["retry_count"] < 3:
        return "generate_sql"   # 回到 SQL 生成 Node
    return "build_response"     # 放弃重试，返回错误信息


def after_validation(state: AnalysisState) -> str:
    """安全校验后的路由"""
    if state["sql_valid"]:
        return "execute_sql"
    return "build_response"     # 校验失败，直接返回错误


# Graph 组装
workflow = StateGraph(AnalysisState)

workflow.add_node("classify_intent", classify_intent_node)
workflow.add_node("retrieve_schema", retrieve_schema_node)
workflow.add_node("generate_sql", generate_sql_node)
workflow.add_node("layer3_validate", layer3_validate_node)        # sqlglot 语法校验
workflow.add_node("layer4_explain", layer4_explain_node)          # EXPLAIN 空跑
workflow.add_node("execute_sql", execute_sql_node)
workflow.add_node("analyze_result", analyze_result_node)
workflow.add_node("generate_chart", generate_chart_node)
workflow.add_node("build_response", build_response_node)

workflow.add_edge(START, "classify_intent")
workflow.add_edge("classify_intent", "retrieve_schema")
workflow.add_edge("retrieve_schema", "generate_sql")
workflow.add_edge("generate_sql", "layer3_validate")
workflow.add_conditional_edges("layer3_validate", after_layer3, {
    "generate_sql": "generate_sql",     # 校验失败 → 回退修正
    "layer4_explain": "layer4_explain", # 通过 → 下一层
    "build_response": "build_response"  # 安全拦截 → 终止
})
workflow.add_conditional_edges("layer4_explain", after_layer4, {
    "generate_sql": "generate_sql",     # 语义错误 → 回退修正
    "execute_sql": "execute_sql",       # 通过 → 执行
    "build_response": "build_response"  # 3次重试用尽 → 终止
})
workflow.add_conditional_edges("execute_sql", should_retry, {
    "generate_sql": "generate_sql",      # 运行时错误 → 回退修正
    "build_response": "build_response"   # 重试用尽 → 返回错误
})
workflow.add_edge("execute_sql", "analyze_result")
workflow.add_edge("analyze_result", "generate_chart")
workflow.add_edge("generate_chart", "build_response")
workflow.add_edge("build_response", END)

app = workflow.compile(checkpointer=memory_saver)
```

**每个 Node 内部使用 LangChain 组件**：

- **ChatOpenAI / ChatAnthropic**：作为 LLM 基座
- **ChatPromptTemplate**：定义各阶段的 Prompt 模板
- **StructuredOutput**：约束 LLM 输出格式（如 SQL 生成输出为 `{"sql": "...", "explanation": "..."}`）
- **VectorStoreRetriever**：Schema 语义检索
- **@tool 装饰器**：将 Python 函数包装为 LangChain Tool，便于 Agent 模式下的动态调用

### 3.2 Schema 探索器与知识库

负责让 LLM 理解数据库中有哪些表、字段、业务规则。**核心设计：当没有文档时，自动从数据源拉取并缓存；当有文档时，文档优先。**

> **模块边界**: `datasource/introspection.py` 仅负责执行原始 SQL 查询系统表返回 `list[dict]`，不做缓存不做语义加工。`knowledge/schema_manager.py` 负责缓存策略、知识优先级合并、`SchemaSnapshot` 组装。两者通过 `KnowledgeEntry` 传递数据，避免循环依赖。

---

#### 3.2.1 知识分类与存储策略

三类知识，同一向量库，不同粒度：

```
┌────────────────────────────────────────────────────────────┐
│                  知识分类与存储方案                          │
├──────────────┬──────────────┬──────────────┬───────────────┤
│  知识类型     │  内容示例     │  索引粒度     │  检索方式      │
├──────────────┼──────────────┼──────────────┼───────────────┤
│ ① 结构 Schema │ 表名、字段名、 │ 表级 + 字段级 │ 语义向量       │
│              │ 类型、主外键   │              │ + 关键词混合   │
├──────────────┼──────────────┼──────────────┼───────────────┤
│ ② 字段语义    │ "status=2    │ 字段级独立    │ 语义向量       │
│  (数据字典)   │ 表示已支付"    │ 索引          │ + 精确匹配     │
│              │ "amount 单位  │              │               │
│              │ 是分不是元"    │              │               │
├──────────────┼──────────────┼──────────────┼───────────────┤
│ ③ 业务规则    │ "GMV = 已支付  │ 文档级索引    │ 语义向量       │
│  (指标口径)   │ 金额 - 退款"   │ + 结构化      │               │
│              │ "新用户 =     │ MetricStore  │               │
│              │ 注册30天内"    │              │               │
├──────────────┼──────────────┼──────────────┼───────────────┤
│ ④ SQL 模板   │ 已验证的      │ 文档级        │ 语义向量       │
│              │ question-SQL  │              │               │
│              │ 历史对        │              │               │
└──────────────┴──────────────┴──────────────┴───────────────┘
```

**为什么不能全塞一个索引里**：表级文档噪音大，用户问 "金额单位是什么" 需要在字段级精确定位；表级索引用于 "找跟订单相关的表"。

---

#### 3.2.2 核心机制：自动拉取 + 分层缓存

智能体启动或查询时，按优先级逐层获取知识：

```
是否需要查某张表的信息？
        │
        ▼
┌──────────────────┐
│ ① 查向量库缓存    │  ← 有 → 直接用 (毫秒级)
│ (ChromaDB)      │
└────────┬─────────┘
         │ 无 (首次查询 / 缓存过期)
         ▼
┌──────────────────┐
│ ② 查文档仓库      │  ← 有 → 解析后写入向量库，返回
│ (docs/metrics/)  │
└────────┬─────────┘
         │ 无
         ▼
┌──────────────────┐
│ ③ 查 DB 系统表    │  ← 自动拉取元数据 →
│ (INFORMATION_    │    写入向量库缓存
│  SCHEMA)         │    标注 source=auto
└────────┬─────────┘
         │ 无权限
         ▼
┌──────────────────┐
│ ④ 返回降级信息    │  "此数据源暂无可用的表结构信息"
└──────────────────┘
```

**关键行为**：③ 是全自动的——查询任何一个数据源，如果本地没有缓存，就实时从 DB 拉取表结构并写入 ChromaDB（标注 `source=auto`），下次查询秒级命中。

---

#### 3.2.3 知识来源与优先级

```python
from enum import Enum
from dataclasses import dataclass, field
from datetime import datetime, timedelta


class KnowledgeSource(Enum):
    """知识来源标记 — 用于判断数据新鲜度和可信度"""
    MANUAL_DOC = "manual_doc"    # 手工编写的 Markdown 指标文档 (最可信)
    ORM_MODEL = "orm_model"      # ORM Model 的 docstring/comment
    DB_COMMENT = "db_comment"    # DB 字段 COMMENT
    AUTO_INTROSPECT = "auto"     # 自动拉取的裸元数据 (最不可信，需人工补标注)
    USER_CORRECTION = "user"     # 用户反馈修正


@dataclass
class KnowledgeEntry:
    """知识库中的一条记录"""
    id: str                       # "user_orders.amount" (字段级) 或 "metric:gmv" (文档级)
    content: str                  # 可供 LLM 理解的自然语言描述
    source: KnowledgeSource       # 来源标记
    table_name: str = ""          # 所属表 (字段级)
    column_name: str = ""         # 字段名 (字段级)
    category: str = "schema"      # "schema" | "field_semantic" | "business_rule" | "sql_template"
    tags: list[str] = field(default_factory=list)  # ["订单", "金额"]
    created_at: datetime = field(default_factory=datetime.now)
    ttl: timedelta = timedelta(days=7)  # AUTO 来源 7 天过期自动刷新
```

**优先级规则**：

| 同一字段存在多个来源时 | 取 source 优先级高的 |
|---|---|
| MANUAL_DOC | 最高 — 人工编写的业务文档 |
| USER_CORRECTION | 用户反馈后人工确认的修正 |
| ORM_MODEL | ORM 代码中的注释 |
| DB_COMMENT | DBA 写的字段注释 |
| AUTO_INTROSPECT | 最低 — 自动拉取的无注释裸结构 |

---

#### 3.2.4 表结构与字段语义（知识类型 ① + ②）

```python
class SchemaManager:
    """
    核心职责: 无论有没有文档，保证 retrieve_schema Node 能拿到表结构。

    流程:
    1. 检查 ChromaDB 缓存
    2. 未命中 → 检查 Markdown 文档
    3. 仍未命中 → 自动拉取 DB 系统表 → 写入缓存 (source=auto)
    """

    def __init__(self, vector_store: Chroma, registry: "DataSourceRegistry"):
        self.vector_store = vector_store
        self.registry = registry

    async def get_or_fetch_schema(
        self, datasource_name: str, table_names: list[str] | None = None
    ) -> "SchemaSnapshot":
        """
        获取 Schema 的主入口。

        - 首次调用: DB 内省 → 写入 ChromaDB → 返回
        - 后续调用: 直接从 ChromaDB 检索 → 毫秒级返回
        - 缓存过期 (AUTO 来源 7 天): 自动重新拉取
        """
        # 1. 查向量库缓存
        cached = await self._query_cache(datasource_name, table_names)
        uncached_tables = self._find_uncached(table_names, cached)

        if not uncached_tables:
            return self._build_snapshot(cached)

        # 2. 查文档仓库
        doc_entries = await self._load_from_docs(datasource_name, uncached_tables)
        for entry in doc_entries:
            await self._upsert_to_cache(entry)

        still_missing = [t for t in uncached_tables if t not in {e.table_name for e in doc_entries}]

        # 3. 自动拉取 DB 元数据 → 兜底
        if still_missing:
            auto_entries = await self._introspect_from_db(datasource_name, still_missing)
            for entry in auto_entries:
                entry.source = KnowledgeSource.AUTO_INTROSPECT
                entry.ttl = timedelta(days=7)     # 7天后自动刷新
                await self._upsert_to_cache(entry)

        all_entries = cached + doc_entries + auto_entries
        return self._build_snapshot(all_entries)

    async def _introspect_from_db(
        self, datasource_name: str, table_names: list[str]
    ) -> list[KnowledgeEntry]:
        """
        自动拉取: 查询 INFORMATION_SCHEMA 获取表结构。

        产出两类索引:
        - 表级: "user_orders 包含 order_id, user_id, amount..."
        - 字段级: "user_orders.amount 订单金额, Decimal(18,2)"
        """
        ds = await self.registry.resolve(datasource_name)
        entries = []

        for table in table_names:
            columns = await self._query_columns(ds, table)
            relations = await self._query_foreign_keys(ds, table)

            # 表级索引 — 用于 "找跟订单相关的表"
            table_text = self._format_table_summary(table, columns, relations)
            entries.append(KnowledgeEntry(
                id=f"table:{datasource_name}.{table}",
                content=table_text,
                source=KnowledgeSource.AUTO_INTROSPECT,
                table_name=table,
                category="schema",
            ))

            # 字段级索引 — 每个字段独立一条，用于精确定位 "amount 单位"
            for col in columns:
                entries.append(KnowledgeEntry(
                    id=f"column:{datasource_name}.{table}.{col.name}",
                    content=self._format_column_detail(table, col),
                    source=KnowledgeSource.AUTO_INTROSPECT,
                    table_name=table,
                    column_name=col.name,
                    category="field_semantic",
                    tags=self._extract_tags(col),
                ))

        return entries

    async def _query_columns(self, ds: DataSourceConfig, table: str) -> list[dict]:
        """查询系统表获取字段元数据 — 不同方言自适应"""
        if ds.dialect == "clickhouse":
            sql = f"SELECT name, type, comment FROM system.columns WHERE table = '{table}'"
        elif ds.dialect == "mysql":
            sql = f"""
                SELECT COLUMN_NAME as name, COLUMN_TYPE as type, COLUMN_COMMENT as comment
                FROM INFORMATION_SCHEMA.COLUMNS
                WHERE TABLE_SCHEMA = '{ds.database}' AND TABLE_NAME = '{table}'
            """
        else:  # postgres
            sql = f"""
                SELECT column_name as name, data_type as type,
                       COALESCE(col_description((SELECT c.oid FROM pg_class c
                         WHERE c.relname = '{table}'), ordinal_position), '') as comment
                FROM INFORMATION_SCHEMA.COLUMNS
                WHERE table_name = '{table}'
            """
        return await self._execute_metadata_query(ds, sql)

    def _format_column_detail(self, table: str, col: dict) -> str:
        """字段级索引内容 — 供 LLM 精确理解每个字段"""
        return (
            f"表 {table} 中的字段 {col['name']}，"
            f"类型 {col['type']}，"
            f"说明: {col.get('comment') or '(数据库未填写注释，请谨慎使用)'}"
        )
```

---

#### 3.2.5 业务规则与指标口径（知识类型 ③）

业务规则是 SQL 正确性的关键——LLM 无法从表结构推断 "GMV = 已支付金额 - 退款"。

```python
class BusinessRuleStore:
    """
    业务规则存储 — 文档优先，自动兜底。

    规则类型:
    - metric: 指标计算公式 (GMV、ARPU、留存率)
    - filter: 筛选条件定义 (有效用户、活跃商家)
    - enum: 枚举值映射 (status=1→待支付)
    - period: 时间口径 (自然月 vs 账单月)
    """

    def __init__(self, vector_store: Chroma, docs_dir: str = "docs/metrics"):
        self.vector_store = vector_store
        self.docs_dir = docs_dir

    async def initialize(self):
        """启动时: 扫描 docs/metrics/ 目录，将 Markdown 文档索引到向量库"""
        if not os.path.isdir(self.docs_dir):
            return  # 没有文档目录 → 跳过，后续全部靠自动拉取

        for md_file in glob.glob(f"{self.docs_dir}/**/*.md", recursive=True):
            await self._index_metric_doc(md_file)

    async def search_business_rules(self, query: str, top_k: int = 5) -> list[KnowledgeEntry]:
        """检索与查询相关的业务规则"""
        results = await self.vector_store.similarity_search(
            query,
            k=top_k,
            filter={"category": "business_rule"},
        )
        return [self._to_entry(r) for r in results]

    async def _index_metric_doc(self, filepath: str):
        """
        将 Markdown 业务文档切片后写入向量库。

        示例文档 docs/metrics/gmv.md:

        # GMV (总交易额)
        ## 计算公式
        GMV = 已支付订单金额 - 已退款金额
        不含: 运费、税费、优惠券抵扣

        ## 涉及表
        - user_orders: 订单主表
        - order_refunds: 退款表

        ## SQL 模板
        见 gmv_template.sql

        ## 注意事项
        - 金额单位均为「分」
        - 按支付时间统计，非下单时间
        """
        content = Path(filepath).read_text(encoding="utf-8")

        # 按 ## 标题切片，每个小节独立索引
        chunks = self._split_by_headings(content)
        for chunk in chunks:
            await self.vector_store.add_documents([Document(
                page_content=chunk["text"],
                metadata={
                    "source": filepath,
                    "category": "business_rule",
                    "heading": chunk["heading"],
                    "tags": self._extract_metric_tags(chunk["text"]),
                }
            )])

    def _split_by_headings(self, content: str) -> list[dict]:
        """按 ## 标题将文档拆分为独立 chunks，每个 chunk 可独立检索"""
        ...
```

**如果没有业务文档**，自动拉取的字段枚举值会被标注 `source=AUTO_INTROSPECT`：

```python
# 没有文档时: 自动发现枚举值
async def _auto_discover_enum_values(
    self, ds: DataSourceConfig, table: str, column: str
) -> KnowledgeEntry | None:
    """对低基数字段自动采样枚举值

    安全约束:
    - 仅对行数 < 100万 的表执行 (避免大表慢查询)
    - 对超阈值表使用近似查询 (ClickHouse: uniqCombined / PG: approx_count_distinct)
    - 超时 5 秒自动放弃
    """
    # 先检查表大小
    row_estimate = await self._estimate_row_count(ds, table)
    if row_estimate and row_estimate > 1_000_000:
        return None  # 大表跳过，改为 LLM 分析时手工标注

    sql = f"SELECT DISTINCT {column} FROM {table} LIMIT 50"
    try:
        rows = await self._execute_metadata_query(ds, sql)
        values = [str(r[0]) for r in rows]
        if len(values) <= 20:  # 低基数 → 可能是枚举
            return KnowledgeEntry(
                id=f"enum:{ds.name}.{table}.{column}",
                content=f"{table}.{column} 的取值: {', '.join(values)}",
                source=KnowledgeSource.AUTO_INTROSPECT,
                category="field_semantic",
                ttl=timedelta(days=1),  # 枚举值变化快，TTL 更短
            )
    except Exception:
        pass
    return None
```

---

#### 3.2.6 知识录入与管理

业务文档不会自己写自己，需要摄入和维护机制：

| 摄入方式 | 说明 | 优先级 |
|---------|------|--------|
| 手工 Markdown 文档 | 指标口径文档、数据字典，存 `docs/metrics/` 目录，启动时自动索引 | 启动阶段必须 |
| DB 字段 COMMENT | 自动从 INFORMATION_SCHEMA 采集，是最基础的字段语义来源 | 自动化 |
| 自动拉取缓存 | 无文档时实时从 DB 拉取，source=auto，7天过期 | 自动化兜底 |
| 管理员标注 | Web UI 中给表和字段补充中文说明，写入 ChromaDB | 日常维护 |
| 用户反馈修正 | "这个 SQL 不对，GMV 应该包含运费" → 管理员审核后更新文档 | 持续迭代 |

**source=auto 的生命周期管理**：

```python
class CacheRefresher:
    """定期刷新自动拉取的缓存

    ⚠️ 并发控制: 多个请求可能同时触发缓存刷新。
    使用 Redis 分布式锁 (SETNX + TTL) 确保同一表同一时刻只有一个刷新任务。
    未获取锁的请求返回旧缓存，不阻塞。
    """
    def __init__(self, redis_client: Redis | None = None):
        self.redis = redis_client

    async def _acquire_refresh_lock(self, table_key: str) -> bool:
        if not self.redis:
            return True  # 无 Redis 时退化为无锁 (单进程)
        return await self.redis.set(f"refresh_lock:{table_key}", "1", nx=True, ex=60)

    async def _release_refresh_lock(self, table_key: str):
        if self.redis:
            await self.redis.delete(f"refresh_lock:{table_key}")

    async def refresh_expired(self):
        """清理过期的 AUTO 来源条目，触发重新拉取"""
        now = datetime.now()
        expired = await self.vector_store.get(
            filter={"source": "auto"},
            where_document={"created_at": {"$lt": now - timedelta(days=7)}}
        )
        for entry in expired:
            # 删除旧条目 → 下次查询自动触发重新拉取
            await self.vector_store.delete(ids=[entry["id"]])

    async def refresh_on_schema_change(self, datasource_name: str):
        """
        监听 DDL 变更 → 主动刷新缓存。
        可选实现: 轮询 INFORMATION_SCHEMA.TABLES 的 UPDATE_TIME，
        或接入 DB 的 DDL 触发器/CDC。
        """
        ...
```

---

#### 3.2.7 检索 Node 的完整流程

```
用户问: "上个月新用户的GMV是多少？"
                │
    ┌───────────┼───────────┐
    │           │           │
    ▼           ▼           ▼
① 关键词      ② 向量检索    ③ 向量检索
提取实体:     表结构 + 字段   业务文档
"新用户"      语义            "新用户定义"
"GMV"        → user_orders  "GMV计算公式"
"上个月"      → users
             → order_refunds
                │
                └───────┬───────┘
                        ▼
              组装 Prompt 注入 LLM:

              ## 相关表结构
              | 表名 | 字段 | 类型 | 说明 |
              | user_orders | amount | Decimal(18,2) | 订单金额，单位分 |
              | user_orders | status | UInt8 | 1待付 2已付 3取消 4退款 |
              | order_refunds | refund_amount | Decimal(18,2) | 退款金额 |

              ## 业务规则
              - 新用户定义: 注册时间在30天内的用户
              - GMV公式: SUM(CASE WHEN status=2 THEN amount ELSE 0 END)
                        - SUM(CASE WHEN status=4 THEN amount ELSE 0 END)
              - 金额单位: 存储为「分」，展示需除以100

              ## 相似 SQL 参考
              - "上月GMV": SELECT SUM(CASE WHEN status=2...) FROM ...
              → 生成 SQL
```

---

#### 3.2.8 关键数据结构

```python
@dataclass
class SchemaSnapshot:
    """检索结果的统一封装 — 无论知识来自文档还是自动拉取，格式一致"""
    tables: list["TableSchema"]
    field_semantics: list[KnowledgeEntry]  # 字段级语义
    business_rules: list[KnowledgeEntry]   # 指标口径
    sql_templates: list[KnowledgeEntry]    # 历史 SQL

    def to_prompt_text(self) -> str:
        """格式化为 LLM Prompt 可用的文本"""
        sections = []

        # 表结构
        sections.append("## 数据库表结构")
        for t in self.tables:
            sections.append(f"\n### {t.name} — {t.description}")
            sections.append("| 字段 | 类型 | 说明 |")
            sections.append("|------|------|------|")
            for c in t.columns:
                sections.append(f"| {c.name} | {c.type} | {c.comment} |")

        # 关键字段说明
        if self.field_semantics:
            sections.append("\n## 关键字段说明")
            for fs in self.field_semantics:
                source_tag = f"[来源:{fs.source.value}]" if fs.source == KnowledgeSource.AUTO_INTROSPECT else ""
                sections.append(f"- {fs.content} {source_tag}")

        # 业务规则
        if self.business_rules:
            sections.append("\n## 业务规则与指标口径")
            for br in self.business_rules:
                sections.append(f"- {br.content}")

        # SQL 参考
        if self.sql_templates:
            sections.append("\n## 相似问题参考")
            for tpl in self.sql_templates:
                sections.append(f"- 问题: {tpl.content}\n  SQL: {tpl.sql}")

        return "\n".join(sections)


@dataclass
class TableSchema:
    name: str
    description: str
    columns: list["ColumnInfo"]
    relations: list["TableRelation"]
    row_count_estimate: int = 0
    partition_key: str = ""  # ClickHouse 分区键
    tags: list[str] = field(default_factory=list)


@dataclass
class ColumnInfo:
    name: str
    type: str
    comment: str
    is_nullable: bool = True
    is_primary_key: bool = False
    enum_values: list[str] = field(default_factory=list)


@dataclass
class TableRelation:
    target_table: str
    join_key: str
    relation_type: str  # "many_to_one" | "one_to_one" | "one_to_many"
```

### 3.3 SQL 生成器

**输入**：
- 用户自然语言问题
- 相关表 Schema（由 Schema 探索器检索）
- 历史相似问题及其 SQL（从向量知识库检索 few-shot 示例）
- 数据源类型（ClickHouse / MySQL / PostgreSQL / Hive）

**输出**：
- 生成的 SQL 语句
- 生成逻辑的解释说明

**跨方言生成策略** — 两层策略确保多数据库适配：

| 策略 | 说明 | 适用场景 |
|------|------|---------|
| A. 让 LLM 生成标准 SQL，sqlglot 转方言 | LLM 输出 ANSI SQL → sqlglot 自动转 ClickHouse/PG 等 | 查询模式在各数据库间通用 |
| B. 直接注入特定方言 Prompt | Prompt 中明确目标方言的函数和语法规则 | 使用了某种数据库独有特性（如 ClickHouse ARRAY JOIN） |

**方言注入 Prompt 示例**（策略 B）：

```
## {datasource_type} 方言速查
- 日期截断: ClickHouse=toStartOfDay() | MySQL=DATE() | PG=DATE_TRUNC()
- 字符串截取: ClickHouse=substring(s, offset, length) | PG=SUBSTRING(s FROM offset FOR length)
- 日期格式化: ClickHouse=formatDateTime(dt, '%Y-%m-%d') | MySQL=DATE_FORMAT(dt, '%Y-%m-%d')
- LIMIT: ClickHouse=LIMIT n | MySQL=LIMIT n OFFSET m
- 数组: ClickHouse=arrayJoin() | PG=UNNEST()
- 时间戳转秒: ClickHouse=toUnixTimestamp(dt) | MySQL=UNIX_TIMESTAMP(dt) | PG=EXTRACT(EPOCH FROM dt)
```

**Few-shot 示例检索（向量库）**：

向量库存储的不只是 SQL，而是 `(question, schema_context, dialect, correct_sql)` 四元组。检索到的示例直接注入 Prompt 的 few-shot 部分，LLM 能复制其中的方言用法。

---

### 3.4 SQL 四层校验体系

保证 SQL 语法和语义绝对正确的核心保障。

```
┌─────────────────────────────────────────┐
│ Layer 1: Prompt 方言规则注入            │
│ 作用: 降低 LLM 生成错误 SQL 的概率      │
│ 技术: 动态组装方言速查表 + 函数映射     │
├─────────────────────────────────────────┤
│ Layer 2: 向量库 Few-shot 示例           │
│ 作用: 提供经过验证的正确 SQL 作为参考   │
│ 技术: ChromaDB 存储 (question, sql,     │
│       schema, dialect) 四元组           │
├─────────────────────────────────────────┤
│ Layer 3: ★ sqlglot 语法校验 + 方言转译  │
│ 作用: 零成本本地校验，覆盖 90%+ 的错误  │
│ 技术: sqlglot.parse() 语法检查          │
│       sqlglot.transpile() 方言转译      │
│       自定义函数白名单验证              │
├─────────────────────────────────────────┤
│ Layer 4: DB EXPLAIN 语义空跑            │
│ 作用: 最终安全网，100% 准确             │
│ 技术: EXPLAIN SYNTAX (ClickHouse)       │
│       EXPLAIN FORMAT=TREE (MySQL)       │
│       EXPLAIN (ANALYZE false) (PG)      │
└─────────────────────────────────────────┘
```

#### 3.4.1 Layer 3: sqlglot 校验 Node — 核心防线

```python
import sqlglot
import sqlglot.exp as exp
from sqlglot.errors import ErrorLevel

# sqlglot 支持的方言 (共 20+)
SUPPORTED_DIALECTS = {
    "clickhouse", "mysql", "postgres", "presto", "trino",
    "hive", "spark", "bigquery", "snowflake", "redshift",
    "duckdb", "sqlite", "tsql", "databricks", "teradata",
    "oracle", "starrocks", "doris", "tableau",
}


def validate_with_sqlglot(sql: str, dialect: str) -> dict:
    """
    sqlglot 三层校验:
    1. 语法解析 → 拦截语法错误
    2. 函数名白名单 → 拦截 LLM 幻觉函数
    3. 表名/列名基本检查 → 拦截明显的拼写错误
    """
    result = {"valid": True, "errors": [], "warnings": [], "transpiled_sql": sql}

    # ---- 1. 语法解析 ----
    try:
        parsed = sqlglot.parse(sql, dialect=dialect)
        if not parsed:
            # sqlglot.parse 对空输入或一些边界情况返回空列表
            result["valid"] = False
            result["errors"].append("无法解析: SQL 解析返回空")
            return result
    except sqlglot.errors.ParseError as e:
        result["valid"] = False
        result["errors"].append({
            "type": "syntax_error",
            "message": str(e),
            "line": e.errors[0].get("line") if hasattr(e, "errors") else None,
            "suggestion": "请检查关键字拼写、括号匹配、引号配对"
        })
        return result

    # ---- 2. 函数白名单校验 ----
    dialect_funcs = _get_dialect_functions(dialect)
    for node in parsed[0].walk():
        if isinstance(node, (exp.Anonymous, exp.Func)):
            func_name = (node.sql_name() or "").upper()
            # 跳过聚合函数(各数据库通用)和已知别名
            if func_name and not _is_universal_func(func_name) and func_name.lower() not in dialect_funcs:
                suggestion = _suggest_correct_function(func_name, dialect)
                result["warnings"].append({
                    "type": "unknown_function",
                    "function": func_name,
                    "suggestion": suggestion or f"'{func_name}' 在 {dialect} 中不存在，请替换为等价函数"
                })

    # ---- 3. 方言转译 (可选: LLM 输出标准 SQL, sqlglot 转为目标方言) ----
    if dialect != "mysql":
        try:
            result["transpiled_sql"] = sqlglot.transpile(
                sql, read="mysql", write=dialect
            )[0]
        except Exception:
            pass  # 转译失败不影响校验结果，执行时用原始 SQL

    return result


def _get_dialect_functions(dialect: str) -> set[str]:
    """获取指定方言的内置函数白名单"""
    try:
        generator = sqlglot.dialects.Dialect[dialect].generator_class()
        # UNION: schema 函数 + 方言扩展函数 + 格式化/聚合函数
        funcs = set()
        funcs.update(k.lower() for k in vars(generator.TRANSFORMS))
        funcs.update(k.lower() for k in vars(exp.Func))
        return funcs
    except KeyError:
        return set()


def _is_universal_func(name: str) -> bool:
    """跨数据库通用函数，跳过方言检查"""
    universal = {"COUNT", "SUM", "AVG", "MIN", "MAX", "COALESCE",
                  "CAST", "CASE", "NULLIF", "ROUND", "ABS",
                  "UPPER", "LOWER", "LENGTH", "TRIM", "CONCAT",
                  "NOW", "CURRENT_DATE", "CURRENT_TIMESTAMP"}
    return name.upper() in universal


def _suggest_correct_function(func_name: str, dialect: str) -> str | None:
    """根据方言提供函数修正建议 (维护一份方言函数映射表)"""
    MAPPINGS = {
        "clickhouse": {
            "DATE": "toDate()",
            "DATE_FORMAT": "formatDateTime()",
            "STR_TO_DATE": "parseDateTimeBestEffort()",
            "UNIX_TIMESTAMP": "toUnixTimestamp()",
            "GROUP_CONCAT": "groupArray()",
            "IFNULL": "ifNull()",
            "ROW_NUMBER": "row_number() | 需结合 WINDOW 子句",
        },
        "postgres": {
            "IFNULL": "COALESCE()",
            "DATE_FORMAT": "TO_CHAR()",
            "STR_TO_DATE": "TO_DATE()",
            "GROUP_CONCAT": "STRING_AGG()",
            "LIMIT n OFFSET m": "LIMIT n OFFSET m (PG 13+ 支持标准语法)",
        },
    }
    dialect_mapping = MAPPINGS.get(dialect, {})
    return dialect_mapping.get(func_name.upper())
```

#### 3.4.2 Layer 4: EXPLAIN 空跑校验 Node — 最终安全网

```python
# 各数据库的 EXPLAIN 语法 (不查数据只验证语义)
EXPLAIN_TEMPLATES = {
    "clickhouse": "EXPLAIN SYNTAX {sql}",          # 直接返回 AST，零开销
    "mysql":      "EXPLAIN FORMAT=TREE {sql}",     # 生成执行计划
    "postgres":   "EXPLAIN (ANALYZE false) {sql}", # 不实际执行
    "presto":     "EXPLAIN (TYPE VALIDATE) {sql}",  # Presto 语法验证
    "hive":       "EXPLAIN {sql}",                  # 生成计划
}

# 可配置跳过 EXPLAIN 的数据库 (云数仓可能产生费用或资源消耗)
EXPLAIN_SKIP_DIALECTS: set[str] = {"snowflake"}  # Settings 中可覆盖


async def explain_check(sql: str, dialect: str, executor) -> dict:
    """在目标数据库执行 EXPLAIN，验证语义正确性"""
    template = EXPLAIN_TEMPLATES.get(dialect)
    if not template:
        return {"valid": True, "errors": []}  # 未知方言跳过

    explain_sql = template.format(sql=sql)
    try:
        await executor.execute(explain_sql, timeout=10)
        return {"valid": True, "errors": []}
    except Exception as e:
        # 提取友好的错误信息 (去除堆栈)
        error_msg = str(e).split("Stack trace:")[0].strip()
        return {
            "valid": False,
            "errors": [{
                "type": "semantic_error",
                "message": error_msg,
                "suggestion": _extract_suggestion(error_msg)
            }]
        }
```

#### 3.4.3 完整校验流水线 & 错误回注

```
LLM 生成 SQL (generate_sql Node)
    │
    ▼
Layer 3: sqlglot 校验 Node
    ├─ parse 失败 → errors 回注 Prompt → 边回到 generate_sql Node
    ├─ 未知函数 → warnings 回注 Prompt → 边回到 generate_sql Node
    └─ 通过 → 进入 Layer 4
    │
    ▼
Layer 4: EXPLAIN 空跑 Node
    ├─ 失败 → errors 回注 Prompt → 边回到 generate_sql Node
    └─ 通过 → 进入 execute_sql Node
```

每次校验失败回退到 `generate_sql` Node 时，将上一轮的 SQL + 错误信息附加到 Prompt 中：

```python
# generate_sql Node 的 Prompt 在重试时会追加:
if state.get("validation_errors"):
    prompt += f"""
    ## 上一轮 SQL 失败，请修正
    错误的 SQL: {state["previous_sql"]}
    错误信息: {state["validation_errors"]}
    请分析上述错误并生成修正后的 SQL。
    """

if state.get("retry_count", 0) >= 2:
    prompt += "\n## 注意: 这是最后一次尝试，请仔细核对字段名和函数名"
```

#### 3.4.4 各层职责总览

| 层 | 技术 | 捕获的错误类型 | 成本 | 拦截率 |
|---|------|--------------|------|--------|
| Layer 1 | Prompt 方言注入 | 基础函数选错、语法格式偏差 | 零 | ~30% |
| Layer 2 | 向量库 Few-shot | 选错表、漏 JOIN、字段名编造 | 低 (检索) | ~20% |
| Layer 3 | sqlglot 本地校验 | 语法错误、非法函数名、括号不匹配 | 极低 (毫秒级) | ~40% |
| Layer 4 | EXPLAIN 空跑 | 列不存在、类型不匹配、表不存在 | 中 (需 DB 连接) | ~10% |

**叠加后总拦截率 → 100%**。语法错误在 Layer 3 被拦截，语义错误在 Layer 4 被捕获，都未命中的极端边缘情况通过执行失败重试兜底。

#### 3.4.5 安全拦截层（独立于语法校验）

安全拦截在 **sqlglot 校验之前** 执行，先于语法检查：

- DML 写操作：INSERT / UPDATE / DELETE / MERGE / REPLACE
- DDL 操作：DROP / CREATE / ALTER / TRUNCATE / RENAME
- 权限操作：GRANT / REVOKE
- 危险函数：sleep() / benchmark() / 存储过程调用
- 可配置限流：最大扫描行数、最大执行时间、每小时查询上限

安全拦截失败直接 `return error` 到 `build_response` Node，**不进入重试循环**。

### 3.5 数据执行器

- **多数据源适配**：通过连接器模式支持 ClickHouse、MySQL、PostgreSQL、Presto/Trino 等
- **连接池管理**：每个数据源维护独立连接池
- **查询超时控制**：`statement_timeout` / `max_execution_time` 设置
- **结果格式化**：将原始查询结果转为结构化数据（DataFrame / JSON），统一后续处理
- **异步执行**：支持异步查询，大查询可轮询获取结果

### 3.6 数据分析引擎

LLM 驱动的多维度自动分析：

| 分析类型 | 说明 | 触发条件 |
|---------|------|---------|
| 描述性统计 | 均值/中位数/分布/标准差 | 所有数值型结果默认执行 |
| 趋势分析 | 同比/环比/移动平均 | 时间序列数据 |
| 归因分析 | 维度下钻找变化原因 | 用户指定"为什么"类问题 |
| 异常检测 | Z-Score / IQR 识别离群值 | 聚合结果中存在明显波动 |
| 占比分析 | 饼图/堆叠图/帕累托 | 分类维度 + 数值指标 |

**实现方式**：
- 规则引擎处理确定性分析（描述统计、趋势计算）
- LLM 处理开放性分析（归因、异常解读）
- Python pandas/numpy 执行计算层面操作

### 3.7 可视化引擎

支持生成以下图表（使用 ECharts / Plotly）：

- **折线图**：时间序列趋势
- **柱状图/条形图**：分类对比
- **饼图/环形图**：占比分析
- **散点图**：相关性分析
- **热力图**：交叉维度分析
- **表格**：明细数据展示

**智能选图逻辑**：
- LLM 根据查询结果的列类型（时间列 / 分类列 / 数值列）自动推荐图表类型
- 用户可以后续通过自然语言调整："用饼图展示"

### 3.8 记忆系统

智能体采用三层记忆架构：短期记忆驱动当前会话的连贯对话，长期记忆积累跨会话的知识复用，工作记忆承载 LangGraph 流水线内的中间状态。

---

#### 3.8.1 三层记忆架构

```
┌─────────────────────────────────────────────────────────────┐
│  记忆层        范围       存储        生命周期                │
├─────────────────────────────────────────────────────────────┤
│  短期记忆      会话级     PostgreSQL    会话期间可读写，      │
│  (Short-term)            (checkpointer)  会话结束后归档       │
├─────────────────────────────────────────────────────────────┤
│  长期记忆      用户/项目级 ChromaDB      跨会话持久化，        │
│  (Long-term)                             语义检索召回          │
├─────────────────────────────────────────────────────────────┤
│  工作记忆      图内单次   AnalysisState  单次 invoke 生命周期  │
│  (Working)     执行       (TypedDict)    执行结束即销毁        │
└─────────────────────────────────────────────────────────────┘
```

---

#### 3.8.2 短期记忆 — LangGraph Checkpointer

**职责**：会话上下文、追问理解、对话连贯性。

**实现**：LangGraph 内置的 checkpointer 机制，每个 Node 的输入输出自动持久化到 PostgreSQL 的 `checkpoints` 表中。恢复会话时读取 `thread_id` 对应状态即可。

```python
from langgraph.checkpoint.postgres import PostgresSaver

checkpointer = PostgresSaver.from_conn_string(POSTGRES_URL)
checkpointer.setup()

app = workflow.compile(checkpointer=checkpointer)

# 会话 A
config_a = {"configurable": {"thread_id": "user_42_session_abc"}}
result = await app.ainvoke({"user_query": "上月销售额Top10"}, config=config_a)

# 追问 — 借助短期记忆理解 "它们" 指代什么
result = await app.ainvoke({"user_query": "它们的环比变化是多少？"}, config=config_a)

# 新会话 B — 隔离的上下文
config_b = {"configurable": {"thread_id": "user_42_session_xyz"}}
result = await app.ainvoke({"user_query": "查一下库存表"}, config=config_b)


# 短期记忆中的状态结构
@dataclass
class SessionContext:
    session_id: str
    thread_id: str
    user_id: str
    created_at: datetime
    last_active_at: datetime

    # 从 checkpointer 恢复的最近 N 轮对话
    conversation_history: list[ConversationTurn]

    # 当前上下文中暂存的状态
    current_datasource: str | None      # 当前操作的数据源
    current_tables: list[str]           # 当前关注的前几张表
    last_sql: str | None                # 上一次执行的 SQL
    last_result_summary: str | None     # 上一次结果的摘要 (不存全量数据)


@dataclass
class ConversationTurn:
    """单轮对话记录"""
    turn_id: int
    user_query: str
    generated_sql: str | None
    execution_success: bool
    analysis_summary: str | None
    chart_type: str | None
    timestamp: datetime
```

**短期记忆的边界**：
- 超过 30 分钟的会话，checkpointer 状态归档到 `sessions_archive` 表，下次访问时注入历史摘要而非完整状态
- 单会话最多保留 50 轮对话，超出后自动摘要前 20 轮为一段概括文本

---

#### 3.8.3 长期记忆 — 用户级与项目级

**职责**：跨会话复用已验证的知识。

```
┌──────────────────────────────────────────┐
│  长期记忆分类                             │
├─────────────┬────────────────────────────┤
│  用户级记忆  │  单个用户的使用偏好与历史    │
│             │  - 惯用数据源              │
│             │  - 偏好的可视化风格         │
│             │  - 常用查询模式            │
│             │  - 个人标注的字段别名       │
├─────────────┼────────────────────────────┤
│  项目级记忆  │  项目内所有用户共享的知识    │
│             │  - 经过验证的 SQL 模板      │
│             │  - 已知的 JOIN 陷阱        │
│             │  - 慢查询的替代方案         │
│             │  - 业务规则的变更日志       │
└─────────────┴────────────────────────────┘
```

```python
from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum


class MemoryType(Enum):
    USER_PREFERENCE = "user_preference"     # 用户偏好
    SQL_TEMPLATE = "sql_template"           # 已验证 SQL
    LEARNED_PATTERN = "learned_pattern"     # 学到的模式
    CORRECTION = "correction"               # 用户纠正记录
    PROJECT_RULE = "project_rule"           # 项目级规则


@dataclass
class LongTermMemory:
    """长期记忆条目"""
    id: str                          # UUID
    memory_type: MemoryType
    scope: str                       # "user:{user_id}" | "project:{project_id}"
    content: str                     # 自然语言描述，用于向量检索
    payload: dict                    # 结构化数据 (SQL 模板、参数等)
    embedding: list[float] | None    # 向量，写入 ChromaDB
    created_at: datetime = field(default_factory=datetime.now)
    last_accessed_at: datetime = field(default_factory=datetime.now)
    access_count: int = 0            # 被检索次数
    confidence: float = 1.0          # 可信度 (用户纠正记录高可信，自动模板低可信)
    ttl_days: int | None = None      # 过期时间 (None = 永久)


class LongTermMemoryStore:
    """
    长期记忆的写入与检索。

    检索策略:
    1. 语义相似: 用当前 query 查 ChromaDB → 取 Top-K
    2. 时效衰减: access_count 低的条目定期降权
    3. 置信度剪枝: confidence < 0.3 的自动模板不再召回
    """

    def __init__(self, vector_store: Chroma, pg_pool: AsyncConnectionPool):
        self.vector_store = vector_store
        self.pg = pg_pool

    async def search(self, query: str, memory_type: MemoryType | None, top_k: int = 5) -> list[LongTermMemory]:
        """语义检索 + 置信度过滤"""
        filter_expr = {"confidence": {"$gte": 0.3}}
        if memory_type:
            filter_expr["memory_type"] = memory_type.value

        results = await self.vector_store.similarity_search(query, k=top_k, filter=filter_expr)
        return [self._to_memory(r) for r in results]

    async def save_sql_template(self, user_query: str, sql: str, dialect: str, verified: bool = False):
        """保存成功的 SQL 为模板 — 下次相似问题直接复用"""
        entry = LongTermMemory(
            memory_type=MemoryType.SQL_TEMPLATE,
            content=f"问题: {user_query}\n方言: {dialect}\nSQL: {sql}",
            payload={"question": user_query, "sql": sql, "dialect": dialect},
            confidence=0.9 if verified else 0.5,
        )
        await self._upsert(entry)

    async def save_correction(self, user_id: str, wrong_sql: str, correct_sql: str, feedback: str):
        """用户纠正 — 高可信度永久记忆"""
        entry = LongTermMemory(
            memory_type=MemoryType.CORRECTION,
            scope=f"user:{user_id}",
            content=f"错误SQL: {wrong_sql}\n正确SQL: {correct_sql}\n原因: {feedback}",
            payload={"wrong_sql": wrong_sql, "correct_sql": correct_sql, "feedback": feedback},
            confidence=0.95,
        )
        await self._upsert(entry)

    async def save_preference(self, user_id: str, preference: str, value: Any):
        """用户偏好 — 影响后续所有查询的行为"""
        entry = LongTermMemory(
            memory_type=MemoryType.USER_PREFERENCE,
            scope=f"user:{user_id}",
            content=f"用户偏好: {preference} = {value}",
            payload={"preference": preference, "value": value},
            confidence=1.0,
        )
        await self._upsert(entry)

    async def get_preferences(self, user_id: str) -> dict:
        """获取用户所有偏好 (精确匹配, 不走向量)"""
        rows = await self.pg.fetch(
            "SELECT payload FROM long_term_memories WHERE memory_type = $1 AND scope = $2",
            MemoryType.USER_PREFERENCE.value, f"user:{user_id}"
        )
        return {r["payload"]["preference"]: r["payload"]["value"] for r in rows}
```

**记忆注入时机**：

```python
# Lifecycle hook: 每次会话开始时
async def on_session_start(thread_id: str, user_id: str, user_query: str):
    prefs = await memory_store.get_preferences(user_id)

    # 注入用户偏好的默认数据源
    if default_ds := prefs.get("default_datasource"):
        ds = default_ds
    else:
        ds = "clickhouse_prod"

    # 检索相关长期记忆
    related_memories = await memory_store.search(user_query, top_k=5)

    # 注入到 system prompt 的 long_term_memories 字段
    ...
```

---

#### 3.8.4 记忆衰减与清理

```python
class MemoryMaintenance:
    """定期维护任务"""

    async def decay_old_templates(self):
        """30天未使用的 SQL 模板降置信度"""
        cutoff = datetime.now() - timedelta(days=30)
        await self.pg.execute("""
            UPDATE long_term_memories
            SET confidence = confidence * 0.5
            WHERE memory_type = 'sql_template'
              AND last_accessed_at < $1
              AND confidence >= 0.4
        """, cutoff)

    async def prune_low_confidence(self):
        """删除置信度过低的自动模板 (保持向量库效率)"""
        await self.pg.execute("""
            DELETE FROM long_term_memories
            WHERE memory_type = 'sql_template'
              AND confidence < 0.3
              AND access_count = 0
        """)

    async def archive_sessions(self):
        """归档超过30天的会话 checkpoint"""
        cutoff = datetime.now() - timedelta(days=30)
        await self.pg.execute("""
            INSERT INTO sessions_archive (thread_id, summary, archived_at)
            SELECT thread_id,
                   -- LLM 对完整会话生成一段摘要
                   summarize_session(thread_id),
                   NOW()
            FROM active_sessions
            WHERE last_active_at < $1
        """, cutoff)
```

#### 3.8.5 上下文窗口管理 — LLM 调用时的内容裁剪

Checkpointer 存储了所有 Node 的完整状态，**但不会直接喂给 LLM**。每个 Node 在调用 LLM 前都通过统一的上下文裁剪函数，只注入精选的必要信息。

---

**核心原则**：Checkpointer 存储 ≠ LLM 上下文。PostgreSQL 存多少都不限，但 Prompt 严格控制在模型上下文窗口的 30-50%。

---

**三层裁剪策略**：

```
┌──────────────────────────────────────────────────────────┐
│ 层级        范围          注入方式        保留量           │
├──────────────────────────────────────────────────────────┤
│ 热数据      最近3轮完整    直接注入 Prompt  完整 SQL +      │
│ (热窗口)    对话                           分析摘要        │
├──────────────────────────────────────────────────────────┤
│ 温数据      4-10轮对话     压缩摘要注入     每轮压缩为      │
│ (近窗口)                                   1-2句概括       │
├──────────────────────────────────────────────────────────┤
│ 冷数据      10轮以上       ChromaDB        语义相似        │
│ (长期记忆)  对话           向量检索召回     匹配 Top-3       │
└──────────────────────────────────────────────────────────┘
```

```python
async def build_llm_context(state: AnalysisState, node_name: str) -> str:
    """
    每个 Node 调用 LLM 前统一走此函数裁剪上下文。

    不同 Node 还可以声明自己需要的额外字段 —
    例如 generate_sql Node 需要 schema，
    analyze_result Node 不需要 schema 但需要 data_sample。
    """
    history = state.get("conversation_history", [])
    parts = []

    # ---- 热数据: 最近3轮完整注入 ----
    for turn in history[-3:]:
        parts.append(f"用户: {turn.user_query}")
        if turn.generated_sql:
            parts.append(f"执行的SQL: {turn.generated_sql}")
        if turn.analysis_summary:
            parts.append(f"分析结论: {turn.analysis_summary}")

    # ---- 温数据: 4-10轮压缩为摘要 ----
    warm_turns = history[-10:-3]
    if warm_turns and len(history) > 3:
        # 用 小模型/本地规则 生成摘要，而非塞原始对话
        summary = await _summarize_turns(warm_turns)
        parts.append(f"[前序对话摘要] {summary}")

    # ---- 冷数据: 11轮以上走向量检索 ----
    if len(history) > 10:
        hits = await long_term_store.search(state["user_query"], top_k=3)
        if hits:
            parts.append(f"[历史相关经验] {[h.payload.get('question') for h in hits]}")

    return "\n---\n".join(parts)


async def _summarize_turns(turns: list) -> str:
    """用小模型将多轮对话压缩为一句话摘要"""
    lines = []
    for t in turns:
        lines.append(f"Q: {t.user_query}")
        if t.analysis_summary:
            lines.append(f"A: {t.analysis_summary[:200]}")
    prompt = f"将以下对话概括为一句话，只保留关键信息:\n" + "\n".join(lines)
    response = await cheap_llm.ainvoke(prompt)  # 用 cheaper model
    return response.content.strip()
```

---

**每个 Node 精选自己的字段**：

```python
# generate_sql Node
async def generate_sql_node(state: AnalysisState) -> dict:
    prompt = ChatPromptTemplate.from_messages([
        ("system", SQL_GENERATION_SYSTEM_PROMPT),   # ~500 tokens
        ("system", "{long_term_memories}"),          # ~800 tokens (精选3条)
        ("system", "{skill_instructions}"),          # ~300 tokens (仅激活的 skill)
        ("user", """
         ## 表结构 (仅相关表)
         {schemas}                                   # ~2000 tokens

         ## 业务规则
         {business_rules}                            # ~500 tokens (仅命中的)

         ## 最近上下文
         {recent_context}                            # ~1500 tokens (3轮)

         ## 当前问题
         {user_query}                                # ~50 tokens
         """),
    ])
    # 总 Prompt ~5000 tokens，远低于模型的 128K 上下文窗口

    # generate_sql Node 不需要的字段:
    # ❌ chart_config (那是 generate_chart Node 用的)
    # ❌ query_result (那是 analyze_result Node 用的)
    # ❌ validation_errors (只在重试时注入，首次为空)
    # ❌ 前 4 轮以上的完整历史

    context = await build_llm_context(state, node_name="generate_sql")
    chain = prompt | llm | parser
    return await chain.ainvoke({
        "schemas": state["resolved_schema"].to_prompt_text(),
        "business_rules": _format_business_rules(state),
        "long_term_memories": state.get("long_term_memories_text", ""),
        "skill_instructions": state.get("skill_prompt_override", ""),
        "recent_context": context,
        "user_query": state["user_query"],
    })


# analyze_result Node — 完全不同的 Prompt 结构
async def analyze_result_node(state: AnalysisState) -> dict:
    prompt = ChatPromptTemplate.from_messages([
        ("system", DATA_ANALYSIS_SYSTEM_PROMPT),     # ~300 tokens
        ("system", "{skill_instructions}"),           # ~300 tokens
        ("user", """
         ## 用户问题: {user_query}                   # ~50 tokens
         ## 执行的 SQL: {generated_sql}               # ~300 tokens
         ## 数据 (前200行): {data_sample}              # ~3000 tokens (截断后)
         ## 统计摘要: {statistics}                     # ~500 tokens
         ## 最近上下文: {recent_context}                # ~1500 tokens
         """),
    ])
    # analyze_result Node 不需要的字段:
    # ❌ schemas (表结构跟分析结果无关)
    # ❌ business_rules (这些用于生成 SQL，不是分析)
    # ❌ validation 相关状态
```

---

**追问场景**：用户追问 "它们的环比变化是多少" 时，理解「它们」不需要全量历史。

```
错误做法: 把所有 50 轮对话塞进 {history} placeholder
正确做法: 从最近 3 轮的 ConversationTurn 中提取关键信息

Q3: 上月销售额Top10品类
SQL: SELECT category, SUM(amount) ...
结果摘要: 电子产品128万居首，家居品类102万次之，美妆98万第三
涉及表: user_orders, products

Q4: "它们的环比变化是多少？"
→ LLM 从 Q3 的摘要中理解 "它们" = [电子, 家居, 美妆]
→ 生成: SELECT category, SUM(amount) ... WHERE category IN ('电子','家居','美妆')
         AND created_at BETWEEN ...
```

---

**数据结果截断**：SQL 返回百万行不能全塞给 LLM。

```python
# execute_sql Node 之后的自动处理
async def execute_sql_node(state: AnalysisState) -> dict:
    rows = await db.execute(state["generated_sql"])
    total = len(rows)

    # LLM 分析用: 只保留前 200 行
    sample = rows[:200]

    # 自动计算统计摘要 (pandas, 不耗 LLM token)
    stats = compute_statistics(rows)

    return {
        "query_result_sample": sample,        # ~3000 tokens
        "query_result_full_count": total,     # "共 1,234,567 行"
        "query_result_statistics": stats,     # 均值/中位数/分布
    }
```

---

**完整 Prompt 大小控制**：

| Node | 涉及字段 | 估算 tokens |
|------|---------|------------|
| classify_intent | user_query + conversation_history(3轮) | ~2000 |
| retrieve_schema | user_query (直接走向量检索，不调 LLM) | ~50 |
| generate_sql | schema + business_rules + long_term_memories + recent_context + query | ~5000-7000 |
| layer3_validate | generated_sql (sqlglot 本地，不调 LLM) | 0 |
| layer4_explain | generated_sql (DB 执行 EXPLAIN，不调 LLM) | 0 |
| execute_sql | generated_sql (DB 执行，不调 LLM) | 0 |
| analyze_result | query + sql + data_sample(截断) + statistics + recent_context | ~5000-7000 |
| generate_chart | data_sample(截断) + column_types | ~3000 |
| build_response | 各 Node 输出组装 (JSON，不调 LLM) | 0 |

**关键结论**：8 个 Node 中只有 4 个调用 LLM（classify_intent、generate_sql、analyze_result、generate_chart），每个都独立裁剪上下文，每次调用 Prompt 控制在 7000 tokens 以内，远低于模型上下文上限。

### 3.9 MCP 服务集成

遵循 Anthropic 的 **Model Context Protocol (MCP)** 规范，智能体可以作为 MCP Client 导入外部服务扩展能力，也可以作为 MCP Server 对外暴露自己的工具。

---

#### 3.9.1 MCP Client — 导入外部服务

智能体内置 MCP Client，启动时通过配置文件发现并连接外部 MCP Server，将其工具注入到 LangGraph 的工具层中。

**MCP Server 配置文件** (`config/mcp_servers.yaml`)：

```yaml
# 智能体启动时读取此文件，自动连接所有 MCP Server
mcp_servers:
  # 示例 1: 文件系统访问 — 允许用户上传 CSV/Excel 直接分析
  filesystem:
    transport: stdio
    command: "npx"
    args: ["-y", "@anthropic-ai/mcp-filesystem", "/data/uploads"]

  # 示例 2: 知识库搜索 — 连接公司内部 Wiki
  confluence:
    transport: sse
    url: "http://confluence-mcp.internal:3000/sse"

  # 示例 3: 指标平台 API — 实时验证指标口径
  metrics_store:
    transport: stdio
    command: "python"
    args: ["-m", "metrics_store_mcp"]
    env:
      METRICS_API_KEY: "${METRICS_API_KEY}"
```

**MCP Client 实现**：

```python
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client
from contextlib import AsyncExitStack
from langchain_core.tools import BaseTool, StructuredTool


class MCPClientManager:
    """
    管理 MCP 客户端生命周期。

    核心职责:
    1. 启动时按配置连接所有 MCP Server
    2. 将 MCP 提供的 tools 转换为 LangChain BaseTool
    3. 健康检查 — 定期 ping，断线自动重连
    4. 关闭时优雅断开所有连接
    """

    def __init__(self, config_path: str = "config/mcp_servers.yaml"):
        self.config_path = config_path
        self.sessions: dict[str, ClientSession] = {}
        self.exit_stack = AsyncExitStack()
        self.langchain_tools: dict[str, BaseTool] = {}

    async def connect_all(self):
        """启动时: 并发连接所有 MCP Server"""
        config = yaml.safe_load(Path(self.config_path).read_text())

        tasks = []
        for name, server_config in config["mcp_servers"].items():
            tasks.append(self._connect_single(name, server_config))

        results = await asyncio.gather(*tasks, return_exceptions=True)
        for name, result in zip(config["mcp_servers"], results):
            if isinstance(result, Exception):
                logger.error(f"MCP Server '{name}' 连接失败: {result}")
            else:
                logger.info(f"MCP Server '{name}' 连接成功, {len(result)} tools")

    async def _connect_single(self, name: str, config: dict) -> list[BaseTool]:
        """连接单个 MCP Server 并注册其 tools"""
        transport = config["transport"]

        if transport == "stdio":
            server_params = StdioServerParameters(
                command=config["command"],
                args=config.get("args", []),
                env=self._resolve_env(config.get("env", {})),
            )
            transport_ctx = stdio_client(server_params)
        elif transport == "sse":
            transport_ctx = self._sse_client(config["url"])
        else:
            raise ValueError(f"Unsupported transport: {transport}")

        streams = await self.exit_stack.enter_async_context(transport_ctx)
        session = await self.exit_stack.enter_async_context(ClientSession(*streams))
        await session.initialize()

        # 获取 MCP Server 的 tools 列表
        mcp_tools = await session.list_tools()

        # 转换为 LangChain BaseTool (带上 namespace 避免冲突)
        langchain_tools = []
        for mcp_tool in mcp_tools:
            langchain_tool = self._mcp_to_langchain_tool(mcp_tool, session, namespace=name)
            langchain_tools.append(langchain_tool)
            self.langchain_tools[f"{name}__{mcp_tool.name}"] = langchain_tool

        self.sessions[name] = session
        return langchain_tools

    def _mcp_to_langchain_tool(self, mcp_tool, session: ClientSession, namespace: str) -> BaseTool:
        """将 MCP tool 适配为 LangChain BaseTool"""

        async def _call(**kwargs) -> str:
            result = await session.call_tool(mcp_tool.name, arguments=kwargs)
            return result.content[0].text if result.content else ""

        return StructuredTool(
            name=f"{namespace}__{mcp_tool.name}",
            description=mcp_tool.description,
            coroutine=_call,
            args_schema=self._build_schema(mcp_tool.inputSchema),
        )

    def get_all_tools(self) -> list[BaseTool]:
        """返回所有 MCP 转换来的工具 — 供 LangGraph Agent 使用"""
        return list(self.langchain_tools.values())

    async def health_check(self):
        """定期健康检查 — 断线自动重连"""
        for name, session in self.sessions.items():
            try:
                await session.send_ping()
            except Exception:
                logger.warning(f"MCP Server '{name}' 断线, 尝试重连...")
                await self._reconnect(name)

    async def close_all(self):
        """关闭所有连接"""
        await self.exit_stack.aclose()
```

---

#### 3.9.2 MCP Server — 对外暴露能力

智能体将自己的分析能力通过 MCP 协议暴露给外部 Agent（如 Claude Code、其他分析平台），实现 Agent-to-Agent 协作。

```python
# 智能体对外暴露的 MCP Tools:
EXPOSED_TOOLS = [
    {
        "name": "query_database",
        "description": "以自然语言查询数据库，智能体自动生成并执行 SQL，返回分析结果与图表",
        "inputSchema": {
            "type": "object",
            "properties": {
                "question": {"type": "string", "description": "自然语言问题，如'上月销售额Top10品类'"},
                "datasource": {"type": "string", "description": "目标数据源名称"},
                "chart": {"type": "boolean", "description": "是否生成图表", "default": True},
            },
            "required": ["question"]
        }
    },
    {
        "name": "list_datasources",
        "description": "列出当前可用的所有数据源及其描述",
        "inputSchema": {"type": "object", "properties": {}}
    },
    {
        "name": "get_table_schema",
        "description": "获取指定表的完整结构信息",
        "inputSchema": {
            "type": "object",
            "properties": {
                "datasource": {"type": "string"},
                "table_name": {"type": "string"},
            },
            "required": ["datasource", "table_name"]
        }
    },
    {
        "name": "get_metrics",
        "description": "查询业务指标口径，返回指标定义和计算公式",
        "inputSchema": {
            "type": "object",
            "properties": {
                "metric_name": {"type": "string", "description": "指标名称，如'GMV'、'ARPU'"}
            },
            "required": ["metric_name"]
        }
    },
]

# 对外 MCP Server 启动
from mcp.server.fastmcp import FastMCP

mcp = FastMCP("data-analysis-agent")

@mcp.tool()
async def query_database(question: str, datasource: str, chart: bool = True) -> dict:
    """以自然语言查询数据库"""
    result = await app.ainvoke({
        "user_query": question,
        "datasource": datasource,
    })
    return result["final_response"]

@mcp.tool()
async def list_datasources() -> list[dict]:
    """列出可用数据源"""
    return [{"name": k, "description": v.description} for k, v in registry.list_all().items()]

@mcp.tool()
async def get_table_schema(datasource: str, table_name: str) -> dict:
    """获取表结构"""
    schema = await schema_manager.get_or_fetch_schema(datasource, [table_name])
    return schema.tables[0].__dict__ if schema.tables else {}

@mcp.tool()
async def get_metrics(metric_name: str) -> dict | None:
    """查询指标口径"""
    rules = await business_rule_store.search_business_rules(metric_name, top_k=1)
    return rules[0].__dict__ if rules else None

# 启动方式:
# python -m src.mcp_server
# 或通过 claude_code_mcp.json 配置为 Claude Code 的 MCP Server
```

---

#### 3.9.3 MCP 在 LangGraph 中的集成位置

```
                    ┌─────────────────────┐
                    │    config/           │
                    │    mcp_servers.yaml  │ ← MCP Server 注册表
                    └──────────┬──────────┘
                               │ 启动时加载
                               ▼
┌──────────────────────────────────────────────────────┐
│              MCPClientManager                        │
│  connect_all() → convert to LangChain BaseTool       │
│                                                     │
│  MCP Tools:           Agent 自有 Tools:              │
│  - filesystem__read   - SchemaExplorerTool          │
│  - confluence__search - SQLGeneratorTool            │
│  - metrics_store__get - DBExecutorTool              │
│                       - DataAnalyzerTool            │
│                       - ChartGeneratorTool          │
└──────────────────────┬───────────────────────────────┘
                       │
                       ▼
┌──────────────────────────────────────────────────────┐
│  LangGraph 工具层 (统一 BaseTool 接口)                │
│  nodes 通过 tool name 调用, 不区分来源                  │
└──────────────────────────────────────────────────────┘
```

**MCP 工具的调用方式**：在需要动态决策的场景（如用户上传了一个 CSV 文件要求分析），LangGraph 可以配备一个 Agent Node 来动态选择和调用 MCP 工具，而非固定走 8 个流水线 Node。

```python
# 扩展的 StateGraph: 增加一个 MCP Agent Node
# 当用户意图被 classify_intent 识别为 "file_analysis" 时路由到此处

workflow.add_node("mcp_agent", mcp_agent_node)

def route_by_intent(state: AnalysisState) -> str:
    if state["intent"] == "file_analysis":
        return "mcp_agent"       # 动态工具调用
    return "retrieve_schema"     # 标准流水线

workflow.add_conditional_edges("classify_intent", route_by_intent, {
    "mcp_agent": "mcp_agent",
    "retrieve_schema": "retrieve_schema",
})

# MCP Agent Node: 使用 LangGraph 的 create_react_agent
from langgraph.prebuilt import create_react_agent

mcp_agent = create_react_agent(
    model=ChatOpenAI(model="gpt-4o"),
    tools=mcp_manager.get_all_tools(),
    system_prompt="你是一个数据分析助手，可以访问文件系统和外部知识库。"
)
```

---

### 3.10 Skills 技能系统

Skills 是自包含、可组合的能力扩展包，遵循 **A2A (Agent-to-Agent) 互操作规范**。每个 Skill 声明自己的触发条件、能力边界和依赖关系。

---

#### 3.10.1 Skill 规范

```yaml
# skills/data_quality_check/SKILL.md
---
name: data-quality-check
version: 1.0.0
description: 对查询结果执行数据质量检查 (空值率、重复值、异常值)
author: data-team
tags: [quality, validation, production]

# 触发规则 — 智能体自动判断何时激活此 Skill
triggers:
  keywords: [数据质量, 空值, 重复, 异常检测, 数据校验, 完整性]
  intents: [aggregation, analysis]     # 或在这些意图类型下自动启用
  tables: []                           # 指定表名列表, 查询这些表时自动激活

# 依赖
depends_on:
  mcp_servers: []
  skills: []                           # 可依赖其他 Skill
  python_packages: [pandas, numpy]

# 此 Skill 提供的工具
tools:
  - name: check_null_rate
    description: 检查指定列的空值率
  - name: check_duplicates
    description: 检查指定列的重复值
  - name: detect_outliers
    description: 用 Z-Score 方法检测异常值

# 注入到 System Prompt 的指令
system_prompt_override: |
  当用户询问数据质量相关问题时，你应当:
  1. 在 SQL 生成后自动附加质量检查
  2. 在分析结论中单独列出数据质量问题
  3. 对于空值率 > 10% 的字段，主动提示用户

# 输出格式扩展
output_schema_extension:
  quality_report:
    type: object
    properties:
      null_rates: {type: array}
      duplicate_counts: {type: array}
      outliers: {type: array}
```

---

#### 3.10.2 Skill 引擎

```python
from dataclasses import dataclass
from pathlib import Path
import yaml


@dataclass
class Skill:
    name: str
    version: str
    description: str
    triggers: dict          # keywords, intents, tables
    depends_on: dict        # mcp_servers, skills, python_packages
    tools: list[dict]       # 导出给 LangGraph 的工具定义
    system_prompt_override: str
    output_schema_extension: dict
    source_path: Path
    enabled: bool = True


class SkillManager:
    """
    管理 Skill 的发现、加载、激活与生命周期。

    Skill 目录结构:
    skills/
    ├── data_quality_check/
    │   ├── SKILL.md           # Skill 描述 (YAML frontmatter)
    │   ├── tools.py           # 工具实现
    │   └── prompts.py         # Skill 专属 Prompt 模板
    ├── anomaly_detection/
    │   ├── SKILL.md
    │   ├── tools.py
    │   └── models/
    │       └── isolation_forest.pkl
    └── custom_report/
        ├── SKILL.md
        └── templates/
            └── weekly_report.jinja2
    """

    def __init__(self, skills_dir: str = "skills"):
        self.skills_dir = Path(skills_dir)
        self.skills: dict[str, Skill] = {}

    async def discover(self):
        """启动时: 扫描 skills/ 目录, 发现所有 Skill"""
        for skill_md in self.skills_dir.glob("*/SKILL.md"):
            skill = self._parse_skill_manifest(skill_md)

            # 检查依赖
            missing = await self._check_dependencies(skill)
            if missing:
                logger.warning(
                    f"Skill '{skill.name}' 缺少依赖: {missing}, 已禁用"
                )
                skill.enabled = False

            self.skills[skill.name] = skill
            logger.info(f"Skill 已加载: {skill.name} v{skill.version}")

    def match_skills(self, user_query: str, intent: str, tables: list[str]) -> list[Skill]:
        """
        根据用户输入匹配应激活的 Skill。

        匹配规则 (OR 逻辑):
        1. keywords 命中查询文本
        2. intents 匹配当前意图
        3. tables 匹配当前查询的表
        """
        activated = []
        query_lower = user_query.lower()

        for skill in self.skills.values():
            if not skill.enabled:
                continue

            triggers = skill.triggers

            # 关键词命中
            if any(kw.lower() in query_lower for kw in triggers.get("keywords", [])):
                activated.append(skill)
                continue

            # 意图匹配
            if intent in triggers.get("intents", []):
                activated.append(skill)
                continue

            # 表匹配
            if set(tables) & set(triggers.get("tables", [])):
                activated.append(skill)
                continue

        return activated

    def get_active_tools(self, activated_skills: list[Skill]) -> list[BaseTool]:
        """获取当前激活 Skills 的所有工具"""
        tools = []
        for skill in activated_skills:
            skill_module = self._load_skill_module(skill)
            for tool_def in skill.tools:
                tools.append(skill_module.get_tool(tool_def["name"]))
        return tools

    def build_skill_prompt(self, activated_skills: list[Skill]) -> str:
        """构建 Skill System Prompt 追加部分"""
        if not activated_skills:
            return ""

        sections = ["\n## 激活的技能\n"]
        for skill in activated_skills:
            sections.append(f"### {skill.name}")
            sections.append(skill.system_prompt_override)
            sections.append("")
        return "\n".join(sections)

    def _parse_skill_manifest(self, skill_md_path: Path) -> Skill:
        """解析 SKILL.md 的 YAML frontmatter"""
        content = skill_md_path.read_text(encoding="utf-8")
        _, frontmatter, body = content.split("---", 2)
        manifest = yaml.safe_load(frontmatter)
        return Skill(
            name=manifest["name"],
            version=manifest["version"],
            description=manifest["description"],
            triggers=manifest.get("triggers", {}),
            depends_on=manifest.get("depends_on", {}),
            tools=manifest.get("tools", []),
            system_prompt_override=body.strip(),
            output_schema_extension=manifest.get("output_schema_extension", {}),
            source_path=skill_md_path.parent,
        )
```

---

#### 3.10.3 Skill 在 LangGraph 中的注入点

```
用户输入 + 意图 + 表列表
        │
        ▼
┌──────────────────────┐
│ SkillManager         │
│ .match_skills()      │  ← 关键词 + 意图 + 表名 三重匹配
│ → 返回激活的 Skill 列表│
└──────────┬───────────┘
           │
    ┌──────┴──────┐
    │             │
    ▼             ▼
① LangGraph    ② LangGraph
   System Prompt   Tool 层
   追加 Skill 的   追加 Skill 的
   instructions   BaseTool
    │             │
    └──────┬──────┘
           ▼
   完整的 LangGraph 执行
```

```python
# classify_intent Node 中调用
async def classify_intent_node(state: AnalysisState) -> dict:
    intent = await classify_intent(state["user_query"])
    tables = state.get("target_tables", [])

    # Skill 匹配
    activated = skill_manager.match_skills(
        state["user_query"], intent, tables
    )
    skill_prompt = skill_manager.build_skill_prompt(activated)
    skill_tools = skill_manager.get_active_tools(activated)

    return {
        "intent": intent,
        "activated_skills": [s.name for s in activated],
        "skill_prompt_override": skill_prompt,
        "skill_tools": skill_tools,
    }
```

---

#### 3.10.4 Skill 发现与分发

| 渠道 | 说明 |
|------|------|
| 本地 `skills/` 目录 | 项目级 Skill，随代码仓库管理，安装即用 |
| Git 子模块 | `skills/community/` 下的社区 Skill，通过 git submodule 引入 |
| Skill Registry (远期) | 中心化的 Skill 市场，类似 VS Code Extension Marketplace |

---

#### 3.10.5 MCP + Skills 与现有组件的关系

```
┌─────────────────────────────────────────────────────────────────┐
│  智能体能力来源                                                  │
├──────────────┬──────────────────────────────────────────────────┤
│ 内置 Tool     │ SchemaExplorer, DBExecutor, DataAnalyzer 等      │
│              │ 始终可用，属于核心流水线                            │
├──────────────┼──────────────────────────────────────────────────┤
│ MCP Client   │ 通过 MCP 协议引入的外部工具和能力                    │
│              │ 启动时建立连接，运行中动态发现                       │
├──────────────┼──────────────────────────────────────────────────┤
│ Skills       │ 触发式激活的能力扩展包                             │
│              │ 包含 Prompt 指令 + 自定义 Tool + 输出格式           │
├──────────────┼──────────────────────────────────────────────────┤
│ MCP Server   │ 对外暴露的分析能力                                  │
│              │ 让其他 Agent 将本智能体作为工具调用                  │
└──────────────┴──────────────────────────────────────────────────┘
```

### 3.11 部署模式与数据源管理

本项目支持两种部署模式：**内置模式**（作为项目依赖嵌入）和**外挂模式**（独立部署为分析平台），通过 Provider 策略模式在统一抽象层隔离差异。

---

#### 3.11.1 架构总览

```
用户请求: "查 clickhouse_prod 的上月订单量"
                          │
                          ▼
┌──────────────────────────────────────────────────┐
│              DataSourceRegistry                  │  ← 统一入口
│  registry.resolve("clickhouse_prod")             │
│  → 返回 DataSourceConfig (无论内置/外挂都一样)    │
└──────────────────────┬───────────────────────────┘
                       │
        ┌──────────────┴──────────────┐
        │                             │
┌───────▼────────┐          ┌─────────▼────────┐
│ 内置 Provider   │          │ 外挂 Provider     │
│ EmbeddedProvider│          │ ExternalProvider  │
├────────────────┤          ├──────────────────┤
│ 适用: pip      │          │ 适用: 独立部署     │
│ install 作为   │          │ Docker/K8s/SaaS   │
│ 项目依赖       │          │                   │
├────────────────┤          ├──────────────────┤
│ 配置发现:       │          │ 配置来源:         │
│ • Django ORM   │          │ • YAML 配置文件   │
│ • SQLAlchemy   │          │ • 管理 API 动态   │
│   engine       │          │   注册            │
│ • .env 环境    │          │ • 数据库存储配置  │
│   变量         │          │ • 密钥管理服务    │
│                │          │   (Vault/KMS)    │
├────────────────┤          ├──────────────────┤
│ Schema 提取:    │          │ Schema 提取:      │
│ ① ORM Model    │          │ ① DB 内省         │
│    类 + docstr │          │    INFORMATION_   │
│ ② migration    │          │    SCHEMA 查询    │
│    文件注释    │          │ ② 手工标注映射    │
│ ③ DB 内省     │          │    (补充中文注释)  │
│    (兜底)     │          │                   │
├────────────────┤          ├──────────────────┤
│ 连接管理:       │          │ 连接管理:         │
│ 复用项目连接池  │          │ 自建连接池        │
│ 零额外配置      │          │ 每个数据源独立隔离 │
└────────────────┘          └──────────────────┘
        │                             │
        └──────────────┬──────────────┘
                       │
                       ▼
┌──────────────────────────────────────────────────┐
│  统一抽象层: DataSourceConfig                     │
│  下游 LangGraph 流水线完全不感知部署模式            │
│  Nodes 只读取 state["dialect"] / state["schema"]  │
│  / state["_engine"]                               │
└──────────────────────────────────────────────────┘
```

---

#### 3.11.2 统一配置对象

```python
from dataclasses import dataclass, field
from typing import Any


@dataclass
class DataSourceConfig:
    """两种模式的公共契约 — 进入 LangGraph 前必须归一化为此结构"""

    name: str                          # "clickhouse_prod"
    dialect: str                       # "clickhouse" | "mysql" | "postgres"
    mode: str                          # "embedded" | "external"

    # 连接信息 (external 从配置解析，embedded 从 ORM engine 提取)
    host: str
    port: int
    database: str
    username: str
    password: str                      # 运行时由 credential_manager 解密

    # 运行时资源 (由 DataSourceRegistry.resolve() 注入)
    engine: Any = None                 # SQLAlchemy async engine
    schema: "SchemaSnapshot" = None    # 预采集的表结构

    # 元数据
    description: str = ""              # "生产环境 ClickHouse 订单库"
    tags: list[str] = field(default_factory=list)  # ["生产", "只读"]
    extra_params: dict = field(default_factory=dict)  # 方言特有参数
```

---

#### 3.11.3 内置模式 (EmbeddedProvider)

适用场景：将 `data-analysis-agent` 作为 Python 包安装到现有项目中。

```python
class EmbeddedDataSourceProvider:
    """
    内置模式的三个核心能力:

    1. 自动发现 — 零配置识别项目中的数据库
    2. 高质量 Schema — 优先使用 ORM Model 的 docstring/comment/verbose_name
    3. 连接复用 — 直接使用项目的连接池
    """

    def auto_discover(self) -> list[DataSourceConfig]:
        """自动发现项目中的所有数据库连接"""
        sources = []

        # Django 项目
        if self._is_django_project():
            from django.conf import settings
            for db_name, db_conf in settings.DATABASES.items():
                sources.append(self._from_django_config(db_name, db_conf))
            return sources

        # Flask / FastAPI + SQLAlchemy
        if self._has_sqlalchemy_engine():
            sources.append(self._from_sqlalchemy_engine())
            return sources

        # 兜底: .env 环境变量
        sources.append(self._from_env_vars())
        return sources

    def extract_schema(self, ds: DataSourceConfig) -> "SchemaSnapshot":
        """
        Schema 提取优先级 (质量递减):

        ① ORM Model 类 → 字段名/类型来自 Column 定义，
          中文含义来自 docstring/comment/verbose_name
        ② migration 文件 → 补充字段变更历史和注释
        ③ DB 内省 → 兜底，覆盖 ORM 未管理的表
        """
        schema = SchemaSnapshot()

        # ① ORM Model 提取 (最优先)
        if orm_models := self._find_orm_models():
            for model in orm_models:
                table = TableSchema(
                    name=model.__tablename__,
                    description=self._extract_model_description(model),
                )
                for col in model.__table__.columns:
                    table.columns.append(ColumnInfo(
                        name=col.name,
                        type=str(col.type),
                        comment=col.comment or "",     # SQLAlchemy comment=
                        # Django 额外: verbose_name, help_text
                    ))
                schema.tables.append(table)

        # ② 补充 DB 内省 (外键关系 + 未管理的表)
        schema.merge(self._introspect_db(ds))

        return schema

    def _extract_model_description(self, model) -> str:
        """从 ORM Model 提取中文描述"""
        # SQLAlchemy: model.__doc__ 或 __table_args__["comment"]
        # Django: model._meta.verbose_name
        return (model.__doc__ or "").strip()
```

---

#### 3.11.4 外挂模式 (ExternalProvider)

适用场景：独立部署的分析平台，一个 Agent 实例连接多个项目的数据源。

```python
class ExternalDataSourceProvider:
    """
    外挂模式的三个核心能力:

    1. 手动注册 — 用户通过 API / 配置文件声明数据源
    2. 完全内省 — Schema 全部来自 DB 系统表查询
    3. 故障隔离 — 每个数据源独立连接池，一个挂了不影响其他
    """

    def __init__(self, config_store: "DataSourceConfigStore"):
        self.store = config_store  # 数据源配置的持久化存储

    async def register(self, req: "DataSourceCreateRequest") -> DataSourceConfig:
        """注册新数据源: 测试连接 → 内省 Schema → 加密存储"""
        # 1. 连通性测试
        await self._test_connection(req)

        # 2. 加密凭证
        encrypted = self._credential_manager.encrypt(req.password)

        # 3. 持久化到配置存储
        ds = await self.store.save(DataSourceConfig(
            name=req.name,
            dialect=req.dialect,
            mode="external",
            host=req.host,
            port=req.port,
            database=req.database,
            username=req.username,
            password=encrypted,
            description=req.description,
            tags=req.tags,
        ))

        # 4. 异步预采集 Schema
        asyncio.create_task(self._refresh_schema(ds))

        return ds

    def extract_schema(self, ds: DataSourceConfig) -> "SchemaSnapshot":
        """
        外挂模式的 Schema 提取 — 仅走 DB 内省:

        1. 查询 INFORMATION_SCHEMA.COLUMNS 获取字段
        2. 查询 INFORMATION_SCHEMA.TABLE_CONSTRAINTS 获取主键/外键
        3. 查询系统统计表获取行数估算
        4. 可选: 加载「手工标注映射文件」补充中文注释
        """
        schema = self._introspect_db(ds)

        # 补充手工标注 (可选)
        if annotation_file := self._find_annotation_file(ds.name):
            schema.merge(self._load_annotations(annotation_file))

        return schema
```

**外挂模式的三类配置来源**：

| 方式 | 格式 | 适用场景 | 配置热更新 |
|------|------|---------|-----------|
| YAML 文件 | `config/datasources.yaml` + 环境变量注入密码 | 部署固定的少量数据源 | 需重启 |
| 管理 API | `POST /api/v1/datasources` 动态注册 | 多租户平台，用户自助接入 | 实时 |
| 配置数据库 | PostgreSQL `datasource_configs` 表 | 多 Agent 实例共享配置 | 实时 |

---

#### 3.11.5 DataSourceRegistry — 统一注册与解析

```python
class DataSourceRegistry:
    """
    对外暴露的唯一入口。
    内置模式: registry 启动时自动填充。
    外挂模式: registry 由 API / 配置文件动态填充。
    """

    def __init__(self):
        self._providers: dict[str, DataSourceProvider] = {}
        self._cache: dict[str, DataSourceConfig] = {}   # 解析后的配置缓存

    def register_provider(self, name: str, provider: "DataSourceProvider"):
        self._providers[name] = provider

    async def resolve(self, datasource_name: str) -> DataSourceConfig:
        """
        解析数据源名称 → 返回已建立连接的 DataSourceConfig。

        对下游完全透明 — LangGraph Node 不知道这个配置
        是来自 ORM 自发现还是 API 手动注册。
        """
        if datasource_name in self._cache:
            return self._cache[datasource_name]

        # 遍历所有 Provider 查找
        for provider in self._providers.values():
            try:
                config = await provider.lookup(datasource_name)
                if config:
                    # 注入连接
                    config.engine = await self._create_engine(config)
                    # 注入 Schema
                    config.schema = await provider.extract_schema(config)
                    self._cache[datasource_name] = config
                    return config
            except DataSourceNotFoundError:
                continue

        raise DataSourceNotFoundError(f"数据源 '{datasource_name}' 未找到")


# ---- 初始化示例 ----

# 内置模式: FastAPI startup
registry = DataSourceRegistry()
embedded = EmbeddedDataSourceProvider()
for ds in embedded.auto_discover():
    registry.register_provider(ds.name, embedded)

# 外挂模式: 加载 YAML 配置
external = ExternalDataSourceProvider(config_store)
for ds_config in load_yaml_datasources("config/datasources.yaml"):
    registry.register_provider(ds_config.name, external)
```

---

#### 3.11.6 LangGraph Node 视角 — 完全无感

```python
# Node 内部代码 — 无任何 if mode == "embedded" 分支

async def generate_sql_node(state: AnalysisState) -> dict:
    schema: SchemaSnapshot = state["resolved_schema"]
    dialect: str = state["dialect"]

    prompt = build_sql_prompt(
        query=state["user_query"],
        schema_text=schema.to_prompt_text(),   # 不管来源，格式化输出都一样
        dialect=dialect,
    )
    ...


async def execute_sql_node(state: AnalysisState) -> dict:
    engine = state["_engine"]   # AsyncEngine，不管是谁创建的
    async with engine.connect() as conn:
        result = await conn.execute(text(state["generated_sql"]))
        return {"query_result": result.mappings().all()}


# 在 FastAPI 路由中完成 resolve，之后进入 Graph:
@router.post("/api/v1/chat")
async def chat(request: ChatRequest):
    ds = await registry.resolve(request.datasource)

    result = await app.ainvoke({
        "user_query": request.query,
        "datasource": ds.name,
        "dialect": ds.dialect,
        "resolved_schema": ds.schema,
        "_engine": ds.engine,
    })
    return result["final_response"]
```

---

#### 3.11.7 模式对比

| 维度 | 内置模式 | 外挂模式 |
|------|---------|---------|
| 部署方式 | `pip install` 作为项目依赖 | 独立部署 (Docker / K8s) |
| 配置发现 | 自动 (扫描 ORM + env) | 手动 (YAML / API / DB) |
| Schema 提取 | ORM Model + migration + DB 内省 | DB 内省 + 手工标注 |
| Schema 质量 | 高 (代码注释 > DB 注释) | 取决于 DBA 注释质量 |
| 连接数 | 通常 1-3 个 (项目内) | 可达几十个 (跨项目) |
| 连接管理 | 复用项目连接池，零额外开销 | 自建连接池，每个数据源独立隔离 |
| 凭证管理 | 随项目配置文件 | 加密存储 + 密钥管理服务 |
| 租户模型 | 单项目单 Agent | 多租户共享 Agent 实例 |
| 典型场景 | 给 Django 项目加一个智能查数机器人 | 公司级分析平台：市场、运营、财务都用 |

## 4. 技术栈选型

| 层次 | 技术 | 说明 |
|-----|------|------|
| 后端框架 | Python FastAPI | 异步支持、生态完善 |
| LLM 接入 | OpenAI API / Claude API | 支持多模型切换 |
| LLM 框架 | LangChain + LangGraph | StateGraph 管理复杂流水线；BaseTool 封装工具复用；ChatPromptTemplate 统一 Prompt 管理；checkpointer 内置会话持久化 |
| MCP 协议 | mcp (Python SDK) + FastMCP | MCP Client 导入外部工具；MCP Server 对外暴露能力；stdio + SSE 双传输 |
| 数据库连接 | SQLAlchemy 2.0 / clickhouse-connect | 异步 + 连接池 |
| SQL 解析校验 | sqlglot | 多方言解析、转译、函数白名单 |
| 数值计算 | pandas + numpy | 统计分析 |
| 可视化 | Plotly + ECharts | 服务端渲染 + 前端渲染 |
| 向量检索 | ChromaDB | Schema 语义检索、字段级索引、业务文档检索、历史 SQL 检索 |
| Agent UI 调试 | LangSmith | 全链路追踪、Node 级耗时、Prompt 版本管理 |
| 前端 | React + TypeScript | 如果选 Web UI |
| Agent UI 调试 | LangSmith | 全链路追踪、Node 级耗时、Prompt 版本管理 |
| 缓存 | Redis | 查询结果缓存、限流计数 |
| 任务队列 | Celery / ARQ | 长查询异步执行 |

---

## 5. API 设计

### 5.1 核心接口

```
POST /api/v1/chat
  - 发送自然语言查询，返回分析结果

POST /api/v1/chat/stream
  - SSE 流式返回分析过程（SQL生成→执行→分析→图表）

GET  /api/v1/schema/tables
  - 获取所有表列表

GET  /api/v1/schema/tables/{table_name}
  - 获取指定表结构

POST /api/v1/schema/refresh
  - 刷新 Schema 缓存

GET  /api/v1/history?session_id=xxx
  - 获取会话历史
```

### 5.2 /api/v1/chat 请求/响应

```json
// 请求
{
  "session_id": "uuid",
  "query": "过去7天各品类的销售额趋势，找出增长最快的3个品类",
  "datasource": "clickhouse_prod"
}

// 响应
{
  "session_id": "uuid",
  "query": "过去7天各品类的销售额趋势...",
  "sql": "SELECT category, toDate(created_at) AS dt, SUM(amount) ...",
  "data": [
    {"category": "电子", "date": "2026-05-28", "sales": 128000}
  ],
  "analysis": {
    "summary": "过去7天销售额排名前三的品类为...",
    "insights": [
      "电子产品以12.8万位居榜首，环比增长23%",
      "家居品类增速最快，环比增长45%"
    ],
    "chart": {
      "type": "line",
      "config": {}
    }
  }
}
```

---

## 6. 数据流示例

### 场景：用户问"上个月哪些用户消费最高，哪个品类卖得最好？"

```
Step 1 — 意图识别
  LLM: 判定为"聚合排名 + 多维度分析"问题

Step 2 — Schema检索
  搜索"用户消费"→ user_orders 表，"品类"→ product_categories 表
  返回表结构 + JOIN 关系

Step 3 — SQL生成
  LLM 生成:
  SELECT u.user_id, u.user_name, SUM(o.amount) AS total_spent,
         p.category_name, COUNT(*) AS order_count
  FROM user_orders o
  JOIN users u ON o.user_id = u.user_id
  JOIN products p ON o.product_id = p.product_id
  WHERE o.created_at >= '2026-05-01' AND o.created_at < '2026-06-01'
  GROUP BY u.user_id, u.user_name, p.category_name
  ORDER BY total_spent DESC

Step 4 — 安全校验
  校验通过（SELECT 语句，带时间范围过滤）

Step 5 — 执行
  ClickHouse 返回结果 → pandas DataFrame

Step 6 — 分析
  LLM 解读结果:
  - Top 10 消费者排名
  - 各品类销售额分布
  - 计算集中度（Top 10 贡献了 X% 的销售额）
  - 对比上上月发现趋势变化

Step 7 — 可视化
  自动生成: 柱状图（Top10消费）+ 饼图（品类占比）

Step 8 — 归档
  将 SQL 模板和分析结论存入知识库
```

---

## 7. 安全设计

### 7.1 SQL 安全

- 白名单模式：默认只允许 SELECT / SHOW / DESCRIBE / EXPLAIN
- 所有 SQL 执行前经过正则校验 + AST 解析双重检查
- 使用只读数据库账号，权限最小化
- SQL 注入基础防护（LLM 输出的 SQL 已经参数化，不会拼接用户输入）

### 7.2 数据安全

- 查询结果自动脱敏（手机号、身份证号、邮箱等字段）
- 敏感表/字段可配置访问白名单
- 查询日志完整记录（谁、什么时候、查了什么）

---

## 8. 实现路线图

### Phase 1 — MVP（1-2周）

- [ ] 项目骨架搭建（FastAPI + Poetry）
- [ ] 配置体系: settings.py (pydantic-settings) + config/mcp_servers.yaml
- [ ] DataSourceRegistry + DataSourceConfig + EmbeddedProvider
- [ ] ClickHouse 连接器 + DB 内省 (INFORMATION_SCHEMA 查询)
- [ ] SchemaManager: 三级回退机制 (ChromaDB 缓存 → 文档仓库 → DB 自动拉取)
- [ ] KnowledgeEntry 数据结构 + ChromaDB 表级/字段级双粒度索引
- [ ] SchemaSnapshot.to_prompt_text() LLM 注入格式化
- [ ] Markdown 文档加载器 (docs/metrics/ 目录扫描 + 按标题切片)
- [ ] MCPClientManager 基础实现 (stdio transport 至少支持 1 个 MCP Server)
- [ ] SkillManager 基础实现 (skills/ 目录扫描 + SKILL.md 解析)
- [ ] LangChain + LangGraph 初始化（ChatOpenAI/ChatAnthropic 工厂）
- [ ] AnalysisState 定义 + 基础图（classify_intent → generate_sql → layer3_validate → execute_sql → build_response）
- [ ] sqlglot Layer 3 语法校验 Node
- [ ] SQL 安全拦截（正则 DDL/DML 黑名单，Layer 3 之前执行）
- [ ] 结果 JSON 返回
- [ ] MemorySaver 会话持久化（开发环境）
- [ ] LangSmith 接入（可观测性）

### Phase 2 — 增强（2-3周）

- [ ] 多表 JOIN 支持（Schema 关系图谱注入 Prompt）
- [ ] 错误重试边（conditional edge: 执行失败 → 带 error 上下文回到 SQL 生成 Node）
- [ ] 数据分析 Node（pandas 统计 + LLM 洞察）
- [ ] 图表生成 Node（ECharts config 自动生成）
- [ ] 向量知识库（ChromaDB 存储历史 SQL 模板，作为 few-shot 检索）
- [ ] PostgresSaver 替换 MemorySaver（生产级会话持久化）

### Phase 3 — 生产化（2-3周）

- [ ] Web Chat 前端（React）
- [ ] 多数据源支持（MySQL、PostgreSQL）
- [ ] 流式响应 (SSE)
- [ ] 查询缓存（Redis）
- [ ] 限流与并发控制
- [ ] 数据脱敏
- [ ] 监控与日志（Prometheus + Grafana）
- [ ] Docker 容器化部署

### Phase 4 — 进阶（持续迭代）

- [ ] 自动 Insight 发现（定期扫描数据，主动推送异常）
- [ ] 定时报告（按日/周/月自动生成分析报告）
- [ ] 知识库自进化（高质量 SQL 自动积累）
- [ ] 多模态输入（上传 Excel/CSV 直接分析）
- [ ] 权限体系完善（多用户、角色管理）

---

## 9. 关键设计决策

| 决策点 | 选择 | 理由 |
|-------|------|------|
| LLM 框架 | LangChain + LangGraph | 标准化工具接口、内置会话持久化(checkpointer)、LangSmith全链路追踪、社区生态成熟 |
| 分析计算 | pandas 侧执行 | LLM 不擅长精确计算，统计交给代码 |
| Schema 检索 | 向量 + 倒排混合 | 语义相似 + 关键词精确匹配互补 |
| 可视化方案 | ECharts (前端渲染) | 交互性强，图表种类丰富 |
| SQL 方言 | 每个数据源独立 Prompt | ClickHouse 和 MySQL 函数差异大 |

---

## 10. 目录结构建议

```
dataAnalysisAgent/
├── src/
│   ├── graph/                       # LangGraph 状态图
│   │   ├── state.py                 # AnalysisState TypedDict 定义
│   │   ├── workflow.py              # StateGraph 组装 + compile
│   │   └── nodes/                   # 各 Node 实现
│   │       ├── classify_intent.py   # 意图识别 Node
│   │       ├── retrieve_schema.py   # Schema 检索 Node
│   │       ├── generate_sql.py      # SQL 生成 Node（含方言 Prompt 注入）
│   │       ├── layer3_validate.py   # ★ sqlglot 语法校验 Node
│   │       ├── layer4_explain.py    # ★ EXPLAIN 空跑校验 Node
│   │       ├── execute_sql.py       # SQL 执行 Node
│   │       ├── analyze_result.py    # 数据分析 Node
│   │       ├── generate_chart.py    # 图表生成 Node
│   │       └── build_response.py    # 响应组装 Node
│   ├── tools/                       # LangChain BaseTool 封装
│   │   ├── schema_explorer.py       # SchemaExplorerTool
│   │   ├── sql_generator.py         # SQLGeneratorTool
│   │   ├── sqlglot_validator.py     # ★ sqlglot 方言校验 Tool
│   │   ├── db_executor.py           # DBExecutorTool
│   │   ├── db_explain.py            # ★ EXPLAIN 空跑校验 Tool
│   │   ├── analyzer.py              # DataAnalyzerTool
│   │   └── chart_generator.py       # ChartGeneratorTool
│   ├── connectors/                  # 数据库连接器
│   │   ├── base.py                  # 连接器基类
│   │   ├── clickhouse.py
│   │   ├── mysql.py
│   │   └── postgres.py
│   ├── datasource/                   # ★ 数据源注册与管理
│   │   ├── registry.py              # DataSourceRegistry 统一入口
│   │   ├── config.py                # DataSourceConfig 定义
│   │   ├── providers/
│   │   │   ├── base.py              # DataSourceProvider 抽象基类
│   │   │   ├── embedded.py          # EmbeddedProvider (内置模式)
│   │   │   └── external.py          # ExternalProvider (外挂模式)
│   │   ├── schema_snapshot.py       # SchemaSnapshot 统一数据结构
│   │   ├── introspection.py         # DB 内省 (INFORMATION_SCHEMA 查询)
│   │   └── credential_manager.py   # 凭证加解密管理
│   ├── memory/
│   │   ├── checkpointer.py          # PostgresSaver / MemorySaver 配置
│   │   ├── long_term_store.py       # 长期记忆 (ChromaDB + PostgreSQL)
│   │   └── session_archive.py       # 会话归档与摘要
│   ├── mcp/                          # ★ MCP 集成
│   │   ├── client_manager.py        # MCPClientManager (连接/转换/健康检查)
│   │   ├── server.py                # FastMCP Server (对外暴露分析能力)
│   │   └── tool_adapter.py          # MCP Tool → LangChain BaseTool 适配器
│   ├── skills/                       # ★ Skills 技能包
│   │   ├── data_quality_check/
│   │   │   ├── SKILL.md             # Skill 清单 (YAML frontmatter)
│   │   │   ├── tools.py             # 工具实现
│   │   │   └── prompts.py           # 专属 Prompt
│   │   └── custom_report/
│   │       ├── SKILL.md
│   │       └── templates/
│   │           └── weekly_report.jinja2
│   ├── skill_manager.py             # ★ Skill 发现/加载/激活引擎
│   ├── config/                       # 配置文件
│   │   └── mcp_servers.yaml         # MCP Server 注册表
│   ├── knowledge/                    # 知识库管理
│   │   ├── schema_manager.py        # Schema 缓存管理 (文档 → 自动拉取 三级回退)
│   │   ├── business_rules.py        # 业务规则存储与检索
│   │   ├── cache_refresher.py       # AUTO 来源过期刷新 + DDL 监听
│   │   ├── enum_discovery.py        # 枚举值自动发现
│   │   └── doc_loader.py            # Markdown 文档解析与索引
│   ├── docs/                         # ★ 手工维护的业务文档
│   │   └── metrics/                 # 指标口径文档 (*.md)
│   │       ├── gmv.md
│   │       ├── user_definitions.md
│   │       └── order_status.md
│   ├── api/
│   │   ├── routes.py                # FastAPI 路由
│   │   ├── schemas.py               # Pydantic 请求/响应模型
│   │   └── streaming.py             # SSE 流式输出 (astream_events)
│   ├── llm/
│   │   ├── client.py                # ChatOpenAI / ChatAnthropic 工厂
│   │   └── prompts.py               # ChatPromptTemplate 集中管理
│   └── config.py                    # Settings (pydantic-settings)
├── tests/
│   ├── test_graph/                  # LangGraph 集成测试
│   │   ├── test_workflow.py
│   │   └── test_nodes/
│   ├── test_tools/                  # Tool 单元测试
│   └── fixtures/                    # 测试用的 schema + 数据 fixtures
├── tests/
├── frontend/                    # 前端项目 (Phase 3)
├── docker-compose.yml
├── requirements.txt
└── SPEC.md
```

---

## 11. LangGraph 与 LangChain 集成细节

### 11.1 Node 内 LLM 调用模式

每个 Node 遵循统一的 LangChain 调用模式：

```python
from langchain_core.prompts import ChatPromptTemplate
from langchain_openai import ChatOpenAI
from langchain_core.output_parsers import JsonOutputParser

# 每个 Node 的核心模式
async def generate_sql_node(state: AnalysisState) -> dict:
    llm = ChatOpenAI(model="gpt-4o", temperature=0)
    prompt = ChatPromptTemplate.from_messages([
        ("system", SQL_GENERATION_SYSTEM_PROMPT),
        ("user", "{user_query}")
    ])
    parser = JsonOutputParser(pydantic_object=SQLOutput)
    chain = prompt | llm | parser
    result = await chain.ainvoke({
        "user_query": state["user_query"],
        "schemas": state["relevant_tables"],
        "datasource_type": state["datasource"],
        "few_shot_examples": state.get("few_shot_examples", [])
    })
    return {"generated_sql": result["sql"]}
```

### 11.2 ChatPromptTemplate 示例（SQL 生成）

```python
from langchain_core.prompts import ChatPromptTemplate

SQL_GENERATION_SYSTEM_PROMPT = """你是一个 {datasource_type} SQL 专家。根据表结构和用户问题生成正确的 SQL。

## 数据库表结构
{schemas}

## 参考示例
{few_shot_examples}

## 规则
1. 只生成 SELECT 语句
2. 大表查询必须包含时间范围过滤
3. 结果集默认限制 1000 行
4. 使用 {datasource_type} 正确的日期/字符串函数
5. 字段名和表名必须来自 Schema，禁止编造

## 输出格式
{format_instructions}
"""

sql_prompt = ChatPromptTemplate.from_messages([
    ("system", SQL_GENERATION_SYSTEM_PROMPT),
    ("placeholder", "{history}"),          # 对话历史自动注入
    ("user", "{user_query}")
])
```

### 11.3 流式输出 (Streaming)

利用 LangGraph 的 `astream_events` 实现 SSE 流式推送：

```python
async def stream_analysis(user_query: str, config: dict):
    """FastAPI SSE endpoint: 逐步推送每个 Node 的执行状态"""
    async for event in app.astream_events(
        {"user_query": user_query, "datasource": "clickhouse_prod"},
        config=config,
        version="v2"
    ):
        kind = event["event"]
        if kind == "on_chat_model_stream":
            # LLM 输出 token 级流式
            yield f"data: {json.dumps({'type': 'token', 'content': event['data']['chunk'].content})}\n\n"
        elif kind == "on_chain_start":
            # Node 开始执行
            yield f"data: {json.dumps({'type': 'node_start', 'node': event['name']})}\n\n"
        elif kind == "on_chain_end":
            # Node 执行完成
            yield f"data: {json.dumps({'type': 'node_end', 'node': event['name'], 'output': event['data'].get('output')})}\n\n"
```

### 11.4 会话持久化 (Checkpointer)

```python
from langgraph.checkpoint.postgres import PostgresSaver

# 生产环境使用 PostgreSQL 存储会话状态
checkpointer = PostgresSaver.from_conn_string(POSTGRES_URL)
checkpointer.setup()

app = workflow.compile(checkpointer=checkpointer)

# 每个用户会话通过 thread_id 隔离
config = {"configurable": {"thread_id": "user_session_abc123"}}
result = await app.ainvoke(
    {"user_query": "上个月销售额Top10品类"},
    config=config
)
# 会话自动持久化，追问时复用上下文

# 恢复历史会话
history = await app.aget_state(config)
```

### 11.5 LangSmith 可观测性

```python
# .env 配置
# LANGCHAIN_TRACING_V2=true
# LANGCHAIN_API_KEY=ls__xxx
# LANGCHAIN_PROJECT=data-analysis-agent

# 每个 Node 的执行自动上报到 LangSmith:
# - Node 级延迟、输入/输出
# - LLM 调用 token 消耗、Prompt 快照
# - Tool 调用链完整追溯
# - 失败 Node 的错误堆栈

# 在 LangSmith UI 中可:
# - 对比不同 Prompt 版本的 SQL 正确率
# - 定位耗时最长的 Node 做优化
# - 查看完整调用链排查生产问题
```

---

## 12. Prompt 模板管理

### 12.1 集中式 Prompt 管理

所有 Prompt 通过 `ChatPromptTemplate` 定义在 `src/llm/prompts.py`，支持版本控制和 A/B 测试：

```
src/llm/prompts.py          # 所有 Prompt 模板
  ├── INTENT_CLASSIFY_PROMPT
  ├── SQL_GENERATION_PROMPT
  ├── DATA_ANALYSIS_PROMPT
  ├── CHART_RECOMMEND_PROMPT
  └── RESPONSE_BUILD_PROMPT
```

### 12.2 数据分析 Node 的 Prompt 模板

```python
from langchain_core.prompts import ChatPromptTemplate, SystemMessagePromptTemplate, HumanMessagePromptTemplate

DATA_ANALYSIS_PROMPT = ChatPromptTemplate.from_messages([
    SystemMessagePromptTemplate.from_template(
        "你是一个资深数据分析师。"
        "根据用户问题、执行的SQL和查询结果给出分析报告。"
        "使用中文回复。"
    ),
    HumanMessagePromptTemplate.from_template("""
## 用户原始问题
{user_query}

## 执行的 SQL
```sql
{generated_sql}
```

## 查询结果 (前 50 行)
{data_sample}

## 自动统计摘要
{statistics}

请按以下格式输出分析报告：
1. **数据摘要**：1-2句话概括
2. **关键发现**：列出2-5条洞察
3. **推荐图表**：建议的可视化类型和维度
4. **值得追问的方向**：用户可能想进一步了解的点

{format_instructions}
""")
])
```

---

## 13. 评估指标与质量保障

| 指标 | 目标 | 测量方式 |
|-----|------|---------|
| SQL 首次生成正确率 | ≥ 85% | LangSmith Dataset + 人工标注 |
| SQL 最终正确率（含重试） | ≥ 95% | 含最多3次重试，LangSmith 全链路统计 |
| 端到端响应时间（简单查询） | ≤ 3秒 | LangSmith Trace 各 Node 延迟汇总 |
| 端到端响应时间（复杂分析） | ≤ 30秒 | 含 LLM 分析 + 图表生成 |
| 危险 SQL 拦截率 | 100% | 安全测试用例集 + CI 自动化 |
| Schema 检索命中率 | ≥ 90% | Top-5 召回率，ChromaDB 检索评估 |

### 13.1 LangSmith 驱动的评估流程

```python
from langsmith import Client

client = Client()

# 1. 上传标注数据集
dataset = client.create_dataset(
    "nl2sql-benchmark",
    data_type="chat",
    inputs=[{"question": "上月销售额Top10", "tables": [...]}],
    outputs=[{"expected_sql": "SELECT ...", "expected_analysis": "..."}],
)

# 2. 定义 evaluator
def sql_correctness(outputs: dict, reference_outputs: dict) -> bool:
    return sqlparse.format(outputs["sql"]) == sqlparse.format(reference_outputs["expected_sql"])

# 3. 批量回归测试
from langsmith import aevaluate

results = await aevaluate(
    lambda x: app.ainvoke(x),  # 你的 LangGraph app
    data="nl2sql-benchmark",
    evaluators=[sql_correctness],
    max_concurrency=4,
)
```

---

## 14. 设计审查与风险缓解

本节汇总设计审查中发现的风险点及缓解措施，作为各模块实现时的硬约束。

---

### 14.1 模块边界与循环依赖

| 风险 | 两个模块职责重叠导致循环 import |
|------|-------------------------------|
| 涉及 | `datasource/introspection.py` ↔ `knowledge/schema_manager.py` |
| 缓解 | **已确定边界**: introspection 仅执行原始 SQL 返回 `list[dict]`，不做缓存不做语义加工。schema_manager 负责缓存策略、优先级合并、`SchemaSnapshot` 组装。数据通过 `KnowledgeEntry` 传递。 |
| 涉及 | SPEC 3.8 记忆系统 与 FEATURES.md 第 7 章记忆系统 — 内容分散在两个位置 |
| 缓解 | **两处保留**：SPEC 3.8 描述记忆架构和 LLM 层面的设计决策；FEATURES 第 7 章是施工级清单。各司其职，不合并。 |

---

### 14.2 理想化设计的工程约束

#### 14.2.1 自动发现枚举值 `_auto_discover_enum_values()`

| 风险 | 对大表（10 亿行）执行 `SELECT DISTINCT col LIMIT 50` 可能非常慢 |
|------|--------------------------------------------------------------|
| 缓解 | ① 先查 `_estimate_row_count()`，行数 > 100 万跳过 ② 对 ClickHouse 使用 `uniqCombined` 近似函数 ③ 超时 5 秒自动放弃 ④ 低基数判断阈值从 20 调整为 10（更保守） |

#### 14.2.2 上下文摘要 `_summarize_turns()`

| 风险 | 每次 LLM 调用都对温数据做 `cheap_llm` 摘要，增加延迟和成本 |
|------|----------------------------------------------------------|
| 缓解 | ① Phase 1 使用规则摘要（拼接 turn 首句），不调 LLM ② Phase 2 改为后台异步预计算：会话归档时一并生成摘要，写入 `ConversationTurn.summary` 字段 ③ `build_llm_context` 直接读取已算好的摘要 |

#### 14.2.3 EXPLAIN 空跑成本

| 风险 | Snowflake 等云数仓执行 EXPLAIN 可能产生计算费用或消耗 warehouse 资源 |
|------|-------------------------------------------------------------------|
| 缓解 | ① 增加配置项 `EXPLAIN_SKIP_DIALECTS`（默认 `{"snowflake"}`）② 用户可在 `config/datasources.yaml` 中按数据源覆盖 ③ 跳过的方言仅依赖 Layer 3 sqlglot 校验 |

#### 14.2.4 `compute_statistics()` 内存安全

| 风险 | 基于完整结果集（可能百万行）执行 pandas 计算导致 OOM |
|------|---------------------------------------------------|
| 缓解 | ① 统计计算输入行数上限 `MAX_STATS_ROWS`（默认 50 万行）② 超限时在 DB 层用 `APPROX_QUANTILES` / `approx_count_distinct` 做近似统计 ③ `query_result_full_count` 告知 LLM 总行数 |

---

### 14.3 异常与降级场景

#### 14.3.1 DB 权限不足

| 风险 | 查询 `INFORMATION_SCHEMA` 权限不足时仅返回降级信息，用户不知道发生了什么 |
|------|---------------------------------------------------------------------|
| 缓解 | ① 权限错误时 `SchemaManager` 在返回的 `SchemaSnapshot` 中插入一条特殊的 `KnowledgeEntry`，`source=SYSTEM_WARNING`，内容说明缺少哪些权限及如何授权 ② LLM 在分析结果中向用户展示 ③ 同时写入 structlog 日志并触发 Prometheus alert |

#### 14.3.2 MCP Server 断线不可恢复

| 风险 | 指数退避重连全部失败后，依赖该 MCP 的查询永久挂起 |
|------|--------------------------------------------------|
| 缓解 | ① 最大重试 5 次（总窗口 ~2 分钟），之后标记该 MCP Server 为 `degraded` ② `degraded` 状态的 MCP 工具从 `get_all_tools()` 中移除，查询走无该工具路径 ③ 健康检查恢复后自动重新启用 ④ 管理员可通过 API 手动 `POST /api/v1/mcp/{name}/reset` 恢复 |

#### 14.3.3 长时间无文档

| 风险 | 用户从不提供 Markdown 文档，全部依赖 `source=auto` 自动拉取，字段语义质量差 |
|------|-------------------------------------------------------------------------|
| 缓解 | ① 当 `source=auto` 占比 > 80% 且有查询活动时，LLM 在分析结果中主动提示用户补充文档 ② 频率限制：每 24 小时最多提示 1 次 |

---

### 14.4 数据一致性与并发

#### 14.4.1 长期记忆双写 `_upsert()`

| 风险 | 先写 ChromaDB 成功但 PostgreSQL 失败 → 向量检索能查到但结构化字段丢失 |
|------|--------------------------------------------------------------------|
| 缓解 | ① 严格执行顺序: **先 PG，后 ChromaDB**。PG 是权威数据源（结构化查询依赖它），ChromaDB 是检索加速层 ② PG 写入失败直接抛异常不回退（向量库无脏数据） ③ PG 成功但 ChromaDB 失败时：记录到 `pending_vector_sync` 表，后台补偿任务每 5 分钟重试 ④ `search()` 始终以 PG 结果为准 |

#### 14.4.2 缓存刷新并发

| 风险 | 多个请求同时发现缓存过期 → 发起多个 DB 内省任务 → 对源数据库造成压力 |
|------|------------------------------------------------------------------|
| 缓解 | ① 使用 Redis `SETNX` 分布式锁（key=`refresh_lock:{ds}:{table}`，TTL=60s）② 未获取锁的请求返回旧缓存（允许过期数据） ③ 无 Redis 时退化为进程内 `asyncio.Lock`（单进程安全） |

---

### 14.5 性能与资源规划

#### 14.5.1 响应时间目标与资源约束

| 指标 | 目标 | 资源约束 |
|------|------|---------|
| 简单查询端到端 | ≤ 3 秒 | 不含 LLM 推理时间，仅计算 SQL 执行 + 结果处理 |
| 复杂分析端到端 | ≤ 30 秒 | 含 LLM 分析 + 图表生成 |
| 连接池大小 | — | ClickHouse/MySQL/PG 每数据源默认 `pool_size=5, max_overflow=10`，可在 `DataSourceConfig.extra_params` 覆盖 |
| ChromaDB 容量 | — | 设计容量 10 万条 KnowledgeEntry，单条 embedding 768d。超过后启用 LRU 淘汰（优先删除 `source=auto` 且 `access_count=0` 的条目） |
| LLM 并发 | — | 默认 5 个并发调用（通过 `asyncio.Semaphore` 控制），可在 Settings 中覆盖 |

#### 14.5.2 分页与大数据量

| 场景 | 策略 |
|------|------|
| API 列表（数据源/表/Schema） | 支持 `?page=1&page_size=20&search=xxx` 分页 |
| SQL 查询结果 | 200 行截断给 LLM，全量通过 `query_result_full_count` 告知行数 |
| 统计计算 | 输入 ≤ 50 万行，超限走 DB 近似统计（见 14.2.4） |

---

### 14.6 API 设计补全

#### 14.6.1 新增路由

| 路由 | 说明 | 优先级 |
|------|------|--------|
| `PUT /api/v1/schema/tables/{table}/columns/{column}/comment` | 手动标注字段中文说明，直接写入 ChromaDB | P1 |
| `GET /api/v1/schema/tables?datasource=x&page=1&page_size=20&search=y` | 表列表分页 + 搜索 | P1 |
| `POST /api/v1/mcp/{name}/reset` | 手动重置 degraded 状态的 MCP Server | P1 |
| `GET /api/v1/metrics` | 查询已注册的指标口径列表 | P2 |

#### 14.6.2 现有路由补全分页参数

`GET /api/v1/schema/tables`、`GET /api/v1/schema/tables/{table_name}`、`GET /api/v1/history`、`GET /api/v1/datasources` 均增加 `?page&page_size` 分页参数。

---

### 14.7 测试策略细化

#### 14.7.1 Mock 方案

| 场景 | Mock 方式 |
|------|----------|
| Mock LLM 输出固定 SQL | 使用 `FakeListChatModel`（langchain_core 内置），按顺序返回预设 `AIMessage` |
| Mock DB 返回特定结果集 | 使用 SQLite 内存数据库（`:memory:`）作为 Connector 的测试后端，建表 + 插数据 → 验证 SQL 正确性 |
| Mock MCP Server | 创建一个 `StaticMCPTestServer`，注册固定 tools，不依赖外部进程 |
| Mock ChromaDB | 使用 `chromadb.Client()` 的 `EphemeralClient` 模式 |
| LangGraph 条件边测试 | 构造特定 `AnalysisState` 直接调用 `after_layer3(state)` / `should_retry(state)`，验证返回值 |

#### 14.7.2 测试分层

```
Layer 1: 纯函数单元测试 (validate_with_sqlglot, compute_statistics...)
         → SQLite / EphemeralClient，无需外部依赖

Layer 2: Node 集成测试 (单个 Node 的 prompt → chain → 输出)
         → FakeListChatModel + SQLite

Layer 3: Graph 集成测试 (完整 StateGraph + 条件边)
         → FakeListChatModel + SQLite + assert 最终 state

Layer 4: API 集成测试 (httpx AsyncClient 完整请求)
         → 需要真实 DB 或 Docker Compose 的测试环境
```

---

### 14.8 版本管理

#### 14.8.1 Prompt 版本与 A/B 测试

| 能力 | 实现方式 |
|------|---------|
| Prompt 版本号 | `VERSION = "1.2.0"` 常量，LangSmith 自动记录每次调用的 Prompt 版本 |
| A/B 测试 | 按 `user_id % 2` 分流：`prompts.py` 中 `get_prompt(name, variant)` 根据分流返回 A 或 B 版本 |
| 回滚 | LangSmith UI 对比版本质量 → 修改 `DEFAULT_PROMPT_VERSIONS` 字典切换到指定版本 |
| 自动升级 | 不自动升级。新版本手动标记为 `active`，旧版本保留 30 天 |

#### 14.8.2 Skill 版本依赖

| 风险 | Skill A 依赖 `pandas>=2.0`，Skill B 依赖 `pandas<2.0` |
|------|--------------------------------------------------------|
| 缓解 | ① Phase 1-2: 依赖声明仅做检查提示，不强制 ② Phase 3: 引入 `pip` resolver 在 Skill 激活时检测冲突，冲突时禁止同时激活 ③ 社区 Skill 要求声明 `depends_on.python_packages` 并给出版本范围 |

---

### 14.9 安全增强

| # | 补充项 | 说明 | 优先级 |
|---|--------|------|--------|
| 14.9.1 | 查询白名单模式 | Phase 3 增加按表/字段粒度的访问白名单（`config/access_policy.yaml`） | P2 |
| 14.9.2 | 查询结果水印 | 分析结果中追加"此数据由 AI 生成，请以原始数据为准"声明 | P2 |
| 14.9.3 | LLM 输出二次校验 | `generate_sql` 输出的 SQL 中引用的表名/字段名必须全在 `state["relevant_tables"]` 中存在 | P1 |

---

### 14.10 变更摘要

| 章节 | 变更类型 | 描述 |
|------|---------|------|
| 3.2 | 边界明确 | introspection ↔ schema_manager 职责划分 |
| 3.2.5 | 安全约束 | `_auto_discover_enum_values` 增加行数阈值和超时 |
| 3.2.6 | 并发控制 | CacheRefresher 增加 Redis 分布式锁 |
| 3.4.2 | 可配置 | EXPLAIN 增加 `EXPLAIN_SKIP_DIALECTS` 跳过配置 |
| 3.8.5 | 性能 | `_summarize_turns` Phase 1 用规则替代 LLM，Phase 2 异步预计算 |
| 4.7 | 内存安全 | compute_statistics 增加 `MAX_STATS_ROWS` 上限 |
| 14.x | 新增 | 异常降级策略、双写事务顺序、权限告警、MCP 降级、Mock 方案、API 补全、版本管理 |
