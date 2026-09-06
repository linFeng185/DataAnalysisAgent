"""流式导出、完整性清单和独立文件校验。"""

import asyncio
import csv
import gzip
import hashlib
import json
from datetime import date
from decimal import Decimal
from pathlib import Path
from typing import Any

from src.logging_config import get_logger
from src.testdata.catalog import SOURCES, build_catalog
from src.testdata.generator import GenerationConfig, iter_batches, row_counts
from src.testdata.sql import build_ddl, insert_sql, schema_comments

logger = get_logger(__name__)


# 将日期与精确金额转换为 JSON 可读表示而不经过浮点。
# Args: value - 待序列化值。
# Returns: 字符串表示。
def _json_value(value: Any) -> str:
    if isinstance(value, (date, Decimal)):
        return str(value)
    raise TypeError(f"不支持序列化类型: {type(value).__name__}")


# 以固定大小读取文件，校验大文件时不加载全量内容。
# Args: path - 目标文件。
# Returns: SHA256 十六进制摘要。
def file_digest(path: Path) -> str:
    """返回文件内容摘要。"""
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        while chunk := stream.read(1024 * 1024):
            digest.update(chunk)
    return digest.hexdigest()


# 统一写入带中文的结构化小型元数据文件。
# Args: path - 文件路径；value - JSON 数据。
# Returns: 无返回值。
def write_json(path: Path, value: Any) -> None:
    """写入不包含真实凭证的测试元数据。"""
    logger.debug("写入造数元数据", file=path.name)
    path.write_text(
        json.dumps(value, ensure_ascii=False, indent=2, default=_json_value) + "\n",
        encoding="utf-8",
    )


# 在工作线程中流式导出选定业务域，最后原子发布成功清单。
# Args: config - 生成配置；output - 新目录；sources - 选定域；fmt - 格式；ddl_only - 仅结构。
# Returns: 完整产物清单。
def _export(
    config: GenerationConfig, output: Path, sources: list[str], fmt: str, ddl_only: bool
) -> dict[str, Any]:
    from src.testdata.knowledge import generate_knowledge, relationship_manifest

    output.mkdir(parents=True, exist_ok=False)
    catalog = build_catalog()
    counts = row_counts(config)
    selected = {name: table for name, table in catalog.items() if table.source in sources}
    manifest: dict[str, Any] = {
        "version": 1,
        "status": "ddl_only" if ddl_only else "complete",
        "config": config.model_dump(),
        "sources": sources,
        "format": fmt,
        "start_date": config.start_date.isoformat(),
        "end_date": config.end_date.isoformat(),
        "future_data_notice": "未来数据为合成情景，不能当作真实观测或可靠预测。",
        "csv_null": "\\N",
        "tables": [],
        "total_rows": 0,
        "artifacts": [],
        "external_dependencies": sorted(
            {
                column.reference
                for table in selected.values()
                for column in table.columns
                if column.reference and column.reference not in selected
            }
        ),
    }
    for source in sources:
        directory = output / source
        directory.mkdir()
        dialect = SOURCES[source][0]
        ddl_path = directory / "schema.sql"
        ddl_path.write_text(
            "\n\n".join(
                f"-- {table.description}\n{build_ddl(table, dialect)}\n"
                + ";\n".join(schema_comments(table, dialect))
                + ";\n"
                for table in selected.values()
                if table.source == source
            ),
            encoding="utf-8",
        )
        manifest["artifacts"].append(
            {"file": ddl_path.relative_to(output).as_posix(), "sha256": file_digest(ddl_path)}
        )
    for name, table in selected.items():
        logger.info("开始导出业务表", table=name, target_rows=counts[name], source=table.source)
        summary: dict[str, Any] = {
            "name": name,
            "source": table.source,
            "description": table.description,
            "target_rows": counts[name],
            "rows": 0,
            "null_counts": {},
            "sums": {},
        }
        if not ddl_only:
            path = output / table.source / f"{name}.{fmt}.gz"
            temporary = path.with_suffix(path.suffix + ".part")
            with (
                temporary.open("wb") as raw,
                gzip.GzipFile(filename="", mode="wb", fileobj=raw, mtime=0) as compressed,
            ):
                import io

                with io.TextIOWrapper(compressed, encoding="utf-8", newline="") as stream:
                    writer = (
                        csv.DictWriter(stream, fieldnames=[column.name for column in table.columns])
                        if fmt == "csv"
                        else None
                    )
                    if writer:
                        writer.writeheader()
                    for batch in iter_batches(table, config):
                        for row in batch:
                            if writer:
                                writer.writerow(
                                    {
                                        key: "\\N" if value is None else value
                                        for key, value in row.items()
                                    }
                                )
                            elif fmt == "jsonl":
                                stream.write(
                                    json.dumps(row, ensure_ascii=False, default=_json_value) + "\n"
                                )
                            else:
                                stream.write(insert_sql(name, row, SOURCES[table.source][0]) + "\n")
                            summary["rows"] += 1
                            event_date = row["event_date"].isoformat()
                            summary["min_date"] = min(
                                summary.get("min_date", event_date), event_date
                            )
                            summary["max_date"] = max(
                                summary.get("max_date", event_date), event_date
                            )
                            for column, value in row.items():
                                if value is None:
                                    summary["null_counts"][column] = (
                                        summary["null_counts"].get(column, 0) + 1
                                    )
                                elif isinstance(value, Decimal):
                                    summary["sums"][column] = (
                                        summary["sums"].get(column, Decimal(0)) + value
                                    )
            temporary.replace(path)
            summary.update(file=path.relative_to(output).as_posix(), sha256=file_digest(path))
            summary["sums"] = {key: str(value) for key, value in summary["sums"].items()}
        manifest["tables"].append(summary)
        manifest["total_rows"] += summary["rows"]
        logger.info("业务表导出完成", table=name, rows=summary["rows"])
    write_json(output / "relationships.json", relationship_manifest())
    documents, questions = generate_knowledge(output, config, manifest)
    write_json(output / "questions.json", questions)
    write_json(output / "knowledge_upload_manifest.json", documents)
    for artifact in ["relationships.json", "questions.json", "knowledge_upload_manifest.json"]:
        manifest["artifacts"].append({"file": artifact, "sha256": file_digest(output / artifact)})
    manifest["artifacts"].extend(
        {"file": document["file"], "sha256": document["sha256"]} for document in documents
    )
    write_json(output / "manifest.json.part", manifest)
    (output / "manifest.json.part").replace(output / "manifest.json")
    return manifest


# 在异步边界验证参数、记录聚合日志并运行文件生成。
# Args: config - 配置；output - 新输出目录；sources - 可选业务域；fmt - 格式；ddl_only - 仅生成结构。
# Returns: 产物清单。
async def export_dataset(
    config: GenerationConfig,
    output: Path,
    sources: list[str] | None = None,
    fmt: str = "csv",
    ddl_only: bool = False,
) -> dict[str, Any]:
    """默认离线导出，不连接或覆盖任何数据库。"""
    selected = list(SOURCES) if sources is None else list(dict.fromkeys(sources))
    if not selected or set(selected) - SOURCES.keys() or fmt not in {"csv", "jsonl", "sql"}:
        raise ValueError("数据源或导出格式无效")
    logger.debug("数据集导出入口", sources=selected, scale=config.scale, output=str(output))
    try:
        manifest = await asyncio.to_thread(_export, config, Path(output), selected, fmt, ddl_only)
    except Exception:
        logger.error("数据集导出失败，未发布成功清单", exc_info=True)
        raise
    logger.info("数据集导出完成", tables=len(manifest["tables"]), rows=manifest["total_rows"])
    return manifest


# 独立检查成功清单与所有文件内容，阻止目录穿越。
# Args: output - 产物目录。
# Returns: 已通过验证的清单。
def _verify(output: Path) -> dict[str, Any]:
    root = output.resolve()
    manifest = json.loads((root / "manifest.json").read_text(encoding="utf-8"))
    if manifest["status"] not in {"complete", "ddl_only"}:
        raise ValueError("数据集未成功生成")
    for item in [*manifest["tables"], *manifest["artifacts"]]:
        if "file" not in item:
            continue
        path = (root / item["file"]).resolve()
        if not path.is_relative_to(root):
            raise ValueError("产物路径越界")
        if file_digest(path) != item["sha256"]:
            raise ValueError(f"文件校验失败: {item['file']}")
    if sum(table["rows"] for table in manifest["tables"]) != manifest["total_rows"]:
        raise ValueError("行数清单校验失败")
    return manifest


# 在线程中执行磁盘校验，避免阻塞调用方事件循环。
# Args: output - 已生成目录。
# Returns: 已验证清单。
async def verify_export(output: Path) -> dict[str, Any]:
    """校验数据文件、知识资料及目录清单的完整性。"""
    logger.debug("数据集校验入口", output=str(output))
    try:
        result = await asyncio.to_thread(_verify, Path(output))
    except Exception:
        logger.error("数据集校验失败", exc_info=True)
        raise
    logger.info("数据集校验完成", rows=result["total_rows"], tables=len(result["tables"]))
    return result
