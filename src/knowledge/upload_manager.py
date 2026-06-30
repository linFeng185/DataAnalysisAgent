"""异步上传任务管理器 — 后台处理文件分块 + ChromaDB 写入。"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from uuid import uuid4

from src.knowledge.doc_parser import ChunkConfig, chunk_text, extract_text
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

    def to_dict(self) -> dict:
        return {
            "id": self.id, "status": self.status, "file_name": self.file_name,
            "chunks_count": self.chunks_count, "error": self.error,
            "elapsed": round(self.finished_at - self.started_at, 2) if self.finished_at else 0,
        }


class UploadManager:
    """管理上传任务的生命周期。"""

    def __init__(self) -> None:
        self._tasks: dict[str, UploadTask] = {}

    def create(self, file_name: str) -> UploadTask:
        t = UploadTask(file_name=file_name)
        self._tasks[t.id] = t
        return t

    def get(self, task_id: str) -> UploadTask | None:
        return self._tasks.get(task_id)

    def list_recent(self, limit: int = 20) -> list[dict]:
        return [t.to_dict() for t in list(self._tasks.values())[-limit:]]

    async def process(self, task: UploadTask, content: bytes, config: ChunkConfig,
                      category: str = ""):
        """后台：提取文本 → 分块 → 写入 ChromaDB。"""
        task.status = "processing"
        task.started_at = time.monotonic()
        logger.info("开始处理上传文件", task_id=task.id, file=task.file_name,
                    strategy=config.strategy.value, category=category)

        try:
            text = extract_text(task.file_name, content)
            if not text.strip():
                raise ValueError("文档内容为空")

            chunks = chunk_text(text, config, task.file_name)
            task.chunks_count = len(chunks)
            logger.info("分块完成", task_id=task.id, chunks=len(chunks))

            await _write_to_chromadb(chunks, task.file_name, config, category)

            task.status = "done"
            task.finished_at = time.monotonic()
            logger.info("上传处理完成", task_id=task.id, chunks=task.chunks_count,
                        elapsed=round(task.finished_at - task.started_at, 2))
        except Exception as e:
            task.status = "error"
            task.error = str(e)
            task.finished_at = time.monotonic()
            logger.error("上传处理失败", task_id=task.id, error=str(e))


async def _write_to_chromadb(chunks, file_name: str, config: ChunkConfig,
                            category: str = ""):
    from src.knowledge.schema_manager import get_schema_manager
    sm = get_schema_manager()
    sm._ensure_initialized()  # noqa: SLF001
    ids, docs, metas = [], [], []
    for c in chunks:
        ids.append(c.id)
        docs.append(c.content)
        metas.append({
            "source": "user_upload",
            "source_file": file_name,
            "category": category or "general",
            "strategy": c.metadata.get("strategy", ""),
            "chunk_size": c.metadata.get("chunk_size", 0),
        })
    if ids:
        sm._collection.add(ids=ids, documents=docs, metadatas=metas)  # noqa: SLF001


_manager: UploadManager | None = None


def get_upload_manager() -> UploadManager:
    global _manager
    if _manager is None:
        _manager = UploadManager()
    return _manager
