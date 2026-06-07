"""Oracle 连接器 -- oracledb 驱动，通过线程池适配异步。"""
from __future__ import annotations
import asyncio
import sqlalchemy as sa
from src.connectors.base import ConnectorBase

class OracleConnector(ConnectorBase):
    def _build_url(self) -> str:
        cfg = self.config
        from urllib.parse import quote_plus
        pwd = quote_plus(cfg.password) if cfg.password else ""
        return f"oracle+oracledb://{cfg.username}:{pwd}@{cfg.host}:{cfg.port}/?service_name={cfg.database}"
    def _get_timeout(self) -> str | None:
        return None
    async def execute(self, sql: str, params: dict | None = None):
        def _run():
            with self._engine.connect() as conn:
                return conn.execute(sa.text(sql), params or {}).fetchall()
        return await asyncio.to_thread(_run)
    async def explain(self, sql: str) -> dict:
        try:
            await self.execute(f"EXPLAIN PLAN FOR {sql}")
            return {"valid": True, "errors": []}
        except Exception as e:
            return {"valid": False, "errors": [{"type": "semantic_error", "message": str(e)[:500]}]}
    async def health_check(self) -> bool:
        try:
            await self.execute("SELECT 1 FROM DUAL")
            return True
        except Exception:
            return False
    def create_engine(self):
        cfg = self.config
        url = sa.URL.create("oracle+oracledb", username=cfg.username, password=cfg.password, host=cfg.host, port=cfg.port, database=cfg.database)
        return sa.create_engine(url, pool_size=2, max_overflow=5, pool_pre_ping=True, pool_recycle=1800)
