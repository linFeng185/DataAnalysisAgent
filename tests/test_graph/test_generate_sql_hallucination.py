"""LLM 生成 SQL 的表名幻觉拦截回归测试。"""

from __future__ import annotations

import logging
from unittest.mock import AsyncMock

from src.graph.nodes.generate_sql import _check_table_hallucination


logger = logging.getLogger(__name__)


class TestGenerateSQLHallucination:
    """覆盖功能 12.1.6：只允许 SQL 引用 Schema 或 CTE 中存在的表。"""

    # 方法作用：验证已知表不会被误判为幻觉。
    # Args: self - pytest 测试类实例。
    # Returns: 无返回值，断言失败时由 pytest 报告。
    def test_known_table_is_allowed(self) -> None:
        """正常路径应接受 Schema 中存在的表。"""
        logger.debug("test_known_table_is_allowed 入口")
        tables = [{"name": "orders"}]

        result = _check_table_hallucination("SELECT id FROM orders", tables)

        assert result == []
        logger.info("test_known_table_is_allowed 完成")

    # 方法作用：验证不存在的物理表会被明确返回。
    # Args: self - pytest 测试类实例。
    # Returns: 无返回值，断言失败时由 pytest 报告。
    def test_unknown_table_is_rejected(self) -> None:
        """错误路径必须拦截 LLM 编造的表名。"""
        logger.debug("test_unknown_table_is_rejected 入口")
        tables = [{"name": "orders"}]

        result = _check_table_hallucination(
            "SELECT * FROM invented_table",
            tables,
        )

        assert result == ["invented_table"]
        logger.info("test_unknown_table_is_rejected 完成", extra={"unknown": result})

    # 方法作用：验证 CTE 别名不被当成数据库物理表。
    # Args: self - pytest 测试类实例。
    # Returns: 无返回值，断言失败时由 pytest 报告。
    def test_cte_alias_is_allowed(self) -> None:
        """边界路径只校验 CTE 内部引用的真实表。"""
        logger.debug("test_cte_alias_is_allowed 入口")
        tables = [{"name": "orders"}]
        sql = "WITH recent AS (SELECT id FROM orders) SELECT id FROM recent"

        result = _check_table_hallucination(sql, tables)

        assert result == []
        logger.info("test_cte_alias_is_allowed 完成")

    # 方法作用：验证多语句 SQL 中的未知表均被去重收集。
    # Args: self - pytest 测试类实例。
    # Returns: 无返回值，断言失败时由 pytest 报告。
    def test_multiple_statements_collect_unknown_tables(self) -> None:
        """多语句边界应稳定返回去重后的未知表。"""
        logger.debug("test_multiple_statements_collect_unknown_tables 入口")
        tables = [{"name": "orders"}]

        result = _check_table_hallucination(
            "SELECT * FROM missing_a; SELECT * FROM missing_a JOIN missing_b ON 1=1",
            tables,
        )

        assert result == ["missing_a", "missing_b"]
        logger.info("test_multiple_statements_collect_unknown_tables 完成")

    # 方法作用：验证 SQL AST 解析失败时表名校验默认阻断执行。
    # Args: self - pytest 测试类实例；monkeypatch - pytest 补丁工具。
    # Returns: 无返回值，断言失败时由 pytest 报告。
    def test_parser_failure_is_closed(self, monkeypatch) -> None:
        """解析器异常不得被当作无幻觉表名而放行。"""
        logger.debug("test_parser_failure_is_closed 入口")
        import sqlglot

        monkeypatch.setattr(
            sqlglot,
            "parse",
            lambda *args, **kwargs: (_ for _ in ()).throw(RuntimeError("parse failed")),
        )
        result = _check_table_hallucination(
            "SELECT id FROM orders",
            [{"name": "orders", "columns": [{"name": "id"}]}],
        )

        assert result
        assert "解析失败" in result[0]
        logger.info("test_parser_failure_is_closed 完成")

    # 方法作用：验证 MySQL 反引号引用的已知表不会被误判为幻觉。
    # Args: self - pytest 测试类实例。
    # Returns: 无返回值，断言失败时由 pytest 报告。
    def test_mysql_quoted_known_table_is_allowed(self) -> None:
        """MySQL 方言解析应接受反引号包裹的已知表和字段。"""
        logger.debug("test_mysql_quoted_known_table_is_allowed 入口")
        # Arrange
        sql = (
            "SELECT `f_date`, `f_value` "
            "FROM `mogu_daily_electricity_consumption` ORDER BY `f_date`"
        )
        tables = [{"name": "mogu_daily_electricity_consumption"}]

        # Act
        result = _check_table_hallucination(sql, tables, "mysql")

        # Assert
        assert result == []
        logger.info("test_mysql_quoted_known_table_is_allowed 完成")

    # 方法作用：验证 SQL 生成节点把当前 MySQL 方言传给表名幻觉校验。
    # Args: self - pytest 测试类实例；monkeypatch - pytest 运行时替换工具。
    # Returns: 无返回值，断言节点输出合法 SQL 且无幻觉错误。
    async def test_generate_node_forwards_mysql_dialect(self, monkeypatch) -> None:
        """节点处理 MySQL 反引号 SQL 时应使用真实方言完成二次校验。"""
        logger.debug("test_generate_node_forwards_mysql_dialect 入口")
        # Arrange
        import src.graph.nodes.generate_sql as generate_module

        sql = (
            "SELECT `f_date`, `f_value` "
            "FROM `mogu_daily_electricity_consumption` ORDER BY `f_date`"
        )
        llm_generate = AsyncMock(return_value=(sql, "", ""))
        monkeypatch.setattr(generate_module, "is_llm_available", lambda: True)
        monkeypatch.setattr(generate_module, "_llm_generate", llm_generate)
        state = {
            "user_query": "列出每日用电量",
            "dialect": "mysql",
            "relevant_tables": [{
                "name": "mogu_daily_electricity_consumption",
                "columns": [
                    {"name": "f_date", "type": "date"},
                    {"name": "f_value", "type": "decimal"},
                ],
            }],
        }

        # Act
        result = await generate_module.generate_sql_node(state, {})

        # Assert
        assert result["generated_sql"] == sql
        assert result["validation_errors"] == []
        assert llm_generate.await_args.kwargs["dialect"] == "mysql"
        logger.info("test_generate_node_forwards_mysql_dialect 完成")
