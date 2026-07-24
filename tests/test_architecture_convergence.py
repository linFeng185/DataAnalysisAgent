"""第二轮架构收口契约测试。"""

from __future__ import annotations

import ast
import logging
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock


logger = logging.getLogger(__name__)
ROOT = Path(__file__).resolve().parents[1]


# 方法作用：收集指定模块顶层导入但从未读取的符号。
# Args: relative_path - 相对项目根目录的 Python 文件路径。
# Returns: 包含行号和符号名的未使用导入列表。
def _unused_module_imports(relative_path: str) -> list[str]:
    logger.debug("收集未使用模块导入入口", extra={"path": relative_path})
    try:
        tree = ast.parse((ROOT / relative_path).read_text(encoding="utf-8"))
        imported: dict[str, int] = {}
        for node in tree.body:
            if isinstance(node, ast.Import):
                for alias in node.names:
                    imported[alias.asname or alias.name.split(".", 1)[0]] = node.lineno
            elif isinstance(node, ast.ImportFrom) and node.module != "__future__":
                for alias in node.names:
                    imported[alias.asname or alias.name] = node.lineno
        loaded = {
            node.id
            for node in ast.walk(tree)
            if isinstance(node, ast.Name) and isinstance(node.ctx, ast.Load)
        }
        result = [
            f"{relative_path}:{line}:{name}"
            for name, line in sorted(imported.items())
            if name not in loaded
        ]
    except Exception as exc:
        logger.error("收集未使用模块导入失败: %s", exc, exc_info=True)
        raise
    logger.info("收集未使用模块导入完成", extra={"path": relative_path, "count": len(result)})
    return result


class TestDependencyConvergence:
    """覆盖功能 20.17-20.18：配置和 LLM 只有一条依赖路径。"""

    # 方法作用：验证兼容配置 getter 返回当前 Context 的同一 Settings 对象。
    # Args: self - pytest 测试类实例。
    # Returns: 无返回值，断言失败时由 pytest 报告。
    def test_get_settings_delegates_to_current_app_context(self) -> None:
        """请求级覆盖不得被新建 Settings 静默替换。"""
        logger.debug("test_get_settings_delegates_to_current_app_context 入口")
        from src.app_context import AppContext, use_app_context
        from src.config import get_settings

        settings = SimpleNamespace(multi_tenant=False, marker="context-settings")
        with use_app_context(AppContext(settings)):
            result = get_settings()

        assert result is settings
        logger.info("test_get_settings_delegates_to_current_app_context 完成")

    # 方法作用：验证任务 LLM 通过统一 Provider 工厂创建本地模型。
    # Args: self - pytest 测试类实例；monkeypatch - pytest 补丁工具。
    # Returns: 无返回值，断言失败时由 pytest 报告。
    def test_task_llm_delegates_to_provider(self, monkeypatch) -> None:
        """本地 OpenAI-compatible 模型也不能绕过 Provider 层。"""
        logger.debug("test_task_llm_delegates_to_provider 入口")
        import src.llm.client as client_module
        from src.app_context import AppContext, use_app_context

        chat_model = object()
        provider = SimpleNamespace(get_chat_model=lambda **kwargs: chat_model)
        calls: list[dict] = []

        # 方法作用：记录任务路由传给统一 Provider 入口的参数。
        # Args: args - 兼容位置参数；kwargs - Provider 解析参数。
        # Returns: 测试 Provider。
        def fake_get_provider(*args, **kwargs):
            logger.debug("fake_get_provider 入口", extra={"kwargs": sorted(kwargs)})
            calls.append(dict(kwargs))
            logger.info("fake_get_provider 完成")
            return provider

        monkeypatch.setattr(client_module, "get_provider", fake_get_provider)
        settings = SimpleNamespace(
            multi_tenant=False,
            llm_provider="openai",
            llm_model="remote-model",
            llm_temperature=0.0,
            llm_max_tokens=4096,
            llm_timeout=60,
            openai_api_key="",
            openai_base_url="",
            local_llm_model="local-model",
            local_llm_base_url="http://127.0.0.1:11434/v1",
            local_llm_api_key="local",
            local_llm_timeout=5,
            llm_remote_tasks="generate_sql",
            llm_allow_remote_fallback=False,
        )

        with use_app_context(AppContext(settings)):
            result = client_module.get_task_llm("classify_intent", reasoning=False)

        assert result is chat_model
        assert calls == [{
            "model_id": "local-model",
            "provider_name": "openai",
            "base_url": "http://127.0.0.1:11434/v1",
            "api_key": "local",
        }]
        logger.info("test_task_llm_delegates_to_provider 完成")


class TestSQLAndFailurePolicy:
    """覆盖功能 20.20、20.22：SQL 与异常策略统一。"""

    # 方法作用：验证 DB Tool 使用 Registry 的真实 SQL Server 方言。
    # Args: self - pytest 测试类实例；monkeypatch - pytest 补丁工具。
    # Returns: 无返回值，断言失败时由 pytest 报告。
    async def test_db_executor_uses_resolved_datasource_dialect(self, monkeypatch) -> None:
        """SQL Server TOP/方括号语法不能再被 MySQL 校验器误杀。"""
        logger.debug("test_db_executor_uses_resolved_datasource_dialect 入口")
        import src.connectors.registry as connector_registry
        import src.datasource.registry as datasource_registry
        from src.tools.db_executor import DBExecutorTool

        connector = SimpleNamespace(
            execute=AsyncMock(return_value=[{"id": 1}]),
            execute_bounded=AsyncMock(return_value=([{"id": 1}], False)),
            explain=AsyncMock(return_value={"valid": True, "errors": []}),
            attach_engine=lambda engine: connector,
        )
        datasource = SimpleNamespace(
            name="mssql-main",
            dialect="mssql",
            engine=object(),
            connector=connector,
        )
        registry = SimpleNamespace(resolve=AsyncMock(return_value=datasource))
        monkeypatch.setattr(datasource_registry, "get_registry", lambda: registry)
        monkeypatch.setattr(connector_registry, "create_connector", lambda config: connector)

        result = await DBExecutorTool()._arun(
            "SELECT TOP 1 [id] FROM [users]",
            "mssql-main",
        )

        assert result["success"] is True
        assert result["data"] == [{"id": 1}]
        logger.info("test_db_executor_uses_resolved_datasource_dialect 完成")

    # 方法作用：验证 SQL 注释中的危险词不会触发误报且真实写语句仍被 AST 阻断。
    # Args: self - pytest 测试类实例。
    # Returns: 无返回值，断言失败时由 pytest 报告。
    def test_sql_comments_cannot_bypass_or_false_positive(self) -> None:
        """危险词判断必须基于无注释 AST，而不是原始字符串。"""
        logger.debug("test_sql_comments_cannot_bypass_or_false_positive 入口")
        from src.graph.nodes.layer3_validate import validate_readonly_sql

        assert validate_readonly_sql("SELECT 1 -- DROP TABLE users", "postgres") == []
        assert validate_readonly_sql("SELECT /* DELETE FROM users */ 1", "postgres") == []
        assert validate_readonly_sql("DROP /* comment */ TABLE users", "postgres")
        logger.info("test_sql_comments_cannot_bypass_or_false_positive 完成")

    # 方法作用：验证安全、数据库与可用性域的异常模式集中声明。
    # Args: self - pytest 测试类实例。
    # Returns: 无返回值，断言失败时由 pytest 报告。
    def test_failure_policy_matrix_is_explicit(self) -> None:
        """关键边界不得继续由各函数临时决定 fail-open/closed。"""
        logger.debug("test_failure_policy_matrix_is_explicit 入口")
        from src.failure_policy import (
            FailureDomain,
            FailureMode,
            fallback_allowed,
            get_failure_mode,
        )

        assert get_failure_mode(FailureDomain.SQL_SECURITY) is FailureMode.FAIL_CLOSED
        assert get_failure_mode(FailureDomain.DATABASE) is FailureMode.FAIL_CLOSED
        assert get_failure_mode(FailureDomain.LLM) is FailureMode.FAIL_OPEN
        assert get_failure_mode(FailureDomain.KNOWLEDGE) is FailureMode.FAIL_OPEN
        assert get_failure_mode(FailureDomain.DATA_PROCESSOR) is FailureMode.FAIL_OPEN
        assert fallback_allowed(FailureDomain.LLM) is True
        assert fallback_allowed(FailureDomain.KNOWLEDGE) is True
        assert fallback_allowed(FailureDomain.DATA_PROCESSOR) is True
        assert fallback_allowed(FailureDomain.SQL_SECURITY) is False
        logger.info("test_failure_policy_matrix_is_explicit 完成")

    # 方法作用：验证 LangChain SQL Tool 不再维护独立安全解析入口。
    # Args: self - pytest 测试类实例。
    # Returns: 无返回值，断言失败时由 pytest 报告。
    def test_sqlglot_tool_delegates_to_unified_validation(self) -> None:
        """工具层必须调用 security.sql_execution，不能自行决定只读安全。"""
        logger.debug("test_sqlglot_tool_delegates_to_unified_validation 入口")
        source = (ROOT / "src/tools/sqlglot_validator.py").read_text(encoding="utf-8")

        assert "from src.security.sql_execution import" in source
        assert "sqlglot.parse(" not in source
        assert "sqlglot.transpile(" not in source
        logger.info("test_sqlglot_tool_delegates_to_unified_validation 完成")


class TestResourceOwnership:
    """覆盖功能 20.21、20.23：PG 与向量资源所有权。"""

    # 方法作用：验证运行时代码不新增绕过共享连接池的 asyncpg.connect。
    # Args: self - pytest 测试类实例。
    # Returns: 无返回值，断言失败时由 pytest 报告。
    def test_raw_asyncpg_connect_is_limited_to_bootstrap_exceptions(self) -> None:
        """只有迁移和 Checkpointer 自动建库允许独立连接。"""
        logger.debug("test_raw_asyncpg_connect_is_limited_to_bootstrap_exceptions 入口")
        allowed = {"src/db/migrations.py", "src/memory/checkpointer.py"}
        offenders: list[str] = []
        for path in (ROOT / "src").rglob("*.py"):
            relative_path = path.relative_to(ROOT).as_posix()
            tree = ast.parse(path.read_text(encoding="utf-8"))
            for node in ast.walk(tree):
                if not isinstance(node, ast.Call) or not isinstance(node.func, ast.Attribute):
                    continue
                if (
                    isinstance(node.func.value, ast.Name)
                    and node.func.value.id == "asyncpg"
                    and node.func.attr == "connect"
                    and relative_path not in allowed
                ):
                    offenders.append(f"{relative_path}:{node.lineno}")

        assert offenders == []
        logger.info("test_raw_asyncpg_connect_is_limited_to_bootstrap_exceptions 完成")

    # 方法作用：验证知识调用方不再持有 ChromaDB 私有 Collection。
    # Args: self - pytest 测试类实例。
    # Returns: 无返回值，断言失败时由 pytest 报告。
    def test_vector_store_owns_chroma_resources(self) -> None:
        """SchemaManager 和业务规则必须只依赖 VectorStore 公共接口。"""
        logger.debug("test_vector_store_owns_chroma_resources 入口")
        schema_source = (ROOT / "src/knowledge/schema_manager.py").read_text(encoding="utf-8")
        factory_source = (ROOT / "src/memory/vector_store.py").read_text(encoding="utf-8")
        rules_source = (ROOT / "src/knowledge/business_rules.py").read_text(encoding="utf-8")

        assert "_collection" not in schema_source
        assert "_ensure_initialized" not in schema_source
        assert "import chromadb" not in schema_source
        assert "sm._collection" not in factory_source
        assert "sm._ensure_initialized" not in factory_source
        assert "self._collection" not in rules_source
        logger.info("test_vector_store_owns_chroma_resources 完成")

    # 方法作用：验证启动编排和 API 不跨模块读取资源管理器私有状态。
    # Args: self - pytest 测试类实例。
    # Returns: 无返回值，断言失败时由 pytest 报告。
    def test_resource_consumers_use_public_lifecycle_interfaces(self) -> None:
        """启动预热与 MCP 路由必须通过公开方法访问资源。"""
        logger.debug("test_resource_consumers_use_public_lifecycle_interfaces 入口")
        bootstrap = (ROOT / "src/bootstrap.py").read_text(encoding="utf-8")
        mcp_routes = (ROOT / "src/api/routes/mcp.py").read_text(encoding="utf-8")
        skill_routes = (ROOT / "src/api/routes/skills.py").read_text(encoding="utf-8")
        structured_query = (ROOT / "src/knowledge/structured_query.py").read_text(
            encoding="utf-8",
        )

        assert "get_file_store()._ensure" not in bootstrap
        assert "get_schema_manager()._ensure_initialized" not in bootstrap
        assert "mgr._server_scopes" not in mcp_routes
        assert "manager._parse_skill_manifest" not in skill_routes
        assert "self.adapter._" not in structured_query
        logger.info("test_resource_consumers_use_public_lifecycle_interfaces 完成")


class TestStatePersistenceBoundary:
    """覆盖功能 20.24：会话状态与请求瞬态状态分层。"""

    # 方法作用：验证大对象和安全上下文使用不落 checkpoint 的 Channel。
    # Args: self - pytest 测试类实例。
    # Returns: 无返回值，断言失败时由 pytest 报告。
    def test_request_fields_use_untracked_channels(self) -> None:
        """结果、Schema、权限和最终响应不得写入每个 checkpoint。"""
        logger.debug("test_request_fields_use_untracked_channels 入口")
        from langgraph.channels import UntrackedValue
        from langgraph.graph import StateGraph
        from src.graph.state import AnalysisState

        graph = StateGraph(AnalysisState)
        transient_fields = {
            "tenant_id", "user_id", "user_role", "datasource_access",
            "allowed_columns", "row_filter_sql", "resolved_schema", "relevant_tables",
            "generated_sql", "multi_source_results", "query_result_sample",
            "analysis_result", "chart_config", "final_response",
        }

        assert all(isinstance(graph.channels[field], UntrackedValue) for field in transient_fields)
        assert not isinstance(graph.channels["conversation_history"], UntrackedValue)
        assert not isinstance(graph.channels["messages"], UntrackedValue)
        logger.info("test_request_fields_use_untracked_channels 完成")

    # 方法作用：验证轻量上一轮快照不复制结果、分析和图表大对象。
    # Args: self - pytest 测试类实例。
    # Returns: 无返回值，断言失败时由 pytest 报告。
    def test_previous_turn_snapshot_is_compact(self) -> None:
        """富结果只能由 HistoryStore 持久化一次。"""
        logger.debug("test_previous_turn_snapshot_is_compact 入口")
        from src.graph.nodes.prepare_turn import build_turn_snapshot

        snapshot = build_turn_snapshot({
            "user_query": "查询订单",
            "datasource": "demo",
            "generated_sql": "SELECT * FROM orders",
            "query_result_sample": [{"payload": "x" * 1000}],
            "multi_source_results": [{"data": [{"payload": "y" * 1000}]}],
            "analysis_result": {"summary": "z" * 1000},
            "chart_config": {"option": {"series": ["w" * 1000]}},
        })

        forbidden = {
            "query_result_sample", "query_result_statistics", "multi_source_results",
            "analysis_result", "chart_config",
        }
        assert forbidden.isdisjoint(snapshot)
        assert snapshot["generated_sql"] == "SELECT * FROM orders"
        assert snapshot["result_available"] is True
        logger.info("test_previous_turn_snapshot_is_compact 完成")


class TestRouteImports:
    """覆盖功能 20.19：领域路由只保留显式最小导入。"""

    # 方法作用：验证复制模板残留的模块级导入已经清理。
    # Args: self - pytest 测试类实例。
    # Returns: 无返回值，断言失败时由 pytest 报告。
    def test_domain_routes_have_no_unused_module_imports(self) -> None:
        """新增 Schema 不应要求同步修改无关路由的导入块。"""
        logger.debug("test_domain_routes_have_no_unused_module_imports 入口")
        targets = (
            "src/api/routes/management.py",
            "src/api/routes/mcp.py",
            "src/api/routes/schema.py",
            "src/api/routes/session.py",
        )
        offenders = [item for target in targets for item in _unused_module_imports(target)]

        assert offenders == []
        logger.info("test_domain_routes_have_no_unused_module_imports 完成")
