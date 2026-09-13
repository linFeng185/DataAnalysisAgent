"""知识条目列表缓存测试，覆盖 spec/44 §4.3。

事实依据：pytest 经 tests/conftest.py 与 tests/test_knowledge/__init__.py 收集；
同目录无列表缓存主题文件；本文件不读写数据文件，只断言进程内缓存的纯内存行为。
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from src.knowledge.management_cache import ManagementListCache  # noqa: E402


class TestManagementListCache:
    """验证 TTL 命中、租户失效与容量约束。"""

    # 方法作用：验证同一键在 TTL 内命中。
    # Args: self - 测试实例。
    # Returns: 无返回值，断言失败时由 pytest 报告。
    def test_hit_within_ttl(self) -> None:
        """重复翻页不应回源。"""
        # Arrange
        cache = ManagementListCache(ttl_seconds=30)
        key = ("tenant_admin", 3, 3, "", "", "document")

        # Act
        cache.set(key, [{"id": "a"}])

        # Assert
        assert cache.get(key) == [{"id": "a"}]

    # 方法作用：验证键不同则不串号。
    # Args: self - 测试实例。
    # Returns: 无返回值，断言失败时由 pytest 报告。
    def test_different_keys_isolated(self) -> None:
        """不同身份与筛选键必须彼此隔离。"""
        # Arrange
        cache = ManagementListCache(ttl_seconds=30)
        cache.set(("tenant_admin", 3, 3, "", "", "document"), [{"id": "a"}])

        # Act
        hit = cache.get(("analyst", 3, 5, "", "", "document"))

        # Assert
        assert hit is None

    # 方法作用：验证关键词不进入缓存键。
    # Args: self - 测试实例。
    # Returns: 无返回值，断言失败时由 pytest 报告。
    def test_search_not_part_of_key(self) -> None:
        """搜索在缓存值上做内存过滤。"""
        # Arrange
        cache = ManagementListCache(ttl_seconds=30)
        key = ("tenant_admin", 3, 3, "", "", "document")

        # Act
        cache.set(key, [{"id": "a", "content": "月度销售额"}])
        hit = cache.get(key)
        filtered = [item for item in hit if "销售" in item["content"]]

        # Assert
        assert len(filtered) == 1

    # 方法作用：验证按租户失效会清空该租户全部键。
    # Args: self - 测试实例。
    # Returns: 无返回值，断言失败时由 pytest 报告。
    def test_invalidate_tenant_clears_all_keys_of_tenant(self) -> None:
        """上传或删除后必须立刻失效。"""
        # Arrange
        cache = ManagementListCache(ttl_seconds=30)
        cache.set(("tenant_admin", 3, 3, "", "", "document"), [{"id": "a"}])
        cache.set(("analyst", 3, 5, "", "", "all"), [{"id": "b"}])
        cache.set(("tenant_admin", 4, 9, "", "", "document"), [{"id": "c"}])

        # Act
        removed = cache.invalidate_tenant(3)

        # Assert
        assert removed == 2
        assert cache.get(("tenant_admin", 3, 3, "", "", "document")) is None
        assert cache.get(("tenant_admin", 4, 9, "", "", "document")) == [{"id": "c"}]

    # 方法作用：验证超过容量时淘汰最旧条目。
    # Args: self - 测试实例。
    # Returns: 无返回值，断言失败时由 pytest 报告。
    def test_capacity_eviction(self) -> None:
        """多身份并发时缓存不得无限增长。"""
        # Arrange
        cache = ManagementListCache(ttl_seconds=30, max_entries=2)

        # Act
        cache.set(("r", 1, 1, "", "", "document"), [{"id": "1"}])
        cache.set(("r", 1, 2, "", "", "document"), [{"id": "2"}])
        cache.set(("r", 1, 3, "", "", "document"), [{"id": "3"}])

        # Assert
        assert cache.stats()["entries"] == 2

    # 方法作用：验证 TTL 过期后回源。
    # Args: self - 测试实例。
    # Returns: 无返回值，断言失败时由 pytest 报告。
    def test_expired_entry_misses(self) -> None:
        """TTL 归零后必须重新查询，避免长期展示陈旧数据。"""
        # Arrange
        cache = ManagementListCache(ttl_seconds=0)
        key = ("tenant_admin", 3, 3, "", "", "document")
        cache.set(key, [{"id": "a"}])

        # Act
        hit = cache.get(key)

        # Assert
        assert hit is None

    # 方法作用：验证 include_system 不同取值互不串号。
    # Args: self - 测试实例。
    # Returns: 无返回值，断言失败时由 pytest 报告。
    def test_include_system_is_part_of_key(self) -> None:
        """取消勾选后不得再从缓存返回系统知识。"""
        # Arrange
        cache = ManagementListCache(ttl_seconds=30)
        base = ("tenant_admin", 3, 3, "", "", "document")

        # Act
        cache.set((*base, True), [{"id": "system-1"}])
        default_hit = cache.get((*base, False))

        # Assert
        assert default_hit is None
        assert cache.get((*base, True)) == [{"id": "system-1"}]

    # 方法作用：验证清空接口返回清除数量。
    # Args: self - 测试实例。
    # Returns: 无返回值，断言失败时由 pytest 报告。
    def test_clear_returns_removed_count(self) -> None:
        """配置热重载与测试需要整体清空能力。"""
        # Arrange
        cache = ManagementListCache(ttl_seconds=30)
        cache.set(("r", 1, 1, "", "", "document"), [{"id": "1"}])
        cache.set(("r", 1, 2, "", "", "document"), [{"id": "2"}])

        # Act
        removed = cache.clear()

        # Assert
        assert removed == 2
        assert cache.stats()["entries"] == 0
