"""报表跨源关系分析回归，覆盖 23.18。

跨源采用两级证据：能键匹配时优先键匹配（证据更强，给出匹配率与基数），
键匹配不可用且两侧都有样本行时才退回分布级秩相关。
"""

from __future__ import annotations


# 方法作用：构造一个跨源对比用的单元数据集。
# Args: unit_id - 单元标识；datasource - 数据源名；values - 各周期取值；time_key - 时间列名，None 表示该侧没有时间列。
# Returns: 可供关系分析器消费的数据集字典。
def _dataset(unit_id: str, datasource: str, values: list[float], time_key: str | None) -> dict:
    columns = ["amount"] if time_key is None else [time_key, "amount"]
    rows = (
        [{"amount": value} for value in values]
        if time_key is None
        else [{time_key: f"2026-{month:02d}", "amount": value} for month, value in enumerate(values, start=1)]
    )
    return {
        "unit_id": unit_id,
        "datasource": datasource,
        "columns": columns,
        "authorized_columns": columns,
        "dimensions": [time_key] if time_key else [],
        "metrics": ["amount"],
        "rows": rows,
        "status": "success",
    }


# 方法作用：构造递增的跨源序列，右值恒为左值两倍，秩相关为 1。
# Args: 无。
# Returns: 12 期递增取值。
def _rising() -> list[float]:
    return [float(value) for value in range(1, 13)]


class TestEvidencePriority:
    """验证 23.18：键匹配优先于分布级对比。"""

    # 方法作用：验证可用连接键存在时不退化为分布级。
    # Args: self - 测试实例。
    # Returns: 无返回值，断言失败时由 pytest 报告。
    def test_key_matching_wins_when_available(self) -> None:
        """两侧同名时间列可作键时，应给出匹配率而不是只给相关系数。"""
        # Arrange
        from src.reporting.relations import ControlledRelationAnalyzer

        left = _dataset("u1", "sales", _rising(), "月份")
        right = _dataset("u2", "ads", _rising(), "月份")

        # Act
        evidence = ControlledRelationAnalyzer().analyze_cross_source(left, right)

        # Assert
        assert evidence.evidence_level == "verified_relation"
        # 键匹配路径会给出匹配率；分布级路径不会
        assert evidence.match_rate is not None

    # 方法作用：验证只有结构没有样本时保持原有的跨源结构证据。
    # Args: self - 测试实例。
    # Returns: 无返回值，断言失败时由 pytest 报告。
    def test_structural_only_keeps_original_evidence(self) -> None:
        """没有样本行时分布级也无从计算，不应改写成 insufficient。"""
        # Arrange
        from src.reporting.relations import ControlledRelationAnalyzer

        left = _dataset("u1", "sales", [], "月份")
        right = _dataset("u2", "ads", [], "月份")

        # Act
        evidence = ControlledRelationAnalyzer().analyze_cross_source(left, right)

        # Assert
        assert evidence.evidence_level == "unsupported_cross_source"


class TestDistributionalFallback:
    """验证 23.18：键匹配不可用时退回分布级秩相关。"""

    # 方法作用：验证两侧时间列异名时按周期标签对齐后算秩相关。
    # Args: self - 测试实例。
    # Returns: 无返回值，断言失败时由 pytest 报告。
    def test_time_aligned_spearman(self) -> None:
        """两侧时间列名不同（真实跨源常见）时共同列只剩指标，应走分布级并按时序对齐。"""
        # Arrange
        from src.reporting.relations import ControlledRelationAnalyzer

        left = _dataset("u1", "sales", _rising(), "月份")
        right = _dataset("u2", "ads", [value * 2 for value in _rising()], "stat_month")

        # Act
        evidence = ControlledRelationAnalyzer().analyze_cross_source(left, right)

        # Assert
        assert evidence.evidence_level == "cross_source_statistical"
        assert evidence.correlation == 1.0
        assert evidence.time_alignment == "same_period"

    # 方法作用：验证没有时间列时退回分布形态比较并如实说明。
    # Args: self - 测试实例。
    # Returns: 无返回值，断言失败时由 pytest 报告。
    def test_without_time_column_uses_distribution_shape(self) -> None:
        """无时间列时比较分位数函数，并声明未按时间对齐。"""
        # Arrange
        from src.reporting.relations import ControlledRelationAnalyzer

        left = _dataset("u1", "sales", _rising(), None)
        right = _dataset("u2", "ads", [value * 3 for value in _rising()], None)

        # Act
        evidence = ControlledRelationAnalyzer().analyze_cross_source(left, right)

        # Assert
        assert evidence.evidence_level == "cross_source_statistical"
        assert evidence.time_alignment == "unknown"
        assert any("未按时间对齐" in item for item in evidence.limitations)

    # 方法作用：验证周期重合不足时判为证据不足。
    # Args: self - 测试实例。
    # Returns: 无返回值，断言失败时由 pytest 报告。
    def test_insufficient_when_overlap_too_small(self) -> None:
        """只有两三个重合周期时不得给出相关系数。"""
        # Arrange
        from src.reporting.relations import ControlledRelationAnalyzer

        left = _dataset("u1", "sales", _rising(), "月份")
        right = _dataset("u2", "ads", _rising()[:3], "stat_month")

        # Act
        evidence = ControlledRelationAnalyzer().analyze_cross_source(left, right)

        # Assert
        assert evidence.evidence_level == "insufficient"

    # 方法作用：验证常数序列判为证据不足。
    # Args: self - 测试实例。
    # Returns: 无返回值，断言失败时由 pytest 报告。
    def test_constant_series_is_insufficient(self) -> None:
        """两侧都无波动时秩相关无定义。"""
        # Arrange
        from src.reporting.relations import ControlledRelationAnalyzer

        left = _dataset("u1", "sales", [5.0] * 12, None)
        right = _dataset("u2", "ads", [5.0] * 12, None)

        # Act
        evidence = ControlledRelationAnalyzer().analyze_cross_source(left, right)

        # Assert
        assert evidence.evidence_level == "insufficient"

    # 方法作用：验证失败单元不产出相关性。
    # Args: self - 测试实例。
    # Returns: 无返回值，断言失败时由 pytest 报告。
    def test_failed_unit_is_blocked(self) -> None:
        """任一侧执行失败时不能使用剩余数据推断关系。"""
        # Arrange
        from src.reporting.relations import ControlledRelationAnalyzer

        left = _dataset("u1", "sales", _rising(), "月份")
        right = _dataset("u2", "ads", _rising(), "stat_month")
        right["status"] = "failed"

        # Act
        evidence = ControlledRelationAnalyzer().analyze_cross_source(left, right)

        # Assert
        assert evidence.evidence_level == "blocked"


class TestCrossSourceHonesty:
    """验证 23.18：跨源证据不得伪造连接信息或宣称因果。"""

    # 方法作用：构造一对走分布级路径的输入。
    # Args: self - 测试实例。
    # Returns: 左侧与右侧数据集。
    def _pair(self):
        return (
            _dataset("u1", "sales", _rising(), "月份"),
            _dataset("u2", "ads", [value * 2 for value in _rising()], "stat_month"),
        )

    # 方法作用：验证限制里始终禁止因果解读。
    # Args: self - 测试实例。
    # Returns: 无返回值，断言失败时由 pytest 报告。
    def test_limitations_always_forbid_causality(self) -> None:
        """跨源结论必须自带不构成因果的声明。"""
        # Arrange
        from src.reporting.relations import ControlledRelationAnalyzer

        left, right = self._pair()

        # Act
        evidence = ControlledRelationAnalyzer().analyze_cross_source(left, right)

        # Assert
        assert any("不构成因果" in item for item in evidence.limitations)

    # 方法作用：验证不伪造连接键与匹配率。
    # Args: self - 测试实例。
    # Returns: 无返回值，断言失败时由 pytest 报告。
    def test_never_fakes_join_keys_or_match_rate(self) -> None:
        """分布级路径没做连接，join_keys 与 match_rate 必须保持空值。"""
        # Arrange
        from src.reporting.relations import ControlledRelationAnalyzer

        left, right = self._pair()

        # Act
        evidence = ControlledRelationAnalyzer().analyze_cross_source(left, right)

        # Assert
        assert evidence.join_keys == []
        assert evidence.match_rate is None
        # 对比指标写进限制文本，而不是冒充连接键
        assert any("对比指标" in item for item in evidence.limitations)


class TestRelationDispatch:
    """验证 23.18：按数据源分派同源与跨源分析。"""

    # 方法作用：验证同源走键匹配、跨源组合走跨源入口。
    # Args: self - 测试实例。
    # Returns: 无返回值，断言失败时由 pytest 报告。
    def test_build_relations_dispatches_by_datasource(self) -> None:
        """跨源组合应走跨源入口，同源组合不应被打上跨源标记。"""
        # Arrange
        from src.reporting.relations import build_relations

        units = [
            _dataset("u1", "sales", _rising(), "月份"),
            _dataset("u2", "sales", _rising(), "月份"),
            _dataset("u3", "ads", [value * 2 for value in _rising()], "stat_month"),
        ]

        # Act
        relations = build_relations(units, None)

        # Assert
        assert len(relations) == 3
        cross = [item for item in relations if item.evidence_level == "cross_source_statistical"]
        assert len(cross) == 2
        same_source = [item for item in relations if {item.left_unit_id, item.right_unit_id} == {"u1", "u2"}]
        assert same_source and same_source[0].evidence_level != "cross_source_statistical"


class TestCorrelationReading:
    """验证 23.18：文档层把相关系数解读为方向与强度。"""

    # 方法作用：验证方向按符号判定、强度按绝对值分档。
    # Args: self - 测试实例。
    # Returns: 无返回值，断言失败时由 pytest 报告。
    def test_direction_and_strength_bands(self) -> None:
        """三档强度与方向要与设计一致。"""
        # Arrange
        from src.reporting.document import correlation_reading

        # Act / Assert
        assert correlation_reading(0.8) == {"direction": "positive", "strength": "strong"}
        assert correlation_reading(-0.45) == {"direction": "negative", "strength": "moderate"}
        assert correlation_reading(-0.1) == {"direction": "negative", "strength": "weak"}

    # 方法作用：验证无系数与噪声级别不硬报方向。
    # Args: self - 测试实例。
    # Returns: 无返回值，断言失败时由 pytest 报告。
    def test_unknown_and_noise(self) -> None:
        """缺失或接近零的系数不得写成「正相关」。"""
        # Arrange
        from src.reporting.document import correlation_reading

        # Act / Assert
        assert correlation_reading(None) == {"direction": "unknown", "strength": "unknown"}
        assert correlation_reading(0.01)["direction"] == "unknown"
        assert correlation_reading(0.0) == {"direction": "unknown", "strength": "weak"}
