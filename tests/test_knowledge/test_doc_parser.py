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
