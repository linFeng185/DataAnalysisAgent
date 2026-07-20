"""查询限流内存回收回归测试。"""

from __future__ import annotations

import time
from types import SimpleNamespace


class TestDataMaskerLimits:
    """覆盖滑动窗口过期 key 的回收。"""

    # 验证新请求会清理已过期用户的限流记录，避免字典无限增长。
    # Args: self - 测试类实例；monkeypatch - pytest 补丁工具。
    # Returns: 无返回值，断言失败时由 pytest 报告。
    def test_rate_limit_removes_stale_user_keys(self, monkeypatch) -> None:
        import src.security.data_masker as masker

        monkeypatch.setattr(masker, "get_settings", lambda: SimpleNamespace(max_queries_per_hour=10))
        masker._rate_limits.clear()  # noqa: SLF001
        masker._rate_limits["rate:stale"] = [time.monotonic() - 3601]  # noqa: SLF001

        assert masker.check_rate_limit("active") is True
        assert "rate:stale" not in masker._rate_limits  # noqa: SLF001
