"""API 路由测试 — Schema + 端点 + 异常处理。"""

from __future__ import annotations

import pytest
from httpx import ASGITransport, AsyncClient


@pytest.fixture
def client():
    from src.main import create_app
    return AsyncClient(transport=ASGITransport(app=create_app()), base_url="http://test")


class TestSchemas:
    """11.2"""

    def test_chat_request(self):
        from src.api.schemas import ChatRequest
        assert ChatRequest(query="q").datasource == "clickhouse_prod"

    def test_chat_request_requires_query(self):
        from src.api.schemas import ChatRequest
        with pytest.raises(Exception):
            ChatRequest()

    def test_datasource_create(self):
        from src.api.schemas import DataSourceCreateRequest
        r = DataSourceCreateRequest(name="ch", dialect="clickhouse")
        assert r.name == "ch"

    def test_health_response(self):
        from src.api.schemas import HealthResponse
        assert HealthResponse().status == "ok"


class TestEndpoints:
    """11.1"""

    async def test_health(self, client):
        r = await client.get("/api/v1/health")
        assert r.status_code == 200
        assert r.json()["status"] == "ok"

    async def test_chat(self, client, monkeypatch):
        monkeypatch.setenv("OPENAI_API_KEY", "")
        r = await client.post("/api/v1/chat", json={"query": "test", "datasource": "ch"})
        assert r.status_code == 200

    async def test_list_datasources(self, client):
        r = await client.get("/api/v1/datasources")
        assert r.status_code == 200

    async def test_create_datasource(self, client):
        r = await client.post("/api/v1/datasources", json={
            "name": "tmp", "dialect": "clickhouse",
            "host": "localhost", "database": "test", "username": "r", "password": "p",
        })
        assert r.status_code in (200, 201)

    async def test_schema_not_found(self, client):
        r = await client.get("/api/v1/schema/tables?datasource=nonexistent")
        assert r.status_code == 404

    async def test_chat_validation(self, client):
        r = await client.post("/api/v1/chat", json={})
        assert r.status_code == 422
