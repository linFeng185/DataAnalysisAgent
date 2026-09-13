"""通知 Store 的 PostgreSQL 分支离线契约测试。

真实数据库上的 prepare 校验见 tests/test_reporting/test_pg_sql_contract.py
（需 RUN_LIVE_PG_TESTS=1）。本文件做两件不依赖数据库的检查：
* 所有静态 SQL 必须是 sqlglot 可解析的 postgres 方言语句；
* 每条 SQL 的最大占位符编号必须与传入的位置参数个数一致（防少传/多传参数）。
"""

from __future__ import annotations

import ast
import pathlib
import re

import sqlglot

_STORE_PATH = pathlib.Path(__file__).resolve().parents[2] / "src" / "notifications" / "store.py"
_SQL_KEYWORDS = ("SELECT ", "INSERT ", "UPDATE ", "DELETE ", "WITH ")
_EXECUTE_METHODS = {"execute", "fetch", "fetchval", "fetchrow", "executemany"}
# asyncpg 位置占位符；归一为 NULL 后交给 sqlglot 做结构校验。
_PARAM_PLACEHOLDER = re.compile(r"\$\d+")


# 方法作用：从 Store 源码中提取 (行号, SQL 文本, 位置参数个数) 三元组。
# Args: 无。
# Returns: 提取结果列表；跳过 f-string 等动态片段。
def _extract_sql_calls() -> list[tuple[int, str, int]]:
    tree = ast.parse(_STORE_PATH.read_text(encoding="utf-8"))
    found: list[tuple[int, str, int]] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        if not isinstance(func, ast.Attribute) or func.attr not in _EXECUTE_METHODS:
            continue
        if not node.args:
            continue
        first = node.args[0]
        if not isinstance(first, ast.Constant) or not isinstance(first.value, str):
            continue
        text = first.value.strip()
        if not text.upper().startswith(_SQL_KEYWORDS):
            continue
        found.append((node.lineno, text, len(node.args) - 1))
    return found


class TestNotificationSqlContract:
    """覆盖通知模块 PostgreSQL 分支的静态 SQL 契约。"""

    # 方法作用：验证 store.py 的每条静态 SQL 都能被 sqlglot 解析为 postgres。
    # Args: self - 测试实例。
    # Returns: 无返回值。
    def test_static_sql_parses_as_postgres(self) -> None:
        """语法错误必须在离线阶段暴露，而不是运行到数据库才崩。

        sqlglot 的 postgres 方言对「多个 $N 占位符并列」存在 tokenize 限制，
        因此先把 $N 归一成 NULL（保留 ::type 转型）再解析结构；占位符编号与
        实参个数的一致性由下一条用例独立校验。
        """
        # Arrange
        calls = _extract_sql_calls()
        assert calls, "未提取到任何 SQL，扫描逻辑可能已失效"

        # Act / Assert
        for lineno, sql, _ in calls:
            normalized = _PARAM_PLACEHOLDER.sub("NULL", sql)
            try:
                sqlglot.parse_one(normalized, dialect="postgres")
            except Exception as exc:  # noqa: BLE001 - 断言信息需要原始异常文本
                raise AssertionError(f"store.py:{lineno} SQL 无法解析为 postgres: {exc}") from exc

    # 方法作用：验证占位符编号与传入参数个数一致。
    # Args: self - 测试实例。
    # Returns: 无返回值。
    def test_placeholder_count_matches_positional_arguments(self) -> None:
        """少传一个参数会在运行时抛错，这里提前拦住。"""
        # Arrange
        calls = _extract_sql_calls()
        assert calls, "未提取到任何 SQL，扫描逻辑可能已失效"

        # Act / Assert
        for lineno, sql, argument_count in calls:
            indexes = [
                int(token[1:])
                for token in sql.replace("::", " \x00").split()
                if token.startswith("$") and token[1:].isdigit()
            ]
            if not indexes:
                continue
            assert max(indexes) == argument_count, (
                f"store.py:{lineno} 占位符最大编号 {max(indexes)} 与实参个数 {argument_count} 不一致"
            )

    # 方法作用：验证所有 SQL 都显式带 tenant_id 条件（RLS 之外的第二道防线）。
    # Args: self - 测试实例。
    # Returns: 无返回值。
    def test_queries_are_tenant_scoped(self) -> None:
        """租户隔离不能只依赖 RLS：应用层 SQL 也必须写 tenant_id 条件。"""
        # Arrange
        calls = _extract_sql_calls()

        # Act / Assert
        for lineno, sql, _ in calls:
            # 租户目录本身按主键维度查询，是唯一允许不带 tenant_id 的语句。
            if sql.upper().startswith("SELECT ID FROM TENANTS"):
                continue
            assert "tenant_id" in sql, f"store.py:{lineno} SQL 缺少 tenant_id 条件"
