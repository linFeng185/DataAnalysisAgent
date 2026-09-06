"""覆盖 19.4 的上传预检，不调用模型或真实数据库。"""

import importlib
import io
import logging
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from fastapi import HTTPException, UploadFile

from tests.test_knowledge.test_image_assets import image_bytes


class TestImageUpload:
    """覆盖 19.4：私有图片上传、伪装拒绝、数量上限。"""

    # 方法作用：验证图片沿既有私有附件存储入口持久化。
    # Args: self - 实例；monkeypatch - 补丁。
    # Returns: 无返回值。
    async def test_valid_image_upload_private(self, monkeypatch) -> None:
        """上传只校验图像，不调用视觉模型、不进入公共知识。"""
        chat_module = importlib.import_module("src.api.routes.chat")
        logging.getLogger(__name__).info("图片上传导入边界 type=%s", type(chat_module).__name__)
        import src.knowledge.file_store as file_module

        store = SimpleNamespace(save=AsyncMock(return_value=7))
        monkeypatch.setattr(file_module, "get_file_store", lambda: store)
        upload = UploadFile(filename="chart.png", file=io.BytesIO(image_bytes()))
        result = await chat_module.upload_chat_files([upload])
        assert result["files"][0]["file_id"] == 7
        assert store.save.await_args.kwargs["knowledge_scope"] == "private"

    # 方法作用：验证伪装文件和过多图片在保存任何文件之前被拒绝。
    # Args: self - 实例；monkeypatch - 补丁；mode - 无效请求类别。
    # Returns: 无返回值。
    @pytest.mark.parametrize("mode", ["fake", "too_many"])
    async def test_invalid_image_batch_never_saved(self, monkeypatch, mode) -> None:
        """整个批次先预检，失败不产生部分持久化。"""
        chat_module = importlib.import_module("src.api.routes.chat")
        import src.knowledge.file_store as file_module

        store = SimpleNamespace(save=AsyncMock())
        monkeypatch.setattr(file_module, "get_file_store", lambda: store)
        content = b"not-an-image" if mode == "fake" else image_bytes()
        uploads = [UploadFile(filename="chart.png", file=io.BytesIO(content)) for _index in range(1 if mode == "fake" else 4)]
        with pytest.raises(HTTPException) as captured:
            await chat_module.upload_chat_files(uploads)
        assert captured.value.status_code == (400 if mode == "fake" else 413)
        store.save.assert_not_awaited()
