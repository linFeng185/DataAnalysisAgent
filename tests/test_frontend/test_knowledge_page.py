"""知识库三范围与标签交互前端回归测试。"""

from __future__ import annotations

import logging
from pathlib import Path

logger = logging.getLogger(__name__)


# 验证知识库页面包含范围筛选、角色控制和多标签上传调用。
# Args: 无。
# Returns: 无返回值，断言失败时由 pytest 报告。
def test_knowledge_page_contains_scope_and_tag_workflows() -> None:
    """用户必须能筛选范围、搜索标签、创建个人标签并随文档上传。"""
    logger.debug("test_knowledge_page_contains_scope_and_tag_workflows 入口")
    try:
        # Arrange / Act：读取知识库页面源码。
        source = Path("frontend/src/pages/KnowledgePage.tsx").read_text(encoding="utf-8")

        # Assert：角色、三范围、标签 API 和上传参数均存在。
        assert "useAuth" in source
        assert "super_admin" in source
        assert "tenant_admin" in source
        assert "system" in source and "tenant" in source and "private" in source
        assert "/knowledge/tags" in source
        assert "tag_ids" in source
        assert "knowledge_scope" in source
        assert 'mode="multiple"' in source
        logger.info("test_knowledge_page_contains_scope_and_tag_workflows 完成")
    except Exception as exc:
        logger.error(
            "test_knowledge_page_contains_scope_and_tag_workflows 异常: %s", exc, exc_info=True,
        )
        raise


# 验证前端类型包含知识范围、标签和文档 ACL 字段。
# Args: 无。
# Returns: 无返回值，断言失败时由 pytest 报告。
def test_knowledge_types_expose_governance_fields() -> None:
    """页面不应依赖无类型的临时对象传递治理数据。"""
    logger.debug("test_knowledge_types_expose_governance_fields 入口")
    try:
        # Arrange / Act：读取 TypeScript 类型声明。
        source = Path("frontend/src/types/index.ts").read_text(encoding="utf-8")

        # Assert：范围联合类型和标签模型已声明。
        assert "KnowledgeScope" in source
        assert "KnowledgeTag" in source
        assert "tag_ids" in source
        assert "owner_user_id" in source
        logger.info("test_knowledge_types_expose_governance_fields 完成")
    except Exception as exc:
        logger.error("test_knowledge_types_expose_governance_fields 异常: %s", exc, exc_info=True)
        raise
