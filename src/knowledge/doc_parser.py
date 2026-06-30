"""文档解析与分块引擎 — Word / PDF / Text / Markdown 提取 + 智能分块。"""

from __future__ import annotations

import io
import os
import re
from dataclasses import dataclass, field
from enum import Enum
from uuid import uuid4

from src.logging_config import get_logger

logger = get_logger(__name__)


class ChunkStrategy(str, Enum):
    FIXED = "fixed"
    PARAGRAPH = "paragraph"
    HEADING = "heading"
    AUTO = "auto"


@dataclass
class ChunkConfig:
    strategy: ChunkStrategy = ChunkStrategy.AUTO
    chunk_size: int = 800
    chunk_overlap: int = 100
    min_chunk_size: int = 50

    def to_dict(self) -> dict:
        return {
            "strategy": self.strategy.value,
            "chunk_size": self.chunk_size,
            "chunk_overlap": self.chunk_overlap,
            "min_chunk_size": self.min_chunk_size,
        }


@dataclass
class DocChunk:
    id: str = field(default_factory=lambda: str(uuid4())[:12])
    content: str = ""
    metadata: dict = field(default_factory=dict)


def extract_text(file_name: str, content: bytes) -> str:
    ext = os.path.splitext(file_name)[1].lower()
    if ext in (".txt", ".md", ".markdown"):
        return content.decode("utf-8", errors="replace")
    elif ext == ".pdf":
        return _extract_pdf(content)
    elif ext in (".docx", ".doc"):
        return _extract_docx(content)
    raise ValueError(f"不支持的文件格式: {ext}")


def chunk_text(text: str, config: ChunkConfig, file_name: str = "") -> list[DocChunk]:
    strategy = config.strategy
    if strategy == ChunkStrategy.AUTO:
        strategy = _auto_detect(text)
    logger.info("文档分块", file=file_name, strategy=strategy.value,
                text_len=len(text), chunk_size=config.chunk_size)
    if strategy == ChunkStrategy.HEADING:
        chunks = _chunk_by_heading(text, config)
    elif strategy == ChunkStrategy.PARAGRAPH:
        chunks = _chunk_by_paragraph(text, config)
    else:
        chunks = _chunk_fixed(text, config)
    return [DocChunk(content=c, metadata={
        "source_file": file_name, "strategy": strategy.value, "chunk_size": len(c),
    }) for c in chunks]


# ── 内部实现 ──

def _extract_pdf(content: bytes) -> str:
    from PyPDF2 import PdfReader
    parts: list[str] = []
    for page in PdfReader(io.BytesIO(content)).pages:
        t = page.extract_text()
        if t:
            parts.append(t)
    return "\n\n".join(parts)


def _extract_docx(content: bytes) -> str:
    from docx import Document
    parts: list[str] = []
    for para in Document(io.BytesIO(content)).paragraphs:
        if not para.text.strip():
            continue
        if para.style and para.style.name and para.style.name.startswith("Heading"):
            parts.append(f"## {para.text.strip()}")
        else:
            parts.append(para.text.strip())
    return "\n\n".join(parts)


def _auto_detect(text: str) -> ChunkStrategy:
    headings = len(re.findall(r"^#{1,4}\s", text, re.MULTILINE))
    if headings >= 3:
        return ChunkStrategy.HEADING
    if len(text) > 2000 and "\n\n" in text:
        return ChunkStrategy.PARAGRAPH
    return ChunkStrategy.FIXED


def _chunk_fixed(text: str, cfg: ChunkConfig) -> list[str]:
    chunks: list[str] = []
    start = 0
    while start < len(text):
        end = min(start + cfg.chunk_size, len(text))
        chunks.append(text[start:end].strip())
        start += cfg.chunk_size - cfg.chunk_overlap
    return [c for c in chunks if len(c) >= cfg.min_chunk_size]


def _chunk_by_paragraph(text: str, cfg: ChunkConfig) -> list[str]:
    paragraphs = re.split(r"\n\s*\n", text)
    chunks: list[str] = []
    current = ""
    for p in paragraphs:
        p = p.strip()
        if not p:
            continue
        if len(current) + len(p) + 2 <= cfg.chunk_size:
            current = f"{current}\n\n{p}" if current else p
        else:
            if len(current) >= cfg.min_chunk_size:
                chunks.append(current)
            current = p
    if len(current) >= cfg.min_chunk_size:
        chunks.append(current)
    return chunks


def _chunk_by_heading(text: str, cfg: ChunkConfig) -> list[str]:
    sections = re.split(r"\n(?=#{1,4}\s)", text)
    chunks: list[str] = []
    current = ""
    for sec in sections:
        sec = sec.strip()
        if not sec:
            continue
        if len(current) + len(sec) + 2 <= cfg.chunk_size:
            current = f"{current}\n\n{sec}" if current else sec
        else:
            if len(current) >= cfg.min_chunk_size:
                chunks.append(current)
            if len(sec) > cfg.chunk_size:
                sub = _chunk_by_paragraph(sec, cfg)
                chunks.extend(sub[:-1])
                current = sub[-1] if sub else ""
            else:
                current = sec
    if len(current) >= cfg.min_chunk_size:
        chunks.append(current)
    return chunks
