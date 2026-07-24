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

    # 方法作用：验证分类和商品名称不依赖 Faker 有限 unique 池。
    # Args: self - pytest 测试类实例；monkeypatch - pytest 补丁工具；tmp_path - 临时目录。
    # Returns: 无返回值，断言失败时由 pytest 报告。
    def test_main_does_not_use_faker_unique_pool(self, monkeypatch, tmp_path) -> None:
        """生成数量超过 Faker 唯一值池时仍应依靠 ID 后缀保证名称唯一。"""
        logger.debug("test_main_does_not_use_faker_unique_pool 入口")
        import src.data_generation as module

        # Arrange
        class FakeFaker:
            """提供生成器所需方法，并在访问 unique 时立即失败。"""

            # 方法作用：模拟 Faker 随机种子初始化。
            # Args: self - 模拟 Faker；seed - 固定种子。
            # Returns: 无返回值。
            def seed_instance(self, seed: int) -> None:
                logger.debug("FakeFaker.seed_instance 入口", extra={"seed": seed})
                logger.info("FakeFaker.seed_instance 完成")

            @property
            # 方法作用：阻止被测实现继续依赖 Faker unique 池。
            # Args: self - 模拟 Faker。
            # Returns: 永不返回，访问即抛出 AssertionError。
            def unique(self):
                logger.error("FakeFaker.unique 被意外访问", exc_info=True)
                raise AssertionError("unique pool must not be used")

            # 方法作用：返回固定分类词。
            # Args: self - 模拟 Faker。
            # Returns: 固定英文单词。
            def word(self) -> str:
                logger.debug("FakeFaker.word 入口")
                logger.info("FakeFaker.word 完成")
                return "category"

            # 方法作用：返回固定商品短语。
            # Args: self - 模拟 Faker。
            # Returns: 固定商品短语。
            def catch_phrase(self) -> str:
                logger.debug("FakeFaker.catch_phrase 入口")
                logger.info("FakeFaker.catch_phrase 完成")
                return "Product"

            # 方法作用：返回固定用户名。
            # Args: self - 模拟 Faker。
            # Returns: 固定用户名。
            def name(self) -> str:
                logger.debug("FakeFaker.name 入口")
                logger.info("FakeFaker.name 完成")
                return "User"

        output = tmp_path / "seed.sql"
        monkeypatch.setattr(module, "fake", FakeFaker())
        monkeypatch.setattr(module, "OUTPUT_FILE", str(output))
        monkeypatch.setattr(module, "USER_COUNT", 1)
        monkeypatch.setattr(module, "CATEGORY_COUNT", 2)
        monkeypatch.setattr(module, "PRODUCT_COUNT", 2)
        monkeypatch.setattr(module, "ORDER_COUNT", 1)
        monkeypatch.setattr(module, "LOG_COUNT", 1)

        # Act
        module.main()

        # Assert
        sql = output.read_text(encoding="utf-8")
        assert "Category类-1" in sql and "Category类-2" in sql
        assert "Product-1" in sql and "Product-2" in sql
        logger.info("test_main_does_not_use_faker_unique_pool 完成")
