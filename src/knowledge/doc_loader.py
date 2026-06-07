"""文档加载器."""
from __future__ import annotations
import os, re
from datetime import datetime, timezone
from src.knowledge.models import KnowledgeEntry, KnowledgeSource
from src.logging_config import get_logger
logger = get_logger(__name__)
_FRONTMATTER_RE = re.compile(r"^---\s*\n(.*?)\n---", re.DOTALL | re.MULTILINE)

class DocLoader:
    def __init__(self, docs_dir: str = "docs/metrics") -> None:
        self.docs_dir = docs_dir

    def scan_and_load(self):
        if not os.path.isdir(self.docs_dir):
            return []
        entries = []
        for root, _, files in os.walk(self.docs_dir):
            for fname in sorted(files):
                if not fname.endswith(".md"):
                    continue
                try:
                    entries.extend(self._load_file(os.path.join(root, fname)))
                except Exception as e:
                    logger.warning("fail", error=str(e))
        return entries

    def _load_file(self, filepath: str):
        with open(filepath, "r", encoding="utf-8") as f:
            content = f.read()
        metadata, body = self._parse_frontmatter(content)
        basename = os.path.splitext(os.path.basename(filepath))[0]
        tags = list(metadata.get("tags", []))
        if isinstance(tags, str):
            tags = [tags]
        tags.append(basename)
        return self._split_by_headings(body, metadata.get("category", "business_rule"), tags, metadata.get("tables", []))

    def _parse_frontmatter(self, content: str):
        m = _FRONTMATTER_RE.search(content)
        if not m:
            return {}, content
        raw_yaml = m.group(1)
        body = content[m.end():]
        try:
            import yaml
            metadata = yaml.safe_load(raw_yaml) or {}
        except Exception:
            metadata = self._simple_parse(raw_yaml)
        return metadata, body.strip()

    def _simple_parse(self, raw: str):
        result = {}
        for line in raw.strip().split(chr(10)):
            line = line.strip()
            if ":" in line and not line.startswith("#"):
                key, _, val = line.partition(":")
                key = key.strip()
                val = val.strip().strip('"').strip("'")
                if val.startswith("[") and val.endswith("]"):
                    result[key] = [v.strip().strip('"').strip("'") for v in val[1:-1].split(",")]
                else:
                    result[key] = val
        return result

    def _split_by_headings(self, body, category, tags, tables):
        bs = chr(92)
        chunks = re.split(bs + "n(?=## )", body)
        entries = []
        now = datetime.now(timezone.utc)
        for i, chunk in enumerate(chunks):
            chunk = chunk.strip()
            if not chunk:
                continue
            lines = chunk.split(chr(10), 1)
            title = lines[0][3:].strip() if lines[0].startswith("## ") else ""
            chunk_body = lines[1].strip() if len(lines) > 1 else ""
            tag_prefix = tags[0] if tags else "doc"
            slug = re.sub(r"[^a-zA-Z0-9_]", "_", title.lower())[:40]
            chunk_id = f"doc:{tag_prefix}.{slug or str(i)}"
            content = f"{title}: {chunk_body[:300]}" if title else chunk_body[:300]
            entries.append(KnowledgeEntry(
                id=chunk_id, content=content, source=KnowledgeSource.MANUAL_DOC,
                category=category, table_name=tables[0] if tables else "",
                tags=tags, created_at=now, ttl=0,
            ))
        return entries