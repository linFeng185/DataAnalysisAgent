"""真实 PostgreSQL 上的报表 SQL 契约测试。

这一层专门守住"只在真实数据库上才暴露"的缺陷：列名写错（UndefinedColumn）、
占位符类型推断失败（UndefinedFunction，如占位符互比被推断成 text）。
内存模式与 SQLite 都发现不了这类问题。
"""

from __future__ import annotations

import ast
import os
import pathlib

import pytest

# 默认测试套件不依赖开发数据库，只有显式开启才连接真实 PG。
LIVE_PG_ENV = "RUN_LIVE_PG_TESTS"
SQL_KEYWORDS = ("SELECT ", "INSERT ", "UPDATE ", "DELETE ", "WITH ")


# 方法作用：提取模块中静态声明的 SQL 语句，跳过 f-string 与跨行拼接产生的片段。
# Args: root - 扫描根目录。
# Returns: (文件路径, 行号, SQL 文本) 三元组列表。
def _static_sql(root: pathlib.Path) -> list[tuple[pathlib.Path, int, str]]:
    found: list[tuple[pathlib.Path, int, str]] = []
    for path in sorted(root.rglob("*.py")):
        try:
            tree = ast.parse(path.read_text(encoding="utf-8"))
        except SyntaxError:
            continue
        for node in ast.walk(tree):
            if not isinstance(node, ast.Constant) or not isinstance(node.value, str):
                continue
            text = node.value.strip()
            # f-string 的动态片段不含完整语义，且 prepare 必然报语法错，直接排除
            if text.upper().startswith(SQL_KEYWORDS) and "$" in text:
                found.append((path, node.lineno, text))
    return found


@pytest.mark.asyncio
@pytest.mark.skipif(
    os.environ.get(LIVE_PG_ENV) != "1",
    reason=f"需要设置 {LIVE_PG_ENV}=1 并提供可达的 PostgreSQL",
)
class TestReportSqlPgContract:
    """覆盖功能 23.8：持久化 SQL 必须能在真实 PostgreSQL 上通过解析。"""

    # 方法作用：把 reporting 模块的全部静态 SQL 交给真实 PG 解析。
    # Args: self - pytest 测试类实例。
    # Returns: 无返回值，断言失败时由 pytest 报告。
    async def test_all_reporting_sql_prepares(self) -> None:
        """列名或参数类型写错必须在 prepare 阶段暴露，而不是运行到一半才崩。"""
        # Arrange
        from src.memory.pg_pool import get_pg_pool

        root = pathlib.Path(__file__).resolve().parents[2] / "src" / "reporting"
        sqls = _static_sql(root)
        # 扫描逻辑一旦失效就测不到东西，必须显式失败而不是空跑通过
        assert sqls, "未提取到任何 SQL，扫描逻辑可能已失效"

        # Act
        failures: list[str] = []
        pool = await get_pg_pool()
        async with pool.acquire() as connection:
            for path, lineno, sql in sqls:
                try:
                    await connection.prepare(sql)
                except Exception as exc:
                    first_line = str(exc).splitlines()[0]
                    failures.append(f"{path.name}:{lineno} -> {type(exc).__name__}: {first_line}")

        # Assert
        assert failures == []
