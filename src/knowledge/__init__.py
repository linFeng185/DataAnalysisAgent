"""
知识库 — Schema 缓存管理 + 业务知识检索。

核心能力:
  - SchemaManager: 三级回退获取表结构（ChromaDB缓存 → 文档 → DB内省）
  - KnowledgeEntry: 统一的知识原子单元（表级 + 字段级双粒度索引）
  - 自动缓存: DB 内省结果自动写入 ChromaDB（TTL 7 天）

待开发:
  - BusinessRuleStore: 业务规则向量检索（6.3）
  - Doc Loader: Markdown 文档解析与索引（6.6）
  - Enum Discovery: 低基数字段枚举值采样（6.5）
  - Cache Refresher: 定时缓存清理（6.4）
"""
