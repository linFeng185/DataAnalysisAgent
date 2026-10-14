"""覆盖 src/knowledge/doc_parser.py 的策略自动检测与分块长度兜底。"""

from src.knowledge.doc_parser import (
    MAX_CHUNK_CHARS,
    ChunkConfig,
    ChunkStrategy,
    DocChunk,
    _auto_detect,
    _split_by_length,
    chunk_text,
    enforce_chunk_limit,
)

# 模拟中文数据字典：含大量反引号字段名与 '### 表:' 分节标记。
DATA_DICTIONARY = "\n".join(
    [
        "# 零售表结构总览",
        "",
        "数据源：零售交易与客户（retail / mysql）",
        "",
        *[
            f"### 表: retail_table_{index} — 测试表 {index}\n\n"
            "## 表结构与字段含义\n\n"
            "| 字段 | 类型 | 字段含义 |\n|---|---|---|\n"
            "| `id` | 整数 | 本表唯一主键 |\n"
            "| `name` | 文本 | 名称 |\n"
            "| `created_at` | 日期 | 创建时间 |\n"
            "| `amount` | 定点数 | 金额 |\n"
            for index in range(3)
        ],
    ]
)


class TestAutoDetectStrategy:
    """覆盖功能 6.7：自动策略检测的优先级。"""

    def test_data_dictionary_prefers_table_over_reference(self) -> None:
        """数据字典含大量反引号时仍必须选中 table 策略。

        回归 2026-09-10 上传失败：reference 抢在 table 之前命中，且找不到
        '####' 锚点，整篇塌成一个 137003 字符分块，超出向量库字段上限。
        """
        # Arrange：构造反引号数量远超 reference 触发阈值的字典文本
        assert DATA_DICTIONARY.count("`") >= 15
        # Act
        strategy = _auto_detect(DATA_DICTIONARY, "表结构总览.md")
        # Assert
        assert strategy is ChunkStrategy.TABLE

    def test_auto_strategy_splits_dictionary_by_table(self) -> None:
        """默认 AUTO 策略下数据字典按表切分，而不是整篇一块。"""
        # Arrange
        config = ChunkConfig(
            strategy=ChunkStrategy.AUTO, chunk_size=800, chunk_overlap=100
        )
        # Act
        chunks = chunk_text(DATA_DICTIONARY, config, "表结构总览.md")
        # Assert
        table_chunks = [c for c in chunks if c.metadata.get("table_name")]
        assert len(table_chunks) == 3
        assert {c.metadata["table_name"] for c in table_chunks} == {
            "retail_table_0",
            "retail_table_1",
            "retail_table_2",
        }


class TestEnforceChunkLimit:
    """覆盖功能 6.7：超长分块的写入前兜底。"""

    def test_oversized_chunk_is_split_under_limit(self) -> None:
        """超过单字段上限的分块必须被切开，且每块不超限、元数据保留。"""
        # Arrange
        oversized = DocChunk(
            content="行内容\n" * 16000, metadata={"source_file": "big.md"}
        )
        assert len(oversized.content) > MAX_CHUNK_CHARS
        # Act
        result = enforce_chunk_limit([oversized])
        # Assert
        assert len(result) > 1
        assert all(len(chunk.content) <= MAX_CHUNK_CHARS for chunk in result)
        assert all(chunk.metadata["source_file"] == "big.md" for chunk in result)

    def test_small_chunk_is_untouched(self) -> None:
        """未超限的分块原样返回，不做改写。"""
        # Arrange
        chunk = DocChunk(content="短内容", metadata={"table_name": "t"})
        # Act
        result = enforce_chunk_limit([chunk])
        # Assert
        assert result == [chunk]

    def test_reference_misfire_is_caught_by_limit(self) -> None:
        """reference 误判产出整篇单块时，兜底仍保证每块在写入上限内。"""
        # Arrange：反引号密集但无锚点的文本会被 reference 判定为整篇一块
        text = "`字段` " * 15000
        config = ChunkConfig(
            strategy=ChunkStrategy.AUTO, chunk_size=800, chunk_overlap=100
        )
        raw_chunks = chunk_text(text, config, "字典.md")
        assert len(raw_chunks) == 1
        assert len(raw_chunks[0].content) > MAX_CHUNK_CHARS
        # Act
        chunks = enforce_chunk_limit(raw_chunks)
        # Assert
        assert len(chunks) > 1
        assert all(len(chunk.content) <= MAX_CHUNK_CHARS for chunk in chunks)

    def test_split_prefers_newline_boundary(self) -> None:
        """切分优先落在换行处，保证每行内容不被劈开。"""
        # Arrange
        text = ("x" * 79 + "\n") * 1000
        # Act
        pieces = _split_by_length(text, 800)
        # Assert：无损切分，且除首段外每段都从换行符开始
        assert "".join(pieces) == text
        assert all(len(piece) <= 800 for piece in pieces)
        assert all(piece.startswith("\n") for piece in pieces[1:])

    def test_hard_split_when_no_newline(self) -> None:
        """完全没有换行的超长文本按字符硬切，且不丢失内容。"""
        # Arrange
        text = "a" * (MAX_CHUNK_CHARS * 2 + 10)
        # Act
        pieces = _split_by_length(text, MAX_CHUNK_CHARS)
        # Assert
        assert len(pieces) == 3
        assert "".join(pieces) == text
        assert all(len(piece) <= MAX_CHUNK_CHARS for piece in pieces)


# 去重叠拼接 helper：供零丢失/零重复断言使用。
def _reassemble(chunks: list[str]) -> str:
    """按“后块去掉与前输出的公共重叠前缀”还原全文，用于零丢失/零重复断言。

    仅适用于非周期性文本（贪心最大重叠匹配在周期/纯重复文本上有歧义）。
    """
    out = chunks[0] if chunks else ""
    for chunk in chunks[1:]:
        overlap = 0
        for size in range(min(len(out), len(chunk)), 0, -1):
            if out.endswith(chunk[:size]):
                overlap = size
                break
        out += chunk[overlap:]
    return out


# 构造可校验的超长单行文本：L{序号:05d}-abcdefghij 循环拼接，无换行。
def _build_long_line(units: int = 5883) -> str:
    """构造约 10 万字符、无换行的可校验单行文本。"""
    return "".join(f"L{index:05d}-abcdefghij" for index in range(units))


class TestChunkOverlapAndNoContentLoss:
    """覆盖切分出口收口：相邻块重叠生效、迷你块零丢失、病态配置与行格式保留。

    回归用户实测：上传表结构文档时重叠不生效、短行/残片被静默丢弃、缩进被吞。
    """

    def test_adjacent_chunks_share_overlap(self) -> None:
        """fixed 策略正常切块路径必须带上下文重叠：后块头部即前块按行对齐的尾部。"""
        # Arrange：40 行、每行约 15 字符，总长超过两块
        text = "\n".join(f"行{index:03d} 字段说明内容填充数据" for index in range(40))
        config = ChunkConfig(
            strategy=ChunkStrategy.FIXED, chunk_size=200, chunk_overlap=50
        )
        # Act
        chunks = chunk_text(text, config, "多行.txt")
        # Assert：至少两块；后块开头恰好等于前块按行对齐的 overlap 字符尾部。
        assert len(chunks) >= 2
        # 按 _finalize_chunks 的行对齐语义计算前块尾部：取末尾 overlap 字符，
        # 丢弃首个不完整行（末行片段从第一个换行符之后开始）。
        tail = chunks[0].content[-config.chunk_overlap:]
        newline_of_tail = tail.find("\n")
        assert newline_of_tail >= 0
        tail = tail[newline_of_tail + 1:]
        assert tail
        assert chunks[1].content.startswith(f"{tail}\n")
        # 线性行序：前块末行仍出现在后块头部区段，后块自己的新内容紧随其后；
        # 且后块内部行号（行000 起定宽编号）保持单调递增，重叠未打乱行序。
        last_line_of_first = chunks[0].content.rsplit("\n", 1)[-1]
        assert last_line_of_first
        assert last_line_of_first in chunks[1].content[:100]
        assert "行012" in chunks[1].content
        second_block_lines = chunks[1].content.splitlines()
        assert len(second_block_lines) >= 2
        assert second_block_lines == sorted(second_block_lines)

    def test_oversized_single_line_no_loss_no_dup(self) -> None:
        """超长单行滑切后：首片是原文前缀、末片是原文后缀，去重叠拼接等于原文。"""
        # Arrange：单行约 10 万字符，不含任何换行
        text = _build_long_line()
        assert "\n" not in text
        assert len(text) >= 100_000
        config = ChunkConfig(
            strategy=ChunkStrategy.FIXED, chunk_size=200, chunk_overlap=50
        )
        # Act
        chunks = chunk_text(text, config, "超长行.txt")
        contents = [chunk.content for chunk in chunks]
        # Assert：零丢失、零重复
        assert len(contents) > 1
        assert text.startswith(contents[0])
        assert text.endswith(contents[-1])
        assert _reassemble(contents) == text

    def test_tiny_remnant_is_merged_not_dropped(self) -> None:
        """长行之间的短行残留块必须并入相邻块，不能被 min_chunk_size 静默丢弃。"""
        # Arrange：长行 190 + 短行 14 + 长行 190，短行被迫单独成块且不足 50
        text = f"{'A' * 190}\n短行特征标记SHORTMARK\n{'B' * 190}"
        config = ChunkConfig(
            strategy=ChunkStrategy.FIXED, chunk_size=200, chunk_overlap=50
        )
        # Act
        chunks = chunk_text(text, config, "残留.txt")
        # Assert：块数固定为两块；短行并入前块尾部且在前块内恰好出现一次。
        assert len(chunks) == 2
        assert chunks[0].content.startswith(f"{'A' * 190}\n")
        assert chunks[0].content.endswith("短行特征标记SHORTMARK")
        assert chunks[0].content.count("SHORTMARK") == 1
        # 出口重叠再把前块尾行带到后块头部：后块 = 重叠行 + 原本后块内容，
        # 短行不是新增副本（后块内同样恰好出现一次），B 内容完整保留。
        assert chunks[1].content.startswith("短行特征标记SHORTMARK\n")
        assert chunks[1].content.count("SHORTMARK") == 1
        assert chunks[1].content.endswith("B" * 190)

    def test_leading_tiny_chunk_merged_into_next(self) -> None:
        """首个有效块即迷你时必须暂存并入后块，不能丢失也不能重复并入。"""
        # Arrange：短首行 + 超长行；超长行被滑切出多片，短首行只能暂存并入首个长片。
        text = f"SHORTSTART\n{'X' * 500}"
        config = ChunkConfig(
            strategy=ChunkStrategy.FIXED, chunk_size=200, chunk_overlap=50
        )
        # Act
        chunks = chunk_text(text, config, "首块迷你.txt")
        # Assert：迷你首行出现在首个长片头部，且全产出中恰好出现一次。
        assert len(chunks) >= 2
        assert chunks[0].content.startswith("SHORTSTART\n")
        assert sum(chunk.content.count("SHORTSTART") for chunk in chunks) == 1

    def test_single_tiny_chunk_is_kept(self) -> None:
        """全文仅一个迷你块时仍保留该块，保证内容零丢失。"""
        # Arrange：单行 14 字符，低于 min_chunk_size=50
        text = "仅一个迷你块ONLYTINY"
        config = ChunkConfig(
            strategy=ChunkStrategy.FIXED, chunk_size=200, chunk_overlap=50
        )
        # Act
        chunks = chunk_text(text, config, "迷你.txt")
        # Assert：唯一一块被原样保留，无相邻块可并入时也不丢弃。
        assert len(chunks) == 1
        assert chunks[0].content == "仅一个迷你块ONLYTINY"

    def test_overlap_not_less_than_size_does_not_crash(self) -> None:
        """overlap ≥ size 的病态配置不抛异常，且内容经去重叠拼接后完整。"""
        # Arrange：chunk_overlap=500 大于 chunk_size=200
        text = _build_long_line(units=600)
        config = ChunkConfig(
            strategy=ChunkStrategy.FIXED, chunk_size=200, chunk_overlap=500
        )
        # Act：修复前硬切步进为负会静默丢内容，步进为 0 会抛 ValueError
        chunks = chunk_text(text, config, "病态配置.txt")
        contents = [chunk.content for chunk in chunks]
        # Assert
        assert len(contents) > 1
        assert _reassemble(contents) == text

    def test_line_indentation_is_preserved(self) -> None:
        """行首缩进（前导空格）必须保留，不能被逐行 strip 吞掉。"""
        # Arrange：缩进行位于块中间，避开出口对块首尾的 strip
        text = "表结构说明\n    字段 id 为主键\n    字段 name 为姓名\n结束行"
        config = ChunkConfig(
            strategy=ChunkStrategy.FIXED, chunk_size=200, chunk_overlap=50
        )
        # Act
        chunks = chunk_text(text, config, "缩进.txt")
        # Assert
        content = "\n".join(chunk.content for chunk in chunks)
        assert "\n    字段 id 为主键" in content
        assert "\n    字段 name 为姓名" in content

    def test_block_first_line_indentation_survives_overlap(self) -> None:
        """块首行的前导缩进在重叠 prepend 之后仍保留（曾被块级 strip 剥掉）。

        回归 2026-10-14：出口 _finalize_chunks 用 content.strip() 做块级清洗，
        会把每个块首行的前导缩进一并吞掉；只有块尾空白应被清洗。
        """
        # Arrange：所有行都带 4 空格缩进、每行约 45 字符、总长超过两块
        lines = [
            f"    field_{index:03d}             说明文字占位 (varchar({index}))"
            for index in range(30)
        ]
        text = "\n".join(lines)
        config = ChunkConfig(
            strategy=ChunkStrategy.FIXED, chunk_size=200, chunk_overlap=50
        )
        # Act
        chunks = chunk_text(text, config, "缩进表.txt")
        # Assert：所有产出块中，来自原文的非空白行都必须带 4 空格缩进
        assert len(chunks) >= 2
        for chunk in chunks:
            for line in chunk.content.splitlines():
                if line.strip():
                    assert line.startswith("    "), f"块内行丢失缩进: {line!r}"
        # 且内容经去重叠拼接后应与原文一致（缩进不丢、内容不重不漏）
        assert _reassemble([chunk.content for chunk in chunks]) == text


class TestSqlDdlChunking:
    """覆盖 SQL_DDL 策略：语句按语法边界成块，块内容只含原文行。"""

    def test_no_fabricated_fragments_and_no_line_change(self) -> None:
        """re.split 捕获组曾把 TABLE/VIEW/INDEX 作为伪行拼进块内容。

        回归 2026-10-14：切分正则使用捕获组 (TABLE|VIEW|INDEX)，re.split 会把
        捕获片段当作独立元素注入结果并被拼进块；改为非捕获组后块内行与原文一致，
        既无伪造行也无丢失/重复（重叠为 0 时逐行计数应完全相等）。
        """
        # Arrange：两条表 + 一条视图，均有前置注释
        text = "\n".join([
            "-- 注释甲",
            "CREATE TABLE t_a (",
            "  id INT",
            ");",
            "",
            "-- 注释乙",
            "CREATE VIEW v_b AS SELECT 1;",
            "",
            "-- 注释丙",
            "CREATE TABLE t_c (",
            "  id INT",
            ");",
        ])
        config = ChunkConfig(
            strategy=ChunkStrategy.SQL_DDL, chunk_size=400, chunk_overlap=0,
            min_chunk_size=50,
        )
        # Act
        chunks = chunk_text(text, config, "ddl.sql")
        # Assert：块内不得出现原文没有的独立 TABLE/VIEW/INDEX 伪行
        produced_lines = [line.strip() for chunk in chunks for line in chunk.content.splitlines() if line.strip()]
        assert "TABLE" not in produced_lines
        assert "VIEW" not in produced_lines
        # Assert：原文每个非空行保留且仅出现一次（仅比较块内行，不比较跨块空白）
        from collections import Counter

        assert Counter(produced_lines) == Counter(line.strip() for line in text.splitlines() if line.strip())
        # Assert：表名元数据仍可从块内提取
        assert any(chunk.metadata.get("table_name") for chunk in chunks)
