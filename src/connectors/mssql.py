"""SQL Server 连接器 -- pymssql 驱动，通过线程池适配异步。"""
from __future__ import annotations
import asyncio
import sqlalchemy as sa
from src.connectors.base import ConnectorBase

class SQLServerConnector(ConnectorBase):
    def _build_url(self) -> str:
        cfg = self.config
        from urllib.parse import quote_plus
        pwd = quote_plus(cfg.password) if cfg.password else ""
        return f"mssql+pymssql://{cfg.username}:{pwd}@{cfg.host}:{cfg.port}/{cfg.database}"
    def _get_timeout(self) -> str | None:
        return None
    async def execute(self, sql: str, params: dict | None = None):
        def _run():
            with self._engine.connect() as conn:
                return conn.execute(sa.text(sql), params or {}).fetchall()
        return await asyncio.to_thread(_run)
    async def explain(self, sql: str) -> dict:
        try:
            await self.execute(f"SET SHOWPLAN_TEXT ON; {sql}; SET SHOWPLAN_TEXT OFF")
            return {"valid": True, "errors": []}
        except Exception as e:
            return {"valid": False, "errors": [{"type": "semantic_error", "message": str(e)[:500]}]}
    async def health_check(self) -> bool:
        try:
            await self.execute("SELECT 1")
            return True
        except Exception:
            return False
    def create_engine(self):
        cfg = self.config
        return sa.create_engine(self._build_url(), pool_size=2, max_overflow=5, pool_pre_ping=True, pool_recycle=1800)
