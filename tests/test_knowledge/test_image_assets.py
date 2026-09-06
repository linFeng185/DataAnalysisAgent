"""覆盖 19.4 图片校验、预算、模型能力和结构化视觉输出。"""

import io
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from src.knowledge.image_assets import ImageAssetError, analyze_image_bytes, prepare_image_bytes

Image = pytest.importorskip("PIL.Image", reason="图片测试需要 vision 可选依赖")


# 方法作用：生成小型无外部资源的图片测试字节。
# Args: image_format - 测试格式；size - 宽高。
# Returns: 内存图片字节。
def image_bytes(image_format: str = "PNG", size: tuple[int, int] = (16, 12)) -> bytes:
    buffer = io.BytesIO()
    Image.new("RGB", size, color="white").save(buffer, format=image_format)
    return buffer.getvalue()


class TestImageAssets:
    """覆盖 19.4：格式、空输入、伪装、尺寸、能力拒绝和失败关闭。"""

    # 方法作用：验证受支持的图片被统一编码且有稳定原图指纹。
    # Args: self - 实例；image_format - 图片格式。
    # Returns: 无返回值。
    @pytest.mark.parametrize("image_format", ["PNG", "JPEG", "WEBP"])
    def test_normalizes_supported_images(self, image_format) -> None:
        """模型只接收重新编码的 JPEG，不转发原始元数据。"""
        prepared = prepare_image_bytes(image_bytes(image_format))
        assert prepared.mime_type == "image/jpeg"
        assert prepared.width == 16 and prepared.height == 12
        assert len(prepared.fingerprint) == 64
        assert prepared.content.startswith(b"\xff\xd8")
        assert "content=" not in repr(prepared)

    # 方法作用：验证损坏、空、SVG、GIF 及预算超限输入被拒绝。
    # Args: self - 实例；content - 无效字节。
    # Returns: 无返回值。
    @pytest.mark.parametrize("content", [b"", b"not-image", b"<svg></svg>", image_bytes("GIF")])
    def test_invalid_image_rejected(self, content) -> None:
        """不能通过文件扩展名绕过实际图片解析。"""
        with pytest.raises(ImageAssetError):
            prepare_image_bytes(content)

    # 方法作用：验证像素和文件大小在解码前有界。
    # Args: self - 实例。
    # Returns: 无返回值。
    def test_image_budgets(self) -> None:
        """拒绝超限图片；允许的图片按最长边缩放。"""
        content = image_bytes(size=(100, 100))
        with pytest.raises(ImageAssetError):
            prepare_image_bytes(content, max_pixels=99)
        with pytest.raises(ImageAssetError):
            prepare_image_bytes(content, max_bytes=10)
        assert prepare_image_bytes(content, max_edge=20).width == 20

    # 方法作用：验证视觉消息通过统一工厂发送，结果不返回原图或 Base64。
    # Args: self - 实例；monkeypatch - 边界补丁。
    # Returns: 无返回值。
    async def test_vision_uses_selected_model(self, monkeypatch) -> None:
        """测试只使用 Fake 响应，验证图片消息、引用和输出限制。"""
        import src.llm.client as client_module
        import src.llm.tenant_config as tenant_module

        model = SimpleNamespace(ainvoke=AsyncMock(return_value=SimpleNamespace(content='{"summary":"图中是柱状图","observations":["包含三个类别"],"extracted_text":"示例","limitations":[]}')))
        monkeypatch.setattr(client_module, "get_task_llm", lambda *args, **kwargs: model)
        monkeypatch.setattr(tenant_module, "get_current_tenant_llm_selection", lambda: SimpleNamespace(tenant_id=3, capabilities={"vision": True}))
        result = await analyze_image_bytes(7, "chart.png", image_bytes(), "解释图表", tenant_id=3, user_id=8)
        assert result["kind"] == "image"
        assert result["asset"]["tenant_id"] == 3
        assert result["citation"]["source_id"] == "file:7"
        assert result["analysis"]["limitations"]
        messages = model.ainvoke.await_args.args[0]
        assert messages[-1].content[1]["image_url"]["url"].startswith("data:image/jpeg;base64,")
        assert "base64" not in str(result)

    # 方法作用：验证未声明视觉或跨租户模型不能读取图片内容。
    # Args: self - 实例；monkeypatch - 补丁；selection - 不满足约束的模型选择。
    # Returns: 无返回值。
    @pytest.mark.parametrize("selection", [None, SimpleNamespace(tenant_id=3, capabilities={}), SimpleNamespace(tenant_id=9, capabilities={"vision": True})])
    async def test_vision_capability_fails_closed(self, monkeypatch, selection) -> None:
        """不调用模型工厂，不自动回退到其他服务。"""
        import src.llm.client as client_module
        import src.llm.tenant_config as tenant_module

        factory = AsyncMock()
        monkeypatch.setattr(client_module, "get_task_llm", factory)
        monkeypatch.setattr(tenant_module, "get_current_tenant_llm_selection", lambda: selection)
        with pytest.raises(ImageAssetError):
            await analyze_image_bytes(7, "chart.png", image_bytes(), "解释", tenant_id=3, user_id=8)
        factory.assert_not_called()

    # 方法作用：验证原图中 EXIF 描述不会转发给模型。
    # Args: self - 实例。
    # Returns: 无返回值。
    def test_image_metadata_is_removed(self) -> None:
        """重新创建像素图像后，原始 EXIF 隐私数据必须消失。"""
        buffer = io.BytesIO()
        metadata = Image.Exif()
        metadata[270] = "private-exif-marker"
        Image.new("RGB", (10, 10)).save(buffer, format="JPEG", exif=metadata)
        assert b"private-exif-marker" in buffer.getvalue()
        prepared = prepare_image_bytes(buffer.getvalue())
        assert b"private-exif-marker" not in prepared.content

    # 方法作用：验证模型错误和不合规输出转换为固定安全错误。
    # Args: self - 实例；monkeypatch - 边界补丁；mode - 模型失败类别。
    # Returns: 无返回值。
    @pytest.mark.parametrize("mode", ["timeout", "invalid", "oversized"])
    async def test_vision_model_error_sanitized(self, monkeypatch, mode) -> None:
        """失败不能返回半成品，也不能泄露上游正文。"""
        import src.llm.client as client_module
        import src.llm.tenant_config as tenant_module

        call = AsyncMock(side_effect=TimeoutError("private-upstream")) if mode == "timeout" else AsyncMock(return_value=SimpleNamespace(content="x" * (20_000 if mode == "oversized" else 1)))
        model = SimpleNamespace(ainvoke=call)
        monkeypatch.setattr(client_module, "get_task_llm", lambda *args, **kwargs: model)
        monkeypatch.setattr(tenant_module, "get_current_tenant_llm_selection", lambda: SimpleNamespace(tenant_id=3, capabilities={"vision": True}))
        with pytest.raises(ImageAssetError) as captured:
            await analyze_image_bytes(7, "chart.png", image_bytes(), "解释", tenant_id=3, user_id=8)
        assert "private-upstream" not in str(captured.value)
