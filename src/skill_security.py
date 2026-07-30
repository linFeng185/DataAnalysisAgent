"""Skill 包摘要、Ed25519 签名和可信签发者校验。"""

from __future__ import annotations

import base64
import hashlib
import json
from pathlib import Path
from typing import Any

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey
from cryptography.hazmat.primitives.serialization import load_pem_public_key
from pydantic import BaseModel, ConfigDict, Field

from src.logging_config import get_logger

logger = get_logger(__name__)
_SIGNATURE_FILE = "SIGNATURE.json"
_IGNORED_PARTS = {"__pycache__", ".pytest_cache"}


class SkillSignatureEnvelope(BaseModel):
    """描述独立于包摘要的 Skill 签名信封。"""

    model_config = ConfigDict(extra="forbid")

    algorithm: str = "ed25519"
    key_id: str = Field(min_length=1, max_length=128)
    digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    signature: str = Field(min_length=1)


class SkillVerificationResult(BaseModel):
    """返回可写入 Skill 运行时元数据的完整性结果。"""

    package_digest: str
    signature_verified: bool = False
    signer_key_id: str = ""


def compute_skill_package_digest(source_path: str | Path) -> str:
    """按稳定相对路径和文件内容计算 Skill 包 SHA-256 摘要。"""
    root = Path(source_path).resolve()
    if not root.is_dir():
        raise ValueError(f"Skill 目录不存在: {root}")
    files = sorted(
        path
        for path in root.rglob("*")
        if path.is_file()
        and path.name != _SIGNATURE_FILE
        and path.suffix != ".pyc"
        and not (_IGNORED_PARTS & set(path.relative_to(root).parts))
    )
    if not files:
        raise ValueError("Skill 包没有可计算摘要的文件")
    digest = hashlib.sha256()
    for path in files:
        if path.is_symlink():
            raise ValueError(f"Skill 包禁止符号链接: {path.name}")
        relative = path.relative_to(root).as_posix().encode("utf-8")
        content = path.read_bytes()
        digest.update(len(relative).to_bytes(4, "big"))
        digest.update(relative)
        digest.update(len(content).to_bytes(8, "big"))
        digest.update(content)
    result = digest.hexdigest()
    logger.info("Skill 包摘要计算完成", path=str(root), file_count=len(files), digest=result[:12])
    return result


def parse_trusted_public_keys(raw: str | dict[str, str] | None) -> dict[str, str]:
    """解析配置中的可信签发者公钥映射。"""
    if isinstance(raw, dict):
        return {str(key): str(value) for key, value in raw.items() if str(value).strip()}
    text = str(raw or "").strip()
    if not text:
        return {}
    try:
        parsed = json.loads(text)
    except json.JSONDecodeError as exc:
        raise ValueError("Skill 可信公钥必须是 JSON 对象") from exc
    if not isinstance(parsed, dict):
        raise ValueError("Skill 可信公钥必须是 JSON 对象")
    return {str(key): str(value) for key, value in parsed.items() if str(value).strip()}


def verify_skill_package(
    source_path: str | Path,
    trusted_public_keys: dict[str, str] | None = None,
    *,
    require_signature: bool,
) -> SkillVerificationResult:
    """校验包摘要和 Ed25519 签名，签名缺失时按策略阻断。"""
    root = Path(source_path).resolve()
    package_digest = compute_skill_package_digest(root)
    signature_path = root / _SIGNATURE_FILE
    if not signature_path.exists():
        if require_signature:
            raise ValueError("非内置 Skill 缺少 SIGNATURE.json")
        return SkillVerificationResult(package_digest=package_digest)
    try:
        envelope = SkillSignatureEnvelope.model_validate_json(
            signature_path.read_text(encoding="utf-8")
        )
    except Exception as exc:
        raise ValueError("Skill 签名信封格式无效") from exc
    if envelope.algorithm.lower() != "ed25519":
        raise ValueError("Skill 仅允许 Ed25519 签名")
    if envelope.digest != package_digest:
        raise ValueError("Skill 包摘要与签名信封不一致")
    public_value = (trusted_public_keys or {}).get(envelope.key_id, "")
    if not public_value:
        raise ValueError(f"Skill 签发者不受信任: {envelope.key_id}")
    public_key = _load_public_key(public_value)
    try:
        public_key.verify(
            base64.b64decode(envelope.signature, validate=True),
            bytes.fromhex(package_digest),
        )
    except (InvalidSignature, ValueError) as exc:
        raise ValueError("Skill Ed25519 签名校验失败") from exc
    logger.info(
        "Skill 包签名校验完成",
        path=str(root),
        key_id=envelope.key_id,
        digest=package_digest[:12],
    )
    return SkillVerificationResult(
        package_digest=package_digest,
        signature_verified=True,
        signer_key_id=envelope.key_id,
    )


def _load_public_key(value: str) -> Ed25519PublicKey:
    """加载 PEM 或 Base64 原始 Ed25519 公钥。"""
    text = value.strip()
    if "BEGIN PUBLIC KEY" in text:
        loaded: Any = load_pem_public_key(text.encode("utf-8"))
        if not isinstance(loaded, Ed25519PublicKey):
            raise ValueError("可信公钥不是 Ed25519 类型")
        return loaded
    try:
        return Ed25519PublicKey.from_public_bytes(base64.b64decode(text, validate=True))
    except (ValueError, TypeError) as exc:
        raise ValueError("Ed25519 公钥格式无效") from exc
