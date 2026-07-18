"""系统知识只读目录扫描器，按 checksum 增量写入全局 VectorStore。"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable

from src.knowledge.doc_parser import ChunkConfig, ChunkStrategy, chunk_text, extract_text
from src.logging_config import get_logger
from src.memory.vector_store import VectorEntry

logger = get_logger(__name__)

SUPPORTED_SYSTEM_KNOWLEDGE_EXTENSIONS = {
    ".md", ".markdown", ".txt", ".pdf", ".docx", ".doc", ".csv",
}


@dataclass
class SystemKnowledgeScanResult:
    """一次系统知识目录扫描的结果摘要。"""

    scanned_files: int = 0
    ingested_files: int = 0
    skipped_files: int = 0
    error_files: int = 0
    written_chunks: int = 0
    errors: list[str] = field(default_factory=list)


# 解析分号分隔的系统知识目录并生成去重绝对路径。
# Args: value - SYSTEM_KNOWLEDGE_DIRS 字符串或路径列表。
# Returns: 保持声明顺序的绝对 Path 列表。
def parse_system_knowledge_dirs(value: str | Iterable[str | Path]) -> list[Path]:
    logger.debug("解析系统知识目录入口", value_type=type(value).__name__)
    if isinstance(value, str):
        raw_paths: Iterable[str | Path] = value.split(";")
    else:
        raw_paths = value
    result: list[Path] = []
    seen: set[str] = set()
    for raw_path in raw_paths:
        text = str(raw_path).strip()
        if not text:
            continue
        path = Path(text).expanduser().resolve()
        key = str(path).casefold()
        if key in seen:
            continue
        seen.add(key)
        result.append(path)
    logger.info("解析系统知识目录完成", directory_count=len(result))
    return result


class SystemKnowledgeScanner:
    """递归扫描受信任目录，并把文档作为系统知识幂等摄取。"""

    # 初始化系统知识扫描器。
    # Args: store - VectorStore 实例；max_file_bytes - 单文件读取上限。
    # Returns: 无返回值。
    def __init__(self, store: Any, max_file_bytes: int = 20 * 1024 * 1024) -> None:
        logger.debug("初始化系统知识扫描器入口", max_file_bytes=max_file_bytes)
        self._store = store
        self._max_file_bytes = max(1, int(max_file_bytes))
        logger.info("初始化系统知识扫描器完成", max_file_bytes=self._max_file_bytes)

    # 扫描多个只读目录并逐文件增量摄取。
    # Args: directories - 路径列表或分号分隔配置字符串。
    # Returns: 扫描、跳过、写入和错误数量摘要。
    async def scan(
        self,
        directories: str | Iterable[str | Path],
    ) -> SystemKnowledgeScanResult:
        paths = parse_system_knowledge_dirs(directories)
        logger.debug("系统知识目录扫描入口", directory_count=len(paths))
        result = SystemKnowledgeScanResult()
        seen_files: set[str] = set()
        for root in paths:
            if not root.is_dir():
                message = f"系统知识目录不存在: {root}"
                result.error_files += 1
                result.errors.append(message)
                logger.warning("系统知识目录扫描跳过", directory=str(root), reason="目录不存在")
                continue
            for file_path in sorted(root.rglob("*")):
                if not file_path.is_file() or file_path.suffix.lower() not in SUPPORTED_SYSTEM_KNOWLEDGE_EXTENSIONS:
                    continue
                resolved_file = file_path.resolve()
                if not resolved_file.is_relative_to(root):
                    message = f"系统知识文件越出配置目录: {resolved_file}"
                    result.error_files += 1
                    result.errors.append(message)
                    logger.warning(
                        "系统知识文件扫描拒绝",
                        file=str(resolved_file),
                        directory=str(root),
                    )
                    continue
                file_key = str(resolved_file).casefold()
                if file_key in seen_files:
                    continue
                seen_files.add(file_key)
                result.scanned_files += 1
                try:
                    chunks_count, skipped = await self._ingest_file(resolved_file)
                    if skipped:
                        result.skipped_files += 1
                    else:
                        result.ingested_files += 1
                        result.written_chunks += chunks_count
                except Exception as exc:
                    result.error_files += 1
                    result.errors.append(f"{resolved_file}: {exc}")
                    logger.error(
                        "系统知识文件摄取失败",
                        file=str(resolved_file),
                        error=str(exc),
                        exc_info=True,
                    )
        logger.info(
            "系统知识目录扫描完成",
            scanned=result.scanned_files,
            ingested=result.ingested_files,
            skipped=result.skipped_files,
            errors=result.error_files,
            chunks=result.written_chunks,
        )
        return result

    # 按 checksum 判断单文件是否需要重新解析和写入。
    # Args: file_path - 已验证位于配置目录内的文件路径。
    # Returns: `(写入分块数, 是否跳过)` 二元组。
    async def _ingest_file(self, file_path: Path) -> tuple[int, bool]:
        logger.debug("系统知识文件摄取入口", file=str(file_path))
        size = file_path.stat().st_size
        if size > self._max_file_bytes:
            logger.warning(
                "系统知识文件摄取拒绝",
                file=str(file_path),
                size=size,
                max_file_bytes=self._max_file_bytes,
            )
            raise ValueError(f"文件超过大小限制 {self._max_file_bytes} 字节")
        content = file_path.read_bytes()
        checksum = hashlib.sha256(content).hexdigest()
        existing = await self._store.get_by_filter(
            {"visibility": "system", "checksum": checksum},
            limit=1,
        )
        if existing:
            logger.info("系统知识文件摄取跳过", file=str(file_path), checksum=checksum[:12])
            return 0, True

        chunks = self._parse_chunks(file_path, content)
        if not chunks:
            raise ValueError("文档内容为空或无法形成有效分块")
        source_path = str(file_path)
        path_digest = hashlib.sha256(source_path.casefold().encode("utf-8")).hexdigest()[:20]
        entries: list[VectorEntry] = []
        for index, chunk in enumerate(chunks):
            locator = chunk.metadata.get("locator", {})
            if not isinstance(locator, dict):
                locator = {}
            if not locator:
                locator = {
                    key: chunk.metadata[key]
                    for key in ("page", "paragraph", "sheet", "cell_range", "line_start", "line_end")
                    if chunk.metadata.get(key) not in (None, "")
                }
            metadata = {
                "source": "system_directory",
                "source_file": file_path.name,
                "source_path": source_path,
                "category": "system",
                "visibility": "system",
                "tenant_id": 0,
                "owner_user_id": 0,
                "checksum": checksum,
                "parser_version": "v1",
                "document_version": f"sha256:{checksum[:12]}",
                "asset_id": f"system-document:{path_digest}",
                "chunk_index": index,
                "tag_ids_json": "[]",
                "tags": "",
                "locator_json": json.dumps(locator, ensure_ascii=False),
            }
            table_name = chunk.metadata.get("table_name", "")
            if table_name:
                metadata["table_name"] = table_name
            entries.append(VectorEntry(
                id=f"system:{path_digest}:{index}",
                content=chunk.content,
                metadata=metadata,
            ))

        # 仅在新分块准备完成后删除该路径旧版本，缩短无有效版本窗口。
        await self._store.delete_by_filter({
            "visibility": "system",
            "source_path": source_path,
        })
        written = await self._store.upsert(entries)
        logger.info(
            "系统知识文件摄取完成",
            file=source_path,
            checksum=checksum[:12],
            chunks=len(entries),
            written=written,
        )
        return len(entries), False

    # 按文档格式提取带定位信息的分块。
    # Args: file_path - 原始文件路径；content - 原始二进制内容。
    # Returns: DocChunk 列表。
    def _parse_chunks(self, file_path: Path, content: bytes) -> list[Any]:
        logger.debug("解析系统知识文件入口", file=str(file_path), size=len(content))
        config = ChunkConfig(strategy=ChunkStrategy.AUTO)
        if file_path.suffix.lower() in {".pdf", ".docx"}:
            from src.knowledge.document_assets import DocumentAssetAdapter, chunk_document

            asset = DocumentAssetAdapter().parse(file_path.name, content)
            chunks = chunk_document(asset, config)
        else:
            text = extract_text(file_path.name, content)
            chunks = chunk_text(text, config, file_path.name) if text.strip() else []
        logger.info("解析系统知识文件完成", file=str(file_path), chunks=len(chunks))
        return chunks


# 使用应用配置执行一次系统知识目录扫描。
# Args: store - 可选 VectorStore 实例，测试或启动预热时可显式传入。
# Returns: 扫描结果；未配置目录时返回空摘要。
async def scan_configured_system_knowledge(store: Any | None = None) -> SystemKnowledgeScanResult:
    from src.config import get_settings
    from src.memory.vector_store import get_vector_store

    settings = get_settings()
    logger.debug("扫描配置系统知识入口", configured=bool(settings.system_knowledge_dirs.strip()))
    if not settings.system_knowledge_dirs.strip():
        logger.info("扫描配置系统知识跳过", reason="SYSTEM_KNOWLEDGE_DIRS 未配置")
        return SystemKnowledgeScanResult()
    resolved_store = store or await get_vector_store()
    result = await SystemKnowledgeScanner(
        resolved_store,
        max_file_bytes=settings.max_upload_bytes,
    ).scan(settings.system_knowledge_dirs)
    logger.info(
        "扫描配置系统知识完成",
        ingested=result.ingested_files,
        skipped=result.skipped_files,
        errors=result.error_files,
    )
    return result
