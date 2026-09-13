"""知识库管理列表的进程内 TTL 缓存，降低翻页与搜索的向量库压力。"""

from __future__ import annotations

import time
from collections import OrderedDict
from typing import Any

from src.logging_config import get_logger

logger = get_logger(__name__)

# 缓存键：角色、租户、用户、分类、范围、来源档位、是否放宽系统知识。
# 关键词不参与键，搜索在缓存值上完成。
CacheKey = tuple[str, int, int, str, str, str, bool]


class ManagementListCache:
    """按身份与筛选维度缓存过滤后的完整条目列表。"""

    # 方法作用：初始化缓存容量与 TTL。
    # Args: ttl_seconds - 条目存活秒数；max_entries - 最大键数量。
    # Returns: 无返回值。
    def __init__(self, ttl_seconds: float = 30.0, max_entries: int = 64) -> None:
        self._ttl = ttl_seconds
        self._max_entries = max_entries
        self._store: OrderedDict[CacheKey, tuple[float, list[dict[str, Any]]]] = OrderedDict()
        logger.debug("管理列表缓存初始化", ttl_seconds=ttl_seconds, max_entries=max_entries)

    # 方法作用：读取未过期的缓存值。
    # Args: key - 缓存键。
    # Returns: 命中且未过期时返回条目列表，否则返回 None。
    def get(self, key: CacheKey) -> list[dict[str, Any]] | None:
        """命中未过期条目时刷新 LRU 次序。"""
        entry = self._store.get(key)
        if entry is None:
            logger.debug("管理列表缓存未命中", key=key)
            return None
        expires_at, value = entry
        if time.monotonic() >= expires_at:
            self._store.pop(key, None)
            logger.info("管理列表缓存过期", key=key)
            return None
        self._store.move_to_end(key)
        logger.debug("管理列表缓存命中", key=key, count=len(value))
        return value

    # 方法作用：写入缓存并在超容量时淘汰最旧键。
    # Args: key - 缓存键；value - 过滤后的完整条目列表。
    # Returns: 无返回值。
    def set(self, key: CacheKey, value: list[dict[str, Any]]) -> None:
        """写入并维护容量上限。"""
        self._store[key] = (time.monotonic() + self._ttl, value)
        self._store.move_to_end(key)
        while len(self._store) > self._max_entries:
            evicted, _ = self._store.popitem(last=False)
            logger.info("管理列表缓存淘汰", key=evicted)
        logger.debug("管理列表缓存写入", key=key, count=len(value))

    # 方法作用：清除指定租户的全部缓存键。
    # Args: tenant_id - 租户 ID。
    # Returns: 被清除的键数量。
    def invalidate_tenant(self, tenant_id: int) -> int:
        """上传、删除等写操作后按租户失效。"""
        targets = [key for key in self._store if key[1] == tenant_id]
        for key in targets:
            self._store.pop(key, None)
        logger.info("管理列表缓存失效", tenant_id=tenant_id, removed=len(targets))
        return len(targets)

    # 方法作用：清除全部缓存键。
    # Args: 无。
    # Returns: 被清除的键数量。
    def clear(self) -> int:
        """测试与配置热重载使用。"""
        count = len(self._store)
        self._store.clear()
        logger.info("管理列表缓存清空", removed=count)
        return count

    # 方法作用：返回缓存运行状态用于观测与测试。
    # Args: 无。
    # Returns: 含 entries 与 ttl_seconds 的字典。
    def stats(self) -> dict[str, Any]:
        """暴露条目数与 TTL。"""
        result = {"entries": len(self._store), "ttl_seconds": self._ttl}
        logger.debug("管理列表缓存状态", **result)
        return result


_CACHE: ManagementListCache | None = None


# 方法作用：获取进程内共享的管理列表缓存单例。
# Args: 无。
# Returns: ManagementListCache 实例。
def get_management_list_cache() -> ManagementListCache:
    """进程内单例，多 Worker 各自独立依赖 TTL 收敛。"""
    global _CACHE
    if _CACHE is None:
        _CACHE = ManagementListCache()
        logger.info("管理列表缓存单例创建完成")
    return _CACHE
