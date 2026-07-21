"""PostgreSQL 轻量迁移执行器。"""

from __future__ import annotations

import hashlib
import re
from pathlib import Path

from src.db.utils import to_asyncpg_url
from src.logging_config import get_logger

logger = get_logger(__name__)

_MIGRATION_PATTERN = re.compile(r"^(\d{3,})_[A-Za-z0-9_-]+\.sql$")
_MIGRATION_LOCK_ID = 4_918_298_299_645_661_761
_PROJECT_ROOT = Path(__file__).resolve().parents[2]


class MigrationError(RuntimeError):
    """数据库迁移无法安全完成。"""


# 方法作用：发现并校验编号唯一的 SQL 迁移文件。
# Args: migrations_dir - SQL 迁移目录。
# Returns: 按版本号升序排列的 (version, path, checksum) 列表。
def _discover_migrations(migrations_dir: Path) -> list[tuple[str, Path, str]]:
    logger.debug("发现数据库迁移入口", migrations_dir=str(migrations_dir))
    if not migrations_dir.is_dir():
        logger.error("数据库迁移目录不存在", migrations_dir=str(migrations_dir))
        raise MigrationError(f"迁移目录不存在: {migrations_dir}")
    discovered: list[tuple[str, Path, str]] = []
    versions: set[str] = set()
    for path in migrations_dir.glob("*.sql"):
        match = _MIGRATION_PATTERN.fullmatch(path.name)
        if match is None:
            logger.warning("忽略非标准迁移文件", migration=path.name)
            continue
        version = match.group(1)
        if version in versions:
            logger.error("数据库迁移版本重复", version=version, migration=path.name)
            raise MigrationError(f"迁移版本重复: {version}")
        versions.add(version)
        content = path.read_bytes()
        discovered.append((version, path, hashlib.sha256(content).hexdigest()))
    result = sorted(discovered, key=lambda item: int(item[0]))
    logger.info("发现数据库迁移完成", migration_count=len(result))
    return result


# 方法作用：移除迁移文件自带的最外层事务包装，由执行器统一控制提交与回滚。
# Args: sql - 原始迁移 SQL；migration_name - 文件名，用于错误定位。
# Returns: 可在 asyncpg transaction 中执行的 SQL。
def _normalize_migration_sql(sql: str, migration_name: str) -> str:
    logger.debug("规范化数据库迁移入口", migration=migration_name, sql_chars=len(sql))
    starts_transaction = re.match(r"\A\s*BEGIN\s*;", sql, flags=re.IGNORECASE) is not None
    ends_transaction = re.search(r"COMMIT\s*;\s*\Z", sql, flags=re.IGNORECASE) is not None
    if starts_transaction != ends_transaction:
        logger.error("数据库迁移事务边界不完整", migration=migration_name)
        raise MigrationError(f"迁移事务边界不完整: {migration_name}")
    normalized = sql
    if starts_transaction:
        normalized = re.sub(r"\A\s*BEGIN\s*;\s*", "", normalized, count=1, flags=re.IGNORECASE)
        normalized = re.sub(r"\s*COMMIT\s*;\s*\Z", "", normalized, count=1, flags=re.IGNORECASE)
    if not normalized.strip():
        logger.error("数据库迁移内容为空", migration=migration_name)
        raise MigrationError(f"迁移内容为空: {migration_name}")
    logger.info("规范化数据库迁移完成", migration=migration_name, sql_chars=len(normalized))
    return normalized.strip()


# 方法作用：按版本顺序执行待应用迁移并维护 schema_migrations 版本表。
# Args: database_url - PostgreSQL asyncpg/SQLAlchemy URL；migrations_dir - 可选迁移目录。
# Returns: 本次实际应用的迁移文件名列表。
async def run_migrations(
    database_url: str,
    *,
    migrations_dir: str | Path | None = None,
) -> list[str]:
    """在 advisory lock 内逐文件事务执行幂等 SQL 迁移。

    Args:
        database_url: 应用状态 PostgreSQL URL。
        migrations_dir: 迁移目录；默认使用项目根目录 migrations。

    Returns:
        本次新应用的迁移文件名。
    """
    import asyncpg

    directory = Path(migrations_dir) if migrations_dir is not None else _PROJECT_ROOT / "migrations"
    migrations = _discover_migrations(directory)
    pg_url = to_asyncpg_url(database_url)
    if not pg_url.startswith(("postgresql://", "postgres://")):
        logger.error("数据库迁移 URL 方言不受支持")
        raise MigrationError("迁移执行器仅支持 PostgreSQL")
    logger.debug("数据库迁移执行入口", migration_count=len(migrations))
    connection = None
    locked = False
    applied_names: list[str] = []
    try:
        connection = await asyncpg.connect(pg_url)
        await connection.execute("SELECT pg_advisory_lock($1)", _MIGRATION_LOCK_ID)
        locked = True
        await connection.execute(
            "CREATE TABLE IF NOT EXISTS schema_migrations ("
            "version VARCHAR(32) PRIMARY KEY, name TEXT NOT NULL, checksum VARCHAR(64) NOT NULL, "
            "applied_at TIMESTAMPTZ NOT NULL DEFAULT NOW())"
        )
        rows = await connection.fetch(
            "SELECT version, name, checksum FROM schema_migrations ORDER BY version"
        )
        applied = {str(row["version"]): row for row in rows}
        for version, path, checksum in migrations:
            previous = applied.get(version)
            if previous is not None:
                if str(previous["checksum"]) != checksum or str(previous["name"]) != path.name:
                    logger.error("数据库迁移 checksum 不一致", version=version, migration=path.name)
                    raise MigrationError(f"迁移 checksum 不一致: {path.name}")
                logger.info("数据库迁移已应用", version=version, migration=path.name)
                continue
            script = _normalize_migration_sql(path.read_text(encoding="utf-8"), path.name)
            try:
                async with connection.transaction():
                    await connection.execute(script)
                    await connection.execute(
                        "INSERT INTO schema_migrations (version, name, checksum) VALUES ($1, $2, $3)",
                        version,
                        path.name,
                        checksum,
                    )
            except Exception as exc:
                logger.error(
                    "数据库迁移执行失败",
                    version=version,
                    migration=path.name,
                    exception_type=type(exc).__name__,
                    error=str(exc),
                    exc_info=True,
                )
                raise MigrationError(f"迁移执行失败: {path.name}") from exc
            applied_names.append(path.name)
            logger.info("数据库迁移应用完成", version=version, migration=path.name)
        logger.info("数据库迁移执行完成", applied_count=len(applied_names))
        return applied_names
    except MigrationError:
        raise
    except Exception as exc:
        logger.error(
            "数据库迁移连接失败",
            exception_type=type(exc).__name__,
            error=str(exc),
            exc_info=True,
        )
        raise MigrationError("数据库迁移无法完成") from exc
    finally:
        if connection is not None:
            if locked:
                try:
                    await connection.execute("SELECT pg_advisory_unlock($1)", _MIGRATION_LOCK_ID)
                except Exception as exc:
                    logger.error("数据库迁移锁释放失败", error=str(exc), exc_info=True)
            await connection.close()
            logger.info("数据库迁移连接已关闭")
