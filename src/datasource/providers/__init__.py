"""
数据源提供者 — 实现数据源的发现与 Schema 提取。

两种发现策略：
  - EmbeddedDataSourceProvider：从环境变量 / Django / SQLAlchemy 自动发现
  - ExternalDataSourceProvider：从 datasources.yaml 或 API 手动注册

统一实现 DataSourceProvider 接口：lookup / list_all / extract_schema / test_connection
"""
