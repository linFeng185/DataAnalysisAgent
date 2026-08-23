"""聊天附件上传端点的资源限制和私有存储回归测试。"""

from __future__ import annotations

import importlib
import io
import logging
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from fastapi import HTTPException, UploadFile

logger = logging.getLogger(__name__)


class TestChatAttachments:
    """覆盖聊天附件上传的格式、大小、数量和私有文件 ID 返回。"""

    # 方法作用：验证聊天附件上传固定写入当前用户私有范围并返回文件 ID。
    # Args: self - pytest 测试类实例；monkeypatch - pytest 补丁工具。
    # Returns: 无返回值，断言失败时由 pytest 报告。
    async def test_upload_chat_files_saves_private_file_ids(self, monkeypatch) -> None:
        """聊天附件不能进入租户公共知识，也不能触发知识库后台索引。"""
        # Arrange
        chat_routes = importlib.import_module("src.api.routes.chat")
        import src.config as config_module
        import src.knowledge.file_store as file_store_module

        store = SimpleNamespace(save=AsyncMock(return_value=17))
        monkeypatch.setattr(file_store_module, "get_file_store", lambda: store)
        monkeypatch.setattr(config_module, "get_settings", lambda: SimpleNamespace(
            max_upload_bytes=1024,
            max_upload_files=10,
            max_upload_total_bytes=2048,
        ))
        upload = UploadFile(filename="orders.csv", file=io.BytesIO(b"id,amount\n1,2\n"))

        # Act
        result = await chat_routes.upload_chat_files([upload])

        # Assert
        assert result["files"] == [{"file_id": 17, "filename": "orders.csv", "size": 14}]
        store.save.assert_awaited_once_with(
            "orders.csv",
            b"id,amount\n1,2\n",
            knowledge_scope="private",
        )

    # 方法作用：验证批量附件部分保存失败时按文件 ID 回滚先前写入。
    # Args: self - pytest 测试类实例；monkeypatch - pytest 补丁工具。
    # Returns: 无返回值，断言失败时由 pytest 报告。
    async def test_upload_chat_files_rolls_back_partial_saves(self, monkeypatch) -> None:
        """第二个文件失败不能让第一个私有文件成为无引用残留。"""
        # Arrange
        chat_routes = importlib.import_module("src.api.routes.chat")
        import src.config as config_module
        import src.knowledge.file_store as file_store_module

        store = SimpleNamespace(
            save=AsyncMock(side_effect=[17, None]),
            delete_by_id=AsyncMock(return_value=True),
        )
        monkeypatch.setattr(file_store_module, "get_file_store", lambda: store)
        monkeypatch.setattr(config_module, "get_settings", lambda: SimpleNamespace(
            max_upload_bytes=1024,
            max_upload_files=10,
            max_upload_total_bytes=2048,
        ))
        files = [
            UploadFile(filename="orders.csv", file=io.BytesIO(b"id\n1\n")),
            UploadFile(filename="customers.csv", file=io.BytesIO(b"id\n2\n")),
        ]

        # Act
        with pytest.raises(HTTPException) as exc_info:
            await chat_routes.upload_chat_files(files)

        # Assert
        assert exc_info.value.status_code == 503
        assert exc_info.value.detail == "附件存储暂不可用"
        store.delete_by_id.assert_awaited_once_with(17, knowledge_scope="private")

    # 方法作用：验证保存抛出异常且回滚失败时仍返回稳定的存储错误。
    # Args: self - pytest 测试类实例；monkeypatch - pytest 补丁工具。
    # Returns: 无返回值，断言失败时由 pytest 报告。
    async def test_upload_chat_files_preserves_error_when_rollback_fails(
        self, monkeypatch,
    ) -> None:
        """补偿删除失败应进入日志，不能覆盖原始上传失败协议。"""
        # Arrange
        chat_routes = importlib.import_module("src.api.routes.chat")
        import src.config as config_module
        import src.knowledge.file_store as file_store_module

        store = SimpleNamespace(
            save=AsyncMock(side_effect=[17, RuntimeError("database unavailable")]),
            delete_by_id=AsyncMock(side_effect=RuntimeError("rollback unavailable")),
        )
        monkeypatch.setattr(file_store_module, "get_file_store", lambda: store)
        monkeypatch.setattr(config_module, "get_settings", lambda: SimpleNamespace(
            max_upload_bytes=1024,
            max_upload_files=10,
            max_upload_total_bytes=2048,
        ))
        files = [
            UploadFile(filename="orders.csv", file=io.BytesIO(b"id\n1\n")),
            UploadFile(filename="customers.csv", file=io.BytesIO(b"id\n2\n")),
        ]

        # Act
        with pytest.raises(HTTPException) as exc_info:
            await chat_routes.upload_chat_files(files)

        # Assert
        assert exc_info.value.status_code == 503
        assert exc_info.value.detail == "附件存储暂不可用"
        store.delete_by_id.assert_awaited_once_with(17, knowledge_scope="private")

    # 方法作用：验证聊天附件上传拒绝数量、单文件和累计大小超限。
    # Args: self - pytest 测试类实例；monkeypatch - pytest 补丁工具。
    # Returns: 无返回值，断言失败时由 pytest 报告。
    @pytest.mark.parametrize("case", ["count", "single", "total"])
    async def test_upload_chat_files_enforces_resource_limits(self, monkeypatch, case: str) -> None:
        """资源限制必须在文件持久化前执行。"""
        # Arrange
        chat_routes = importlib.import_module("src.api.routes.chat")
        import src.config as config_module
        import src.knowledge.file_store as file_store_module

        store = SimpleNamespace(save=AsyncMock(return_value=1))
        monkeypatch.setattr(file_store_module, "get_file_store", lambda: store)
        monkeypatch.setattr(config_module, "get_settings", lambda: SimpleNamespace(
            max_upload_bytes=4,
            max_upload_files=1,
            max_upload_total_bytes=5,
        ))
        if case == "count":
            files = [
                UploadFile(filename="one.txt", file=io.BytesIO(b"1")),
                UploadFile(filename="two.txt", file=io.BytesIO(b"2")),
            ]
        elif case == "single":
            files = [UploadFile(filename="large.txt", file=io.BytesIO(b"12345"))]
        else:
            files = [
                UploadFile(filename="one.txt", file=io.BytesIO(b"123")),
                UploadFile(filename="two.txt", file=io.BytesIO(b"456")),
            ]
            monkeypatch.setattr(config_module, "get_settings", lambda: SimpleNamespace(
                max_upload_bytes=4,
                max_upload_files=2,
                max_upload_total_bytes=5,
            ))

        # Act / Assert
        with pytest.raises(HTTPException) as exc_info:
            await chat_routes.upload_chat_files(files)
        assert exc_info.value.status_code == 413
        store.save.assert_not_awaited()

    # 方法作用：验证聊天附件上传拒绝不可分析的扩展名。
    # Args: self - pytest 测试类实例；monkeypatch - pytest 补丁工具。
    # Returns: 无返回值，断言失败时由 pytest 报告。
    async def test_upload_chat_files_rejects_unsupported_extension(self, monkeypatch) -> None:
        """附件白名单必须与模型侧解析器能力一致。"""
        # Arrange
        chat_routes = importlib.import_module("src.api.routes.chat")
        import src.config as config_module
        import src.knowledge.file_store as file_store_module

        store = SimpleNamespace(save=AsyncMock(return_value=1))
        monkeypatch.setattr(file_store_module, "get_file_store", lambda: store)
        monkeypatch.setattr(config_module, "get_settings", lambda: SimpleNamespace(
            max_upload_bytes=1024,
            max_upload_files=10,
            max_upload_total_bytes=2048,
        ))
        upload = UploadFile(filename="payload.exe", file=io.BytesIO(b"MZ"))

        # Act / Assert
        with pytest.raises(HTTPException) as exc_info:
            await chat_routes.upload_chat_files([upload])
        assert exc_info.value.status_code == 400
        store.save.assert_not_awaited()

    # 方法作用：验证只有附件的聊天请求不会在文件子图前强制解析数据源。
    # Args: self - pytest 测试类实例；monkeypatch - pytest 补丁工具。
    # Returns: 无返回值，断言失败时由 pytest 报告。
    async def test_file_only_chat_skips_datasource_resolution(self, monkeypatch) -> None:
        """file_ids 已提供且未选数据源时应返回空授权快照。"""
        # Arrange
        import src.api.routes as routes_package
        from src.api.schemas import ChatRequest

        chat_routes = importlib.import_module("src.api.routes.chat")
        resolver = AsyncMock(side_effect=AssertionError("不应解析数据源"))
        monkeypatch.setattr(routes_package, "_resolve_chat_access", resolver)
        request = ChatRequest(query="分析附件", file_ids=[17])

        # Act
        result = await chat_routes._resolve_chat_access_for_request(request)

        # Assert
        assert result == {}
        resolver.assert_not_awaited()
