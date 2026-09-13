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

    # 方法作用：验证表名白名单完整包含所有已检索表且不依赖详细 Schema 长度。
    # Args: self - pytest 测试类实例。
    # Returns: 无返回值，断言失败时由 pytest 报告。
    def test_compact_table_whitelist_contains_every_table(self) -> None:
        """详细 Schema 即使被预算裁剪，合法表名仍必须完整进入独立白名单。"""
        # Arrange
        from src.graph.nodes.generate_sql import build_table_whitelist_context

        tables = [{"name": f"table_{index}", "columns": []} for index in range(27)]

        # Act
        result = build_table_whitelist_context(tables)

        # Assert
        assert all(f"table_{index}" in result for index in range(27))
        assert result.count("table_") == 27

    # 方法作用：验证幻觉重试只回注未知表和合法白名单。
    # Args: self - pytest 测试类实例。
    # Returns: 无返回值，断言失败时由 pytest 报告。
    def test_hallucination_retry_context_excludes_failed_sql(self) -> None:
        """数千字符失败 SQL 不得再次挤占 Schema 预算并强化错误表名。"""
        # Arrange
        from src.graph.nodes.generate_sql import build_retry_context

        failed_sql = "SELECT * FROM invented_table " + "x" * 4000
        state = {
            "generated_sql": failed_sql,
            "validation_errors": [{
                "type": "hallucination",
                "unknown_tables": ["invented_table"],
            }],
        }

        # Act
        result = build_retry_context(state, 1, [{"name": "orders"}])

        # Assert
        assert failed_sql not in result
        assert "invented_table" in result
        assert "orders" in result

    # 方法作用：验证第二次表名幻觉后停止无效重试。
    # Args: self - pytest 测试类实例。
    # Returns: 无返回值，断言失败时由 pytest 报告。
    def test_second_hallucination_stops_retry(self) -> None:
        """同类幻觉最多执行初次生成加一次纠正。"""
        # Arrange
        from src.graph.workflow import after_generate_sql

        state = {
            "generated_sql": "SELECT * FROM invented_table",
            "retry_count": 2,
            "validation_errors": [{"type": "hallucination"}],
        }

        # Act / Assert
        assert after_generate_sql(state) == "build_response"


class TestSystemPseudoTableWhitelist:
    """覆盖系统伪表白名单：dual 是合法引用，不得判为幻觉（线上重试耗尽失败回归）。"""

    # 方法作用：验证 Oracle 下 FROM dual 不判为幻觉。
    # Args: self - pytest 用例实例。
    # Returns: 无返回值，断言失败由 pytest 报告。
    def test_oracle_dual_is_allowed(self) -> None:
        """线上真实形态：SELECT ... FROM dual 被误拦，重试耗尽后单元失败。"""
        logger.debug("test_oracle_dual_is_allowed 入口")
        tables = [{"name": "orders"}]

        result = _check_table_hallucination(
            "SELECT TO_CHAR(SYSDATE - 90, 'YYYY-MM-DD') AS d FROM dual",
            tables,
            dialect="oracle",
        )

        assert result == []
        logger.info("test_oracle_dual_is_allowed 完成")

    # 方法作用：验证 MySQL 下 dual 同样放行。
    # Args: self - pytest 用例实例。
    # Returns: 无返回值，断言失败由 pytest 报告。
    def test_mysql_dual_is_allowed(self) -> None:
        """MySQL 也支持 SELECT ... FROM dual。"""
        logger.debug("test_mysql_dual_is_allowed 入口")
        tables = [{"name": "orders"}]

        result = _check_table_hallucination("SELECT 1 FROM dual", tables, dialect="mysql")

        assert result == []
        logger.info("test_mysql_dual_is_allowed 完成")

    # 方法作用：验证白名单按方言隔离，其他方言的 dual 仍判为幻觉。
    # Args: self - pytest 用例实例。
    # Returns: 无返回值，断言失败由 pytest 报告。
    def test_dual_still_rejected_in_other_dialects(self) -> None:
        """PostgreSQL 没有 dual 表，引用它确实是幻觉，不能跨库误放行。"""
        logger.debug("test_dual_still_rejected_in_other_dialects 入口")
        tables = [{"name": "orders"}]

        result = _check_table_hallucination("SELECT 1 FROM dual", tables, dialect="postgres")

        assert result == ["dual"]
        logger.info("test_dual_still_rejected_in_other_dialects 完成")

    # 方法作用：验证 dual 与真实幻觉表并存时仍只收集幻觉表。
    # Args: self - pytest 用例实例。
    # Returns: 无返回值，断言失败由 pytest 报告。
    def test_hallucination_alongside_dual_still_detected(self) -> None:
        """放行 dual 不得顺带放行真正的幻觉表。"""
        logger.debug("test_hallucination_alongside_dual_still_detected 入口")
        tables = [{"name": "orders"}]

        result = _check_table_hallucination(
            "SELECT * FROM dual JOIN invented_table ON 1 = 1",
            tables,
            dialect="oracle",
        )

        assert result == ["invented_table"]
        logger.info("test_hallucination_alongside_dual_still_detected 完成")
