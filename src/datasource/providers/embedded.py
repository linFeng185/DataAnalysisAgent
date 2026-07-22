"""内置模式 Provider — 自发现数据源 + ORM Schema 优先提取。"""

from __future__ import annotations

import os
from importlib import import_module

from src.datasource.config import DataSourceConfig
from src.datasource.introspection import introspect_database
from src.datasource.providers.base import DataSourceProvider
from src.datasource.schema_snapshot import SchemaSnapshot
from src.logging_config import get_logger

logger = get_logger(__name__)

_DIALECT_DEFAULTS = {"clickhouse": 9000, "mysql": 3306, "postgres": 5432}


class EmbeddedDataSourceProvider(DataSourceProvider):
    """内置模式 — 从环境变量 + ORM 自发现数据源。

    Schema 提取优先级 (SPEC §3.11.3):
    ① ORM Model docstring/comment → 高
    ② DB 内省 → 兜底
    """

    def __init__(self) -> None:
        self._sources: dict[str, DataSourceConfig] = {}
        self._load_from_env()
        self._load_from_orm()

    # ========== 环境变量发现 (2.2.2) ==========

    def _load_from_env(self) -> None:
        if name := os.getenv("DATASOURCE_NAME"):
            self._register(_parse_ds_from_prefix("", name))
        for n in range(5):
            name = os.getenv(f"DATASOURCE_{n}_NAME")
            if not name:
                break
            self._register(_parse_ds_from_prefix(f"DATASOURCE_{n}_", name))

    # ========== ORM 自发现 (2.2.3-2.2.7) ==========

    def _load_from_orm(self) -> None:
        """从 Django/SQLAlchemy 项目自动发现数据库连接。"""
        if self._is_django_project():
            for config in self._from_django_config():
                self._register(config)
        elif self._has_sqlalchemy_engine():
            config = self._from_sqlalchemy_engine()
            if config:
                self._register(config)

    def _is_django_project(self) -> bool:
        """2.2.3 检测当前环境是否为 Django 项目。"""
        try:
            import_module("django")
        except ImportError:
            logger.info("Django 项目检测完成", installed=False)
            return False
        settings_module = os.getenv("DJANGO_SETTINGS_MODULE", "").strip()
        if not settings_module:
            logger.info("Django 项目检测完成", installed=True, configured=False)
            return False
        try:
            import django

            django.setup()
            from django.conf import settings

            result = settings.configured
            logger.info("Django 项目检测完成", installed=True, configured=result)
            return result
        except Exception as exc:
            logger.error("Django 项目检测失败", error=str(exc), exc_info=True)
            return False

    def _from_django_config(self) -> list[DataSourceConfig]:
        """2.2.4 从 Django DATABASES 解析数据源配置。"""
        try:
            from django.conf import settings
            configs = []
            for db_name, db_conf in settings.DATABASES.items():
                dialect = _normalize_dialect(db_conf.get("ENGINE", ""))
                configs.append(DataSourceConfig(
                    name=db_name,
                    mode="embedded",
                    dialect=dialect,
                    host=db_conf.get("HOST", "localhost"),
                    port=int(db_conf.get("PORT", 0)) or _DIALECT_DEFAULTS.get(dialect, 0),
                    database=db_conf.get("NAME", ""),
                    username=db_conf.get("USER", ""),
                    password=db_conf.get("PASSWORD", ""),
                    description=f"Django {db_name} 数据库",
                ))
            return configs
        except Exception as e:
            logger.error("Django 配置解析失败", error=str(e), exc_info=True)
            return []

    def _has_sqlalchemy_engine(self) -> bool:
        """2.2.5 检测是否存在 SQLAlchemy engine 实例。"""
        try:
            import_module("sqlalchemy")
            return True
        except ImportError:
            return False

    def _from_sqlalchemy_engine(self) -> DataSourceConfig | None:
        """2.2.6 从 SQLAlchemy engine 提取连接信息。"""
        try:
            # 尝试获取默认 engine
            import sqlalchemy as sa
            if hasattr(sa, "engine") and sa.engine:
                engine = sa.engine
            else:
                return None
            url = engine.url
            return DataSourceConfig(
                name="default",
                mode="embedded",
                dialect=_normalize_dialect(url.drivername),
                host=url.host or "localhost",
                port=url.port or _DIALECT_DEFAULTS.get(_normalize_dialect(url.drivername), 0),
                database=url.database or "",
                username=url.username or "",
                description="SQLAlchemy 项目数据库",
            )
        except Exception as e:
            logger.error("SQLAlchemy engine 提取失败", error=str(e), exc_info=True)
            return None

    # ========== ORM Model Schema (2.2.8-2.2.10) ==========

    def _find_orm_models(self) -> dict[str, list[type]]:
        """2.2.8 扫描 ORM Model 类。返回 {table_name: [model_class]}。"""
        models: dict[str, list[type]] = {}

        # Django
        try:
            from django.apps import apps
            for model in apps.get_models():
                table = model._meta.db_table
                models.setdefault(table, []).append(model)
        except Exception as exc:
            logger.debug(
                "Django ORM Model 扫描跳过",
                error=str(exc),
                exc_info=True,
            )

        # SQLAlchemy
        try:
            import sqlalchemy as sa
            if hasattr(sa, "orm"):
                # 遍历 declarative_base 的子类
                for mapper in sa.orm.registry.mappers:
                    model = mapper.class_
                    if hasattr(model, "__tablename__"):
                        table = model.__tablename__
                        models.setdefault(table, []).append(model)
        except Exception as exc:
            logger.debug(
                "SQLAlchemy ORM Model 扫描跳过",
                error=str(exc),
                exc_info=True,
            )

        return models

    def _extract_model_description(self, model: type) -> str:
        """2.2.9 从 ORM Model 提取中文描述。"""
        # SQLAlchemy: __doc__ 或 __table_args__["comment"]
        if model.__doc__:
            return model.__doc__.strip().split("\n")[0]
        try:
            table_args = getattr(model, "__table_args__", None)
            if isinstance(table_args, dict) and "comment" in table_args:
                return table_args["comment"]
        except Exception as exc:
            logger.debug(
                "ORM Model 表注释读取跳过",
                model=getattr(model, "__name__", ""),
                error=str(exc),
                exc_info=True,
            )
        # Django: Meta.verbose_name
        if hasattr(model, "_meta") and hasattr(model._meta, "verbose_name"):
            return str(model._meta.verbose_name)
        return ""

    # ========== Schema 提取 (2.2.10) ==========

    async def extract_schema(self, ds: DataSourceConfig) -> SchemaSnapshot:
        """ORM 优先 → DB 内省兜底。"""
        schema = SchemaSnapshot()

        # ① ORM Model 提取
        orm_models = self._find_orm_models()
        if orm_models:
            from src.datasource.schema_snapshot import ColumnInfo, TableSchema
            for table_name, model_classes in orm_models.items():
                model = model_classes[0]
                desc = self._extract_model_description(model)
                columns = []
                if hasattr(model, "_meta") and hasattr(model._meta, "fields"):
                    # Django
                    for field in model._meta.fields:
                        columns.append(ColumnInfo(
                            name=field.column,
                            type=field.get_internal_type(),
                            comment=getattr(field, "verbose_name", "") or getattr(field, "help_text", ""),
                            is_nullable=field.null,
                            is_primary_key=field.primary_key,
                        ))
                elif hasattr(model, "__table__"):
                    # SQLAlchemy
                    for col in model.__table__.columns:
                        columns.append(ColumnInfo(
                            name=col.name,
                            type=str(col.type),
                            comment=col.comment or "",
                            is_nullable=col.nullable,
                            is_primary_key=col.primary_key,
                        ))
                schema.tables.append(TableSchema(
                    name=table_name, description=desc, columns=columns,
                ))

        # ② DB 内省兜底 — 补充 ORM 未管理的表和字段
        try:
            async def _executor(ds_cfg, sql, params):
                import sqlalchemy as sa
                async with ds_cfg.engine.connect() as conn:
                    result = await conn.execute(sa.text(sql), params)
                    return [dict(row._mapping) for row in result]

            db_schema = await introspect_database(ds, _executor)
            schema.merge(db_schema)
        except Exception as e:
            logger.debug("DB 内省兜底失败", error=str(e))

        return schema

    # ========== 注册与公共接口 ==========

    def _register(self, ds: DataSourceConfig) -> None:
        self._sources[ds.name] = ds
        logger.info("发现数据源", name=ds.name, dialect=ds.dialect)

    async def lookup(self, name: str) -> DataSourceConfig | None:
        return self._sources.get(name)

    async def list_all(self) -> list[DataSourceConfig]:
        return list(self._sources.values())

    async def test_connection(self, ds: DataSourceConfig) -> bool:
        try:
            import sqlalchemy as sa
            async with ds.engine.connect() as conn:
                await conn.execute(sa.text("SELECT 1"))
            return True
        except Exception as exc:
            logger.error(
                "内置数据源连接测试失败",
                datasource=ds.name,
                error=str(exc),
                exc_info=True,
            )
            return False


def _parse_ds_from_prefix(prefix: str, name: str) -> DataSourceConfig:
    dialect = os.getenv(f"{prefix}DIALECT", "clickhouse")
    return DataSourceConfig(
        name=name, mode="embedded", dialect=dialect,
        host=os.getenv(f"{prefix}HOST", "localhost"),
        port=int(os.getenv(f"{prefix}PORT", "0")) or _DIALECT_DEFAULTS.get(dialect, 0),
        database=os.getenv(f"{prefix}DATABASE", ""),
        username=os.getenv(f"{prefix}USERNAME", ""),
        password=os.getenv(f"{prefix}PASSWORD", ""),
    )


def _normalize_dialect(engine_name: str) -> str:
    """归一化 Django/SQLAlchemy 引擎名到方言名。"""
    engine_name = engine_name.lower()
    if "clickhouse" in engine_name:
        return "clickhouse"
    if "postgres" in engine_name:
        return "postgres"
    if "mysql" in engine_name or "mariadb" in engine_name:
        return "mysql"
    if "sqlite" in engine_name:
        return "sqlite"
    return "postgres"
