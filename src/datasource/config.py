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
    schema: object = None  # SchemaSnapshot, resolve() 时注入

    description: str = ""
    tags: list[str] = field(default_factory=list)
    extra_params: dict = field(default_factory=dict)
