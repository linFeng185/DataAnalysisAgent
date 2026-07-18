"""跨资产 JoinContract 安全校验测试。"""

from __future__ import annotations

import pytest


class TestJoinContract:
    """覆盖匹配率、基数和多对多膨胀保护。"""

    def test_build_join_contract_reports_match_and_cardinality(self):
        """一对多 Join 应返回匹配率和基数，不要求人工确认。"""
        # Arrange
        from src.tools.join_contract import build_join_contract

        left = [{"customer_id": 1}, {"customer_id": 2}]
        right = [{"customer_id": 1, "order": 10}, {"customer_id": 1, "order": 11}, {"customer_id": 2, "order": 12}]

        # Act
        contract = build_join_contract("customers", "orders", left, right, "customer_id", "customer_id")

        # Assert
        assert contract.cardinality == "one_to_many"
        assert contract.match_rate == 1.0
        assert contract.matched_rows == 3
        assert contract.requires_confirmation is False

    def test_build_join_contract_blocks_low_match_and_many_to_many(self):
        """低匹配率或多对多膨胀必须标记人工确认。"""
        # Arrange
        from src.tools.join_contract import build_join_contract

        left = [{"id": 1}, {"id": 1}, {"id": 2}, {"id": 4}]
        right = [{"id": 1}, {"id": 1}, {"id": 1}, {"id": 9}]

        # Act
        contract = build_join_contract("left", "right", left, right, "id", "id")

        # Assert
        assert contract.requires_confirmation is True
        assert contract.cardinality == "many_to_many"
        assert contract.expansion_factor > 1

    def test_build_join_contract_rejects_missing_key(self):
        """Join 键不存在时必须报错而不是静默产生笛卡尔积。"""
        from src.tools.join_contract import JoinContractError, build_join_contract

        with pytest.raises(JoinContractError, match="Join 键"):
            build_join_contract("left", "right", [{"id": 1}], [{"other": 1}], "id", "id")
