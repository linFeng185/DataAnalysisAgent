"""覆盖 18.8.1 的响应、跨源契约和持久化恢复边界。"""

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from src.graph.contracts import SourceQueryResult
from src.graph.nodes.build_response import build_response_node
from src.graph.nodes.restore_previous_result import restore_previous_result_node

MASKING = {"applied": True, "fields": [{"column": "email", "categories": ["邮箱"], "masked_count": 1}]}


class TestMaskingGraph:
    """覆盖功能 18.8.1：补查、多源和历史恢复不丢标识。"""

    # 方法作用：验证最终响应同时传递主查询与补查元数据。
    # Args: self - 实例。
    # Returns: 无返回值。
    @pytest.mark.asyncio
    async def test_build_response_preserves_metadata(self) -> None:
        """结果构造只传递真实元数据，不重新对星号数据执行脱敏。"""
        result = await build_response_node({
            "query_result_sample": [{"email": "u***@example.com"}],
            "query_result_statistics": {"masking": MASKING},
            "supplemental_evidence": [{"success": True, "masking": MASKING}],
        })
        response = result["final_response"]
        assert response["masking"] == MASKING
        assert response["supplemental_masking"] == [MASKING]

    # 方法作用：验证没有元数据的历史数据不会从星号推断脱敏状态。
    # Args: self - 实例。
    # Returns: 无返回值。
    @pytest.mark.asyncio
    async def test_legacy_response_no_false_masking(self) -> None:
        """未升级的历史结果应保持未知，不误报已脱敏。"""
        result = await build_response_node({"query_result_sample": [{"text": "hello***"}]})
        assert result["final_response"]["masking"] == {"applied": False, "fields": []}

    # 方法作用：验证来源标准契约接受且保留脱敏字段。
    # Args: self - 实例。
    # Returns: 无返回值。
    def test_source_contract_keeps_masking(self) -> None:
        """跨源序列化不应丢弃安全展示元数据。"""
        source = SourceQueryResult(datasource="sales", success=True, sql="SELECT 1", masking=MASKING)
        assert source.to_legacy_dict()["masking"] == MASKING

    # 方法作用：验证历史权威富结果恢复后可再次返回同样标识。
    # Args: self - 实例；monkeypatch - 边界补丁。
    # Returns: 无返回值。
    @pytest.mark.asyncio
    async def test_restore_rich_history_masking(self, monkeypatch) -> None:
        """checkpoint 不保存富结果，脱敏字段应从 final_result 恢复。"""
        import src.memory.history_store as history_module

        history = SimpleNamespace(list_session=AsyncMock(return_value=[{"final_result": {
            "data": [{"email": "u***@example.com"}], "masking": MASKING,
            "supplemental_masking": [MASKING],
        }}]))
        monkeypatch.setattr(history_module, "get_history_store", lambda: history)
        restored = await restore_previous_result_node({
            "datasource": "sales", "selected_datasources": ["sales"], "session_id": "session-1",
            "previous_turn_snapshot": {"datasource": "sales", "selected_datasources": ["sales"], "result_available": True},
        })
        assert restored["query_result_statistics"]["masking"] == MASKING
        rebuilt = await build_response_node(restored)
        assert rebuilt["final_response"]["supplemental_masking"] == [MASKING]

    # 方法作用：验证 SQL 节点在三种数据库方言下均输出真实脱敏元数据。
    # Args: self - 实例；monkeypatch - 边界补丁；dialect - 数据库方言。
    # Returns: 无返回值。
    @pytest.mark.asyncio
    @pytest.mark.parametrize("dialect", ["clickhouse", "mysql", "postgres"])
    async def test_execute_sql_emits_metadata(self, monkeypatch, dialect) -> None:
        """隔离数据库和审计写入，仅验证执行结果到状态的安全边界。"""
        import src.graph.nodes.execute_sql as execute_module
        import src.security.sql_execution as execution_module

        monkeypatch.setattr(execute_module, "get_tenant_policy", lambda: SimpleNamespace(datasource_isolation_enabled=False))
        monkeypatch.setattr(execute_module, "get_settings", lambda: SimpleNamespace(max_result_rows=100))
        monkeypatch.setattr(execute_module, "_record_query_audit", AsyncMock())
        execution = AsyncMock(return_value=SimpleNamespace(
            sql="SELECT 'a@example.com' AS email", success=True,
            data=[{"email": "a@example.com"}], truncated=False,
        ))
        monkeypatch.setattr(execution_module, "validate_and_execute_sql", execution)
        result = await execute_module.execute_sql_node({
            "generated_sql": "SELECT 'a@example.com' AS email", "dialect": dialect,
            "datasource": "sales", "tenant_id": 3, "user_id": 7,
            "request_rate_limit_checked": True,
        })
        assert result["query_result_sample"][0]["email"] == "a***@example.com"
        assert result["query_result_statistics"]["masking"]["applied"] is True
        assert execution.await_args.args[2] == dialect
