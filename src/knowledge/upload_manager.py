"""异步上传任务管理器 — 后台处理文件分块 + ChromaDB 写入。"""

from __future__ import annotations

import time
import json
from dataclasses import dataclass, field
from uuid import uuid4

from src.knowledge.doc_parser import ChunkConfig, chunk_text, extract_text
from src.memory.vector_store import VectorEntry, get_vector_store
from src.logging_config import get_logger

logger = get_logger(__name__)


@dataclass
class UploadTask:
    id: str = field(default_factory=lambda: str(uuid4())[:12])
    status: str = "pending"  # pending / processing / done / error
    file_name: str = ""
    chunks_count: int = 0
    error: str = ""
    created_at: float = 0
    started_at: float = 0
    finished_at: float = 0
    tenant_id: int = 1
    user_id: int = 0
    knowledge_scope: str = "private"
    tag_ids: list[int] = field(default_factory=list)
    tag_names: list[str] = field(default_factory=list)
    datasource: str = ""

    def to_dict(self) -> dict:
        """转换为不暴露内部身份字段的响应字典。

        Returns:
            上传任务状态摘要。
        """
        return {
            "id": self.id, "status": self.status, "file_name": self.file_name,
            "chunks_count": self.chunks_count, "error": self.error,
            "elapsed": round(self.finished_at - self.started_at, 2) if self.finished_at else 0,
        }


class UploadManager:
    """管理上传任务的生命周期。"""

    def __init__(self, max_tasks: int = 1000, retention_seconds: float = 86400) -> None:
        """初始化有界的上传任务管理器。

        Args:
            max_tasks: 进程内最多保留的任务数。
            retention_seconds: 终态任务的保留秒数。

        Returns:
            无返回值。
        """
        logger.debug(
            "初始化上传任务管理器入口",
            max_tasks=max_tasks,
            retention_seconds=retention_seconds,
        )
        if max_tasks <= 0:
            logger.error("初始化上传任务管理器失败", error="max_tasks 必须大于 0")
            raise ValueError("max_tasks 必须大于 0")
        if retention_seconds < 0:
            logger.error("初始化上传任务管理器失败", error="retention_seconds 不能小于 0")
            raise ValueError("retention_seconds 不能小于 0")
        self._tasks: dict[str, UploadTask] = {}
        self._max_tasks = max_tasks
        self._retention_seconds = retention_seconds
        logger.info("初始化上传任务管理器完成", max_tasks=max_tasks)

    def _prune_tasks(self, now: float | None = None) -> int:
        """清除超过保留期限的完成或失败任务。

        Args:
            now: 可注入的 monotonic 当前时间。

        Returns:
            被清除的任务数量。
        """
        current = time.monotonic() if now is None else now
        logger.debug("清理上传任务入口", task_count=len(self._tasks), now=current)
        expired_ids = []
        for task_id, task in self._tasks.items():
            if task.status not in {"done", "error"}:
                continue
            terminal_at = task.finished_at or task.created_at
            if current - terminal_at >= self._retention_seconds:
                expired_ids.append(task_id)
        for task_id in expired_ids:
            del self._tasks[task_id]
        logger.info("清理上传任务完成", pruned=len(expired_ids), remaining=len(self._tasks))
        return len(expired_ids)

    def create(
        self,
        file_name: str,
        *,
        knowledge_scope: str = "private",
        tag_ids: list[int] | None = None,
        tag_names: list[str] | None = None,
        datasource: str = "",
    ) -> UploadTask:
        """为当前身份创建上传任务。

        Args:
            file_name: 原始文件名。
            knowledge_scope: system/tenant/private 知识范围。
            tag_ids: 已校验的标签 ID。
            tag_names: 已校验的标签名称。
            datasource: 可选绑定的数据源名称。

        Returns:
            新建的上传任务。
        """
        from src.api.auth import get_current_tenant_id, get_current_user_id

        from src.app_context import get_tenant_policy
        from src.knowledge.governance import normalize_knowledge_scope
        from src.api.auth import get_current_role
        from src.security.tenant_policy import RequestIdentity

        normalized_scope = normalize_knowledge_scope(knowledge_scope).value
        role = get_current_role()
        user_id = get_current_user_id()
        now = time.monotonic()
        logger.debug(
            "创建上传任务入口",
            file_name=file_name,
            knowledge_scope=normalized_scope,
            role=role,
        )
        self._prune_tasks(now)
        if len(self._tasks) >= self._max_tasks:
            logger.warning("创建上传任务拒绝", reason="上传任务队列已满", max_tasks=self._max_tasks)
            raise RuntimeError("上传任务队列已满")
        tenant_id = get_current_tenant_id()
        if not get_tenant_policy().can_write_scope(
            normalized_scope,
            RequestIdentity(tenant_id=tenant_id, user_id=user_id, role=role),
        ):
            logger.warning(
                "创建上传任务拒绝",
                file_name=file_name,
                knowledge_scope=normalized_scope,
                role=role,
            )
            raise PermissionError(f"当前角色无权写入 {normalized_scope} 知识")
        t = UploadTask(
            file_name=file_name,
            created_at=now,
            tenant_id=tenant_id,
            user_id=user_id,
            knowledge_scope=normalized_scope,
            tag_ids=list(tag_ids or []),
            tag_names=list(tag_names or []),
            datasource=datasource.strip(),
        )
        self._tasks[t.id] = t
        logger.info("创建上传任务完成", task_id=t.id, tenant_id=t.tenant_id, user_id=t.user_id)
        return t

    def get(self, task_id: str) -> UploadTask | None:
        """获取当前身份拥有的上传任务。

        Args:
            task_id: 上传任务 ID。

        Returns:
            可见任务；不存在或无权访问返回 None。
        """
        from src.api.auth import get_current_tenant_id, get_current_user_id

        logger.debug("获取上传任务入口", task_id=task_id)
        self._prune_tasks()
        task = self._tasks.get(task_id)
        visible = bool(
            task
            and task.tenant_id == get_current_tenant_id()
            and task.user_id == get_current_user_id()
        )
        logger.info("获取上传任务完成", task_id=task_id, found=visible)
        return task if visible else None

    def list_recent(self, limit: int = 20) -> list[dict]:
        """列出当前身份最近的上传任务。

        Args:
            limit: 最大返回条数。

        Returns:
            可见任务状态列表。
        """
        from src.api.auth import get_current_tenant_id, get_current_user_id

        logger.debug("列出上传任务入口", limit=limit)
        self._prune_tasks()
        tenant_id = get_current_tenant_id()
        user_id = get_current_user_id()
        visible = [
            task for task in self._tasks.values()
            if task.tenant_id == tenant_id and task.user_id == user_id
        ]
        result = [task.to_dict() for task in visible[-limit:]]
        logger.info("列出上传任务完成", count=len(result), tenant_id=tenant_id, user_id=user_id)
        return result

    async def process(
        self,
        task: UploadTask,
        content: bytes,
        config: ChunkConfig,
        category: str = "",
    ) -> None:
        """后台提取文本、分块并写入 VectorStore。

        Args:
            task: 已绑定身份、范围和标签的上传任务。
            content: 原始文件二进制内容。
            config: 文档分块配置。
            category: 兼容旧调用方的知识分类。

        Returns:
            无返回值，处理结果写回 task。
        """
        task.status = "processing"
        task.started_at = time.monotonic()
        logger.info("开始处理上传文件", task_id=task.id, file=task.file_name,
                    strategy=config.strategy.value, category=category)

        try:
            if task.file_name.lower().endswith((".pdf", ".docx")):
                from src.knowledge.document_assets import DocumentAssetAdapter, chunk_document
                asset = DocumentAssetAdapter().parse(task.file_name, content)
                chunks = chunk_document(asset, config)
            else:
                text = extract_text(task.file_name, content)
                if not text.strip():
                    raise ValueError("文档内容为空")
                chunks = chunk_text(text, config, task.file_name)
            task.chunks_count = len(chunks)
            logger.info("分块完成", task_id=task.id, chunks=len(chunks))

            await _write_to_chromadb(
                chunks, task.file_name, config, category,
                tenant_id=task.tenant_id, user_id=task.user_id,
                knowledge_scope=task.knowledge_scope,
                tag_ids=task.tag_ids,
                tag_names=task.tag_names,
                datasource=task.datasource,
            )

            task.status = "done"
            task.finished_at = time.monotonic()
            logger.info("上传处理完成", task_id=task.id, chunks=task.chunks_count,
                        elapsed=round(task.finished_at - task.started_at, 2))
        except Exception as e:
            task.status = "error"
            task.error = str(e)
            task.finished_at = time.monotonic()
            logger.error("上传处理失败", task_id=task.id, error=str(e), exc_info=True)


async def _write_to_chromadb(
    chunks,
    file_name: str,
    config: ChunkConfig,
    category: str = "",
    tenant_id: int = 1,
    user_id: int = 0,
    knowledge_scope: str = "private",
    tag_ids: list[int] | None = None,
    tag_names: list[str] | None = None,
    datasource: str = "",
) -> None:
    """把文档分块按知识范围和标签写入 VectorStore。

    Args:
        chunks: 文档分块列表。
        file_name: 原始文件名。
        config: 分块配置。
        category: 知识分类。
        tenant_id: 创建任务的租户 ID。
        user_id: 创建任务的用户 ID。
        knowledge_scope: system/tenant/private 知识范围。
        tag_ids: 标签 ID 列表。
        tag_names: 标签名称列表。
        datasource: 可选绑定的数据源名称。

    Returns:
        无返回值。
    """
    from src.knowledge.governance import KnowledgeScope, normalize_knowledge_scope

    normalized_scope = normalize_knowledge_scope(knowledge_scope)
    serialized_tag_ids = json.dumps(list(tag_ids or []), ensure_ascii=False)
    serialized_tag_names = ",".join(str(name).strip() for name in (tag_names or []) if str(name).strip())
    logger.debug(
        "写入上传分块入口",
        file_name=file_name,
        tenant_id=tenant_id,
        user_id=user_id,
        knowledge_scope=normalized_scope.value,
        tag_count=len(tag_ids or []),
    )
    ids, docs, metas = [], [], []
    for c in chunks:
        if normalized_scope is KnowledgeScope.SYSTEM:
            entry_id = f"system:{c.id}"
        elif normalized_scope is KnowledgeScope.TENANT:
            entry_id = f"tenant:{tenant_id}:{c.id}"
        else:
            entry_id = f"private:{tenant_id}:{user_id}:{c.id}"
        ids.append(entry_id)
        docs.append(c.content)
        locator = c.metadata.get("locator", {})
        if not isinstance(locator, dict):
            locator = {}
        if not locator:
            locator = {
                key: c.metadata[key]
                for key in ("page", "paragraph", "sheet", "cell_range", "line_start", "line_end")
                if c.metadata.get(key) not in (None, "")
            }
        meta = {
            "source": "user_upload",
            "source_file": file_name,
            "category": category or "general",
            "strategy": c.metadata.get("strategy", ""),
            "chunk_size": c.metadata.get("chunk_size", 0),
            "visibility": normalized_scope.value,
            "asset_id": f"document:{normalized_scope.value}:{tenant_id}:{user_id}:{file_name}",
            "document_version": "v1",
            "tag_ids_json": serialized_tag_ids,
            "tags": serialized_tag_names,
            # Chroma metadata 只允许标量值，定位结构序列化后再写入。
            "locator_json": json.dumps(locator, ensure_ascii=False),
        }
        if normalized_scope is KnowledgeScope.SYSTEM:
            meta["tenant_id"] = 0
            meta["owner_user_id"] = 0
        else:
            meta["tenant_id"] = tenant_id
        if normalized_scope is KnowledgeScope.PRIVATE:
            meta["owner_user_id"] = user_id
        elif normalized_scope is KnowledgeScope.TENANT:
            meta["uploaded_by_user_id"] = user_id
        if datasource.strip():
            meta["datasource"] = datasource.strip()
        tbl = c.metadata.get("table_name", "")
        if tbl:
            meta["table_name"] = tbl
        metas.append(meta)
    if ids:
        store = await get_vector_store()
        await store.upsert([
            VectorEntry(id=ids[index], content=docs[index], metadata=metas[index])
            for index in range(len(ids))
        ])
    logger.info(
        "写入上传分块完成",
        file_name=file_name,
        count=len(ids),
        tenant_id=tenant_id,
        knowledge_scope=normalized_scope.value,
    )


# 方法作用：从当前 AppContext 获取上传任务管理器。
# Args: 无。
# Returns: 当前应用独享的 UploadManager 实例。
def get_upload_manager() -> UploadManager:
    """获取当前应用的 UploadManager。

    Returns:
        当前 AppContext 的 UploadManager 实例。
    """
    from src.app_context import get_app_context

    logger.debug("获取 UploadManager 入口")
    result = get_app_context().get_or_create("upload_manager", UploadManager)
    logger.info("获取 UploadManager 完成")
    return result
