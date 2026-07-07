"""VectorStore + FakeVectorStore 单元测试。"""

from __future__ import annotations

import pytest

from src.memory.vector_store import VectorEntry, VectorSearchResult, VectorStore


class FakeVectorStore(VectorStore):
    """内存实现 VectorStore，供测试。"""

    def __init__(self):
        self._entries: dict[str, VectorEntry] = {}

    async def search(self, query, top_k=5, filters=None):
        results = []
        for e in self._entries.values():
            if filters:
                if any(e.metadata.get(k) != v for k, v in filters.items()):
                    continue
            score = 0.8 if query.lower() in e.content.lower() else 0.1
            if score > 0.1:
                results.append(VectorSearchResult(id=e.id, content=e.content,
                    metadata=e.metadata, score=score))
        results.sort(key=lambda r: r.score, reverse=True)
        return results[:top_k]

    async def get_by_id(self, entry_id):
        return self._entries.get(entry_id)

    async def get_by_filter(self, filters, limit=100):
        r = [e for e in self._entries.values()
             if all(e.metadata.get(k) == v for k, v in filters.items())]
        return r[:limit]

    async def upsert(self, entries):
        for e in entries:
            self._entries[e.id] = e
        return len(entries)

    async def delete_by_ids(self, ids):
        c = 0
        for i in ids:
            if self._entries.pop(i, None):
                c += 1
        return c

    async def delete_by_filter(self, filters):
        to_del = [e.id for e in (await self.get_by_filter(filters, limit=9999))]
        for i in to_del:
            self._entries.pop(i, None)
        return len(to_del)

    async def count(self, filters=None):
        if filters:
            return len(await self.get_by_filter(filters))
        return len(self._entries)

    async def health_check(self):
        return True


class TestVectorStore:
    @pytest.fixture
    def vs(self):
        return FakeVectorStore()

    @pytest.mark.asyncio
    async def test_upsert_and_count(self, vs):
        await vs.upsert([VectorEntry(id="1", content="hello", metadata={})])
        assert await vs.count() == 1

    @pytest.mark.asyncio
    async def test_search_match(self, vs):
        await vs.upsert([VectorEntry(id="1", content="MySQL 8.0 doc", metadata={"ds": "mysql"})])
        r = await vs.search("MySQL")
        assert len(r) == 1
        assert r[0].score > 0.5

    @pytest.mark.asyncio
    async def test_search_filter(self, vs):
        await vs.upsert([
            VectorEntry(id="m1", content="MySQL", metadata={"ds": "mysql"}),
            VectorEntry(id="p1", content="PG", metadata={"ds": "postgres"}),
        ])
        r = await vs.search("MySQL", filters={"ds": "mysql"})
        assert len(r) == 1
        assert r[0].id == "m1"

    @pytest.mark.asyncio
    async def test_get_by_id(self, vs):
        await vs.upsert([VectorEntry(id="abc", content="test", metadata={"x": "y"})])
        e = await vs.get_by_id("abc")
        assert e is not None
        assert e.content == "test"

    @pytest.mark.asyncio
    async def test_delete(self, vs):
        await vs.upsert([VectorEntry(id=str(i), content=f"d{i}", metadata={}) for i in range(3)])
        assert await vs.delete_by_ids(["0", "1"]) == 2
        assert await vs.count() == 1

    @pytest.mark.asyncio
    async def test_upsert_overwrite(self, vs):
        await vs.upsert([VectorEntry(id="1", content="old", metadata={})])
        await vs.upsert([VectorEntry(id="1", content="new", metadata={})])
        e = await vs.get_by_id("1")
        assert e.content == "new"

    @pytest.mark.asyncio
    async def test_health(self, vs):
        assert await vs.health_check()
