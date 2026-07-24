"""结构化文件 SQL 执行层测试。"""

from __future__ import annotations

import logging
import threading

import pytest


logger = logging.getLogger(__name__)


class TestStructuredQueryEngine:
    """覆盖只读 SQL、未知表和 DuckDB 结果资源上限。"""

    def test_validate_sql_rejects_write_and_unknown_table(self):
        """结构化查询只允许单条 SELECT，禁止写操作和未注册表。"""
        # Arrange
        from src.knowledge.structured_query import StructuredQueryEngine, StructuredQueryError

        engine = StructuredQueryEngine()

        # Act / Assert
        assert engine.validate_sql("SELECT * FROM data", {"data"}) == "SELECT * FROM data"
        with pytest.raises(StructuredQueryError, match="只读"):
            engine.validate_sql("DELETE FROM data", {"data"})
        with pytest.raises(StructuredQueryError, match="未注册表"):
            engine.validate_sql("SELECT * FROM secret", {"data"})

    def test_execute_csv_with_duckdb(self):
        """CSV 应注册为 data 表并返回 JSON 兼容行。"""
        pytest.importorskip("duckdb")
        from src.knowledge.structured_query import StructuredQueryEngine

        content = b"id,value\n1,10\n2,20\n3,30\n"
        result = __import__("asyncio").run(
            StructuredQueryEngine(max_rows=2).execute("orders.csv", content, "SELECT id, value * 2 AS total FROM data ORDER BY id")
        )
        assert result.engine == "duckdb"
        assert result.rows == [{"id": 1, "total": 20}, {"id": 2, "total": 40}]
        assert result.truncated is True
        assert result.tables == {"data": "data"}

    async def test_execute_requires_optional_engine_when_unavailable(self, monkeypatch):
        """未安装 DuckDB 时必须给出安装提示，不能回退到不安全的 eval。"""
        # Arrange
        import src.knowledge.structured_query as query_module
        from src.knowledge.structured_query import StructuredQueryEngine, StructuredQueryError

        monkeypatch.setattr(query_module, "_load_duckdb", lambda: None)

        # Act / Assert
        with pytest.raises(StructuredQueryError, match="DuckDB"):
            await StructuredQueryEngine().execute("orders.csv", b"id\n1\n", "SELECT * FROM data")

    # 方法作用：验证 Pandas/DuckDB 同步工作在线程池中执行。
    # Args: self - pytest 测试类实例；monkeypatch - pytest 补丁工具。
    # Returns: 无返回值，断言失败时由 pytest 报告。
    async def test_execute_offloads_sync_pipeline(self, monkeypatch) -> None:
        """文件解析和 DuckDB 查询不得占用 asyncio 事件循环线程。"""
        logger.debug("test_execute_offloads_sync_pipeline 入口")
        pytest.importorskip("duckdb")
        from src.knowledge.structured_query import StructuredQueryEngine

        # Arrange
        engine = StructuredQueryEngine(max_rows=2)
        original_load = engine.adapter.load_tables
        worker_threads: list[int] = []

        # 方法作用：记录结构化文件解析实际运行的线程。
        # Args: args/kwargs - 原 load_tables 调用参数。
        # Returns: 原方法解析结果。
        def tracked_load(*args, **kwargs):
            logger.debug("tracked_load 入口")
            worker_threads.append(threading.get_ident())
            result = original_load(*args, **kwargs)
            logger.info("tracked_load 完成")
            return result

        monkeypatch.setattr(engine.adapter, "load_tables", tracked_load)
        event_loop_thread = threading.get_ident()

        # Act
        result = await engine.execute(
            "orders.csv",
            b"id,value\n1,10\n2,20\n",
            "SELECT SUM(value) AS total FROM data",
        )

        # Assert
        assert result.rows == [{"total": 30.0}]
        assert worker_threads and worker_threads[0] != event_loop_thread
        logger.info("test_execute_offloads_sync_pipeline 完成")
