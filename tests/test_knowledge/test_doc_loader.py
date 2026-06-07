"""DocLoader 测试."""
from __future__ import annotations
import os, tempfile
from src.knowledge.doc_loader import DocLoader
from src.knowledge.models import KnowledgeSource

class TestParseFrontmatter:
    def test_with_frontmatter(self):
        loader = DocLoader()
        fm = "---" + chr(10) + "category: test" + chr(10) + "---" + chr(10) + chr(10) + "body"
        meta, body = loader._parse_frontmatter(fm)
        assert meta.get("category") == "test"
    def test_without_frontmatter(self):
        loader = DocLoader()
        meta, body = loader._parse_frontmatter("# Just heading" + chr(10) + "text")
        assert meta == {}

class TestSplitByHeadings:
    def test_splits_correctly(self):
        loader = DocLoader()
        body = "intro" + chr(10) + chr(10) + "## A" + chr(10) + "aa" + chr(10) + chr(10) + "## B" + chr(10) + "bb"
        entries = loader._split_by_headings(body, "business_rule", ["test"], ["orders"])
        assert len(entries) == 3
        assert all(e.source == KnowledgeSource.MANUAL_DOC for e in entries)
        assert all(e.ttl == 0 for e in entries)

class TestScanAndLoad:
    def test_loads_from_directory(self):
        with tempfile.TemporaryDirectory() as tmp:
            md = os.path.join(tmp, "test.md")
            with open(md, "w", encoding="utf-8") as f:
                f.write("---" + chr(10) + "category: business_rule" + chr(10) + "tags: [demo]" + chr(10) + "---" + chr(10) + chr(10) + "# T" + chr(10) + chr(10) + "## O" + chr(10) + "Desc")
            loader = DocLoader(docs_dir=tmp)
            entries = loader.scan_and_load()
            assert len(entries) >= 1
    def test_empty_directory(self):
        with tempfile.TemporaryDirectory() as tmp:
            assert DocLoader(docs_dir=tmp).scan_and_load() == []
    def test_nonexistent_directory(self):
        assert DocLoader(docs_dir="/nonexistent/path/99999").scan_and_load() == []
