"""知识库文档接口的系统知识放宽测试，覆盖 spec/44 §2.1。

路由函数依赖完整应用上下文，单元级测试锁定源码契约：include_system 只放宽
可见性，不放宽删除权。
"""

from __future__ import annotations

import re
from pathlib import Path

ROUTE_SOURCE = Path("src/api/routes/knowledge.py").read_text(encoding="utf-8")


class TestDocIncludeSystem:
    """验证 include_system 只放宽可见性、不放宽删除。"""

    # 方法作用：验证文档列表接口暴露 include_system 参数。
    # Args: self - 测试实例。
    # Returns: 无返回值，断言失败时由 pytest 报告。
    def test_doc_list_accepts_include_system(self) -> None:
        """文档列表需要与条目列表一致的开关。"""
        # Act / Assert
        assert re.search(
            r"async def list_knowledge_docs\(include_system: bool = Query\(default=False\)\)",
            ROUTE_SOURCE,
        )

    # 方法作用：验证非超管在未勾选时仍被拒绝读取系统文档。
    # Args: self - 测试实例。
    # Returns: 无返回值，断言失败时由 pytest 报告。
    def test_doc_content_still_guarded_without_flag(self) -> None:
        """未勾选时保持 404，不泄漏系统文档存在性。"""
        # Act / Assert
        assert "not is_platform_super_admin() and not include_system" in ROUTE_SOURCE

    # 方法作用：验证内容与原文接口都接受 include_system。
    # Args: self - 测试实例。
    # Returns: 无返回值，断言失败时由 pytest 报告。
    def test_content_and_raw_accept_flag(self) -> None:
        """两个读取端点必须口径一致。"""
        # Act / Assert
        assert ROUTE_SOURCE.count('include_system: bool = False') >= 2

    # 方法作用：验证删除文档路径未引入 include_system 放宽。
    # Args: self - 测试实例。
    # Returns: 无返回值，断言失败时由 pytest 报告。
    def test_doc_delete_not_relaxed(self) -> None:
        """删除路径必须保持超管独占，不得出现放宽参数。"""
        # Act
        delete_section = ROUTE_SOURCE.split("async def delete_knowledge_doc")[1]

        # Assert
        assert "include_system" not in delete_section
