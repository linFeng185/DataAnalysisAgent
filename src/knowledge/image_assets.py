"""受限图片解析和租户已授权视觉模型分析。"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import io
import warnings
from typing import Any

from langchain_core.messages import HumanMessage
from pydantic import BaseModel, Field

from src.knowledge.asset_models import DataAsset
from src.logging_config import get_logger

logger = get_logger(__name__)
IMAGE_EXTENSIONS = frozenset({".png", ".jpg", ".jpeg", ".webp"})
MAX_IMAGE_BYTES = 5 * 1024 * 1024
MAX_REQUEST_IMAGES = 3


class ImageAssetError(ValueError):
    """不包含图片字节或上游请求正文的安全错误。"""


class PreparedImage(BaseModel):
    """仅保留在调用栈中的受限图片，不写入 Graph state。"""

    content: bytes = Field(repr=False)
    mime_type: str = "image/jpeg"
    width: int
    height: int
    fingerprint: str


# 方法作用：限制图片体积与像素，重新编码以去除可泄露隐私的元数据。
# Args: content - 原始图片；max_bytes/max_pixels/max_edge - 字节、像素和输出边长预算。
# Returns: 受限 JPEG 及原图指纹，不依赖文件扩展名判断内容。
def prepare_image_bytes(
    content: bytes, *, max_bytes: int = MAX_IMAGE_BYTES,
    max_pixels: int = 16_000_000, max_edge: int = 2048,
) -> PreparedImage:
    """拒绝动画、损坏及超限图片，模型永远不接收原始 EXIF。"""
    if not content or len(content) > max_bytes or min(max_bytes, max_pixels, max_edge) <= 0:
        raise ImageAssetError("图片为空或超过文件大小限制")
    try:
        from PIL import Image, ImageOps

        with warnings.catch_warnings():
            warnings.simplefilter("error", Image.DecompressionBombWarning)
            with Image.open(io.BytesIO(content)) as probe:
                if probe.format not in {"PNG", "JPEG", "WEBP"} or getattr(probe, "n_frames", 1) != 1:
                    raise ImageAssetError("只支持非动画 PNG、JPEG 和 WebP 图片")
                if probe.width * probe.height > max_pixels:
                    raise ImageAssetError("图片像素超过限制")
                probe.verify()
            with Image.open(io.BytesIO(content)) as original:
                image = ImageOps.exif_transpose(original)
                image.thumbnail((max_edge, max_edge))
                rgba = image.convert("RGBA")
                clean = Image.new("RGB", rgba.size, "white")
                clean.paste(rgba, mask=rgba.getchannel("A"))
                encoded = io.BytesIO()
                clean.save(encoded, format="JPEG", quality=90)
                data = encoded.getvalue()
                if len(data) > max_bytes:
                    raise ImageAssetError("图片重新编码后仍超过模型输入限制")
                return PreparedImage(content=data, width=clean.width, height=clean.height, fingerprint=hashlib.sha256(content).hexdigest())
    except ImageAssetError:
        raise
    except ImportError:
        raise ImageAssetError("图片解析需要安装 vision 可选依赖") from None
    except Exception:
        raise ImageAssetError("图片格式不合法、已损坏或解码预算超限") from None


# 方法作用：只使用已授权且声明视觉能力的租户模型读取受限图片。
# Args: file_id/filename - 文件标识；content - 原始字节；query - 用户问题；tenant_id/user_id - 请求身份；owner_id - 文件实际所有者。
# Returns: 有真实文件引用和限制说明的统一图片分析结果，不返回图片字节。
async def analyze_image_bytes(
    file_id: int, filename: str, content: bytes, query: str, *, tenant_id: int, user_id: int, owner_id: int | None = None,
) -> dict[str, Any]:
    """图片中的文字是待分析数据，不赋予工具或数据库执行权限。"""
    from langsmith import tracing_context

    from src.llm.client import get_task_llm
    from src.llm.invocation import prepare_invocation
    from src.llm.output_contracts import ImageAnalysisOutput, parse_json_model
    from src.llm.prompt_budget import PromptSection
    from src.llm.tenant_config import get_current_tenant_llm_selection

    logger.debug("图片视觉分析入口", file_id=file_id, tenant_id=tenant_id)
    selection = get_current_tenant_llm_selection()
    if tenant_id <= 0 or user_id <= 0 or selection is None or selection.tenant_id != tenant_id:
        raise ImageAssetError("图片分析需要当前租户授权的模型")
    if selection.capabilities.get("vision") is not True:
        raise ImageAssetError("当前模型未声明视觉能力，请选择支持图片的模型")
    prepared_image = await asyncio.to_thread(prepare_image_bytes, content)
    try:
        model = get_task_llm("image_analysis", temperature=0.0, reasoning=False, max_tokens=2048, stream=False)
        invocation = prepare_invocation(
            "image.analysis", [PromptSection("query", query, priority=100, max_chars=2000)], model=model,
        )
        invocation.messages[-1] = HumanMessage(content=[
            {"type": "text", "text": str(invocation.messages[-1].content)},
            {"type": "image_url", "image_url": {"url": "data:image/jpeg;base64," + base64.b64encode(prepared_image.content).decode("ascii")}},
        ])
        # 图片内容不参加外部追踪；只保留当前请求的文件 ID 和结果摘要。
        with tracing_context(enabled=False):
            response = await asyncio.wait_for(invocation.model.ainvoke(invocation.messages, config={"callbacks": []}), timeout=30)
        raw = getattr(response, "content", response)
        if isinstance(raw, list):
            raw = "\n".join(str(block.get("text", "")) for block in raw if isinstance(block, dict))
        if len(str(raw)) > 16_000:
            raise ImageAssetError("视觉模型输出超过限制")
        analysis = parse_json_model(raw, ImageAnalysisOutput)
    except ImageAssetError:
        raise
    except Exception:
        raise ImageAssetError("视觉模型调用失败、超时或输出格式不合法") from None
    analysis.limitations = list(dict.fromkeys([*analysis.limitations[:9], "图片识别可能存在文字或数值误差，重要结论需要核对原图。"] ))
    asset = DataAsset(
        id=f"image:{tenant_id}:{file_id}:{prepared_image.fingerprint[:16]}", kind="image", uri=f"file:{file_id}",
        tenant_id=tenant_id, owner_id=user_id if owner_id is None else owner_id, mime_type=prepared_image.mime_type,
        fingerprint=prepared_image.fingerprint,
        provenance={"file_id": file_id, "filename": filename},
        schema_info={"width": prepared_image.width, "height": prepared_image.height},
    )
    logger.info("图片视觉分析完成", file_id=file_id, width=prepared_image.width, height=prepared_image.height)
    return {
        "kind": "image", "file_id": file_id, "filename": filename,
        "asset": asset.model_dump(by_alias=True), "analysis": analysis.model_dump(),
        "citation": {"source_id": f"file:{file_id}", "locator": {"file_id": file_id, "filename": filename, "kind": "image", "fingerprint": prepared_image.fingerprint}},
    }
