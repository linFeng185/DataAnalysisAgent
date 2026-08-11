"""前端只读请求并发去重契约测试。"""

from pathlib import Path


class TestApiRequestDeduplication:
    """覆盖 React StrictMode 下重复 GET 请求合并。"""

    # 方法作用：验证 API Client 复用同一路径的进行中 GET Promise。
    # Args: self - pytest 测试类实例。
    # Returns: 无返回值，断言失败时由 pytest 报告。
    def test_get_requests_share_inflight_promise(self) -> None:
        """并发读取同一资源只能发送一次网络请求，并在完成后清理缓存。"""
        # Arrange
        source = Path("frontend/src/api/client.ts").read_text(encoding="utf-8")

        # Act / Assert
        assert "pendingGetRequests" in source
        assert "pendingGetRequests.get(path)" in source
        assert "pendingGetRequests.delete(path)" in source
        assert ".finally(" in source

    # 方法作用：验证认证探针也复用统一 GET 去重入口。
    # Args: self - pytest 测试类实例。
    # Returns: 无返回值，断言失败时由 pytest 报告。
    def test_auth_probe_uses_deduplicated_client(self) -> None:
        """StrictMode 重挂载认证 Provider 时不应重复请求 `/auth/me`。"""
        # Arrange
        source = Path("frontend/src/hooks/AuthContext.tsx").read_text(encoding="utf-8")

        # Act / Assert
        assert "get<AuthProbe>('/auth/me')" in source
        assert "fetch('/api/v1/auth/me'" not in source
