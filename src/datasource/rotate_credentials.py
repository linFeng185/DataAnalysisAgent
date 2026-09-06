"""离线维护命令：核验或原子轮换数据源与租户模型的存储加密密钥。"""

from __future__ import annotations

import argparse
import asyncio
from typing import Any

from src.datasource.credential_manager import CredentialManager
from src.logging_config import get_logger, setup_logging

logger = get_logger(__name__)
_CREDENTIAL_COLUMNS = (
    ("datasource_configs", "encrypted_password"),
    ("tenant_llm_connections", "encrypted_api_key"),
)


# 方法作用：在互斥事务中验证全部租户凭证，可选写回当前版本密文。
# Args: connection - 具有全表维护权限的独立连接；manager - 新旧密钥集合；apply - 是否写入。
# Returns: 核验数、改写数和剩余待改写数，不含任何凭证。
async def rotate_persisted_credentials(
    connection: Any, manager: CredentialManager, *, apply: bool = False,
) -> dict[str, int]:
    """调用前必须停止业务写入；默认只读核验，任何失败均回滚两张表。"""
    logger.debug("rotate_persisted_credentials 入口", apply=apply)
    if not manager.key_id:
        raise ValueError("轮换要求配置新的 credential_key_id")
    result = {"checked": 0, "changed": 0, "pending": 0}
    async with connection.transaction():
        locked = await connection.fetchval("SELECT pg_try_advisory_xact_lock(726038519)")
        if not locked:
            raise RuntimeError("其他凭证轮换正在执行")
        # 不具有全表权限的维护连接应报错，而不是静默遗漏其他租户。
        await connection.execute("SET LOCAL row_security = off")
        await connection.execute("SET LOCAL lock_timeout = '5s'")
        for table, column in _CREDENTIAL_COLUMNS:
            after_id = 0
            while rows := await connection.fetch(
                f"SELECT id, {column} AS ciphertext FROM {table} "
                "WHERE id > $1 ORDER BY id LIMIT $2 FOR UPDATE", after_id, 100,
            ):
                for row in rows:
                    ciphertext = str(row["ciphertext"] or "")
                    after_id = int(row["id"])
                    result["checked"] += 1
                    if not ciphertext:
                        continue
                    plain = await asyncio.to_thread(manager.decrypt, ciphertext)
                    if ciphertext.startswith(f"v3:{manager.key_id}:"):
                        continue
                    if not apply:
                        result["pending"] += 1
                        continue
                    replacement = await asyncio.to_thread(manager.encrypt, plain)
                    await connection.execute(
                        f"UPDATE {table} SET {column}=$1, updated_at=NOW() WHERE id=$2",
                        replacement, after_id,
                    )
                    result["changed"] += 1
    logger.info("凭证轮换事务完成", apply=apply, **result)
    return result


# 方法作用：加载托管配置并在独立应用容器中运行维护事务。
# Args: apply - 是否执行写入。
# Returns: 无返回值；失败由 CLI 统一记录并以非零状态退出。
async def _run_rotation(apply: bool) -> None:
    from src.app_context import AppContext, use_app_context_async
    from src.config import Settings, validate_production_settings
    from src.datasource.secret_provider import load_secret_settings
    from src.memory.pg_pool import get_pg_pool

    settings = await load_secret_settings(Settings())
    validate_production_settings(settings)
    context = AppContext(settings)
    async with use_app_context_async(context):
        try:
            pool = await get_pg_pool()
            async with pool.acquire() as connection:
                await rotate_persisted_credentials(connection, CredentialManager(), apply=apply)
        finally:
            await context.close()


# 方法作用：提供显式停机确认与默认只核验的维护命令入口。
# Args: argv - 可选命令行参数，用于测试。
# Returns: 成功为 0，执行失败为 1；参数错误由 argparse 返回 2。
def main(argv: list[str] | None = None) -> int:
    """不接受命令行明文密钥，不写入 Vault，也不自动删除历史密钥。"""
    parser = argparse.ArgumentParser(description="离线核验和轮换存储凭证，默认不写入")
    parser.add_argument("--apply", action="store_true", help="将凭证改写为当前密钥版本")
    parser.add_argument("--offline-confirmed", action="store_true", help="确认已经停止全部业务写入")
    options = parser.parse_args(argv)
    if options.apply and not options.offline_confirmed:
        parser.error("写入轮换必须提供 --offline-confirmed 并已停止所有应用和 Worker")
    setup_logging()
    logger.debug("凭证轮换 CLI 入口", apply=options.apply)
    try:
        asyncio.run(_run_rotation(options.apply))
    except Exception:
        logger.error("凭证轮换失败，事务已回滚", exc_info=True)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
