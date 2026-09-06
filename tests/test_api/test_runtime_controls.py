"""25.1、25.2 平台运行时边界测试。"""

from contextlib import asynccontextmanager
from unittest.mock import AsyncMock

from fastapi import FastAPI
from fastapi.testclient import TestClient


class TestRuntimeControls:
    """覆盖可搜索用户目录、认证和动态配置输入验证。"""

    # 验证目录不返回密码、不枚举其他租户用户，搜索不解释 SQL 通配符。
    # Args: self - 实例；monkeypatch - 边界替身。
    # Returns: 无返回值。
    def test_recipients_search(self, monkeypatch) -> None:
        """仅返回实际可以接受授权的启用平台账号。"""
        import src.api.routes.emergency_access as route
        connection = AsyncMock()
        connection.fetch.return_value = [{"id": 1, "username": "admin"}]
        # 提供不访问真实数据库的事务边界。
        # Args: kwargs - 当前身份。
        # Returns: 模拟连接。
        @asynccontextmanager
        async def connect(**kwargs):
            yield connection
        monkeypatch.setattr(route, "pg_connection", connect)
        monkeypatch.setattr(route, "get_current_user_id", lambda: 7)
        app = FastAPI()
        app.include_router(route.router)
        client = TestClient(app)
        response = client.get("/knowledge/emergency-grants/recipients?query=ad%25")
        assert response.status_code == 200
        assert response.json() == {"items": [{"id": 1, "username": "admin"}]}
        assert "strpos" in connection.fetch.await_args.args[0]
        assert connection.fetch.await_args.args[1:] == (1, "ad%")
        monkeypatch.setattr(route, "get_current_user_id", lambda: 0)
        assert client.get("/knowledge/emergency-grants/recipients").status_code == 401
