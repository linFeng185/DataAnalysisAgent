"""DataAsset/Evidence/AnalysisPlan/AnalysisArtifact 契约测试，覆盖 Phase A。"""

from __future__ import annotations

import logging

import pytest

logger = logging.getLogger(__name__)


class TestDataAsset:
    """覆盖统一数据资产模型。"""

    # 验证数据库资产能携带租户、来源和结构信息。
    # Args: self - pytest 测试类实例。
    # Returns: 无返回值，断言失败时由 pytest 报告。
    def test_database_asset_round_trip(self):
        """DataAsset 应保留数据库指纹、Schema 和 provenance。"""
        logger.debug("test_database_asset_round_trip 入口")
        from src.knowledge.asset_models import DataAsset

        asset = DataAsset(
            id="asset-db-orders",
            kind="database",
            uri="datasource://orders",
            tenant_id=7,
            owner_id=3,
            fingerprint="f" * 64,
            schema_info={"tables": ["orders"]},
            provenance={"connector": "postgres", "as_of": "2026-07-18T00:00:00Z"},
        )

        assert asset.kind == "database"
        assert asset.tenant_id == 7
        assert asset.schema_info["tables"] == ["orders"]
        assert asset.model_dump()["fingerprint"] == "f" * 64
        logger.info("test_database_asset_round_trip 完成")

    # 验证资产不允许空 URI 或非 SHA-256 指纹。
    # Args: self - pytest 测试类实例。
    # Returns: 无返回值，断言失败时由 pytest 报告。
    def test_invalid_asset_is_rejected(self):
        """非法资产身份必须在模型层拒绝。"""
        logger.debug("test_invalid_asset_is_rejected 入口")
        from src.knowledge.asset_models import DataAsset

        with pytest.raises(ValueError):
            DataAsset(
                id="bad", kind="document", uri="", tenant_id=1, owner_id=1,
                fingerprint="short",
            )
        logger.info("test_invalid_asset_is_rejected 完成")


class TestEvidenceContracts:
    """覆盖证据、引用和分析产物结构。"""

    # 验证证据必须带来源和定位，支持回答可回溯。
    # Args: self - pytest 测试类实例。
    # Returns: 无返回值，断言失败时由 pytest 报告。
    def test_evidence_requires_locator(self):
        """缺少 source_id 或 locator 的证据应被拒绝。"""
        logger.debug("test_evidence_requires_locator 入口")
        from src.knowledge.asset_models import Evidence

        evidence = Evidence(
            content="GMV 只统计已支付订单",
            source_id="doc-1",
            version="v3",
            locator={"page": 4, "heading_path": ["指标", "GMV"]},
            scores={"dense": 0.91},
        )
        assert evidence.locator["page"] == 4
        with pytest.raises(ValueError):
            Evidence(content="没有来源", source_id="", version="v1", locator={})
        logger.info("test_evidence_requires_locator 完成")

    # 验证计划和产物能承载假设、限制和证据。
    # Args: self - pytest 测试类实例。
    # Returns: 无返回值，断言失败时由 pytest 报告。
    def test_analysis_plan_and_artifact_contract(self):
        """分析产物必须带 evidence、limitations 和 reproducibility。"""
        logger.debug("test_analysis_plan_and_artifact_contract 入口")
        from src.knowledge.asset_models import (
            AnalysisArtifact,
            AnalysisPlan,
            Evidence,
        )

        plan = AnalysisPlan(
            objective="解释退款率变化",
            asset_ids=["asset-db-orders"],
            steps=[{"name": "profile", "tool": "statistics"}],
            assumptions=["退款口径不含取消订单"],
            required_evidence=["status 定义"],
            validation_rules=[{"name": "no_causal_claim_without_control"}],
            resource_budget={"timeout_seconds": 30},
        )
        artifact = AnalysisArtifact(
            kind="report",
            data={"refund_rate": 0.2},
            narrative={"summary": "退款率上升"},
            evidence=[Evidence(
                content="退款率从 10% 上升到 20%", source_id="query-1", version="v1",
                locator={"rows": [1, 2]},
            )],
            limitations=["不能据此证明因果"],
            confidence="medium",
            reproducibility={"plan_hash": "abc"},
        )

        assert plan.asset_ids == ["asset-db-orders"]
        assert artifact.evidence[0].source_id == "query-1"
        logger.info("test_analysis_plan_and_artifact_contract 完成")
