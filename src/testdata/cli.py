"""大规模造数命令入口，默认只生成离线文件。"""

import argparse
import asyncio
import os
from datetime import datetime
from pathlib import Path

from dotenv import load_dotenv

from src.connectors.seed_verifier import verify_database_dataset
from src.connectors.seed_writer import import_dataset, validate_baseline, validate_targets
from src.datasource.config import DataSourceConfig
from src.logging_config import get_logger, setup_logging
from src.testdata.catalog import SOURCES
from src.testdata.exporter import export_dataset, verify_export, write_json
from src.testdata.generator import GenerationConfig

logger = get_logger(__name__)
PORTS = {"mysql": 3306, "postgres": 5432, "mssql": 1433, "oracle": 1521, "clickhouse": 8123}


# 读取显式测试连接环境变量，绝不继承旧服务器或系统库默认值。
# Args: sources - 业务域列表。
# Returns: 数据源配置映射。
def _targets(sources: list[str]) -> dict[str, DataSourceConfig]:
    targets = {}
    for source in sources:
        dialect = SOURCES[source][0]
        prefix = f"TESTDATA_{dialect.upper()}_"
        values = {
            key: os.environ.get(prefix + key, "").strip()
            for key in ["HOST", "DATABASE", "USER", "PASSWORD"]
        }
        missing = [prefix + key for key, value in values.items() if not value]
        if missing:
            raise ValueError("缺少显式测试连接配置: " + ", ".join(missing))
        targets[source] = DataSourceConfig(
            name=source,
            dialect=dialect,
            mode="external",
            host=values["HOST"],
            port=int(os.environ.get(prefix + "PORT", PORTS[dialect])),
            database=values["DATABASE"],
            username=values["USER"],
            password=values["PASSWORD"],
        )
    return targets


# 解析兼容原脚本的主要参数，并新增显式入库保护。
# Args: 无。
# Returns: 参数解析器。
def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="中文五业务域测试数据：每源 125 张专属表，共 625 张，每源默认两百万行，前后十年。"
    )
    parser.add_argument(
        "--db", default="all", choices=["all", *PORTS], help="数据库引擎，对应不同业务域"
    )
    parser.add_argument(
        "--scale", type=int, default=1_000_000, help="每源基础池和扩展池各自目标行数，默认各一百万"
    )
    parser.add_argument(
        "--anchor-year", type=int, default=datetime.now().year, help="模拟窗口中心年"
    )
    parser.add_argument("--years-before", type=int, default=10, help="向前覆盖年数")
    parser.add_argument("--years-after", type=int, default=10, help="向后覆盖年数")
    parser.add_argument("--seed", type=int, default=42, help="固定随机种子")
    parser.add_argument("--batch-size", type=int, default=5000, help="每批最多行数")
    parser.add_argument(
        "--format", default="csv", choices=["csv", "jsonl", "sql"], help="压缩数据文件格式"
    )
    parser.add_argument("--output-dir", type=Path, help="新的产物目录；已有目录拒绝覆盖")
    parser.add_argument("--file-only", action="store_true", help="兼容旧参数，默认已经是仅导出")
    parser.add_argument("--ddl-only", action="store_true", help="只生成表结构和全部中文知识资料")
    parser.add_argument(
        "--verify-only", action="store_true", help="独立核验已生成文件和知识资料的校验和"
    )
    parser.add_argument(
        "--import-db", action="store_true", help="生成完成后导入显式配置的专用测试数据库"
    )
    parser.add_argument(
        "--confirm-test-database", action="store_true", help="确认目标为允许写入的专用测试库"
    )
    parser.add_argument(
        "--extend-from", type=Path, help="旧完整产物目录；核验旧表后只导入新增表，必须显式入库"
    )
    return parser


# 执行配置校验、离线生成和可选显式入库。
# Args: args - 已解析参数。
# Returns: 无返回值。
async def _run(args: argparse.Namespace) -> None:
    if args.extend_from is not None and (not args.import_db or args.verify_only):
        raise ValueError("--extend-from 仅允许与显式 --import-db 同用")
    if args.verify_only:
        if args.output_dir is None or args.import_db or args.ddl_only:
            raise ValueError("--verify-only 必须指定 --output-dir，且不能同时导入或生成结构")
        await verify_export(args.output_dir)
        return
    if args.import_db and (args.file_only or args.ddl_only or not args.confirm_test_database):
        raise ValueError("入库须明确 --confirm-test-database，且不可与 --file-only/--ddl-only 同用")
    config = GenerationConfig(
        scale=args.scale,
        anchor_year=args.anchor_year,
        years_before=args.years_before,
        years_after=args.years_after,
        seed=args.seed,
        batch_size=args.batch_size,
    )
    sources = [source for source, (dialect, _) in SOURCES.items() if args.db in {"all", dialect}]
    targets = _targets(sources) if args.import_db else {}
    if targets:
        validate_targets(targets, args.confirm_test_database)
    baseline = None
    if args.extend_from is not None:
        baseline = await verify_export(args.extend_from)
        validate_baseline(config, baseline)
    output = (
        args.output_dir or Path("data/generated") / f"multidomain_{datetime.now():%Y%m%d_%H%M%S_%f}"
    )
    await export_dataset(config, output, sources=sources, fmt=args.format, ddl_only=args.ddl_only)
    manifest = await verify_export(output)
    if targets:
        report = await import_dataset(
            config, targets, confirm_test_database=True, baseline=baseline
        )
        await asyncio.to_thread(write_json, output / "import_report.json", report)
        verified = await verify_database_dataset(targets, manifest)
        await asyncio.to_thread(write_json, output / "database_verification.json", verified)
    logger.info(
        "测试数据任务完成",
        output=str(output),
        imported=bool(targets),
        knowledge_manifest="knowledge_upload_manifest.json",
    )


# 提供可测试的退出码边界，所有错误均记录结构化日志。
# Args: argv - 可选命令参数列表。
# Returns: 成功为 0，失败为 1。
def main(argv: list[str] | None = None) -> int:
    """命令行入口，默认离线导出，不连接真实模型或数据库。"""
    args = _parser().parse_args(argv)
    load_dotenv()
    setup_logging()
    logger.debug("造数命令入口", database=args.db, scale=args.scale)
    try:
        asyncio.run(_run(args))
    except ValueError, OSError:
        logger.error("造数命令参数或文件操作失败", exc_info=True)
        return 1
    except Exception:
        logger.error("造数命令执行失败", exc_info=True)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
