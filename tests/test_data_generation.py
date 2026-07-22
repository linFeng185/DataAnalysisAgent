"""测试数据 SQL 生成器公共行为测试。"""

from __future__ import annotations

import logging
from datetime import date


logger = logging.getLogger(__name__)


class TestDataGeneration:
    """覆盖测试数据日期边界和最小 SQL 文件生成。"""

    # 方法作用：验证随机日期始终落在闭区间内。
    # Args: self - pytest 测试类实例。
    # Returns: 无返回值，断言失败时由 pytest 报告。
    def test_random_date_respects_closed_range(self) -> None:
        """零长度区间和普通区间都不得越界。"""
        logger.debug("test_random_date_respects_closed_range 入口")
        from src.data_generation import random_date

        start = date(2026, 1, 1)
        end = date(2026, 1, 3)

        assert random_date(start, start) == start
        assert all(start <= random_date(start, end) <= end for _ in range(20))
        logger.info("test_random_date_respects_closed_range 完成")

    # 方法作用：验证最小配置可生成各业务表的 SQL。
    # Args: self - pytest 测试类实例；monkeypatch - pytest 补丁工具；tmp_path - 临时目录。
    # Returns: 无返回值，断言失败时由 pytest 报告。
    def test_main_writes_minimal_seed_file(self, monkeypatch, tmp_path) -> None:
        """生成器必须使用配置输出路径且产物包含完整表集合。"""
        logger.debug("test_main_writes_minimal_seed_file 入口")
        import src.data_generation as module

        output = tmp_path / "seed.sql"
        monkeypatch.setattr(module, "OUTPUT_FILE", str(output))
        monkeypatch.setattr(module, "USER_COUNT", 1)
        monkeypatch.setattr(module, "CATEGORY_COUNT", 1)
        monkeypatch.setattr(module, "PRODUCT_COUNT", 1)
        monkeypatch.setattr(module, "ORDER_COUNT", 1)
        monkeypatch.setattr(module, "LOG_COUNT", 1)

        module.main()

        sql = output.read_text(encoding="utf-8")
        assert "INSERT INTO categories" in sql
        assert "INSERT INTO products" in sql
        assert "INSERT INTO users" in sql
        assert "INSERT INTO orders" in sql
        assert "INSERT INTO order_items" in sql
        assert "INSERT INTO user_level_log" in sql
        logger.info("test_main_writes_minimal_seed_file 完成")
