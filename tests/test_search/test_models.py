"""受控联网搜索模型层测试：SearchHit / SearchResponse / 显式意图判定 / URL 引用安全。

覆盖功能 32.1、32.2（设计与边界见 spec/58-controlled-web-search.md）。
"""

from __future__ import annotations

import logging
from datetime import timezone

import pytest

logger = logging.getLogger(__name__)


class TestExplicitWebSearchIntent:
    """覆盖功能 32.1：只有明确联网意图才构成外送授权。"""

    # 方法作用：验证 spec/58 列出的明确联网问法全部命中。
    # Args: self - 测试实例；query - 待判定问法。
    # Returns: 无返回值，漏判时由断言报告问法。
    @pytest.mark.parametrize(
        "query",
        [
            "联网搜索",
            "帮我联网搜索最新价格",
            "联网调研一下竞品动态",
            "网上搜索相关资料",
            "搜索网页看看",
            "web search",
            "WEB SEARCH for news",
            "please search the web for pricing",
            "联网查询最新公告",
            # 首尾与内部空白容错正例：此前「空  联网检索  空白」把有语义的「空」字
            # 当作噪声前缀，契约错误，已改为纯空白包裹形态（非空白前缀按未知拒绝）。
            "  联网检索  空白  ",
        ],
    )
    def test_explicit_intent_hits(self, query: str) -> None:
        """明确联网关键词必须被识别，否则用户无法触发受控联网。"""
        logger.debug("test_explicit_intent_hits 入口", extra={"query_chars": len(query)})
        try:
            # Arrange / Act
            from src.search.models import is_explicit_web_search

            # Assert
            assert is_explicit_web_search(query) is True
            logger.info("test_explicit_intent_hits 完成")
        except Exception as exc:
            logger.error("test_explicit_intent_hits 异常", extra={"error": str(exc)}, exc_info=True)
            raise

    # 方法作用：验证市场研究类问法不构成联网授权。
    # Args: self - 测试实例；query - 待判定问法。
    # Returns: 无返回值，误判时由断言报告问法。
    @pytest.mark.parametrize(
        "query",
        [
            "",
            "   ",
            "市场研究",
            "行业研究怎么做",
            "竞品研究分析报告",
            "搜索一下销售数据",
            "帮我找找资料",
            "调研一下市场",
            "分析一下这份表格",
        ],
    )
    def test_explicit_intent_misses(self, query: str) -> None:
        """单独的市场研究/行业研究/竞品研究不构成外送授权。"""
        logger.debug("test_explicit_intent_misses 入口", extra={"query_chars": len(query)})
        try:
            # Arrange / Act
            from src.search.models import is_explicit_web_search

            # Assert
            assert is_explicit_web_search(query) is False
            logger.info("test_explicit_intent_misses 完成")
        except Exception as exc:
            logger.error("test_explicit_intent_misses 异常", extra={"error": str(exc)}, exc_info=True)
            raise


class TestSearchHitModel:
    """覆盖功能 32.2：引用条目字段边界与稳定 source_id。"""

    # 方法作用：验证 source_id 由 URL 稳定摘要生成且不同 URL 不碰撞。
    # Args: self - 测试实例。
    # Returns: 无返回值，摘要不稳定时由断言报告。
    def test_source_id_is_stable_url_digest(self) -> None:
        """同一 URL 必须得到同一 source_id，保证引用去重与回指稳定。"""
        logger.debug("test_source_id_is_stable_url_digest 入口")
        try:
            # Arrange
            from src.search.models import SearchHit

            # Act
            first = SearchHit(url="https://example.com/report", title="a", snippet="s")
            second = SearchHit(url="https://example.com/report", title="b", snippet="t")
            other = SearchHit(url="https://example.com/other", title="a", snippet="s")

            # Assert
            assert first.source_id == second.source_id
            assert first.source_id != other.source_id
            assert len(first.source_id) >= 12
            logger.info("test_source_id_is_stable_url_digest 完成")
        except Exception as exc:
            logger.error("test_source_id_is_stable_url_digest 异常", extra={"error": str(exc)}, exc_info=True)
            raise

    # 方法作用：验证标题至多 300 字符、摘要至多 2000 字符。
    # Args: self - 测试实例。
    # Returns: 无返回值，超限未截断时由断言报告。
    def test_title_and_snippet_are_truncated(self) -> None:
        """条目字段必须按上限截断，防止单个来源撑爆证据预算。"""
        logger.debug("test_title_and_snippet_are_truncated 入口")
        try:
            # Arrange
            from src.search.models import SearchHit

            # Act
            hit = SearchHit(
                url="https://example.com/long",
                title="标" * 350,
                snippet="文" * 2500,
            )

            # Assert
            assert len(hit.title) == 300
            assert len(hit.snippet) == 2000
            logger.info("test_title_and_snippet_are_truncated 完成")
        except Exception as exc:
            logger.error("test_title_and_snippet_are_truncated 异常", extra={"error": str(exc)}, exc_info=True)
            raise

    # 方法作用：验证检索时间为 UTC 时区。
    # Args: self - 测试实例。
    # Returns: 无返回值，时区不符时由断言报告。
    def test_retrieved_at_is_utc(self) -> None:
        """引用时间统一 UTC，跨时区研究层才能可信排序。"""
        logger.debug("test_retrieved_at_is_utc 入口")
        try:
            # Arrange / Act
            from src.search.models import SearchHit

            hit = SearchHit(url="https://example.com/t", title="t", snippet="s")

            # Assert
            assert hit.retrieved_at.tzinfo is not None
            assert hit.retrieved_at.utcoffset() == timezone.utc.utcoffset(None)
            logger.info("test_retrieved_at_is_utc 完成")
        except Exception as exc:
            logger.error("test_retrieved_at_is_utc 异常", extra={"error": str(exc)}, exc_info=True)
            raise

    # 方法作用：验证失败响应不携带条目且错误码来自固定集合。
    # Args: self - 测试实例。
    # Returns: 无返回值，结构不符时由断言报告。
    def test_response_failure_shape(self) -> None:
        """失败响应必须脱敏：无正文、无底层异常，只有固定错误码。"""
        logger.debug("test_response_failure_shape 入口")
        try:
            # Arrange / Act
            from src.search.models import SearchResponse

            response = SearchResponse(success=False, hits=[], error_code="no_tool", truncated=False)

            # Assert
            assert response.hits == []
            assert response.error_code == "no_tool"
            assert response.truncated is False
            logger.info("test_response_failure_shape 完成")
        except Exception as exc:
            logger.error("test_response_failure_shape 异常", extra={"error": str(exc)}, exc_info=True)
            raise


class TestUrlReferenceSafety:
    """覆盖功能 32.2：引用 URL 清洗边界（不抓取、只做静态判定）。"""

    # 方法作用：验证合法公网 HTTP(S) 引用被接受。
    # Args: self - 测试实例；url - 待判定 URL。
    # Returns: 无返回值，误拒合法引用时由断言报告。
    @pytest.mark.parametrize(
        "url",
        [
            "https://example.com/report?id=1",
            "http://news.example.org/a/b",
            "https://sub.domain.example.co.uk/page",
            "https://8.8.8.8/dns-query",
        ],
    )
    def test_public_http_urls_accepted(self, url: str) -> None:
        """公网 HTTP(S) 链接是唯一允许的引用形态。"""
        logger.debug("test_public_http_urls_accepted 入口", extra={"url_chars": len(url)})
        try:
            # Arrange / Act
            from src.search.models import is_safe_public_url

            # Assert
            assert is_safe_public_url(url) is True
            logger.info("test_public_http_urls_accepted 完成")
        except Exception as exc:
            logger.error("test_public_http_urls_accepted 异常", extra={"error": str(exc)}, exc_info=True)
            raise

    # 方法作用：验证凭证、危险 scheme、本地与字面私网地址全部拒绝。
    # Args: self - 测试实例；url - 待判定 URL。
    # Returns: 无返回值，误收危险引用时由断言报告。
    @pytest.mark.parametrize(
        "url",
        [
            "http://user:pass@example.com/x",
            "http://user@example.com/x",
            "javascript:alert(1)",
            "data:text/html;base64,PA==",
            "file:///etc/passwd",
            "ftp://example.com/x",
            "http://localhost/admin",
            "http://app.localhost/admin",
            "http://intranet:8080/x",
            "http://127.0.0.1:9000/x",
            "http://10.1.2.3/x",
            "http://172.16.0.9/x",
            "http://192.168.1.5/x",
            "http://169.254.169.254/latest/meta-data",
            "http://[::1]/x",
            "http://[fe80::1]/x",
            "http://0x7f.0.0.1/x",
            "https:///missing-host",
            "",
            "not a url",
        ],
    )
    def test_dangerous_urls_rejected(self, url: str) -> None:
        """SSRF 与凭证泄漏面必须在引用层直接拒绝，不做网络解析。"""
        logger.debug("test_dangerous_urls_rejected 入口", extra={"url_chars": len(url)})
        try:
            # Arrange / Act
            from src.search.models import is_safe_public_url

            # Assert
            assert is_safe_public_url(url) is False
            logger.info("test_dangerous_urls_rejected 完成")
        except Exception as exc:
            logger.error("test_dangerous_urls_rejected 异常", extra={"error": str(exc)}, exc_info=True)
            raise
