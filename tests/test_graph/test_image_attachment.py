"""覆盖 19.4 的请求白名单、文件身份、缓存及产物引用。"""

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from src.graph.nodes.mcp_agent import _build_attachment_tools
from tests.test_knowledge.test_image_assets import image_bytes


class TestImageAttachment:
    """覆盖 19.4：统一附件执行链和租户隔离。"""

    # 方法作用：验证同请求复用视觉结果但每次都重新读取文件权限。
    # Args: self - 实例；monkeypatch - 边界补丁。
    # Returns: 无返回值。
    async def test_image_cached_but_access_rechecked(self, monkeypatch) -> None:
        """缓存不能绕过 FileStore 撤权，同时避免重复计费。"""
        import src.knowledge.file_store as file_module
        import src.knowledge.image_assets as image_module

        record = {"filename": "chart.png", "file_data": image_bytes(), "tenant_id": 3, "owner_user_id": 8, "scope": "private"}
        store = SimpleNamespace(get=AsyncMock(return_value=record))
        analysis = AsyncMock(return_value={"kind": "image", "analysis": {"summary": "图表"}})
        monkeypatch.setattr(file_module, "get_file_store", lambda: store)
        monkeypatch.setattr(image_module, "analyze_image_bytes", analysis)
        tool = _build_attachment_tools({"file_ids": [7], "tenant_id": 3, "user_id": 8})[0]
        first = await tool.ainvoke({"file_id": 7})
        second = await tool.ainvoke({"file_id": 7})
        assert first == second and analysis.await_count == 1
        assert store.get.await_count == 2
        store.get.return_value = None
        with pytest.raises(FileNotFoundError):
            await tool.ainvoke({"file_id": 7})

    # 方法作用：验证显式请求身份与文件所有者不一致时拒绝视觉调用。
    # Args: self - 实例；monkeypatch - 补丁。
    # Returns: 无返回值。
    async def test_image_owner_mismatch_rejected(self, monkeypatch) -> None:
        """即使 FileStore 替身返回了其他用户记录，第二道防线仍阻断。"""
        import src.knowledge.file_store as file_module
        import src.knowledge.image_assets as image_module

        store = SimpleNamespace(get=AsyncMock(return_value={"filename": "chart.png", "file_data": image_bytes(), "tenant_id": 3, "owner_user_id": 9, "scope": "private"}))
        analysis = AsyncMock()
        monkeypatch.setattr(file_module, "get_file_store", lambda: store)
        monkeypatch.setattr(image_module, "analyze_image_bytes", analysis)
        tool = _build_attachment_tools({"file_ids": [7], "tenant_id": 3, "user_id": 8})[0]
        with pytest.raises(PermissionError):
            await tool.ainvoke({"file_id": 7})
        analysis.assert_not_awaited()

    # 方法作用：验证失败视觉调用也消耗请求预算，禁止无限重试计费。
    # Args: self - 实例；monkeypatch - 补丁。
    # Returns: 无返回值。
    async def test_image_failures_consume_request_budget(self, monkeypatch) -> None:
        """达到三次尝试后不再调用视觉服务。"""
        import src.knowledge.file_store as file_module
        import src.knowledge.image_assets as image_module

        store = SimpleNamespace(get=AsyncMock(return_value={"filename": "chart.png", "file_data": image_bytes(), "tenant_id": 3, "owner_user_id": 8, "scope": "private"}))
        analysis = AsyncMock(side_effect=ValueError("视觉服务失败"))
        monkeypatch.setattr(file_module, "get_file_store", lambda: store)
        monkeypatch.setattr(image_module, "analyze_image_bytes", analysis)
        tool = _build_attachment_tools({"file_ids": [7], "tenant_id": 3, "user_id": 8})[0]
        for _attempt in range(4):
            with pytest.raises(ValueError):
                await tool.ainvoke({"file_id": 7})
        assert analysis.await_count == 3

    # 方法作用：验证统一产物携带真实图片文件引用与识别限制。
    # Args: self - 实例。
    # Returns: 无返回值。
    def test_artifact_keeps_image_citation(self) -> None:
        """图片引用不能在最终 AnalysisArtifact 中退化成不可追溯的通用来源。"""
        from src.graph.artifacts import build_analysis_artifact

        artifact = build_analysis_artifact({}, {"source": "file_analysis", "status": "success", "analysis": {
            "summary": "图片分析", "citations": [{"source_id": "file:7", "locator": {"file_id": 7, "kind": "image", "fingerprint": "a" * 64}}],
        }})
        assert any(evidence["source_id"] == "file:7" for evidence in artifact["evidence"])
        assert any("图片" in limitation for limitation in artifact["limitations"])
