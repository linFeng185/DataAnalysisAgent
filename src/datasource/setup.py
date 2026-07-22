"""演示数据源 — SQLite 内存库自动初始化，零配置即可体验。"""

from __future__ import annotations

from src.datasource.config import DataSourceConfig
from src.datasource.schema_snapshot import ColumnInfo, SchemaSnapshot, TableSchema
from src.logging_config import get_logger

logger = get_logger(__name__)

DEMO = "demo"

SCHEMA = {
    "orders": {
        "desc": "订单表 — 金额单位元, status: paid=已支付 refunded=已退款",
        "cols": [
            ("order_id", "INTEGER", "订单ID", True),
            ("user_id", "INTEGER", "用户ID", False),
            ("category", "TEXT", "品类: 电子/美妆/家居", False),
            ("amount", "REAL", "订单金额(元)", False),
            ("status", "TEXT", "paid=已支付 refunded=已退款", False),
            ("created_at", "TEXT", "日期 YYYY-MM-DD", False),
        ],
    },
    "users": {
        "desc": "用户表",
        "cols": [
            ("user_id", "INTEGER", "用户ID", True),
            ("name", "TEXT", "姓名", False),
            ("city", "TEXT", "城市", False),
        ],
    },
}


async def ensure_demo_datasource() -> DataSourceConfig:
    """启动时自动创建 SQLite 内存演示库。"""
    logger.debug("初始化演示数据源入口")
    from sqlalchemy.ext.asyncio import create_async_engine
    import sqlalchemy as sa

    engine = create_async_engine("sqlite+aiosqlite:///:memory:", poolclass=sa.pool.StaticPool)

    async with engine.begin() as conn:
        await conn.execute(sa.text("""
            CREATE TABLE orders (order_id INTEGER PRIMARY KEY, user_id INTEGER,
                category TEXT, amount REAL, status TEXT, created_at TEXT)"""))
        await conn.execute(sa.text("""
            CREATE TABLE users (user_id INTEGER PRIMARY KEY, name TEXT, city TEXT)"""))

        # 15 条订单 + 3 个用户
        await conn.execute(sa.text("""
            INSERT INTO orders VALUES
            (1,1,'电子',128000,'paid','2026-06-01'),(2,2,'家居',45000,'paid','2026-06-01'),
            (3,1,'家居',102000,'paid','2026-06-02'),(4,3,'美妆',28000,'paid','2026-06-02'),
            (5,2,'电子',88000,'paid','2026-06-02'),(6,1,'美妆',156000,'paid','2026-06-03'),
            (7,3,'电子',67000,'paid','2026-06-03'),(8,2,'家居',98000,'paid','2026-06-04'),
            (9,1,'美妆',32000,'paid','2026-06-04'),(10,3,'家居',215000,'paid','2026-06-04'),
            (11,2,'电子',145000,'paid','2026-06-05'),(12,1,'美妆',89000,'paid','2026-06-05'),
            (13,3,'家居',56000,'paid','2026-06-05'),(14,2,'美妆',134000,'refunded','2026-06-05'),
            (15,1,'电子',99000,'paid','2026-06-05')"""))
        await conn.execute(sa.text("""
            INSERT INTO users VALUES (1,'张三','北京'),(2,'李四','上海'),(3,'王五','深圳')"""))

    # 构建 Schema
    tables = []
    for name, info in SCHEMA.items():
        cols = [ColumnInfo(name=n, type=t, comment=d, is_primary_key=p) for n, t, d, p in info["cols"]]
        tables.append(TableSchema(name=name, description=info["desc"], columns=cols))

    ds = DataSourceConfig(name=DEMO, mode="embedded", dialect="sqlite",
                          description="演示数据源 (SQLite 内存)", extra_params={"db_path": ":memory:"})
    ds.engine = engine
    ds.schema = SchemaSnapshot(tables=tables)

    from src.datasource.registry import get_registry
    get_registry().register_config(ds)

    logger.info("演示数据源就绪", tables=["orders(15行)", "users(3行)"])
    return ds
