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
    FIXED = "fixed"          # 固定大小滑动窗口
    PARAGRAPH = "paragraph"  # 按空行分段
    HEADING = "heading"      # 按 Markdown 标题切
    TABLE = "table"          # 按 ### 表: 或 CREATE TABLE 切，每块一张完整表定义
    SQL_DDL = "sql_ddl"      # 按 SQL DDL 语句切（CREATE TABLE/VIEW/INDEX）
    REFERENCE = "reference"  # 按函数/语法条目切（官方文档密集函数签名时使用）
    AUTO = "auto"            # 自动检测最佳策略


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
    if ext in (".txt", ".md", ".markdown", ".csv"):
        return content.decode("utf-8", errors="replace")
    elif ext == ".pdf":
        return _extract_pdf(content)
    elif ext in (".docx", ".doc"):
        return _extract_docx(content)
    raise ValueError(f"不支持的文件格式: {ext}")


def chunk_text(text: str, config: ChunkConfig, file_name: str = "") -> list[DocChunk]:
    strategy = config.strategy
    if strategy == ChunkStrategy.AUTO:
        strategy = _auto_detect(text, file_name)
    logger.info("文档分块", file=file_name, strategy=strategy.value,
                text_len=len(text), chunk_size=config.chunk_size)
    if strategy == ChunkStrategy.HEADING:
        chunks = _chunk_by_heading(text, config)
    elif strategy == ChunkStrategy.PARAGRAPH:
        chunks = _chunk_by_paragraph(text, config)
    elif strategy == ChunkStrategy.TABLE:
        chunks = _chunk_by_table(text, config)
    elif strategy == ChunkStrategy.SQL_DDL:
        chunks = _chunk_by_sql_ddl(text, config)
    elif strategy == ChunkStrategy.REFERENCE:
        chunks = _chunk_by_reference(text, config)
    else:
        chunks = _chunk_by_line(text, config)

    # 每块附加元数据，尝试提取表名
    enriched: list[DocChunk] = []
    for c in chunks:
        table_name = _extract_table_name(c)
        meta = {
            "source_file": file_name,
            "strategy": strategy.value,
            "chunk_size": len(c),
        }
        if table_name:
            meta["table_name"] = table_name
        enriched.append(DocChunk(content=c, metadata=meta))
    return enriched


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


def _auto_detect(text: str, file_name: str = "") -> ChunkStrategy:
    """自动检测最佳分块策略。

    优先级: CSV > DDL > REFERENCE > table > heading > paragraph > line(原fixed)
    """
    # CSV 文件按行切，避免固定字节切分行内数据
    if file_name.lower().endswith(".csv"):
        return ChunkStrategy.FIXED  # FIXED 策略现在映射到 _chunk_by_line
    # 1) DDL: 多行 CREATE TABLE/VIEW/INDEX
    create_count = len(re.findall(r'\bCREATE\s+(TABLE|VIEW|INDEX)\b', text, re.IGNORECASE))
    if create_count >= 2:
        return ChunkStrategy.SQL_DDL

    # 2) REFERENCE: 密集的行内代码标记或函数签名（官方文档特征）
    code_spans = len(re.findall(r'`[^`]+`', text))
    func_sigs = len(re.findall(r'\*\*\w+\([^)]*\)\*\*', text))
    if code_spans >= 15 or func_sigs >= 5:
        return ChunkStrategy.REFERENCE

    # 3) TABLE: 多个 '### 表:' 或 '### 表名 —'
    table_marks = len(re.findall(r'^###\s+表[:\s]', text, re.MULTILINE))
    if table_marks >= 2:
        return ChunkStrategy.TABLE

    # 4) HEADING: 多个 Markdown 标题
    headings = len(re.findall(r"^#{1,4}\s", text, re.MULTILINE))
    if headings >= 3:
        return ChunkStrategy.HEADING

    if len(text) > 2000 and "\n\n" in text:
        return ChunkStrategy.PARAGRAPH
    return ChunkStrategy.FIXED


def _chunk_by_table(text: str, cfg: ChunkConfig) -> list[str]:
    """按 '### 表: 表名' 或 '### 表名 —' 标记分块，每块一张完整表定义。"""
    # 分割点：### 开头的行后面跟着表名
    sections = re.split(r'\n(?=###\s)', text)
    chunks: list[str] = []
    current = ""
    for sec in sections:
        sec = sec.strip()
        if not sec:
            continue
        if re.match(r'###\s', sec):
            # 新表开始
            if len(current) >= cfg.min_chunk_size:
                chunks.append(current)
            current = sec
        else:
            current = f"{current}\n\n{sec}" if current else sec
    if len(current) >= cfg.min_chunk_size:
        chunks.append(current)
    # 超大块再切（如单表有几十列）
    result: list[str] = []
    for c in chunks:
        if len(c) > cfg.chunk_size * 3:
            result.extend(_chunk_by_paragraph(c, cfg))
        else:
            result.append(c)
    return [r for r in result if len(r) >= cfg.min_chunk_size]


def _chunk_by_sql_ddl(text: str, cfg: ChunkConfig) -> list[str]:
    """按 SQL DDL 语句切（CREATE TABLE/VIEW/INDEX），每条 DDL + 注释作为一个块。"""
    # 在 CREATE 前分割，但保留前面的注释行
    parts = re.split(r'\n(?=CREATE\s+(TABLE|VIEW|INDEX)\b)', text, flags=re.IGNORECASE)
    chunks: list[str] = []
    # 合并前置注释到紧跟的 DDL
    current = ""
    for part in parts:
        part = part.strip()
        if not part:
            continue
        if re.match(r'CREATE\s+(TABLE|VIEW|INDEX)\b', part, re.IGNORECASE):
            if len(current) >= cfg.min_chunk_size:
                chunks.append(current)
            current = part
        elif re.match(r'^\s*--', part):
            # 注释行追加到当前块
            current = f"{current}\n{part}" if current else part
        else:
            current = f"{current}\n\n{part}" if current else part
    if len(current) >= cfg.min_chunk_size:
        chunks.append(current)
    return [c for c in chunks if len(c) >= cfg.min_chunk_size]


def _chunk_by_reference(text: str, cfg: ChunkConfig) -> list[str]:
    """按函数/语法条目切（官方文档模式）。

    分割点优先级: #### 标题 > **函数名()** 粗体签名 > 空行段落。
    每块包含一个完整条目的说明。
    """
    # 先尝试按 #### 标题切（大多数官方文档的函数条目级别）
    if re.search(r'\n####\s', text):
        sections = re.split(r'\n(?=####\s)', text)
    elif re.search(r'\n\*\*\w+\(', text):
        # 按 **函数名()** 切
        sections = re.split(r'\n(?=\*\*\w+\([^)]*\)\*\*)', text)
    else:
        sections = [text]

    chunks: list[str] = []
    current = ""
    for sec in sections:
        sec = sec.strip()
        if not sec:
            continue
        # 新条目开始
        if re.match(r'^(####\s|\*\*\w+\()', sec):
            if len(current) >= cfg.min_chunk_size:
                chunks.append(current)
            current = sec
        else:
            combined = f"{current}\n\n{sec}" if current else sec
            if len(combined) > cfg.chunk_size * 2:
                # 太大 —— 截断当前块
                if len(current) >= cfg.min_chunk_size:
                    chunks.append(current)
                current = sec
            else:
                current = combined
    if len(current) >= cfg.min_chunk_size:
        chunks.append(current)
    return [c for c in chunks if len(c) >= cfg.min_chunk_size]


def _extract_table_name(chunk: str) -> str:
    """从 chunk 内容中提取表名。"""
    # 匹配 ### 表: orders — xxx
    m = re.search(r'###\s+表[:\s]*(\w+)', chunk)
    if m:
        return m.group(1)
    # 匹配 CREATE TABLE orders (
    m = re.search(r'CREATE\s+TABLE\s+(?:IF\s+NOT\s+EXISTS\s+)?(\w+)', chunk, re.IGNORECASE)
    if m:
        return m.group(1)
    return ""


def _chunk_fixed(text: str, cfg: ChunkConfig) -> list[str]:
    """旧固定字节切分，已废弃，保留兼容。"""
    return _chunk_by_line(text, cfg)


def _chunk_by_line(text: str, cfg: ChunkConfig) -> list[str]:
    """按行切分 — 在 chunk_size 附近找最近的换行符切分，保证每行完整。

    用于 CSV 等无空行结构的文本，替代固定字节切分。
    """
    lines = text.split("\n")
    chunks: list[str] = []
    current = ""
    for line in lines:
        line = line.strip()
        if not line:
            continue
        if len(current) + len(line) + 1 <= cfg.chunk_size:
            current = f"{current}\n{line}" if current else line
        else:
            if len(current) >= cfg.min_chunk_size:
                chunks.append(current)
            # 超长单行强制截断
            if len(line) > cfg.chunk_size:
                for i in range(0, len(line), cfg.chunk_size - cfg.chunk_overlap):
                    piece = line[i:i + cfg.chunk_size].strip()
                    if len(piece) >= cfg.min_chunk_size:
                        chunks.append(piece)
                current = ""
            else:
                current = line
    if len(current) >= cfg.min_chunk_size:
        chunks.append(current)
    return chunks


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
