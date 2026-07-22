"""VectorStore — 向量存储抽象层。

统一 ChromaDB / pgvector / Milvus 的检索与写入接口。
通过 config.vector_store_type 切换实现，新增向量库只需加一个实现类。
"""

from __future__ import annotations

import inspect
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from functools import partial
from typing import Any

from src.logging_config import get_logger

logger = get_logger(__name__)


@dataclass
class VectorEntry:
    """向量库中的一条记录。

    embedding 为 None 时由 embedding_fn 自动计算。
    """
    id: str
    content: str
    metadata: dict[str, Any] = field(default_factory=dict)
    embedding: list[float] | None = None


@dataclass
class VectorSearchResult:
    """语义检索的返回结果。

    score 为相似度 0~1，越大越相关。
    """
    id: str
    content: str
    metadata: dict[str, Any]
    score: float


class VectorStore(ABC):
    """向量存储抽象接口。

    实现类需覆盖全部抽象方法。
    调用方通过 get_vector_store() 工厂获取实例，
    不直接依赖具体实现。
    """

    # ── 检索 ──

    @abstractmethod
    async def search(
        self, query: str, top_k: int = 5,
        filters: dict[str, str] | None = None,
    ) -> list[VectorSearchResult]:
        """语义向量搜索，按相似度降序返回 top_k 条。

        Args:
            query: 搜索查询文本
            top_k: 返回结果数上限（内部限制 ≤50）
            filters: metadata 精确过滤 {"datasource": "mysql", "source": "user_upload"}

        Returns: 相关结果列表，已按 score 降序排列
        """
        ...

    @abstractmethod
    async def get_by_id(self, entry_id: str) -> VectorEntry | None:
        """按 ID 精确获取单条记录。

        Args:
            entry_id: ChromaDB 文档 ID

        Returns: 匹配的 VectorEntry，不存在返回 None
        """
        ...

    @abstractmethod
    async def get_by_filter(
        self, filters: dict[str, str], limit: int = 100,
    ) -> list[VectorEntry]:
        """按 metadata 精确过滤（非语义搜索，不做向量匹配）。

        Args:
            filters: metadata 键值对 {"category": "table", "datasource": "mysql"}
            limit: 返回条数上限

        Returns: 匹配的条目列表
        """
        ...

    # ── 写入 ──

    @abstractmethod
    async def upsert(self, entries: list[VectorEntry]) -> int:
        """批量插入或更新（按 ID 判断）。

        Args:
            entries: 待写入的条目列表

        Returns: 实际写入数
        """
        ...

    @abstractmethod
    async def delete_by_ids(self, ids: list[str]) -> int:
        """按 ID 列表批量删除。

        Args:
            ids: 待删除的文档 ID 列表

        Returns: 实际删除数
        """
        ...

    @abstractmethod
    async def delete_by_filter(self, filters: dict[str, str]) -> int:
        """按 metadata 过滤批量删除。

        Args:
            filters: metadata 键值对

        Returns: 实际删除数（ChromaDB 不返回精确数，返回 -1）
        """
        ...

    # ── 管理 ──

    @abstractmethod
    async def count(self, filters: dict[str, str] | None = None) -> int:
        """获取条目总数。

        Args:
            filters: 可选的 metadata 过滤条件

        Returns: 条目数量
        """
        ...

    @abstractmethod
    async def health_check(self) -> bool:
        """连通性检查。

        Returns: True 表示可用
        """
        ...


_VECTOR_STORE_RESOURCE = "vector_store"


async def get_vector_store() -> VectorStore:
    """获取当前 AppContext 的 VectorStore。

    首次调用时按 config.vector_store_type 选择实现并初始化。
    hot reload 时通过 health_check 自动检测失效并重建。
    调用方不关心底层是 ChromaDB/pgvector/Milvus。

    Returns: VectorStore 实例
    """
    from src.app_context import get_app_context

    context = get_app_context()
    store = context.get_resource(_VECTOR_STORE_RESOURCE)
    logger.debug("获取 VectorStore 入口", existing=store is not None)
    if store is not None:
        try:
            if await store.health_check():
                logger.info("获取 VectorStore 完成", reused=True)
                return store
        except Exception:
            logger.warning("VectorStore 健康检查失败，重建", exc_info=True)
        await context.close_resource(_VECTOR_STORE_RESOURCE)

    result = await context.get_or_create_async(
        _VECTOR_STORE_RESOURCE,
        partial(_create_configured_vector_store, context.settings),
        closer=_close_vector_store_resource,
    )
    logger.info("获取 VectorStore 完成", reused=False, backend=type(result).__name__)
    return result


# 方法作用：按当前应用配置创建对应的 VectorStore 后端。
# Args: settings - 当前 AppContext 的应用配置。
# Returns: 初始化完成的 VectorStore 实例。
async def _create_configured_vector_store(settings: Any) -> VectorStore:
    logger.debug(
        "创建 VectorStore 入口",
        backend=getattr(settings, "vector_store_type", "chroma"),
    )
    if not settings.vector_store_abstract_enabled:
        logger.info("VectorStore 抽象层未启用，使用 ChromaDB 直连")
        result = await _create_chroma_store()
        logger.info("创建 VectorStore 完成", backend=type(result).__name__)
        return result

    if settings.vector_store_type == "pgvector":
        logger.info("VectorStore 类型: pgvector")
        from src.memory.vector_store_pg import PgVectorStore
        result = await PgVectorStore.create(settings.database_url)
        logger.info("创建 VectorStore 完成", backend=type(result).__name__)
        return result

    if settings.vector_store_type == "milvus":
        logger.info("VectorStore 类型: Milvus", uri=settings.milvus_uri)
        from src.memory.vector_store_milvus import MilvusVectorStore
        result = await MilvusVectorStore.create(settings.milvus_uri)
        logger.info("创建 VectorStore 完成", backend=type(result).__name__)
        return result

    logger.info("VectorStore 类型: ChromaDB（默认）")
    result = await _create_chroma_store()
    logger.info("创建 VectorStore 完成", backend=type(result).__name__)
    return result


# 方法作用：释放支持 close/aclose 的 VectorStore 资源。
# Args: store - 当前 AppContext 持有的 VectorStore。
# Returns: 无返回值。
async def _close_vector_store_resource(store: VectorStore) -> None:
    logger.debug("关闭 VectorStore 资源入口", backend=type(store).__name__)
    close = getattr(store, "aclose", None) or getattr(store, "close", None)
    if close is None:
        logger.info("关闭 VectorStore 资源完成", skipped=True)
        return
    try:
        result = close()
        if inspect.isawaitable(result):
            await result
    except Exception:
        logger.error("关闭 VectorStore 资源失败", exc_info=True)
        raise
    logger.info("关闭 VectorStore 资源完成", skipped=False)


async def _create_chroma_store() -> VectorStore:
    """创建 ChromaDB 适配器实例。

    复用现有 SchemaManager 的 Collection 和 embedding_fn，
    不创建新连接，兼容已有数据。

    Returns: ChromaVectorStore 实例
    """
    from src.memory.vector_store_chroma import ChromaVectorStore
    from src.knowledge.schema_manager import get_schema_manager
    logger.debug("初始化 ChromaVectorStore")
    sm = get_schema_manager()
    sm._ensure_initialized()  # noqa: SLF001
    return ChromaVectorStore(sm._collection)  # noqa: SLF001
