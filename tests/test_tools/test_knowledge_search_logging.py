"""知识检索工具日志分级回归：高频成功静默、异常保留堆栈、不泄露正文。"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

from src.knowledge.asset_models import Evidence
from src.security.tenant_policy import RequestIdentity


# 方法作用：构造一条带敏感正文的知识证据，验证日志不回传正文。
# Args: source_id - 证据标识。
# Returns: 示例 Evidence。
def _evidence(source_id: str = "doc-log") -> Evidence:
    return Evidence(
        content="synthetic-secret-metric-definition",
        source_id=source_id,
        version="v1",
        locator={"page": 1},
        scores={"relevance": 0.9},
        metadata={"category": "metric", "source": "manual_doc"},
    )


# 方法作用：构造测试用的可信请求身份。
# Args: 无。
# Returns: RequestIdentity。
def _identity() -> RequestIdentity:
    return RequestIdentity(tenant_id=2, user_id=7, role="analyst")


# 方法作用：收集 logger 上指定方法名的调用关键字参数。
# Args: logger - MagicMock 日志器；method - 日志方法名。
# Returns: 调用关键字参数列表。
def _calls(logger: MagicMock, method: str) -> list[dict]:
    return [call.kwargs for call in getattr(logger, method).call_args_list]


class TestKnowledgeSearchLogging:
    """覆盖 27.1.3/27.1.6：工具高频路径不得新增 INFO，异常必须可诊断。"""

    # 方法作用：成功与构建路径只留 DEBUG，且任何级别都不包含查询或正文。
    # Args: monkeypatch - pytest 内置夹具
    # Returns: 无返回值，断言失败由 pytest 报告
    async def test_success_path_logs_no_info_and_no_content(self, monkeypatch) -> None:
        """成功与构建路径只留 DEBUG，且任何级别都不包含查询或正文。"""
        # Arrange
        import src.tools.knowledge_search as module

        logger = MagicMock()
        monkeypatch.setattr(module, "logger", logger)
        monkeypatch.setattr(module, "search_knowledge", AsyncMock(return_value=[_evidence()]))
        tools = module.build_knowledge_tools(
            identity=_identity(),
            store_provider=AsyncMock(return_value=SimpleNamespace()),
        )

        # Act
        payload = await module.execute_knowledge_search(
            tools[0], {"query": "synthetic-secret-query"}, identity=_identity(),
        )

        # Assert
        assert payload["status"] == "ok"
        assert not _calls(logger, "info"), "成功路径不得新增 INFO 日志"
        logged = repr(logger.mock_calls)
        assert "synthetic-secret-query" not in logged
        assert "synthetic-secret-metric-definition" not in logged

    # 方法作用：正常构建工具只记 DEBUG，避免每次调用都刷 INFO。
    # Args: monkeypatch - pytest 内置夹具
    # Returns: 无返回值，断言失败由 pytest 报告
    def test_build_path_for_trusted_identity_has_no_info(self, monkeypatch) -> None:
        """正常构建工具只记 DEBUG，避免每次调用都刷 INFO。"""
        # Arrange
        import src.tools.knowledge_search as module

        logger = MagicMock()
        monkeypatch.setattr(module, "logger", logger)

        # Act
        module.build_knowledge_tools(identity=_identity())

        # Assert
        assert not _calls(logger, "info")

    # 方法作用：故障必须保留异常堆栈供诊断，同时不得写入查询或正文。
    # Args: monkeypatch - pytest 内置夹具
    # Returns: 无返回值，断言失败由 pytest 报告
    async def test_failure_keeps_exception_and_no_content(self, monkeypatch) -> None:
        """故障必须保留异常堆栈供诊断，同时不得写入查询或正文。"""
        # Arrange
        import src.tools.knowledge_search as module

        logger = MagicMock()
        monkeypatch.setattr(module, "logger", logger)
        monkeypatch.setattr(
            module, "search_knowledge",
            AsyncMock(side_effect=RuntimeError("synthetic-backend-failure")),
        )
        tools = module.build_knowledge_tools(
            identity=_identity(),
            store_provider=AsyncMock(return_value=SimpleNamespace()),
        )

        # Act
        payload = await module.execute_knowledge_search(
            tools[0], {"query": "synthetic-secret-query"}, identity=_identity(),
        )

        # Assert
        assert payload["status"] == "unavailable"
        errors = _calls(logger, "error")
        assert errors and errors[0].get("exc_info") is True
        assert errors[0].get("error_type") == "RuntimeError"
        assert "synthetic-secret-query" not in repr(logger.mock_calls)

    # 方法作用：拒绝路径用 WARNING 记录原因标识，不记录模型传入的查询文本。
    # Args: monkeypatch - pytest 内置夹具
    # Returns: 无返回值，断言失败由 pytest 报告
    async def test_denied_request_logs_warning_without_query(self, monkeypatch) -> None:
        """拒绝路径用 WARNING 记录原因标识，不记录模型传入的查询文本。"""
        # Arrange
        import src.tools.knowledge_search as module

        logger = MagicMock()
        monkeypatch.setattr(module, "logger", logger)
        monkeypatch.setattr(
            module, "resolve_datasource_authorization", AsyncMock(return_value=False), raising=False,
        )
        tools = module.build_knowledge_tools(
            identity=_identity(),
            store_provider=AsyncMock(return_value=SimpleNamespace()),
        )

        # Act
        payload = await module.execute_knowledge_search(
            tools[0],
            {"query": "synthetic-secret-query", "datasource": "payments"},
            identity=_identity(),
        )

        # Assert
        assert payload["status"] == "forbidden"
        warnings = _calls(logger, "warning")
        assert warnings and warnings[-1].get("reason") == "datasource_unauthorized"
        assert "synthetic-secret-query" not in repr(logger.mock_calls)
