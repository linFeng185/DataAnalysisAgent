"""知识标签持久化：全局固定标签与用户个人标签的搜索和治理。"""

from __future__ import annotations

import re
import unicodedata
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any, Literal

from pydantic import BaseModel, Field

from src.config import get_settings
from src.knowledge.governance import is_super_admin
from src.logging_config import get_logger
from src.memory.pg_pool import pg_connection

logger = get_logger(__name__)

DEFAULT_GLOBAL_TAGS: tuple[dict[str, str], ...] = (
    {"name": "数据字典", "tag_group": "knowledge_type"},
    {"name": "表结构", "tag_group": "knowledge_type"},
    {"name": "字段说明", "tag_group": "knowledge_type"},
    {"name": "指标口径", "tag_group": "knowledge_type"},
    {"name": "业务规则", "tag_group": "knowledge_type"},
    {"name": "枚举字典", "tag_group": "knowledge_type"},
    {"name": "SQL模板", "tag_group": "knowledge_type"},
    {"name": "数据质量", "tag_group": "knowledge_type"},
    {"name": "分析方法", "tag_group": "knowledge_type"},
    {"name": "报表模板", "tag_group": "knowledge_type"},
    {"name": "操作手册", "tag_group": "knowledge_type"},
    {"name": "故障排查", "tag_group": "knowledge_type"},
    {"name": "安全合规", "tag_group": "knowledge_type"},
    {"name": "产品文档", "tag_group": "knowledge_type"},
    {"name": "接口文档", "tag_group": "knowledge_type"},
    {"name": "MySQL", "tag_group": "technology"},
    {"name": "PostgreSQL", "tag_group": "technology"},
    {"name": "ClickHouse", "tag_group": "technology"},
    {"name": "Oracle", "tag_group": "technology"},
    {"name": "SQL Server", "tag_group": "technology"},
    {"name": "SQLite", "tag_group": "technology"},
)


class KnowledgeTag(BaseModel):
    """可供上传和检索使用的标签记录。"""

    id: int
    name: str
    slug: str
    tag_group: str = "custom"
    aliases: list[str] = Field(default_factory=list)
    description: str = ""
    scope: Literal["global", "private"]
    tenant_id: int | None = None
    owner_user_id: int | None = None
    is_active: bool = True
    is_seed: bool = False


# 将中英文标签名称规范化为稳定唯一键。
# Args: name - 用户输入或预置标签名称。
# Returns: 小写、去重空白且不含特殊字符的 slug。
def normalize_tag_slug(name: str) -> str:
    logger.debug("规范化知识标签入口", name=name)
    normalized = unicodedata.normalize("NFKC", str(name)).strip().lower()
    normalized = re.sub(r"\s+", "-", normalized)
    normalized = re.sub(r"[^\w\-]+", "-", normalized, flags=re.UNICODE)
    result = re.sub(r"-+", "-", normalized).strip("-_")
    if not result:
        logger.error("规范化知识标签失败", name=name, error="标签名称为空")
        raise ValueError("标签名称不能为空")
    logger.info("规范化知识标签完成", slug=result)
    return result


class KnowledgeTagStore:
    """通过 PostgreSQL 管理全局标签和用户个人标签。"""

    # 初始化标签存储状态。
    # Args: 无。
    # Returns: 无返回值。
    def __init__(self) -> None:
        logger.debug("初始化知识标签存储入口")
        self._ready = False
        logger.info("初始化知识标签存储完成")

    # 确保标签表、唯一索引和默认标签存在。
    # Args: 无。
    # Returns: 无返回值；数据库未配置时保持不可用状态。
    async def _ensure(self) -> None:
        logger.debug("知识标签表初始化入口", ready=self._ready)
        if self._ready:
            logger.info("知识标签表初始化命中缓存")
            return
        url = get_settings().database_url
        if not url or "postgres" not in url:
            logger.warning("知识标签表初始化跳过", reason="PostgreSQL 未配置")
            return
        try:
            async with pg_connection(tenant_id=1, user_id=0, role="super_admin") as conn:
                await conn.execute(
                    """
                    CREATE TABLE IF NOT EXISTS knowledge_tags (
                        id BIGSERIAL PRIMARY KEY,
                        name VARCHAR(128) NOT NULL,
                        slug VARCHAR(160) NOT NULL,
                        tag_group VARCHAR(32) NOT NULL DEFAULT 'custom',
                        aliases TEXT[] NOT NULL DEFAULT '{}',
                        description TEXT NOT NULL DEFAULT '',
                        scope VARCHAR(16) NOT NULL CHECK (scope IN ('global', 'private')),
                        tenant_id INT NULL,
                        owner_user_id INT NULL,
                        is_active BOOLEAN NOT NULL DEFAULT TRUE,
                        is_seed BOOLEAN NOT NULL DEFAULT FALSE,
                        created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                        updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                        CHECK (
                            (scope = 'global' AND tenant_id IS NULL AND owner_user_id IS NULL)
                            OR (scope = 'private' AND tenant_id IS NOT NULL AND owner_user_id IS NOT NULL)
                        )
                    )
                    """
                )
                await conn.execute(
                    "CREATE UNIQUE INDEX IF NOT EXISTS uq_knowledge_tags_global_slug "
                    "ON knowledge_tags (slug) WHERE scope = 'global'"
                )
                await conn.execute(
                    "CREATE UNIQUE INDEX IF NOT EXISTS uq_knowledge_tags_private_slug "
                    "ON knowledge_tags (tenant_id, owner_user_id, slug) WHERE scope = 'private'"
                )
                for tag in DEFAULT_GLOBAL_TAGS:
                    await conn.execute(
                        "INSERT INTO knowledge_tags (name, slug, tag_group, scope, is_seed) "
                        "VALUES ($1, $2, $3, 'global', TRUE) "
                        "ON CONFLICT (slug) WHERE scope = 'global' DO NOTHING",
                        tag["name"], normalize_tag_slug(tag["name"]), tag["tag_group"],
                    )
            self._ready = True
            logger.info("知识标签表初始化完成", seed_count=len(DEFAULT_GLOBAL_TAGS))
        except Exception as exc:
            logger.error("知识标签表初始化失败", error=str(exc), exc_info=True)
            raise
    # 创建标签数据库连接。
    # Args: 无。
    # Returns: asyncpg 连接；配置缺失时抛出 RuntimeError。
    @asynccontextmanager
    async def _connect(self) -> AsyncIterator[Any]:
        logger.debug("知识标签数据库连接入口")
        await self._ensure()
        url = get_settings().database_url
        if not url or "postgres" not in url:
            logger.error("知识标签数据库连接失败", error="PostgreSQL 未配置")
            raise RuntimeError("知识标签需要 PostgreSQL 存储")
        try:
            from src.api.auth import get_current_role, get_current_tenant_id, get_current_user_id
            from src.knowledge.governance import normalize_role

            async with pg_connection(
                tenant_id=get_current_tenant_id(),
                user_id=get_current_user_id(),
                role=normalize_role(get_current_role()),
            ) as conn:
                yield conn
            logger.info("知识标签数据库连接完成")
        except Exception as exc:
            logger.error("知识标签数据库连接失败", error=str(exc), exc_info=True)
            raise

    # 把数据库记录转换为标签模型。
    # Args: row - asyncpg Record 或字典。
    # Returns: KnowledgeTag 模型。
    def _to_tag(self, row: Any) -> KnowledgeTag:
        logger.debug("转换知识标签记录入口", tag_id=row.get("id") if row else None)
        result = KnowledgeTag.model_validate(dict(row))
        logger.info("转换知识标签记录完成", tag_id=result.id, scope=result.scope)
        return result

    # 搜索当前用户可见的全局标签和个人标签。
    # Args: query - 名称、别名或 slug 关键词；tenant_id - 当前租户；user_id - 当前用户；
    #       limit - 返回数量；include_inactive - 是否包含停用标签。
    # Returns: 匹配的标签列表，全局标签优先。
    async def search(
        self,
        query: str = "",
        *,
        tenant_id: int,
        user_id: int,
        limit: int = 30,
        include_inactive: bool = False,
        include_all_private: bool = False,
    ) -> list[KnowledgeTag]:
        logger.debug(
            "搜索知识标签入口",
            query=query,
            tenant_id=tenant_id,
            user_id=user_id,
            limit=limit,
            include_all_private=include_all_private,
        )
        try:
            async with self._connect() as conn:
                pattern = f"%{query.strip()}%" if query.strip() else ""
                rows = await conn.fetch(
                    """
                    SELECT id, name, slug, tag_group, aliases, description, scope,
                           tenant_id, owner_user_id, is_active, is_seed
                    FROM knowledge_tags
                    WHERE ($3::boolean OR scope = 'global' OR (
                        scope = 'private' AND tenant_id = $1 AND owner_user_id = $2
                    ))
                      AND ($4::boolean OR is_active)
                      AND ($5 = '' OR name ILIKE $5 OR slug ILIKE $5
                           OR EXISTS (SELECT 1 FROM unnest(aliases) alias WHERE alias ILIKE $5))
                    ORDER BY CASE scope WHEN 'global' THEN 0 ELSE 1 END, name
                    LIMIT $6
                    """,
                    tenant_id, user_id, include_all_private, include_inactive, pattern,
                    min(max(limit, 1), 100),
                )
                result = [self._to_tag(row) for row in rows]
                logger.info("搜索知识标签完成", count=len(result), tenant_id=tenant_id, user_id=user_id)
                return result
        except Exception as exc:
            logger.error("搜索知识标签失败", error=str(exc), exc_info=True)
            raise

    # 按 ID 解析当前用户可见且启用的标签。
    # Args: tag_ids - 待解析标签 ID；tenant_id - 当前租户；user_id - 当前用户。
    # Returns: 保持请求顺序的可见标签列表，不可见 ID 不返回。
    async def get_visible_by_ids(
        self,
        tag_ids: list[int],
        *,
        tenant_id: int,
        user_id: int,
    ) -> list[KnowledgeTag]:
        logger.debug(
            "按 ID 解析可见知识标签入口",
            tag_count=len(tag_ids),
            tenant_id=tenant_id,
            user_id=user_id,
        )
        unique_ids = list(dict.fromkeys(int(tag_id) for tag_id in tag_ids if int(tag_id) > 0))
        if not unique_ids:
            logger.info("按 ID 解析可见知识标签完成", count=0)
            return []
        try:
            async with self._connect() as conn:
                rows = await conn.fetch(
                    """
                    SELECT id, name, slug, tag_group, aliases, description, scope,
                           tenant_id, owner_user_id, is_active, is_seed
                    FROM knowledge_tags
                    WHERE id = ANY($1::bigint[]) AND is_active
                      AND (scope = 'global' OR (
                          scope = 'private' AND tenant_id = $2 AND owner_user_id = $3
                      ))
                    """,
                    unique_ids, tenant_id, user_id,
                )
                by_id = {int(row["id"]): self._to_tag(row) for row in rows}
                result = [by_id[tag_id] for tag_id in unique_ids if tag_id in by_id]
                logger.info("按 ID 解析可见知识标签完成", count=len(result), requested=len(unique_ids))
                return result
        except Exception as exc:
            logger.error("按 ID 解析可见知识标签失败", error=str(exc), exc_info=True)
            raise

    # 创建仅当前用户可见的自定义标签。
    # Args: name - 标签名称；tenant_id - 当前租户；user_id - 当前用户；
    #       description - 标签说明；aliases - 标签别名。
    # Returns: 新建或重新启用的个人标签。
    async def create_personal(
        self,
        name: str,
        *,
        tenant_id: int,
        user_id: int,
        description: str = "",
        aliases: list[str] | None = None,
    ) -> KnowledgeTag:
        logger.debug("创建个人知识标签入口", name=name, tenant_id=tenant_id, user_id=user_id)
        clean_name = str(name).strip()
        if not clean_name or user_id <= 0:
            logger.error("创建个人知识标签失败", name=name, user_id=user_id, error="身份或名称无效")
            raise ValueError("标签名称不能为空且用户必须已登录")
        try:
            async with self._connect() as conn:
                row = await conn.fetchrow(
                    """
                    INSERT INTO knowledge_tags (
                        name, slug, tenant_id, owner_user_id, description, aliases, scope, is_active
                    ) VALUES ($1, $2, $3, $4, $5, $6, 'private', TRUE)
                    ON CONFLICT (tenant_id, owner_user_id, slug) WHERE scope = 'private'
                    DO UPDATE SET name = EXCLUDED.name, description = EXCLUDED.description,
                                  aliases = EXCLUDED.aliases, is_active = TRUE, updated_at = NOW()
                    RETURNING id, name, slug, tag_group, aliases, description, scope,
                              tenant_id, owner_user_id, is_active, is_seed
                    """,
                    clean_name, normalize_tag_slug(clean_name), tenant_id, user_id,
                    description.strip(), aliases or [],
                )
                result = self._to_tag(row)
                logger.info("创建个人知识标签完成", tag_id=result.id, tenant_id=tenant_id, user_id=user_id)
                return result
        except Exception as exc:
            logger.error("创建个人知识标签失败", error=str(exc), exc_info=True)
            raise

    # 创建或重新启用平台全局标签。
    # Args: name - 标签名称；actor_role - 操作者角色；tag_group - 标签分组；
    #       description - 标签说明；aliases - 标签别名。
    # Returns: 创建或更新后的全局标签。
    async def create_global(
        self,
        name: str,
        *,
        actor_role: str,
        tag_group: str = "custom",
        description: str = "",
        aliases: list[str] | None = None,
    ) -> KnowledgeTag:
        logger.debug("创建全局知识标签入口", name=name, actor_role=actor_role)
        if not is_super_admin(actor_role):
            logger.warning("创建全局知识标签拒绝", actor_role=actor_role)
            raise PermissionError("只有超级管理员可以维护全局标签")
        clean_name = str(name).strip()
        if not clean_name:
            logger.error("创建全局知识标签失败", error="标签名称为空")
            raise ValueError("标签名称不能为空")
        try:
            async with self._connect() as conn:
                row = await conn.fetchrow(
                    """
                    INSERT INTO knowledge_tags (
                        name, slug, tag_group, description, aliases, scope, is_active
                    ) VALUES ($1, $2, $3, $4, $5, 'global', TRUE)
                    ON CONFLICT (slug) WHERE scope = 'global'
                    DO UPDATE SET name = EXCLUDED.name, tag_group = EXCLUDED.tag_group,
                                  description = EXCLUDED.description, aliases = EXCLUDED.aliases,
                                  is_active = TRUE, updated_at = NOW()
                    RETURNING id, name, slug, tag_group, aliases, description, scope,
                              tenant_id, owner_user_id, is_active, is_seed
                    """,
                    clean_name, normalize_tag_slug(clean_name), tag_group.strip() or "custom",
                    description.strip(), aliases or [],
                )
                result = self._to_tag(row)
                logger.info("创建全局知识标签完成", tag_id=result.id)
                return result
        except Exception as exc:
            logger.error("创建全局知识标签失败", error=str(exc), exc_info=True)
            raise

    # 将个人标签复制提升为平台全局标签并停用原标签。
    # Args: tag_id - 待提升的个人标签 ID；actor_role - 操作者角色。
    # Returns: 提升后的全局标签。
    async def promote_to_global(self, tag_id: int, *, actor_role: str) -> KnowledgeTag:
        logger.debug("提升知识标签为全局入口", tag_id=tag_id, actor_role=actor_role)
        if not is_super_admin(actor_role):
            logger.warning("提升知识标签为全局拒绝", tag_id=tag_id, actor_role=actor_role)
            raise PermissionError("只有超级管理员可以提升全局标签")
        try:
            async with self._connect() as conn:
                source = await conn.fetchrow(
                    "SELECT name, tag_group, aliases, description FROM knowledge_tags "
                    "WHERE id = $1 AND scope = 'private'",
                    tag_id,
                )
                if source is None:
                    logger.warning("提升知识标签为全局失败", tag_id=tag_id, reason="个人标签不存在")
                    raise ValueError("待提升的个人标签不存在")
                row = await conn.fetchrow(
                    """
                    INSERT INTO knowledge_tags (
                        name, slug, tag_group, aliases, description, scope, is_active
                    ) VALUES ($1, $2, $3, $4, $5, 'global', TRUE)
                    ON CONFLICT (slug) WHERE scope = 'global'
                    DO UPDATE SET name = EXCLUDED.name, tag_group = EXCLUDED.tag_group,
                                  aliases = EXCLUDED.aliases, description = EXCLUDED.description,
                                  is_active = TRUE, updated_at = NOW()
                    RETURNING id, name, slug, tag_group, aliases, description, scope,
                              tenant_id, owner_user_id, is_active, is_seed
                    """,
                    source["name"], normalize_tag_slug(source["name"]), source["tag_group"],
                    source["aliases"], source["description"],
                )
                await conn.execute(
                    "UPDATE knowledge_tags SET is_active = FALSE, updated_at = NOW() WHERE id = $1",
                    tag_id,
                )
                result = self._to_tag(row)
                logger.info("提升知识标签为全局完成", source_tag_id=tag_id, global_tag_id=result.id)
                return result
        except Exception as exc:
            logger.error("提升知识标签为全局失败", tag_id=tag_id, error=str(exc), exc_info=True)
            raise

    # 启用或停用全局标签或本人个人标签。
    # Args: tag_id - 标签 ID；is_active - 目标状态；actor_role - 操作者角色；
    #       tenant_id - 当前租户；user_id - 当前用户。
    # Returns: 找到并更新标签时返回 True。
    async def set_active(
        self,
        tag_id: int,
        is_active: bool,
        *,
        actor_role: str,
        tenant_id: int,
        user_id: int,
    ) -> bool:
        logger.debug(
            "更新知识标签状态入口", tag_id=tag_id, is_active=is_active, actor_role=actor_role,
        )
        try:
            async with self._connect() as conn:
                if is_super_admin(actor_role):
                    status = await conn.execute(
                        "UPDATE knowledge_tags SET is_active = $2, updated_at = NOW() WHERE id = $1",
                        tag_id, is_active,
                    )
                else:
                    status = await conn.execute(
                        "UPDATE knowledge_tags SET is_active = $2, updated_at = NOW() "
                        "WHERE id = $1 AND scope = 'private' AND tenant_id = $3 AND owner_user_id = $4",
                        tag_id, is_active, tenant_id, user_id,
                    )
                result = not status.endswith(" 0")
                if not result:
                    logger.warning("更新知识标签状态未命中", tag_id=tag_id, actor_role=actor_role)
                logger.info("更新知识标签状态完成", tag_id=tag_id, updated=result)
                return result
        except Exception as exc:
            logger.error("更新知识标签状态失败", tag_id=tag_id, error=str(exc), exc_info=True)
            raise


# 获取当前 AppContext 的知识标签存储。
# Args: 无。
# Returns: 当前应用独享的 KnowledgeTagStore 实例。
def get_knowledge_tag_store() -> KnowledgeTagStore:
    from src.app_context import get_app_context

    logger.debug("获取知识标签存储入口")
    result = get_app_context().get_or_create("knowledge_tag_store", KnowledgeTagStore)
    logger.info("获取知识标签存储完成")
    return result
