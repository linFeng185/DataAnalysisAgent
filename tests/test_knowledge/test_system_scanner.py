"""系统知识目录扫描测试，覆盖 6.9.6。"""

from __future__ import annotations

import logging
from types import SimpleNamespace
from unittest.mock import AsyncMock

logger = logging.getLogger(__name__)


class TestSystemKnowledgeScanner:
    """验证多目录解析、系统 metadata 和 checksum 幂等摄取。"""

    # 验证 Windows 与 Linux 目录配置使用分号分隔且去重。
    # Args: self - pytest 测试类实例；tmp_path - pytest 临时目录。
    # Returns: 无返回值，断言失败时由 pytest 报告。
    def test_parse_directories_deduplicates_paths(self, tmp_path) -> None:
        """重复目录不能导致同一批文件扫描两次。"""
        logger.debug("test_parse_directories_deduplicates_paths 入口")
        try:
            # Arrange：构造重复目录和空配置片段。
            from src.knowledge.system_scanner import parse_system_knowledge_dirs

            value = f"{tmp_path};;{tmp_path}"

            # Act：解析目录配置。
            result = parse_system_knowledge_dirs(value)

            # Assert：路径已解析为绝对路径并去重。
            assert result == [tmp_path.resolve()]
            logger.info("test_parse_directories_deduplicates_paths 完成")
        except Exception as exc:
            logger.error(
                "test_parse_directories_deduplicates_paths 异常: %s", exc, exc_info=True,
            )
            raise

    # 验证系统目录文件首次写入、二次按 checksum 跳过。
    # Args: self - pytest 测试类实例；tmp_path - pytest 临时目录。
    # Returns: 无返回值，断言失败时由 pytest 报告。
    async def test_scan_is_idempotent_by_checksum(self, tmp_path) -> None:
        """服务重启扫描相同文件时不得重复生成向量。"""
        logger.debug("test_scan_is_idempotent_by_checksum 入口")
        try:
            # Arrange：创建可分块文档，并让第二次 checksum 查询命中。
            from src.knowledge.system_scanner import SystemKnowledgeScanner
            from src.memory.vector_store import VectorEntry

            document = tmp_path / "sql-guide.md"
            document.write_text("# SQL 指南\n\n" + "系统通用分析方法。" * 30, encoding="utf-8")
            store = SimpleNamespace(
                get_by_filter=AsyncMock(side_effect=[
                    [],
                    [VectorEntry(id="existing", content="已存在", metadata={})],
                ]),
                delete_by_filter=AsyncMock(return_value=0),
                upsert=AsyncMock(return_value=1),
            )
            scanner = SystemKnowledgeScanner(store)

            # Act：连续扫描相同目录两次。
            first = await scanner.scan([tmp_path])
            second = await scanner.scan([tmp_path])

            # Assert：首次摄取一次，第二次跳过且 metadata 固定为系统范围。
            assert first.ingested_files == 1
            assert second.skipped_files == 1
            store.upsert.assert_awaited_once()
            entry = store.upsert.await_args.args[0][0]
            assert entry.id.startswith("system:")
            assert entry.metadata["visibility"] == "system"
            assert entry.metadata["tenant_id"] == 0
            assert entry.metadata["owner_user_id"] == 0
            assert len(entry.metadata["checksum"]) == 64
            logger.info("test_scan_is_idempotent_by_checksum 完成")
        except Exception as exc:
            logger.error("test_scan_is_idempotent_by_checksum 异常: %s", exc, exc_info=True)
            raise

    # 验证不存在目录被记录但不会终止其他目录扫描。
    # Args: self - pytest 测试类实例；tmp_path - pytest 临时目录。
    # Returns: 无返回值，断言失败时由 pytest 报告。
    async def test_missing_directory_is_reported(self, tmp_path) -> None:
        """配置错误应可观测，启动流程仍可继续。"""
        logger.debug("test_missing_directory_is_reported 入口")
        try:
            # Arrange：构造空向量存储和不存在目录。
            from src.knowledge.system_scanner import SystemKnowledgeScanner

            store = SimpleNamespace(
                get_by_filter=AsyncMock(), delete_by_filter=AsyncMock(), upsert=AsyncMock(),
            )
            scanner = SystemKnowledgeScanner(store)

            # Act：扫描不存在目录。
            result = await scanner.scan([tmp_path / "missing"])

            # Assert：错误计数正确且没有发生向量写入。
            assert result.error_files == 1
            assert result.errors
            store.upsert.assert_not_awaited()
            logger.info("test_missing_directory_is_reported 完成")
        except Exception as exc:
            logger.error("test_missing_directory_is_reported 异常: %s", exc, exc_info=True)
            raise
