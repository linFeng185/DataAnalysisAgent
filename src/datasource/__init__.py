"""
数据源管理层 — 数据库注册、发现、Schema 提取。

业务职责：
  管理「这个智能体可以查询哪些数据库」。支持两种发现方式：
  - 嵌入式：自动扫描本地 Django/SQLAlchemy ORM 模型
  - 外部式：从 YAML 配置或 API 注册远程数据库

核心模块：
  config.py           — DataSourceConfig 统一连接参数模型
  registry.py         — DataSourceRegistry 单例（按名解析 + 创建引擎）
  schema_snapshot.py  — SchemaSnapshot/TableSchema/ColumnInfo 数据模型
  credential_manager.py — Fernet 加密密码 + ENV_VAR 占位符解析
  introspection.py    — 多方言数据库内省（列/外键/行数）
  setup.py            — 演示数据源初始化（内存 SQLite）
  providers/          — 数据源提供者子包
"""
