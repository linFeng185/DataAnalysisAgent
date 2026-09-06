"""覆盖 16.5.5 的中文文档和私有资料隔离。"""

import json
from pathlib import Path

from src.testdata.catalog import build_catalog
from src.testdata.exporter import export_dataset
from src.testdata.generator import GenerationConfig


class TestKnowledgeFixtures:
    """覆盖功能 16.5.5。"""

    # 验证文档按同一表目录生成并标注正确上传范围。
    # Args: self - 测试实例；tmp_path - 临时目录。
    # Returns: 无返回值。
    async def test_schema_documents_and_private_isolation(self, tmp_path: Path) -> None:
        """625 表结构齐全，私有标记不进入租户共享正文。"""
        output = tmp_path / "docs"
        await export_dataset(GenerationConfig(scale=100), output, ddl_only=True)
        entries = json.loads(
            (output / "knowledge_upload_manifest.json").read_text(encoding="utf-8")
        )
        schemas = [entry for entry in entries if entry["category"] == "表结构"]
        assert len(schemas) == 625
        subjects = [entry for entry in entries if entry["category"] == "业务主题"]
        assert len(subjects) == 50
        for entry in schemas:
            body = (output / entry["file"]).read_text(encoding="utf-8")
            assert "字段含义" in body and "关联" in body
            for column in build_catalog()[entry["table"]].columns:
                assert f"`{column.name}`" in body
        private = [entry for entry in entries if entry["knowledge_scope"] == "private"]
        shared = [entry for entry in entries if entry["knowledge_scope"] == "tenant"]
        assert len(private) >= 6
        assert {entry["intended_owner"] for entry in private} == {
            "财务负责人",
            "人力负责人",
            "经营负责人",
        }
        shared_text = "\n".join(
            (output / entry["file"]).read_text(encoding="utf-8") for entry in shared
        )
        assert "私有检索校验词" not in shared_text
        assert "秘密薪酬样本" not in shared_text
        for entry in private:
            body = (output / entry["file"]).read_text(encoding="utf-8")
            assert "knowledge_scope=private" in body
            assert entry["intended_owner"] in body
            assert "完全虚构" in body
        assert all(entry["knowledge_scope"] != "system" for entry in entries)
        guide = (output / "knowledge" / "上传操作说明.md").read_text(encoding="utf-8")
        assert "/api/v1/knowledge/docs/upload" in guide

    # 验证关系文档同时包含实体键和弱关联边界。
    # Args: self - 测试实例；tmp_path - 临时目录。
    # Returns: 无返回值。
    async def test_strong_and_weak_relationship_documents(self, tmp_path: Path) -> None:
        """跨源问答明确引用键、聚合粒度及相关不代表因果。"""
        output = tmp_path / "relations"
        await export_dataset(GenerationConfig(scale=100), output, ddl_only=True)
        relations = json.loads((output / "relationships.json").read_text(encoding="utf-8"))
        assert any(
            item["from_table"] == "finance_invoices" and item["to_table"] == "retail_orders"
            for item in relations["strong"]
        )
        assert any(item["from_table"] == "insight_weather_daily" for item in relations["weak"])
        assert all("因果" in item["warning"] for item in relations["weak"])
        questions = json.loads((output / "questions.json").read_text(encoding="utf-8"))
        assert len(questions) >= 12
        assert all(item["question"] and item["sources"] for item in questions)
