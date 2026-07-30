"""Skill 包签名与完整性校验测试。"""

from __future__ import annotations

import base64
import json

import pytest


class TestSkillPackageSecurity:
    """覆盖功能 9.1.13 的摘要、Ed25519 签名和篡改阻断。"""

    def test_signed_skill_verifies_and_tampering_is_blocked(self, tmp_path) -> None:
        """可信签发者签名应通过，签名后的任意文件修改都必须被阻断。"""
        # Arrange
        from cryptography.hazmat.primitives import serialization
        from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

        from src.skill_security import compute_skill_package_digest, verify_skill_package

        skill_dir = tmp_path / "signed"
        skill_dir.mkdir()
        manifest = skill_dir / "SKILL.md"
        manifest.write_text("---\nname: signed\nversion: 1.0.0\n---\n说明\n", encoding="utf-8")
        (skill_dir / "tools.py").write_text("VALUE = 1\n", encoding="utf-8")
        private_key = Ed25519PrivateKey.generate()
        public_key = private_key.public_key().public_bytes(
            serialization.Encoding.Raw,
            serialization.PublicFormat.Raw,
        )
        digest = compute_skill_package_digest(skill_dir)
        envelope = {
            "algorithm": "ed25519",
            "key_id": "release-key",
            "digest": digest,
            "signature": base64.b64encode(
                private_key.sign(bytes.fromhex(digest))
            ).decode("ascii"),
        }
        (skill_dir / "SIGNATURE.json").write_text(
            json.dumps(envelope),
            encoding="utf-8",
        )

        # Act
        verified = verify_skill_package(
            skill_dir,
            {"release-key": base64.b64encode(public_key).decode("ascii")},
            require_signature=True,
        )
        (skill_dir / "tools.py").write_text("VALUE = 2\n", encoding="utf-8")

        # Assert
        assert verified.signature_verified is True
        assert verified.signer_key_id == "release-key"
        assert verified.package_digest == digest
        with pytest.raises(ValueError, match="摘要"):
            verify_skill_package(
                skill_dir,
                {"release-key": base64.b64encode(public_key).decode("ascii")},
                require_signature=True,
            )

    def test_unsigned_managed_skill_is_rejected_when_required(self, tmp_path) -> None:
        """生产策略开启时，受管目录中的未签名 Skill 不得进入缓存。"""
        # Arrange
        from src.skill_manager import SkillManager

        skill_dir = tmp_path / "managed" / "private" / "2" / "7" / "unsigned"
        skill_dir.mkdir(parents=True)
        (skill_dir / "SKILL.md").write_text(
            "---\nname: unsigned\nversion: 1.0.0\n---\n说明\n",
            encoding="utf-8",
        )
        manager = SkillManager(
            builtin_dir=str(tmp_path / "builtin"),
            managed_dir=str(tmp_path / "managed"),
            require_signatures=True,
        )

        # Act / Assert
        with pytest.raises(ValueError, match="SIGNATURE.json"):
            manager.load_skill_manifest(
                skill_dir / "SKILL.md",
                scope="private",
                tenant_id=2,
                owner_user_id=7,
            )
