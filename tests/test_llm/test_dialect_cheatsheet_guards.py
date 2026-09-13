"""方言速查表的防错规则回归测试（spec/08 §SQL 方言适配）。

这两条规则来自真实故障现场，速查表必须钉住，否则模型会重新生成同样的错误 SQL：

- ClickHouse：别名与聚合列同名 → 同 SELECT 列表里列名被按别名展开 → 嵌套聚合 code 184
- Oracle：`FROM "dual"` 按大小写敏感解析 → 找不到对象 ORA-00942
"""

from __future__ import annotations

import logging

logger = logging.getLogger(__name__)


class TestDialectCheatsheetGuards:
    """覆盖各方言速查表必须携带的防错规则。"""

    # 方法作用：验证 ClickHouse 速查表明确禁止聚合别名与列同名。
    # Args: self - pytest 用例实例。
    # Returns: 无返回值，断言失败由 pytest 报告。
    def test_clickhouse_cheatsheet_forbids_aggregate_alias_reuse(self) -> None:
        """别名遮蔽会让后续列引用被展开成嵌套聚合，报 code 184 ILLEGAL_AGGREGATION。"""
        logger.debug("test_clickhouse_cheatsheet_forbids_aggregate_alias_reuse 入口")
        from src.llm.prompts import get_dialect_cheatsheet

        # Act
        text = get_dialect_cheatsheet("clickhouse")

        # Assert：规则必须点名别名同名这一具体诱因，并给出可识别的报错码，
        # 否则模型无法把「聚合别名」和「嵌套聚合报错」两件事关联起来
        assert "别名" in text
        assert "同名" in text
        assert "code 184" in text
        assert "ILLEGAL_AGGREGATION" in text
        logger.info("test_clickhouse_cheatsheet_forbids_aggregate_alias_reuse 完成")

    # 方法作用：验证 Oracle 速查表要求 dual 不加引号。
    # Args: self - pytest 用例实例。
    # Returns: 无返回值，断言失败由 pytest 报告。
    def test_oracle_cheatsheet_requires_unquoted_dual(self) -> None:
        """`FROM "dual"` 引用不到公共同义词 DUAL，EXPLAIN 直接 ORA-00942。"""
        logger.debug("test_oracle_cheatsheet_requires_unquoted_dual 入口")
        from src.llm.prompts import get_dialect_cheatsheet

        # Act
        text = get_dialect_cheatsheet("oracle")

        # Assert：正确写法与错误写法必须同时出现，只写其一会让模型无从对照
        assert "FROM dual" in text
        assert 'FROM "dual"' in text
        assert "ORA-00942" in text
        logger.info("test_oracle_cheatsheet_requires_unquoted_dual 完成")

    # 方法作用：验证 Oracle 速查表说明 NVARCHAR2 的字符集约束。
    # Args: self - pytest 用例实例。
    # Returns: 无返回值，断言失败由 pytest 报告。
    def test_oracle_cheatsheet_covers_nvarchar2_charset_mismatch(self) -> None:
        """NVARCHAR2 与 VARCHAR2 混入同一列会报 ORA-12704，必须给出 TO_CHAR 修法。"""
        logger.debug("test_oracle_cheatsheet_covers_nvarchar2_charset_mismatch 入口")
        from src.llm.prompts import get_dialect_cheatsheet

        # Act
        text = get_dialect_cheatsheet("oracle")

        # Assert：症状码、诱因类型与修法三者必须齐全，缺一则模型无从对应
        assert "NVARCHAR2" in text
        assert "ORA-12704" in text
        assert "TO_CHAR" in text
        logger.info("test_oracle_cheatsheet_covers_nvarchar2_charset_mismatch 完成")

    # 方法作用：验证新增规则没有挤掉其他方言的速查内容。
    # Args: self - pytest 用例实例。
    # Returns: 无返回值，断言失败由 pytest 报告。
    def test_other_dialects_keep_their_core_guidance(self) -> None:
        """改动只应追加规则，不得影响其他方言的核心函数指引。"""
        logger.debug("test_other_dialects_keep_their_core_guidance 入口")
        from src.llm.prompts import get_dialect_cheatsheet

        # Arrange / Act / Assert
        assert "DATE_FORMAT" in get_dialect_cheatsheet("mysql")
        assert "COALESCE" in get_dialect_cheatsheet("postgres")
        assert "TRUNC(dt, 'DD')" in get_dialect_cheatsheet("oracle")
        logger.info("test_other_dialects_keep_their_core_guidance 完成")
