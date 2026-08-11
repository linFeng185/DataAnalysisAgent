"""触点归因确定性引擎测试。"""

from __future__ import annotations

from decimal import Decimal


def _path_rows() -> list[dict]:
    """构造两条已转化路径和一条未转化路径。"""
    return [
        {"path_id": "p1", "touchpoint": "搜索", "touch_order": 1,
         "touch_time": "2026-01-01T00:00:00Z", "is_conversion": 0,
         "conversion_value": None},
        {"path_id": "p1", "touchpoint": "内容", "touch_order": 2,
         "touch_time": "2026-01-02T00:00:00Z", "is_conversion": 0,
         "conversion_value": None},
        {"path_id": "p1", "touchpoint": "短信", "touch_order": 3,
         "touch_time": "2026-01-03T00:00:00Z", "is_conversion": 1,
         "conversion_value": 100},
        {"path_id": "p2", "touchpoint": "搜索", "touch_order": 1,
         "touch_time": "2026-01-05T00:00:00Z", "is_conversion": 0,
         "conversion_value": None},
        {"path_id": "p2", "touchpoint": "直播", "touch_order": 2,
         "touch_time": "2026-01-06T00:00:00Z", "is_conversion": 1,
         "conversion_value": 200},
        {"path_id": "p3", "touchpoint": "内容", "touch_order": 1,
         "touch_time": "2026-01-07T00:00:00Z", "is_conversion": 0,
         "conversion_value": None},
        {"path_id": "p3", "touchpoint": "搜索", "touch_order": 2,
         "touch_time": "2026-01-08T00:00:00Z", "is_conversion": 0,
         "conversion_value": None},
    ]


class TestTouchpointAttributionEngine:
    """覆盖功能 13.16 的字段资格和五种规则模型。"""

    # 方法作用：验证首次互动把每条路径全部价值分给第一个触点。
    # Args: self - pytest 测试类实例。
    # Returns: 无返回值，断言失败时由 pytest 报告。
    def test_first_touch_assigns_all_value_to_first_interaction(self) -> None:
        """两条已转化路径都从搜索开始时，搜索应获得全部价值。"""
        # Arrange
        from src.tools.touchpoint_attribution import (
            TouchpointAttributionEngine,
            TouchpointAttributionModel,
        )

        # Act
        result = TouchpointAttributionEngine().calculate(
            _path_rows(),
            TouchpointAttributionModel.FIRST_TOUCH,
        )

        # Assert
        assert result.total_attributed_value == Decimal("300.00")
        assert len(result.results) == 1
        assert result.results[0].touchpoint == "搜索"
        assert result.results[0].attribution_pct == Decimal("100.00")

    # 方法作用：验证末次互动只把价值分给转化前最后触点。
    # Args: self - pytest 测试类实例。
    # Returns: 无返回值，断言失败时由 pytest 报告。
    def test_last_touch_assigns_value_to_final_interaction(self) -> None:
        """短信和直播应分别获得两条路径的完整转化价值。"""
        # Arrange
        from src.tools.touchpoint_attribution import (
            TouchpointAttributionEngine,
            TouchpointAttributionModel,
        )

        # Act
        result = TouchpointAttributionEngine().calculate(
            _path_rows(),
            TouchpointAttributionModel.LAST_TOUCH,
        )

        # Assert
        by_touchpoint = {item.touchpoint: item for item in result.results}
        assert by_touchpoint["短信"].attributed_value == Decimal("100.00")
        assert by_touchpoint["直播"].attributed_value == Decimal("200.00")
        assert sum(item.attribution_pct for item in result.results) == Decimal("100.00")

    # 方法作用：验证线性归因按路径内触点次数平均分配。
    # Args: self - pytest 测试类实例。
    # Returns: 无返回值，断言失败时由 pytest 报告。
    def test_linear_attribution_splits_each_path_evenly(self) -> None:
        """重复出现在两条路径中的搜索应累加两条路径的线性份额。"""
        # Arrange
        from src.tools.touchpoint_attribution import (
            TouchpointAttributionEngine,
            TouchpointAttributionModel,
        )

        # Act
        result = TouchpointAttributionEngine().calculate(
            _path_rows(),
            TouchpointAttributionModel.LINEAR,
        )

        # Assert
        by_touchpoint = {item.touchpoint: item for item in result.results}
        assert by_touchpoint["搜索"].attributed_value == Decimal("133.34")
        assert by_touchpoint["直播"].attributed_value == Decimal("100.00")
        assert sum(item.attributed_value for item in result.results) == Decimal("300.00")
        assert sum(item.attribution_pct for item in result.results) == Decimal("100.00")
        assert sum(item.conversion_credit for item in result.results) == Decimal("2.00")

    # 方法作用：验证时间衰减让临近转化的触点获得更高权重。
    # Args: self - pytest 测试类实例。
    # Returns: 无返回值，断言失败时由 pytest 报告。
    def test_time_decay_favors_recent_interactions(self) -> None:
        """同一路径内越接近转化时间的触点归因信用应越高。"""
        # Arrange
        from src.tools.touchpoint_attribution import (
            TouchpointAttributionEngine,
            TouchpointAttributionModel,
        )
        rows = _path_rows()[:3] + [
            {**row, "path_id": "p4"}
            for row in _path_rows()[:3]
        ]

        # Act
        result = TouchpointAttributionEngine(half_life_days=1).calculate(
            rows,
            TouchpointAttributionModel.TIME_DECAY,
        )

        # Assert
        by_touchpoint = {item.touchpoint: item for item in result.results}
        assert by_touchpoint["短信"].conversion_credit > by_touchpoint["内容"].conversion_credit
        assert by_touchpoint["内容"].conversion_credit > by_touchpoint["搜索"].conversion_credit
        assert result.half_life_days == Decimal("1")
        assert sum(item.attribution_pct for item in result.results) == Decimal("100.00")

    # 方法作用：验证 U 型模型首末各占四成且中间平分两成。
    # Args: self - pytest 测试类实例。
    # Returns: 无返回值，断言失败时由 pytest 报告。
    def test_position_based_uses_u_shaped_weights(self) -> None:
        """三触点路径按 40/20/40，两触点路径按 50/50。"""
        # Arrange
        from src.tools.touchpoint_attribution import (
            TouchpointAttributionEngine,
            TouchpointAttributionModel,
        )

        # Act
        result = TouchpointAttributionEngine().calculate(
            _path_rows(),
            TouchpointAttributionModel.POSITION_BASED,
        )

        # Assert
        by_touchpoint = {item.touchpoint: item for item in result.results}
        assert by_touchpoint["搜索"].attributed_value == Decimal("140.00")
        assert by_touchpoint["内容"].attributed_value == Decimal("20.00")
        assert by_touchpoint["短信"].attributed_value == Decimal("40.00")
        assert by_touchpoint["直播"].attributed_value == Decimal("100.00")

    # 方法作用：验证资格报告统计未转化路径并提示规则模型的边界。
    # Args: self - pytest 测试类实例。
    # Returns: 无返回值，断言失败时由 pytest 报告。
    def test_eligibility_reports_converted_and_unconverted_samples(self) -> None:
        """未转化路径参与数据资格报告，但不分配转化价值。"""
        # Arrange
        from src.tools.touchpoint_attribution import (
            TouchpointAttributionEngine,
            TouchpointAttributionModel,
        )

        # Act
        eligibility = TouchpointAttributionEngine().evaluate(
            _path_rows(),
            TouchpointAttributionModel.LINEAR,
        )

        # Assert
        assert eligibility.eligible is True
        assert eligibility.total_paths == 3
        assert eligibility.converted_paths == 2
        assert eligibility.unconverted_paths == 1
        assert eligibility.mapping.path_id == "path_id"

    # 方法作用：验证样本量不足时资格检查阻断归因。
    # Args: self - pytest 测试类实例。
    # Returns: 无返回值，断言失败时由 pytest 报告。
    def test_eligibility_rejects_too_few_converted_paths(self) -> None:
        """单条转化路径不能被包装成具有业务代表性的归因结果。"""
        # Arrange
        from src.tools.touchpoint_attribution import (
            TouchpointAttributionEngine,
            TouchpointAttributionModel,
        )

        # Act
        eligibility = TouchpointAttributionEngine().evaluate(
            _path_rows()[:3],
            TouchpointAttributionModel.LINEAR,
        )

        # Assert
        assert eligibility.eligible is False
        assert any("至少需要 2 条" in gap for gap in eligibility.missing_requirements)

    # 方法作用：验证时间衰减模型必须使用真实时间字段。
    # Args: self - pytest 测试类实例。
    # Returns: 无返回值，断言失败时由 pytest 报告。
    def test_time_decay_rejects_sequence_only_data(self) -> None:
        """只有顺序没有时间时不能伪造衰减间隔。"""
        # Arrange
        from src.tools.touchpoint_attribution import (
            TouchpointAttributionEngine,
            TouchpointAttributionModel,
        )
        rows = [
            {key: value for key, value in row.items() if key != "touch_time"}
            for row in _path_rows()
        ]

        # Act
        eligibility = TouchpointAttributionEngine().evaluate(
            rows,
            TouchpointAttributionModel.TIME_DECAY,
        )

        # Assert
        assert eligibility.eligible is False
        assert "时间衰减归因缺少触点时间 touch_time" in eligibility.missing_requirements

    # 方法作用：验证重复顺序和非法转化标记不会被静默修复。
    # Args: self - pytest 测试类实例。
    # Returns: 无返回值，断言失败时由 pytest 报告。
    def test_eligibility_rejects_ambiguous_order_and_conversion_flag(self) -> None:
        """无法确定触点先后或转化语义时必须明确失败。"""
        # Arrange
        from src.tools.touchpoint_attribution import (
            TouchpointAttributionEngine,
            TouchpointAttributionModel,
        )
        rows = _path_rows()
        rows[1] = {**rows[1], "touch_order": 1}
        rows[4] = {**rows[4], "is_conversion": "未知"}

        # Act
        eligibility = TouchpointAttributionEngine().evaluate(
            rows,
            TouchpointAttributionModel.FIRST_TOUCH,
        )

        # Assert
        assert eligibility.eligible is False
        assert "转化标记存在无法识别的值" in eligibility.missing_requirements
        assert any("重复顺序" in gap for gap in eligibility.missing_requirements)

    # 方法作用：验证字段级重复的路径转化价值只累计一次。
    # Args: self - pytest 测试类实例。
    # Returns: 无返回值，断言失败时由 pytest 报告。
    def test_repeated_path_value_is_counted_once(self) -> None:
        """同一路径每个触点重复携带订单金额时不能按触点倍增。"""
        # Arrange
        from src.tools.touchpoint_attribution import (
            TouchpointAttributionEngine,
            TouchpointAttributionModel,
        )
        rows = [
            {**row, "conversion_value": 100 if row["path_id"] == "p1" else 200}
            if row["path_id"] in {"p1", "p2"} else row
            for row in _path_rows()
        ]

        # Act
        result = TouchpointAttributionEngine().calculate(
            rows,
            TouchpointAttributionModel.LINEAR,
        )

        # Assert
        assert result.total_attributed_value == Decimal("300.00")

    # 方法作用：验证转化价值列存在时不允许混入缺失值或冲突值。
    # Args: self - pytest 测试类实例。
    # Returns: 无返回值，断言失败时由 pytest 报告。
    def test_eligibility_rejects_missing_or_conflicting_path_values(self) -> None:
        """金额归因不能把缺失金额按一次转化计算，也不能任选冲突金额。"""
        # Arrange
        from src.tools.touchpoint_attribution import (
            TouchpointAttributionEngine,
            TouchpointAttributionModel,
        )
        missing_rows = _path_rows()
        missing_rows[4] = {**missing_rows[4], "conversion_value": None}
        conflicting_rows = _path_rows()
        conflicting_rows[0] = {**conflicting_rows[0], "conversion_value": 90}

        # Act
        missing = TouchpointAttributionEngine().evaluate(
            missing_rows,
            TouchpointAttributionModel.LINEAR,
        )
        conflicting = TouchpointAttributionEngine().evaluate(
            conflicting_rows,
            TouchpointAttributionModel.LINEAR,
        )

        # Assert
        assert missing.eligible is False
        assert any("缺少转化价值" in gap for gap in missing.missing_requirements)
        assert conflicting.eligible is False
        assert any("多个不同的转化价值" in gap for gap in conflicting.missing_requirements)
