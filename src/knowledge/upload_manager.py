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

    def __init__(self) -> None:
        self._tasks: dict[str, UploadTask] = {}

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

        from src.config import get_settings
        from src.knowledge.governance import can_write_knowledge_scope, normalize_knowledge_scope
        from src.api.auth import get_current_role

        normalized_scope = normalize_knowledge_scope(knowledge_scope).value
        role = get_current_role()
        user_id = get_current_user_id()
        logger.debug(
            "创建上传任务入口",
            file_name=file_name,
            knowledge_scope=normalized_scope,
            role=role,
        )
        if not can_write_knowledge_scope(
            normalized_scope,
            role=role,
            user_id=user_id,
            multi_tenant=get_settings().multi_tenant,
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
            tenant_id=get_current_tenant_id(),
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


_manager: UploadManager | None = None


def get_upload_manager() -> UploadManager:
    """获取 UploadManager 单例。

    Returns:
        模块级 UploadManager 实例。
    """
    global _manager
    if _manager is None:
        _manager = UploadManager()
    return _manager
