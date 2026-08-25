"""报表分享 token、授权码和用户范围安全辅助。"""

from __future__ import annotations

import hashlib
import hmac
import secrets
from typing import Any

import bcrypt

from src.config import get_settings
from src.logging_config import get_logger

logger = get_logger(__name__)


# 方法作用：使用部署密钥生成分享 token 的不可逆摘要。
# Args: token - 仅在创建响应中返回的明文 token。
# Returns: 可写入数据库和审计索引的 HMAC 摘要。
def hash_share_token(token: str) -> str:
    secret = str(getattr(get_settings(), "jwt_secret", "") or "report-share-fallback").encode("utf-8")
    return hmac.new(secret, token.encode("utf-8"), hashlib.sha256).hexdigest()


# 方法作用：生成至少 128 bit 熵的公开分享 token。
# Args: 无。
# Returns: URL 安全 token 和摘要。
def generate_share_token() -> tuple[str, str]:
    token = secrets.token_urlsafe(32)
    return token, hash_share_token(token)


# 方法作用：异步哈希授权码，避免明文进入数据库或日志。
# Args: code - 用户设置的授权码。
# Returns: bcrypt 哈希。
async def hash_authorization_code(code: str) -> str:
    if len(code.encode("utf-8")) > 72:
        raise ValueError("授权码不能超过 72 字节")
    return bcrypt.hashpw(code.encode("utf-8"), bcrypt.gensalt(rounds=12)).decode("ascii")


# 方法作用：校验用户提交的授权码并保持 bcrypt 安全边界。
# Args: code - 用户输入授权码；code_hash - 数据库哈希。
# Returns: 校验成功返回 True，否则返回 False。
def verify_authorization_code(code: str, code_hash: str) -> bool:
    if not code_hash or len(code.encode("utf-8")) > 72:
        return False
    try:
        return bool(bcrypt.checkpw(code.encode("utf-8"), code_hash.encode("ascii")))
    except (ValueError, TypeError):
        return False


# 方法作用：判断当前身份能否访问指定用户范围的分享。
# Args: identity - 当前身份；created_by - 分享创建者；scope - public/user；user_ids - 指定用户。
# Returns: 有权访问返回 True。
def can_access_user_scope(identity: Any, created_by: int, scope: str, user_ids: list[int]) -> bool:
    role = str(getattr(identity, "role", "") or "")
    user_id = int(getattr(identity, "user_id", 0) or 0)
    if scope == "public":
        return True
    if role in {"super_admin", "tenant_admin"}:
        return True
    return user_id == int(created_by) or user_id in {int(item) for item in user_ids}
