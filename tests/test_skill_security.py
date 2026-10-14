"""Skill 包签名与完整性校验测试。"""

from __future__ import annotations

import base64
import hashlib
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


class TestSkillRootGitMetadataDigest:
    """覆盖 9.1.13 / 9.4.2：技能根 .git 元数据不参与摘要，实际技能文件仍全量参与。"""

    # 基准包内容：两种 .git 形态对照时保持不变
    _BASE_FILES = {
        "SKILL.md": "---\nname: signed\nversion: 1.0.0\n---\n说明\n",
        "tools.py": "VALUE = 1\n",
    }

    # 方法作用：构造技能包目录并写入给定文件。
    # Args: root - 技能包根目录；files - 相对路径到内容的映射。
    # Returns: 无返回值。
    def _write_package(self, root, files: dict[str, str]) -> None:
        root.mkdir(parents=True, exist_ok=True)
        for relative, content in files.items():
            target = root / relative
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(content, encoding="utf-8")

    # 方法作用：验证普通仓库 .git 目录与子模块 .git 指针文件两种形态得到同一摘要。
    # Args: self - 测试实例；tmp_path - 临时目录。
    # Returns: 无返回值，断言失败时由 pytest 报告。
    def test_root_git_metadata_form_does_not_change_digest(self, tmp_path) -> None:
        """安装从目录形态变为指针文件形态，技能内容未变时摘要必须保持稳定。"""
        # Arrange：两种 .git 形态的包内容完全一致
        plain = tmp_path / "plain"
        self._write_package(plain, self._BASE_FILES)
        (plain / ".git").mkdir()
        (plain / ".git" / "HEAD").write_text("ref: refs/heads/main\n", encoding="utf-8")
        (plain / ".git" / "config").write_text("[core]\n", encoding="utf-8")
        submodule = tmp_path / "submodule"
        self._write_package(submodule, self._BASE_FILES)
        (submodule / ".git").write_text(
            "gitdir: ../.git/modules/submodule\n", encoding="utf-8",
        )

        from src.skill_security import compute_skill_package_digest

        # Act
        plain_digest = compute_skill_package_digest(plain)
        submodule_digest = compute_skill_package_digest(submodule)

        # Assert：形态不同摘要相同，且 .git 内部内容变化不影响摘要
        assert plain_digest == submodule_digest
        (plain / ".git" / "HEAD").write_text("ref: refs/heads/other\n", encoding="utf-8")
        assert compute_skill_package_digest(plain) == submodule_digest

    # 方法作用：验证排除只限技能根，一层子目录内名为 .git 的条目仍参与摘要。
    # Args: self - 测试实例；tmp_path - 临时目录。
    # Returns: 无返回值，断言失败时由 pytest 报告。
    def test_nested_git_named_entry_is_still_digested(self, tmp_path) -> None:
        """不得把排除规则泛化到其它目录，否则实际内容变化会漏出摘要。"""
        # Arrange
        root = tmp_path / "pkg"
        self._write_package(
            root,
            {**self._BASE_FILES, "sub/.git": "gitdir: ../real-git-dir\n"},
        )

        from src.skill_security import compute_skill_package_digest

        # Act
        before = compute_skill_package_digest(root)
        (root / "sub" / ".git").write_text("gitdir: ../other-git-dir\n", encoding="utf-8")
        after = compute_skill_package_digest(root)

        # Assert
        assert after != before

    # 方法作用：验证无技能根 .git 的包摘要与历史算法逐字节一致（既不加宽也不收紧）。
    # Args: self - 测试实例；tmp_path - 临时目录。
    # Returns: 无返回值，断言失败时由 pytest 报告。
    def test_digest_without_root_git_matches_legacy_algorithm(self, tmp_path) -> None:
        """保持既有包摘要不变：嵌套目录、字节码与子目录内 .git 条目全部按旧规则参与。"""
        # Arrange
        root = tmp_path / "pkg"
        self._write_package(
            root,
            {**self._BASE_FILES, "assets/template.txt": "模板\n", "sub/.git": "gitdir: x\n"},
        )
        (root / "tools.pyc").write_bytes(b"\x00\x01")
        (root / "__pycache__").mkdir()
        (root / "__pycache__" / "tools.pyc").write_bytes(b"\x02")
        legacy = hashlib.sha256()
        legacy_files = sorted(
            path
            for path in root.rglob("*")
            if path.is_file()
            and path.name != "SIGNATURE.json"
            and path.suffix != ".pyc"
            and not ({"__pycache__", ".pytest_cache"} & set(path.relative_to(root).parts))
        )
        for path in legacy_files:
            relative = path.relative_to(root).as_posix().encode("utf-8")
            content = path.read_bytes()
            legacy.update(len(relative).to_bytes(4, "big"))
            legacy.update(relative)
            legacy.update(len(content).to_bytes(8, "big"))
            legacy.update(content)

        from src.skill_security import compute_skill_package_digest

        # Act / Assert
        assert compute_skill_package_digest(root) == legacy.hexdigest()

    # 方法作用：验证摘要一致但签发者不在可信集合时签名校验仍然拒绝。
    # Args: self - 测试实例；tmp_path - 临时目录。
    # Returns: 无返回值，断言失败时由 pytest 报告。
    def test_unknown_signer_key_is_rejected(self, tmp_path) -> None:
        """包未篡改、签名有效也不够，签发者身份必须由可信公钥映射确认。"""
        # Arrange
        from cryptography.hazmat.primitives import serialization
        from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

        from src.skill_security import compute_skill_package_digest, verify_skill_package

        root = tmp_path / "signed"
        self._write_package(root, self._BASE_FILES)
        private_key = Ed25519PrivateKey.generate()
        digest = compute_skill_package_digest(root)
        (root / "SIGNATURE.json").write_text(
            json.dumps(
                {
                    "algorithm": "ed25519",
                    "key_id": "release-key",
                    "digest": digest,
                    "signature": base64.b64encode(
                        private_key.sign(bytes.fromhex(digest))
                    ).decode("ascii"),
                }
            ),
            encoding="utf-8",
        )
        other_public = base64.b64encode(
            private_key.public_key().public_bytes(
                serialization.Encoding.Raw,
                serialization.PublicFormat.Raw,
            )
        ).decode("ascii")

        # Act / Assert：可信集合中不存在 release-key
        with pytest.raises(ValueError, match="签发者"):
            verify_skill_package(
                root,
                {"other-key": other_public},
                require_signature=True,
            )
