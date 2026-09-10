"""MilvusVectorStore — Milvus 实现的 VectorStore。

前置条件: Docker: docker run -d --name milvus -p 19530:19530 milvusdb/milvus:latest
          pip install pymilvus
启用: config.py 中 vector_store_type = "milvus"
"""

from __future__ import annotations

import asyncio
import json
import logging as stdlib_logging
import time
from collections.abc import Callable, Generator
from typing import Any

from src.config import get_settings
from src.logging_config import get_logger
from src.memory.vector_store import (
    MetadataFilters,
    VectorEntry,
    VectorSearchResult,
    VectorStore,
    normalize_metadata_filters,
)

logger = get_logger(__name__)
_COLLECTION = "data_agent_knowledge"
_DIM = 384
_QUERY_BATCH_SIZE = 1_000
_RPC_TIMEOUT_SECONDS = 10.0
_ITERATION_TIMEOUT_SECONDS = 30.0
# load 失败后的熔断窗口：集合不可用时让后续请求快速失败，而不是各等一次完整加载超时。
_LOAD_FAILURE_COOLDOWN_SECONDS = 60.0
# 服务端「集合未加载」类错误关键字，用于失效本地加载标记后重建。
_NOT_LOADED_MARKERS = (
    "not loaded",
    "collection not found",
    "collection not loaded",
    "no available query node",
)


class MilvusCollectionUnavailable(RuntimeError):
    """集合处于加载熔断期时抛出，调用方应降级而不是重试。"""


class MilvusVectorStore(VectorStore):
    """Milvus 向量存储。嵌入复用 all-MiniLM-L6-v2（384维）。"""

    # 在线程中初始化 Milvus，所有远程阶段设置期限，避免阻塞服务启动与热重载。
    # Args: cls - 存储类；uri - Milvus 连接地址，不写入日志。
    # Returns: 已初始化的存储实例。
    @classmethod
    async def create(cls, uri: str) -> "MilvusVectorStore":
        """连接到 Milvus，创建 Collection（幂等）。

        Args:
            uri: Milvus 地址，如 http://localhost:19530

        Returns: MilvusVectorStore 实例
        """
        from pymilvus import (
            Collection,
            CollectionSchema,
            DataType,
            FieldSchema,
            connections,
            utility,
        )

        # 集中执行阻塞初始化，不把 SDK 的同步 RPC 放在事件循环线程。
        # Args: 无，读取连接地址及集合定义。
        # Returns: 无返回值。
        def initialize() -> None:
            connections.connect("default", uri=uri, timeout=_RPC_TIMEOUT_SECONDS)
            if not utility.has_collection(_COLLECTION, timeout=_RPC_TIMEOUT_SECONDS):
                id_f = FieldSchema(name="id", dtype=DataType.VARCHAR, is_primary=True, max_length=128)
                emb_f = FieldSchema(name="embedding", dtype=DataType.FLOAT_VECTOR, dim=_DIM)
                cont_f = FieldSchema(name="content", dtype=DataType.VARCHAR, max_length=65535)
                meta_f = FieldSchema(name="metadata", dtype=DataType.VARCHAR, max_length=65535)
                schema = CollectionSchema(fields=[id_f, emb_f, cont_f, meta_f])
                col = Collection(name=_COLLECTION, schema=schema, timeout=_RPC_TIMEOUT_SECONDS)
                col.create_index(field_name="embedding", index_params={
                    "metric_type": "COSINE", "index_type": "IVF_FLAT", "params": {"nlist": 128}},
                    timeout=_RPC_TIMEOUT_SECONDS)
                logger.info("Milvus Collection 已创建", name=_COLLECTION, dim=_DIM)

        store = cls()
        await store._run_sync(initialize)
        await store._get_embed_fn()
        logger.info("Milvus 初始化完成", collection=_COLLECTION)
        return store

    # 初始化写互斥，确保取消请求时未结束的写入仍受保护。
    # Args: self - 实例。
    # Returns: 无返回值。
    def __init__(self) -> None:
        self._embed_fn = None
        self._embed_lock = asyncio.Lock()
        self._write_lock = asyncio.Lock()
        # 集合是否已确认加载完成；确认后不再重复发起 LoadCollection RPC。
        self._collection_loaded = False
        # 熔断截止时间（monotonic）；大于当前时间表示处于熔断窗口内。
        self._load_failure_until = 0.0
        # 最近一次加载失败原因，用于熔断期的诊断日志。
        self._load_error_detail = ""

    # 在线程中执行同步 SDK，取消时等待当前操作退出再释放外层写锁。
    # Args: self - 实例；operation - 已设置有限 RPC 超时的同步操作。
    # Returns: 同步操作结果；取消和异常继续向处理边界传播。
    async def _run_sync[Result](self, operation: Callable[[], Result]) -> Result:
        task = asyncio.create_task(asyncio.to_thread(operation))
        try:
            return await asyncio.shield(task)
        except asyncio.CancelledError:
            while not task.done():
                try:
                    await asyncio.shield(task)
                except asyncio.CancelledError:
                    continue
                except Exception:
                    break
            if not task.cancelled():
                task.exception()
            raise

    # 判断异常是否属于「集合未加载」类错误，用于失效本地加载标记。
    # Args: exc - 捕获到的异常。
    # Returns: 属于未加载类错误时返回 True。
    @staticmethod
    def _is_not_loaded_error(exc: BaseException) -> bool:
        text = str(exc).lower()
        return any(marker in text for marker in _NOT_LOADED_MARKERS)

    # 熔断窗口内立即抛出，避免集合不可用时每个请求都白等一次完整加载超时。
    # Args: self - 实例。
    # Returns: 无返回值。
    # Raises: MilvusCollectionUnavailable - 当前处于熔断窗口内。
    def _raise_if_circuit_open(self) -> None:
        remaining = self._load_failure_until - time.monotonic()
        if remaining <= 0:
            return
        logger.debug("集合加载熔断中", collection=_COLLECTION,
                     remaining_seconds=round(remaining, 1))
        raise MilvusCollectionUnavailable(
            f"集合 {_COLLECTION} 加载失败处于熔断期，剩余 {remaining:.0f}s，"
            f"原因: {self._load_error_detail}"
        )

    # 获取可查询集合，显式限制加载等待，调用方必须通过工作线程执行。
    # Args: self - 实例。
    # Returns: 已加载的 Milvus 集合。
    # Raises: MilvusCollectionUnavailable - 熔断窗口内；其余 load 异常原样上抛。
    def _get_collection(self) -> Any:
        from pymilvus import Collection
        self._raise_if_circuit_open()
        col = Collection(name=_COLLECTION, timeout=_RPC_TIMEOUT_SECONDS)
        # 已确认加载过就不再重发 LoadCollection：它在服务端是重量级集群操作，
        # 集合卡死时每次调用都要白白等满整个 _RPC_TIMEOUT_SECONDS。
        if self._collection_loaded:
            logger.debug("复用已加载集合", collection=_COLLECTION)
            return col
        started = time.monotonic()
        try:
            col.load(timeout=_RPC_TIMEOUT_SECONDS)
        except Exception as exc:
            # 加载失败立即熔断，避免下游每个请求重复付出同样的超时代价。
            self._collection_loaded = False
            self._load_failure_until = time.monotonic() + _LOAD_FAILURE_COOLDOWN_SECONDS
            self._load_error_detail = str(exc)
            logger.error("集合加载失败，进入熔断", collection=_COLLECTION,
                         cooldown_seconds=_LOAD_FAILURE_COOLDOWN_SECONDS,
                         error=str(exc), exc_info=True)
            raise
        self._collection_loaded = True
        self._load_failure_until = 0.0
        self._load_error_detail = ""
        logger.info("集合加载完成", collection=_COLLECTION,
                    elapsed_ms=round((time.monotonic() - started) * 1000))
        return col

    # 在工作线程中对已加载集合执行操作，并在集合被外部释放时失效本地加载标记。
    # Args: self - 实例；operation - 接收集合句柄的同步操作。
    # Returns: 操作结果。
    async def _run_on_collection[Result](self, operation: Callable[[Any], Result]) -> Result:
        # 熔断前置检查放在线程外，避免集合不可用时白白占用线程池。
        self._raise_if_circuit_open()

        # 集合加载、业务操作与异常归类留在同一工作线程，保持 SDK 边界隔离。
        # Args: 无，读取外层操作闭包。
        # Returns: 操作结果。
        def execute() -> Result:
            col = self._get_collection()
            try:
                return operation(col)
            except Exception as exc:
                # 服务端可能驱逐或人工 release 集合，失效标记让下次调用重建。
                if self._is_not_loaded_error(exc):
                    self._collection_loaded = False
                    logger.warning("集合加载标记失效，下次操作将重新加载",
                                   collection=_COLLECTION, error=str(exc))
                raise

        return await self._run_sync(execute)

    # 连同重量级库导入一起在线程中初始化模型，合并并发首次请求。
    # Args: self - 实例。
    # Returns: 已缓存的嵌入编码函数。
    async def _get_embed_fn(self) -> Callable[..., Any]:
        if self._embed_fn is not None:
            return self._embed_fn

        # 导入阶段也可能长时间扫描依赖，必须位于工作线程内。
        # Args: 无，读取模型路径配置。
        # Returns: 模型编码函数。
        def load_model() -> Callable[..., Any]:
            from sentence_transformers import SentenceTransformer
            from transformers.utils import logging as transformers_logging

            settings = get_settings()
            path = settings.embedding_model_path or "all-MiniLM-L6-v2"
            stdlib_logging.getLogger("sentence_transformers").setLevel(stdlib_logging.WARNING)
            stdlib_logging.getLogger("transformers").setLevel(stdlib_logging.WARNING)
            transformers_logging.disable_progress_bar()
            model = SentenceTransformer(path)
            logger.info("嵌入模型加载完成", path=path, dim=_DIM)
            return model.encode

        async with self._embed_lock:
            if self._embed_fn is None:
                self._embed_fn = await self._run_sync(load_model)
        return self._embed_fn

    # 在线程中编码文本并复用初始化完成的模型。
    # Args: self - 实例；text - 待编码文本。
    # Returns: 向量值列表。
    async def _embed(self, text: str) -> list[float]:
        encode = await self._get_embed_fn()
        result = await asyncio.to_thread(encode, text, show_progress_bar=False)
        return result.tolist()

    # 批量编码条目向量：逐条调用模型会让 1386 条的刷新把 30 秒预算吃满。
    # Args: self - 实例；entries - 待写入条目。
    # Returns: 与 entries 一一对应的向量列表，已带 embedding 的条目原样复用。
    async def _embed_entries(self, entries: list[VectorEntry]) -> list[list[float]]:
        pending = [entry.content for entry in entries if not entry.embedding]
        if not pending:
            return [entry.embedding for entry in entries]
        encode = await self._get_embed_fn()
        started = time.monotonic()
        # sentence-transformers 的 encode 接受列表入参，一次前向即可编码整批。
        vectors = await asyncio.to_thread(encode, pending, show_progress_bar=False)
        logger.info("向量批量编码完成", count=len(pending),
                    elapsed_ms=round((time.monotonic() - started) * 1000))
        batch = [vector.tolist() for vector in vectors]
        result: list[list[float]] = []
        cursor = 0
        for entry in entries:
            if entry.embedding:
                result.append(entry.embedding)
            else:
                result.append(batch[cursor])
                cursor += 1
        return result

    def _to_expr(self, filters: MetadataFilters | None) -> str | None:
        """把 metadata 等值条件转换为安全的 Milvus 候选过滤表达式。

        Args:
            filters: metadata 精确过滤条件。

        Returns:
            Milvus LIKE 表达式；无正向条件时返回 None。
        """
        logger.debug("构建 Milvus metadata 表达式入口", filter_count=len(filters or {}))
        if not filters:
            return None
        parts: list[str] = []
        for key, is_not, value in normalize_metadata_filters(filters):
            pattern = (
                f"%{json.dumps(key, ensure_ascii=False)}: "
                f"{json.dumps(value, ensure_ascii=False)}%"
            )
            predicate = f"metadata like {json.dumps(pattern, ensure_ascii=False)}"
            parts.append(f"not ({predicate})" if is_not else predicate)
        result = " && ".join(parts) if parts else None
        logger.debug("构建 Milvus metadata 表达式完成", has_expression=bool(result))
        return result

    @staticmethod
    def _metadata_matches(metadata: dict[str, Any], filters: MetadataFilters | None) -> bool:
        """对 Milvus 候选记录执行 metadata 精确匹配。

        Args:
            metadata: 记录的 metadata。
            filters: 等值或不等值过滤条件。

        Returns:
            全部条件精确满足时返回 True。
        """
        for key, is_not, expected in normalize_metadata_filters(filters):
            matched = metadata.get(key) == expected
            if matched == is_not:
                return False
        return True

    # 方法作用：通过 Milvus 查询迭代器分批读取候选，避免超过服务端结果窗口。
    # Args: collection - Milvus Collection；expr - 查询表达式；output_fields - 返回字段。
    # Returns: 逐条产出的候选记录生成器。
    @staticmethod
    def _iter_query_rows(
        collection: Any,
        *,
        expr: str,
        output_fields: list[str],
    ) -> Generator[dict[str, Any], None, None]:
        iterator = collection.query_iterator(
            batch_size=_QUERY_BATCH_SIZE,
            limit=-1,
            expr=expr,
            output_fields=output_fields,
            timeout=_RPC_TIMEOUT_SECONDS,
        )
        deadline = time.monotonic() + _ITERATION_TIMEOUT_SECONDS
        try:
            while True:
                if time.monotonic() >= deadline:
                    raise TimeoutError("Milvus 查询迭代超时")
                rows = iterator.next()
                if not rows:
                    break
                yield from rows
        finally:
            iterator.close()

    # ── 检索 ──

    # 异步执行有界向量检索，不让远程 RPC 阻塞请求事件循环。
    # Args: self - 实例；query - 查询；top_k - 数量；filters - 元数据条件。
    # Returns: 精确过滤后的向量结果。
    async def search(self, query: str, top_k: int = 5,
                     filters: MetadataFilters | None = None) -> list[VectorSearchResult]:
        """执行语义检索并对 metadata 候选做精确过滤。

        Args:
            query: 查询文本。
            top_k: 最大返回数。
            filters: metadata 精确过滤条件。

        Returns:
            匹配的向量检索结果。
        """
        logger.debug("Milvus search 入口", top_k=top_k, filter_count=len(filters or {}))
        _start = time.monotonic()
        emb = await self._embed(query)
        candidate_limit = 50 if filters else min(top_k, 50)
        results = await self._run_on_collection(lambda col: col.search(
            data=[emb], anns_field="embedding",
            param={"metric_type": "COSINE", "params": {"nprobe": 16}},
            limit=candidate_limit, expr=self._to_expr(filters),
            output_fields=["id", "content", "metadata"], timeout=_RPC_TIMEOUT_SECONDS))
        hits = []
        for hit in results[0]:
            e = hit.entity
            m = e.get("metadata") or "{}"
            metadata = json.loads(m) if isinstance(m, str) else (m or {})
            if not self._metadata_matches(metadata, filters):
                continue
            hits.append(VectorSearchResult(
                id=e.get("id") or "", content=e.get("content") or "",
                metadata=metadata,
                score=round(hit.score, 4)))
        result = hits[:min(top_k, 50)]
        logger.info("Milvus search 完成", hits=len(result),
                    elapsed_ms=round((time.monotonic()-_start)*1000))
        return result

    # 在线程中按 ID 查询，保留缺失结果语义。
    # Args: self - 实例；entry_id - 记录 ID。
    # Returns: 找到的记录或 None。
    async def get_by_id(self, entry_id: str) -> VectorEntry | None:
        """按安全引用的 ID 精确查询记录。

        Args:
            entry_id: 向量记录 ID。

        Returns:
            匹配记录；不存在时返回 None。
        """
        logger.debug("Milvus ID 查询入口", entry_id=entry_id)
        r = await self._run_on_collection(lambda col: col.query(
            expr=f"id == {json.dumps(entry_id, ensure_ascii=False)}",
            output_fields=["id", "content", "metadata"], limit=1, timeout=_RPC_TIMEOUT_SECONDS))
        if not r:
            logger.debug("Milvus ID 查询完成", entry_id=entry_id, found=False)
            return None
        m = r[0].get("metadata") or "{}"
        result = VectorEntry(id=r[0]["id"], content=r[0].get("content") or "",
                             metadata=json.loads(m) if isinstance(m, str) else (m or {}))
        logger.debug("Milvus ID 查询完成", entry_id=entry_id, found=True)
        return result

    # 在线程中查询元数据，限制总迭代时长并关闭游标。
    # Args: self - 实例；filters - 元数据条件；其余参数遵循接口契约。
    # Returns: 查询结果或处理数量。
    async def get_by_filter(self, filters: MetadataFilters, limit: int = 100) -> list[VectorEntry]:
        """按 metadata 精确条件查询记录。

        Args:
            filters: metadata 过滤条件。
            limit: 最大返回数。

        Returns:
            精确匹配的记录列表。
        """
        logger.debug("Milvus metadata 查询入口", filter_count=len(filters), limit=limit)
        if limit <= 0:
            return []
        # 将集合加载、迭代与资源关闭放在同一工作线程。
        # Args: 无，读取外层查询条件。
        # Returns: 精确过滤后的查询结果。
        def read_entries(col: Any) -> list[VectorEntry]:
            result: list[VectorEntry] = []
            rows = self._iter_query_rows(
                col,
                expr=self._to_expr(filters) or "",
                output_fields=["id", "content", "metadata"],
            )
            try:
                for row in rows:
                    raw_metadata = row.get("metadata") or "{}"
                    metadata = (
                        json.loads(raw_metadata) if isinstance(raw_metadata, str) else raw_metadata
                    )
                    if self._metadata_matches(metadata, filters):
                        result.append(VectorEntry(
                            id=row["id"], content=row.get("content") or "", metadata=metadata,
                        ))
                        if len(result) >= limit:
                            break
            finally:
                rows.close()
            logger.debug("Milvus metadata 查询完成", count=len(result))
            return result

        return await self._run_on_collection(read_entries)

    # ── 写入 ──

    # 批量写入在工作线程内保持串行，取消不能提前释放写锁。
    # Args: self - 实例；entries - 待写入条目。
    # Returns: 写入条目数。
    async def upsert(self, entries: list[VectorEntry]) -> int:
        """写入及 flush 设置有限超时，空批次不访问数据库。"""
        if not entries:
            return 0
        async with self._write_lock:
            ids = [e.id for e in entries]
            embs = await self._embed_entries(entries)

            # 保持远程写入与 flush 同一线程生命周期，避免取消后继续无锁修改。
            # Args: 无，使用外层批次与嵌入结果。
            # Returns: 无返回值。
            def write(col: Any) -> None:
                existing = col.query(expr=f'id in {json.dumps(ids)}', output_fields=["id"], timeout=_RPC_TIMEOUT_SECONDS)
                if existing:
                    col.delete(expr=f'id in {json.dumps([row["id"] for row in existing])}', timeout=_RPC_TIMEOUT_SECONDS)
                col.insert([ids, embs, [entry.content for entry in entries],
                            [json.dumps(entry.metadata, ensure_ascii=False) for entry in entries]], timeout=_RPC_TIMEOUT_SECONDS)
                col.flush(timeout=_RPC_TIMEOUT_SECONDS)

            await self._run_on_collection(write)
        logger.debug("Milvus upsert 完成", count=len(entries))
        return len(entries)

    # 在线程中删除指定条目，取消时等待写操作退出。
    # Args: self - 实例；ids - 待删除 ID。
    # Returns: 请求删除的条目数。
    async def delete_by_ids(self, ids: list[str]) -> int:
        """删除遵循同一写互斥和有限 RPC 超时。"""
        if not ids:
            return 0
        async with self._write_lock:
            await self._run_on_collection(
                lambda col: col.delete(
                    expr=f'id in {json.dumps(ids)}', timeout=_RPC_TIMEOUT_SECONDS))
        return len(ids)

    # 在线程中精确筛选后删除，避免扩大删除范围。
    # Args: self - 实例；filters - 元数据条件；其余参数遵循接口契约。
    # Returns: 查询结果或处理数量。
    async def delete_by_filter(self, filters: MetadataFilters) -> int:
        """仅删除 metadata 精确匹配的记录。

        Args:
            filters: metadata 过滤条件。

        Returns:
            实际删除的记录数。
        """
        logger.debug("Milvus metadata 删除入口", filter_count=len(filters))
        if not filters:
            logger.info("Milvus metadata 删除完成", count=0)
            return 0
        # 扫描与删除共享写互斥，整个过程都在线程中完成。
        # Args: 无，读取外层过滤条件。
        # Returns: 实际删除数量。
        def remove_entries(col: Any) -> int:
            rows = self._iter_query_rows(
                col,
                expr=self._to_expr(filters) or "",
                output_fields=["id", "metadata"],
            )
            ids = []
            for row in rows:
                raw_metadata = row.get("metadata") or "{}"
                metadata = (
                    json.loads(raw_metadata) if isinstance(raw_metadata, str) else raw_metadata
                )
                if self._metadata_matches(metadata, filters):
                    ids.append(row["id"])
            if ids:
                col.delete(expr=f"id in {json.dumps(ids, ensure_ascii=False)}", timeout=_RPC_TIMEOUT_SECONDS)
            return len(ids)

        async with self._write_lock:
            count = await self._run_on_collection(remove_entries)
        logger.info("Milvus metadata 删除完成", count=count)
        return count

    # ── 管理 ──

    # 在线程中获取带期限的计数，避免无期限属性 RPC。
    # Args: self - 实例；filters - 元数据条件；其余参数遵循接口契约。
    # Returns: 查询结果或处理数量。
    async def count(self, filters: MetadataFilters | None = None) -> int:
        """统计全部记录或 metadata 精确匹配记录。

        Args:
            filters: 可选的 metadata 过滤条件。

        Returns:
            记录数量。
        """
        logger.debug("Milvus count 入口", filter_count=len(filters or {}))
        # 将集合加载、迭代与资源关闭放在同一工作线程。
        # Args: 无，读取外层查询条件。
        # Returns: 精确过滤后的查询结果。
        def read_count(col: Any) -> int:
            if filters:
                rows = self._iter_query_rows(
                    col,
                    expr=self._to_expr(filters) or "",
                    output_fields=["id", "metadata"],
                )
                result = 0
                for row in rows:
                    raw_metadata = row.get("metadata") or "{}"
                    metadata = (
                        json.loads(raw_metadata) if isinstance(raw_metadata, str) else raw_metadata
                    )
                    result += int(self._metadata_matches(metadata, filters))
            else:
                counts = col.query(expr="", output_fields=["count(*)"], timeout=_RPC_TIMEOUT_SECONDS)
                result = int(counts[0]["count(*)"]) if counts else 0
            logger.debug("Milvus count 完成", count=result)
            return result

        return await self._run_on_collection(read_count)

    # 方法作用：检查默认 Milvus 连接是否存在且服务端可响应。
    # Args: self - 当前 MilvusVectorStore 实例。
    # Returns: 连接可用时返回 True，否则返回 False。
    async def health_check(self) -> bool:
        logger.debug("MilvusVectorStore 健康检查入口")
        # 熔断期内集合必然不可服务，直接判定不健康，不再发起无意义的探测 RPC。
        if time.monotonic() < self._load_failure_until:
            logger.warning("MilvusVectorStore 健康检查完成", healthy=False,
                           reason="集合加载熔断中", error=self._load_error_detail)
            return False
        try:
            from pymilvus import connections, utility

            # 把服务探针也放在线程中，不能因健康检查再次卡住整个服务。
            # Args: 无。
            # Returns: 连接及远程服务是否可用。
            def probe() -> bool:
                if not connections.has_connection("default"):
                    logger.warning("MilvusVectorStore 健康检查完成", healthy=False, reason="连接不存在")
                    return False
                utility.has_collection(_COLLECTION, using="default", timeout=_RPC_TIMEOUT_SECONDS)
                return True

            return await self._run_sync(probe)
        except Exception as exc:
            logger.error(
                "MilvusVectorStore 健康检查失败",
                error=str(exc),
                exc_info=True,
            )
            return False
