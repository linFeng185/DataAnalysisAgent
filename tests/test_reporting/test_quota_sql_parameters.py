"""报表配额 SQL 的参数类型契约测试。"""

from __future__ import annotations

import io
import re
import tokenize
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]

# 单个占位符：结尾不允许再接数字，否则 $12 会被回溯截断成 $1 而误报。
_PARAM = r"\$\d+(?!\d)"
# 占位符之间直接比较没有列上下文，PostgreSQL 无法推断类型、默认按 text 处理，
# 会与同一占位符在 `analysis_units+$6` 中的整数用法冲突，在 prepare 阶段就抛
# UndefinedFunctionError: operator does not exist: integer + text。
# 右侧已显式转型的写法不受影响，故用负向前瞻排除。
BARE_PARAM_COMPARISON_RE = re.compile(rf"{_PARAM}\s*(?:<=|>=|<|>|=)\s*{_PARAM}(?!::)")


# 方法作用：把 # 注释替换为等长空白，避免注释里举的例子被当成真实 SQL 命中。
# Args: text - 待扫描的源码文本。
# Returns: 注释被抹除且行号保持不变的文本。
def strip_comments(text: str) -> str:
    lines = text.splitlines(keepends=True)
    try:
        for token in tokenize.generate_tokens(io.StringIO(text).readline):
            if token.type != tokenize.COMMENT:
                continue
            row, col = token.start
            raw = lines[row - 1]
            lines[row - 1] = raw[:col] + ("\n" if raw.endswith("\n") else "")
    except (tokenize.TokenError, IndentationError):
        # 语法不完整的文本无法分词，退回原文扫描，宁可误报也不漏报。
        return text
    return "".join(lines)


class TestQuotaSqlParameterTypes:
    """覆盖功能 23.8：持久化配额 SQL 的参数类型契约。"""

    # 方法作用：扫描 src 下全部 Python 源码，阻止占位符互比未加显式转型。
    # Args: self - pytest 测试类实例。
    # Returns: 无返回值，断言失败时由 pytest 报告。
    def test_no_uncasted_parameter_comparison(self) -> None:
        """占位符之间比较必须显式转型，否则真实 PostgreSQL 会拒绝整个语句。"""
        # Arrange
        offenders: list[str] = []

        # Act
        for path in sorted((ROOT / "src").rglob("*.py")):
            text = strip_comments(path.read_text(encoding="utf-8"))
            for match in BARE_PARAM_COMPARISON_RE.finditer(text):
                lineno = text.count("\n", 0, match.start()) + 1
                offenders.append(f"{path.relative_to(ROOT)}:{lineno}: {match.group(0)}")

        # Assert
        assert offenders == []

    # 方法作用：验证守卫正则能命中真实缺陷且不误伤已转型写法。
    # Args: self - pytest 测试类实例。
    # Returns: 无返回值，断言失败时由 pytest 报告。
    def test_guard_regex_detects_real_defect(self) -> None:
        """守卫必须命中未转型写法，并放过已转型与多位数占位符。"""
        # Act / Assert
        assert BARE_PARAM_COMPARISON_RE.search("AND $6 <= $15 AND $7 <= $16")
        assert not BARE_PARAM_COMPARISON_RE.search("AND $6::bigint <= $15::bigint")
        assert not BARE_PARAM_COMPARISON_RE.search("AND jobs_created+$3 <= $12::bigint")
