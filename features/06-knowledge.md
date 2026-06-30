# 6. 知识库管理

## 6. 知识库管理 (knowledge/) `[P0:14 P1:10 P2:4 P3:1]`

### 6.1 Schema 缓存管理

| # | 功能 | 文件 | 描述 | 状态 |
|---|------|------|------|------|
| 6.1.1 | SchemaManager 类 | `src/knowledge/schema_manager.py` | 三级回退: ChromaDB 缓存 → 文档仓库 → DB 自动拉取 | 开发完成 |
| 6.1.2 | get_or_fetch_schema() | 同上 | Schema 获取的主入口，包装完整三级回退逻辑 | 开发完成 |
| 6.1.3 | _query_cache() | 同上 | 查 ChromaDB 缓存，检查 TTL 是否过期 | 开发完成 |
| 6.1.4 | _find_uncached() | 同上 | 找出哪些表尚未缓存或已过期 → 合并为 _any_expired() | 开发完成 |
| 6.1.5 | _load_from_docs() | 同上 | 从 docs/metrics/ 目录加载 Markdown 文档 → 通过 DocLoader 实现 | 开发完成 |
| 6.1.6 | _introspect_from_db() | 同上 | DB 系统表自动拉取，生成表级 + 字段级双粒度索引 | 开发完成 |
| 6.1.7 | _upsert_to_cache() | 同上 | KnowledgeEntry 写入 ChromaDB | 开发完成 |
| 6.1.8 | _build_snapshot() | 同上 | 将所有 KnowledgeEntry 组装为 SchemaSnapshot | 开发完成 |
| 6.1.9 | _format_table_summary() | 同上 | 格式化为表级描述文本 | 开发完成 |
| 6.1.10 | _format_column_detail() | 同上 | 格式化为字段级描述文本 | 开发完成 |
| 6.1.11 | _execute_metadata_query() | 同上 | 执行元数据查询 → 通过 introspect_database() 间接实现 | 开发完成 |

### 6.2 知识条目

| # | 功能 | 文件 | 描述 | 状态 |
|---|------|------|------|------|
| 6.2.1 | KnowledgeSource Enum | `src/knowledge/models.py` | MANUAL_DOC / ORM_MODEL / DB_COMMENT / AUTO_INTROSPECT / USER_CORRECTION / SYSTEM_WARNING | 开发完成 |
| 6.2.2 | KnowledgeEntry dataclass | 同上 | id / content / source / table_name / column_name / category / tags / created_at / ttl / metadata | 开发完成 |
| 6.2.3 | 知识优先级判断 | 同上 | MANUAL_DOC > USER_CORRECTION > ORM_MODEL > DB_COMMENT > AUTO_INTROSPECT | 开发完成 |


### 6.3 业务规则存储

| # | 功能 | 文件 | 描述 | 状态 |
|---|------|------|------|------|
| 6.3.1 | BusinessRuleStore 类 | `src/knowledge/business_rules.py` | 业务规则存储: metric / filter / enum / period | 开发完成 |
| 6.3.2 | initialize() | 同上 | 启动时扫描 docs/metrics/ 目录，通过 DocLoader 索引 | 开发完成 |
| 6.3.3 | search_business_rules() | 同上 | 向量检索 Top-K 条相关规则（过滤 category="business_rule"） | 开发完成 |
| 6.3.4 | _index_metric_doc() | → `doc_loader.py` | Markdown 解析/切片/写入 → DocLoader.scan_and_load() 实现 | 开发完成 |
| 6.3.5 | _split_by_headings() | → `doc_loader.py` | 按 Markdown 标题拆分 → DocLoader._split_by_headings() | 开发完成 |
| 6.3.6 | _extract_metric_tags() | → `doc_loader.py` | 标签提取 → YAML frontmatter tags 字段 | 开发完成 |

### 6.4 缓存刷新

| # | 功能 | 文件 | 描述 | 状态 |
|---|------|------|------|------|
| 6.4.1 | CacheRefresher 类 | `src/knowledge/cache_refresher.py` | 定期刷新自动拉取的缓存 | 待开发 |
| 6.4.2 | refresh_expired() | 同上 | 清理 source=auto 且超过 7 天未更新的条目 | 待开发 |
| 6.4.3 | refresh_on_schema_change() | 同上 | DDL 变更监听 → 主动刷新 (轮询 INFORMATION_SCHEMA.TABLES UPDATE_TIME) | 待开发 |
| 6.4.4 | DDL 触发器/CDC 集成 (远期) | 同上 | 接入数据库原生 DDL 变更通知 | 待开发 | P3 |
| 6.4.5 | Redis 分布式锁 | 同上 | SETNX 防止多请求同时触发同一表的缓存刷新 | 待开发 | P1 |

### 6.5 枚举值发现

| # | 功能 | 文件 | 描述 | 状态 |
|---|------|------|------|------|
| 6.5.1 | auto_discover_enum_values() | `src/knowledge/enum_discovery.py` | 对低基数列采样枚举值 (SELECT DISTINCT ... LIMIT 50) | 开发完成 |
| 6.5.2 | is_low_cardinality_candidate() | 同上 | 唯一值 ≤ 20 → 可能是枚举 | 开发完成 |
| 6.5.3 | TTL 设置 | 同上 | 枚举值 TTL=1天 (比表结构变化快) | 开发完成 |

### 6.6 文档加载器

| # | 功能 | 文件 | 描述 | 状态 |
|---|------|------|------|------|
| 6.6.1 | Markdown 文档扫描 | `src/knowledge/doc_loader.py` | 递归扫描 docs/metrics/ 目录下所有 *.md 文件 | 开发完成 |
| 6.6.2 | YAML frontmatter 解析 | 同上 | 解析 Markdown 文件中的 YAML 元数据 (tags / category / tables) | 开发完成 |
| 6.6.3 | 按标题切片 | 同上 | 按 ## 将文档拆分为独立索引单元 | 开发完成 |
| 6.6.4 | ChromaDB 批量写入 | 同上 | Document 列表写入 ChromaDB collection | 开发完成 |

### 6.7 文档解析与异步上传（新增）

| # | 功能 | 文件 | 描述 | 状态 | 优先级 |
|---|------|------|------|------|--------|
| 6.7.1 | 多格式解析 | `src/knowledge/doc_parser.py` | PDF (PyPDF2) / Word / TXT / MD 文本提取 | 开发完成 | P2 |
| 6.7.2 | 智能分块引擎 | 同上 | 4 种策略 + 重叠 + 最小块配置 | 开发完成 | P2 |
| 6.7.3 | 异步上传任务 | `src/knowledge/upload_manager.py` | UploadTask 状态 + asyncio 后台 + ChromaDB 写入 | 开发完成 | P2 |
| 6.7.4 | 上传/查询 API | `src/api/routes.py` | POST upload + GET status + GET content + GET raw | 开发完成 | P2 |
| 6.7.5 | 删除保护 | 同上 | 系统条目/文档禁止删除 | 开发完成 | P2 |

---
