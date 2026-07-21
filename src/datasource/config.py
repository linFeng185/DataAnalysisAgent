"""DataSourceConfig — 内置/外挂两种模式的统一配置契约。"""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass
class DataSourceConfig:
    """进入 LangGraph 前必须归一化为此结构。"""

    name: str
    dialect: str  # "clickhouse" | "mysql" | "postgres" | "presto" | "hive"
    mode: str  # "embedded" | "external"

    host: str = "localhost"
    port: int = 0
    database: str = ""
    username: str = ""
    password: str = ""

    engine: object = None  # SQLAlchemy AsyncEngine, resolve() 时注入
    connector: object = None  # ConnectorBase, resolve() 时注入
    schema: object = None  # SchemaSnapshot, resolve() 时注入

    description: str = ""
    version: str = ""  # 数据库版本号 (如 "8.0", "16", "24.x")，影响函数可用性和知识库匹配
    tags: list[str] = field(default_factory=list)
    extra_params: dict = field(default_factory=dict)
