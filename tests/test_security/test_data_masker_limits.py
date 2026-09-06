"""G24.2 旧限流门面迁移到共享服务的回归测试。"""

from unittest.mock import AsyncMock


class TestDataMaskerLimits:
    """验证不再使用用户字符串或进程内缓存进行跨租户计数。"""

    # 验证门面将独立租户和用户传递给异步共享服务。
    # Args: self - 测试实例；monkeypatch - 服务替身。
    # Returns: 无返回值。
    async def test_rate_limit_delegates_identity(self, monkeypatch) -> None:
        """入口不拼接身份，不吞掉存储异常。"""
        from src.security.data_masker import check_rate_limit

        consume = AsyncMock(return_value=True)
        monkeypatch.setattr("src.security.query_quota.consume_query_quota", consume)
        assert await check_rate_limit(3, tenant_id=2)
        consume.assert_awaited_once_with(2, 3)
        assert not await check_rate_limit("invalid", tenant_id=2)
