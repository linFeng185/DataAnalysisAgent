"""
数据源提供者 — 实现数据源的发现与 Schema 提取。

配置与提取策略：
  - EmbeddedDataSourceProvider：保留 ORM 元数据提取适配，构造时不自动发现
  - ExternalDataSourceProvider：仅通过应用 API 创建，启动时从状态数据库恢复

统一实现 DataSourceProvider 接口：lookup / list_all / extract_schema / test_connection
"""
