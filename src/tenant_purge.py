"""租户物理删除与运行时资源清理服务。"""

from __future__ import annotations

import asyncio
import shutil
from collections.abc import Iterable
from pathlib import Path
from typing import Any

from src.logging_config import get_logger

logger = get_logger(__name__)

# 先删叶子表，再删用户和租户，避免外键残留或半删除状态。
_TENANT_DELETE_STATEMENTS: tuple[tuple[str, str], ...] = (
    # 通知模块（迁移 033）：先删收件人行，再删通知主体与租户渠道配置；
    # 旧表 analysis_notifications 已由迁移 034 删除。
    ("notification_recipients", "DELETE FROM notification_recipients WHERE tenant_id=$1"),
    ("notifications", "DELETE FROM notifications WHERE tenant_id=$1"),
    ("notification_channels", "DELETE FROM notification_channels WHERE tenant_id=$1"),
    ("analysis_schedule_runs", "DELETE FROM analysis_schedule_runs WHERE tenant_id=$1"),
    ("analysis_schedules", "DELETE FROM analysis_schedules WHERE tenant_id=$1"),
    ("tenant_llm_defaults", "DELETE FROM tenant_llm_defaults WHERE tenant_id=$1"),
    ("tenant_llm_connection_models", "DELETE FROM tenant_llm_connection_models "
     "WHERE connection_id IN (SELECT id FROM tenant_llm_connections WHERE tenant_id=$1)"),
    ("tenant_llm_connections", "DELETE FROM tenant_llm_connections WHERE tenant_id=$1"),
    ("knowledge_sql_observations", "DELETE FROM knowledge_sql_observations WHERE tenant_id=$1"),
    ("pending_vector_sync", "DELETE FROM pending_vector_sync WHERE entry_id IN "
     "(SELECT id FROM long_term_memories WHERE tenant_id=$1)"),
    ("long_term_memories", "DELETE FROM long_term_memories WHERE tenant_id=$1"),
    ("sessions_archive", "DELETE FROM sessions_archive WHERE tenant_id=$1"),
    ("sessions", "DELETE FROM sessions WHERE tenant_id=$1"),
    ("query_history", "DELETE FROM query_history WHERE tenant_id=$1"),
    ("knowledge_files", "DELETE FROM knowledge_files WHERE tenant_id=$1"),
    ("knowledge_tags", "DELETE FROM knowledge_tags WHERE tenant_id=$1"),
    ("mcp_servers", "DELETE FROM mcp_servers WHERE tenant_id=$1"),
    ("datasource_permissions", "DELETE FROM datasource_permissions WHERE tenant_id=$1"),
    ("datasource_configs", "DELETE FROM datasource_configs WHERE tenant_id=$1"),
    ("query_audit_log", "DELETE FROM query_audit_log WHERE tenant_id=$1"),
    ("users", "DELETE FROM users WHERE tenant_id=$1 AND id<>1"),
)


# 方法作用：执行固定 SQL 清理一个租户的全部关系型数据。
# Args: connection - 已开启事务的 PostgreSQL 连接；tenant_id - 待删除租户 ID。
# Returns: 每张表的删除行数摘要。
async def purge_tenant_rows(connection: Any, tenant_id: int) -> dict[str, int]:
    logger.info("租户关系数据清理入口", tenant_id=tenant_id)
    counts: dict[str, int] = {}
    for table, statement in _TENANT_DELETE_STATEMENTS:
        status = await connection.execute(statement, int(tenant_id))
        counts[table] = _status_count(status)
        logger.debug("租户关系表清理完成", tenant_id=tenant_id, table=table, deleted=counts[table])
    logger.info(
        "租户关系数据清理完成",
        tenant_id=tenant_id,
        table_count=len(counts),
        deleted_rows=sum(counts.values()),
    )
    return counts


# 方法作用：删除租户在进程内、向量库和缓存后端留下的运行时资源。
# Args: tenant_id - 租户 ID；datasource_rows/session_rows - 预先读取的数据源和会话；context - 可选 AppContext。
# Returns: 各运行时后端的清理摘要。
async def purge_tenant_runtime(
    tenant_id: int,
    datasource_rows: Iterable[Any] = (),
    session_rows: Iterable[Any] = (),
    *,
    context: Any | None = None,
) -> dict[str, int]:
    logger.info("租户运行时资源清理入口", tenant_id=tenant_id)
    result = {
        "datasources": 0,
        "vector_entries": 0,
        "cache_entries": 0,
        "skills": 0,
        "mcp_servers": 0,
        "checkpoints": 0,
        "upload_tasks": 0,
        "sessions": 0,
        "history": 0,
    }
    if context is None:
        from src.app_context import get_app_context

        context = get_app_context()

    # 先注销运行时数据源，释放连接池后再删数据库记录。
    try:
        from src.datasource.registry import get_registry

        registry = get_registry()
        names = {str(_row_value(row, "name")) for row in datasource_rows if _row_value(row, "name")}
        for name in names:
            if await registry.unregister(name, tenant_id=int(tenant_id)):
                result["datasources"] += 1
    except Exception as exc:
        logger.error("租户数据源运行时清理失败", tenant_id=tenant_id, error=str(exc), exc_info=True)
        raise RuntimeError("租户数据源连接清理失败") from exc

    # 删除向量知识与长期记忆副本；空过滤器永远不允许触发全库删除。
    try:
        from src.memory.vector_store import get_vector_store

        vector_store = context.get_resource("vector_store")
        if vector_store is None:
            vector_store = await get_vector_store()
        result["vector_entries"] = max(
            0, int(await vector_store.delete_by_filter({"tenant_id": int(tenant_id)})),
        )
    except Exception as exc:
        logger.error("租户向量知识清理失败", tenant_id=tenant_id, error=str(exc), exc_info=True)
        raise RuntimeError("租户向量知识清理失败") from exc

    # 删除连接级 Schema 缓存，兼容本地和 Redis 两种后端。
    try:
        from src.knowledge.datasource_cache import (
            build_connection_fingerprint,
            get_datasource_cache,
        )

        cache = context.get_resource("datasource_cache") or get_datasource_cache()
        for row in datasource_rows:
            config = _datasource_from_row(row)
            if config is None:
                continue
            await cache.delete(build_connection_fingerprint(config))
            result["cache_entries"] += 1
    except Exception as exc:
        logger.error("租户 Schema 缓存清理失败", tenant_id=tenant_id, error=str(exc), exc_info=True)
        raise RuntimeError("租户 Schema 缓存清理失败") from exc

    # 清理租户和私有 Skill 目录以及缓存项，内置 system Skill 永不删除。
    try:
        from src.skill_manager import get_skill_manager

        manager = context.get_resource("skill_manager") or get_skill_manager()
        for skill in list(getattr(manager, "skills", {}).values()):
            if int(getattr(skill, "tenant_id", 0) or 0) != int(tenant_id):
                continue
            if str(getattr(skill, "scope", "")) not in {"tenant", "private"}:
                continue
            manager.remove_skill(
                skill.name,
                scope=skill.scope,
                tenant_id=tenant_id,
                user_id=int(getattr(skill, "owner_user_id", 0) or 0),
            )
            result["skills"] += 1
        root = Path(manager.managed_dir)
        for directory in (root / "tenant" / str(tenant_id), root / "private" / str(tenant_id)):
            if directory.exists():
                shutil.rmtree(directory)
    except Exception as exc:
        logger.error("租户 Skill 清理失败", tenant_id=tenant_id, error=str(exc), exc_info=True)
        raise RuntimeError("租户 Skill 清理失败") from exc

    # 关闭租户 MCP 会话并清除身份加载标记，避免已删除配置继续提供工具。
    try:
        mcp_manager = context.get_resource("mcp_client_manager")
        if mcp_manager is not None:
            server_tenants = dict(getattr(mcp_manager, "_server_tenants", {}) or {})
            for internal_name, owner_tenant in server_tenants.items():
                if int(owner_tenant or 0) != int(tenant_id):
                    continue
                await mcp_manager.remove_server(internal_name)
                result["mcp_servers"] += 1
            loaded_identities = getattr(mcp_manager, "_loaded_identities", None)
            if isinstance(loaded_identities, set):
                for loaded_identity in list(loaded_identities):
                    if loaded_identity and int(loaded_identity[0]) == int(tenant_id):
                        loaded_identities.discard(loaded_identity)
    except Exception as exc:
        logger.error("租户 MCP 运行时清理失败", tenant_id=tenant_id, error=str(exc), exc_info=True)
        raise RuntimeError("租户 MCP 运行时清理失败") from exc

    # 删除租户全部作用域化和历史兼容 Checkpointer 线程，避免关系表删除后仍能恢复对话。
    try:
        rows = list(session_rows)
        if rows:
            from src.memory.checkpointer import get_checkpointer

            checkpointer = await get_checkpointer()
            semaphore = asyncio.Semaphore(16)

            # 方法作用：在有界并发内删除一个会话的作用域化与旧格式 checkpoint。
            # Args: row - 含 session_id 和 user_id 的会话行。
            # Returns: 完成删除的线程数量。
            async def _delete_checkpoint_row(row: Any) -> int:
                session_id = str(_row_value(row, "session_id") or "").strip()
                user_id = int(_row_value(row, "user_id") or 0)
                if not session_id:
                    return 0
                scoped_id = f"tenant:{int(tenant_id)}:user:{user_id}:session:{session_id}"
                async with semaphore:
                    await checkpointer.adelete_thread(scoped_id)
                    await checkpointer.adelete_thread(session_id)
                return 2

            deleted = await asyncio.gather(*(_delete_checkpoint_row(row) for row in rows))
            result["checkpoints"] = sum(deleted)
    except Exception as exc:
        logger.error("租户 Checkpointer 清理失败", tenant_id=tenant_id, error=str(exc), exc_info=True)
        raise RuntimeError("租户 Checkpointer 清理失败") from exc

    # 清理单进程回退缓冲，避免数据库删除后旧进程继续返回数据。
    for resource_name, counter_key in (
        ("session_store", "sessions"),
        ("history_store", "history"),
    ):
        resource = context.get_resource(resource_name)
        items = getattr(resource, "_items", None) if resource is not None else None
        if isinstance(items, list):
            before = len(items)
            resource._items = [
                item for item in items
                if int(item.get("tenant_id", 0) or 0) != int(tenant_id)
            ]
            result[counter_key] = before - len(resource._items)

    upload_manager = context.get_resource("upload_manager")
    tasks = getattr(upload_manager, "_tasks", None) if upload_manager is not None else None
    if isinstance(tasks, dict):
        task_ids = [
            task_id for task_id, task in tasks.items()
            if int(getattr(task, "tenant_id", 0) or 0) == int(tenant_id)
        ]
        for task_id in task_ids:
            del tasks[task_id]
        result["upload_tasks"] = len(task_ids)

    evolution = context.get_resource("knowledge_evolution_service")
    if evolution is not None:
        for collection_name in ("_local_observations", "_promoted", "_local_claims"):
            collection = getattr(evolution, collection_name, None)
            if isinstance(collection, (dict, set)):
                for key in list(collection):
                    if isinstance(key, tuple) and key and int(key[0]) == int(tenant_id):
                        if isinstance(collection, dict):
                            collection.pop(key, None)
                        else:
                            collection.discard(key)

    logger.info("租户运行时资源清理完成", tenant_id=tenant_id, **result)
    return result


# 方法作用：从 asyncpg execute 返回值提取受影响行数。
# Args: status - 形如 DELETE 3 的 PostgreSQL 状态字符串。
# Returns: 删除行数，无法解析时返回 0。
def _status_count(status: Any) -> int:
    try:
        return int(str(status).rsplit(" ", 1)[-1])
    except (TypeError, ValueError):
        return 0


# 方法作用：安全读取 asyncpg Record 或字典字段。
# Args: row - 数据源行；key - 字段名。
# Returns: 字段值；读取失败返回 None。
def _row_value(row: Any, key: str) -> Any:
    try:
        return row.get(key) if hasattr(row, "get") else row[key]
    except (KeyError, TypeError, AttributeError):
        return None


# 方法作用：把数据库数据源行转换为连接缓存指纹所需配置。
# Args: row - datasource_configs 查询结果。
# Returns: DataSourceConfig；缺少方言或名称时返回 None。
def _datasource_from_row(row: Any) -> Any | None:
    name = str(_row_value(row, "name") or "").strip()
    dialect = str(_row_value(row, "dialect") or "").strip()
    if not name or not dialect:
        return None
    from src.datasource.config import DataSourceConfig

    extra = _row_value(row, "extra_params") or {}
    if isinstance(extra, str):
        import json

        try:
            extra = json.loads(extra)
        except json.JSONDecodeError:
            extra = {}
    return DataSourceConfig(
        name=name,
        mode="external",
        dialect=dialect,
        host=str(_row_value(row, "host") or "localhost"),
        port=int(_row_value(row, "port") or 0),
        database=str(_row_value(row, "database_name") or ""),
        username=str(_row_value(row, "username") or ""),
        extra_params=dict(extra) if isinstance(extra, dict) else {},
    )
