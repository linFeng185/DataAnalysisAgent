"""结构化文件摄取前静态检查工具。"""

from __future__ import annotations

import base64
import binascii
import codecs
import csv
import hashlib
from io import StringIO
from typing import Any

from langchain_core.tools import BaseTool

from src.logging_config import get_logger

logger = get_logger(__name__)
_DELIMITERS = [",", "\t", ";", "|"]
_BOMS = [
    (codecs.BOM_UTF8, "utf-8", "utf-8-sig"),
    (codecs.BOM_UTF16_LE, "utf-16-le", "utf-16"),
    (codecs.BOM_UTF16_BE, "utf-16-be", "utf-16"),
]


class InspectFileIngestionTool(BaseTool):
    """检查 Base64 文件样本和调用方声明的归档元数据。"""

    name: str = "inspect_file_ingestion"
    description: str = "检查文件样本的编码、BOM、分隔符、表头、换行、公式前缀和归档风险。"

    def _run(self, *args: Any, **kwargs: Any) -> dict[str, Any]:
        """拒绝同步入口。"""
        del args, kwargs
        raise RuntimeError("inspect_file_ingestion 只允许异步执行")

    async def _arun(
        self,
        filename: str,
        sample_base64: str,
        declared_encoding: str | None = None,
        archive_metadata: dict[str, Any] | None = None,
        max_archive_ratio: float = 100.0,
        max_archive_entries: int = 10000,
        run_manager: Any = None,
    ) -> dict[str, Any]:
        """解码有界样本并返回摄取建议，不访问文件系统。"""
        del run_manager
        if not filename.strip():
            raise ValueError("filename 不能为空")
        if max_archive_ratio <= 1:
            raise ValueError("max_archive_ratio 必须大于 1")
        if max_archive_entries < 1:
            raise ValueError("max_archive_entries 必须大于零")
        try:
            sample = base64.b64decode(sample_base64, validate=True)
        except (binascii.Error, ValueError) as exc:
            raise ValueError("sample_base64 不是有效的 Base64 文本") from exc
        logger.info(
            "文件摄取检查开始",
            filename=filename,
            sample_bytes=len(sample),
            declared_encoding=declared_encoding,
            archive_metadata_present=archive_metadata is not None,
        )
        findings: list[dict[str, Any]] = []

        def add(code: str, severity: str, message: str) -> None:
            findings.append({"code": code, "severity": severity, "message": message})

        encoding, confidence, bom, text = _decode_sample(sample, declared_encoding, add)
        newline = _newline_style(text)
        if newline == "mixed":
            add("MIXED_NEWLINES", "warning", "样本混合使用多种换行符")
        delimiter, delimiter_confidence = _detect_delimiter(text)
        if delimiter is None and text.strip():
            add("DELIMITER_UNCERTAIN", "warning", "无法从样本稳定识别字段分隔符")
        rows = _parse_rows(text, delimiter)
        header = rows[0] if rows else []
        empty_header_columns = [index for index, value in enumerate(header) if not value.strip()]
        normalized_header = [value.strip().casefold() for value in header if value.strip()]
        duplicate_headers = sorted(
            {value for value in normalized_header if normalized_header.count(value) > 1}
        )
        if empty_header_columns:
            add("EMPTY_HEADER", "error", "表头包含空字段名")
        if duplicate_headers:
            add("DUPLICATE_HEADER", "error", "表头包含重复字段名")
        formula_cells = sum(
            isinstance(cell, str) and cell.lstrip().startswith(("=", "+", "-", "@"))
            for row in rows[:100]
            for cell in row
        )
        if formula_cells:
            add("FORMULA_PREFIX", "warning", "样本包含可能触发电子表格公式执行的单元格")
        if "\x00" in text and not encoding.startswith("utf-16"):
            add("NULL_BYTE", "error", "文本样本包含异常空字节")

        archive_result = _inspect_archive(
            archive_metadata,
            max_archive_ratio,
            max_archive_entries,
            add,
        )
        errors = sum(item["severity"] == "error" for item in findings)
        warnings = sum(item["severity"] == "warning" for item in findings)
        result = {
            "safe_to_ingest": errors == 0 and encoding != "unknown",
            "filename": filename,
            "sample_bytes": len(sample),
            "sample_sha256": hashlib.sha256(sample).hexdigest(),
            "encoding": encoding,
            "encoding_confidence": confidence,
            "bom": bom,
            "newline_style": newline,
            "delimiter": delimiter,
            "delimiter_confidence": delimiter_confidence,
            "estimated_rows": len(rows),
            "estimated_columns": max((len(row) for row in rows), default=0),
            "header": {
                "present": bool(header),
                "empty_column_indices": empty_header_columns,
                "duplicate_names": duplicate_headers,
            },
            "formula_prefix_cells": formula_cells,
            "archive": archive_result,
            "summary": {"errors": errors, "warnings": warnings},
            "findings": findings,
            "filesystem_accessed": False,
        }
        logger.info(
            "文件摄取检查完成",
            safe_to_ingest=result["safe_to_ingest"],
            encoding=encoding,
            delimiter=delimiter,
            error_count=errors,
            warning_count=warnings,
        )
        return result


def _decode_sample(
    sample: bytes,
    declared_encoding: str | None,
    add: Any,
) -> tuple[str, str, str, str]:
    """检测 BOM 并按显式或保守候选编码解码样本。"""
    bom_name = "none"
    bom_codec = ""
    for marker, name, codec in _BOMS:
        if sample.startswith(marker):
            bom_name = name
            bom_codec = codec
            break
    if declared_encoding:
        try:
            codec_name = codecs.lookup(declared_encoding).name
            text = sample.decode(declared_encoding, errors="strict")
        except (LookupError, UnicodeDecodeError) as exc:
            raise ValueError("declared_encoding 无效或无法严格解码样本") from exc
        compatible_codecs = {
            "utf-8": {"utf-8", "utf-8-sig"},
            "utf-16-le": {"utf-16", "utf-16-le"},
            "utf-16-be": {"utf-16", "utf-16-be"},
        }
        if bom_codec and codec_name not in compatible_codecs[bom_name]:
            add("BOM_ENCODING_MISMATCH", "error", "声明编码与 BOM 不一致")
        return codec_name, "high", bom_name, text
    if bom_codec:
        return bom_name, "high", bom_name, sample.decode(bom_codec, errors="strict")
    try:
        return "utf-8", "high", bom_name, sample.decode("utf-8", errors="strict")
    except UnicodeDecodeError:
        pass
    try:
        text = sample.decode("gb18030", errors="strict")
        add("ENCODING_INFERRED", "warning", "无 BOM 样本按 GB18030 候选编码解码")
        return "gb18030", "medium", bom_name, text
    except UnicodeDecodeError:
        add("ENCODING_UNKNOWN", "error", "样本无法按 UTF-8 或 GB18030 严格解码")
        return "unknown", "low", bom_name, ""


def _newline_style(text: str) -> str:
    """识别文本换行风格。"""
    crlf = text.count("\r\n")
    remaining = text.replace("\r\n", "")
    lf = remaining.count("\n")
    cr = remaining.count("\r")
    active = sum(count > 0 for count in (crlf, lf, cr))
    if active > 1:
        return "mixed"
    if crlf:
        return "crlf"
    if lf:
        return "lf"
    if cr:
        return "cr"
    return "none"


def _detect_delimiter(text: str) -> tuple[str | None, float]:
    """根据前五十个非空行的字段数一致性识别分隔符。"""
    lines = [line for line in text.splitlines() if line.strip()][:50]
    if not lines:
        return None, 0.0
    candidates: list[tuple[float, int, str]] = []
    for delimiter in _DELIMITERS:
        counts = [len(next(csv.reader([line], delimiter=delimiter))) for line in lines]
        columns = max(set(counts), key=lambda count: (counts.count(count), count))
        if columns <= 1:
            continue
        consistency = counts.count(columns) / len(counts)
        candidates.append((consistency, columns, delimiter))
    if not candidates:
        return None, 0.0
    consistency, _, delimiter = max(
        candidates, key=lambda item: (item[0], item[1], -_DELIMITERS.index(item[2]))
    )
    return delimiter, round(consistency, 6)


def _parse_rows(text: str, delimiter: str | None) -> list[list[str]]:
    """解析有界文本样本用于表头和公式检查。"""
    if not text:
        return []
    if delimiter is None:
        return [[line] for line in text.splitlines()[:100]]
    return list(csv.reader(StringIO(text), delimiter=delimiter))[:100]


def _inspect_archive(
    metadata: dict[str, Any] | None,
    max_ratio: float,
    max_entries: int,
    add: Any,
) -> dict[str, Any]:
    """只根据调用方声明的归档元数据评估风险。"""
    if metadata is None:
        return {
            "provided": False,
            "compression_ratio": None,
            "entry_count": None,
            "encrypted": False,
            "contains_macros": False,
            "external_links": False,
        }
    if not isinstance(metadata, dict):
        raise ValueError("archive_metadata 必须是对象或空值")
    compressed = metadata.get("compressed_bytes", 0)
    uncompressed = metadata.get("uncompressed_bytes", 0)
    entries = metadata.get("entry_count", 0)
    if any(
        not isinstance(value, int) or isinstance(value, bool) or value < 0
        for value in (compressed, uncompressed, entries)
    ):
        raise ValueError("归档字节数和条目数必须是非负整数")
    ratio = uncompressed / compressed if compressed else 0.0
    encrypted = bool(metadata.get("encrypted", False))
    macros = bool(metadata.get("contains_macros", False))
    external_links = bool(metadata.get("external_links", False))
    if ratio > max_ratio:
        add("ARCHIVE_RATIO_EXCEEDED", "error", "归档解压比超过安全上限")
    if entries > max_entries:
        add("ARCHIVE_ENTRIES_EXCEEDED", "error", "归档条目数超过安全上限")
    if encrypted:
        add("ARCHIVE_ENCRYPTED", "error", "加密归档无法在摄取前检查内部内容")
    if macros:
        add("MACROS_PRESENT", "error", "归档声明包含宏，禁止执行")
    if external_links:
        add("EXTERNAL_LINKS_PRESENT", "warning", "归档声明包含外部链接，禁止自动刷新")
    return {
        "provided": True,
        "compression_ratio": round(ratio, 6),
        "entry_count": entries,
        "encrypted": encrypted,
        "contains_macros": macros,
        "external_links": external_links,
    }


_TOOLS: dict[str, BaseTool] = {"inspect_file_ingestion": InspectFileIngestionTool()}


def get_tool(name: str) -> BaseTool | None:
    """按名称返回已注册工具。"""
    return _TOOLS.get(name)


def get_tools() -> list[BaseTool]:
    """返回该 Skill 暴露的全部工具。"""
    return list(_TOOLS.values())
