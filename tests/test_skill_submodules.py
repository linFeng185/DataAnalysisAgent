"""功能 9.4.2 Git 子模块支持 —— 社区子模块发现、固定校验与信任边界。

设计依据：spec/57-skill-submodule-discovery.md；上层渠道见 spec/03-core-components.md §3.10.4。

全部用例使用 tmp_path 下的真实本地 Git 仓库（git init + 本地 submodule add + 白名单 HTTPS URL），
不联网、不调用远程模型或数据库；生产代码也从不执行 clone/fetch/pull/update。
"""

from __future__ import annotations

import asyncio
import base64
import json
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path

import pytest

_GIT_AVAILABLE = shutil.which("git") is not None

pytestmark = pytest.mark.skipif(
    not _GIT_AVAILABLE,
    reason="社区子模块用例需要本地 git 可执行文件（真实本地仓库夹具）",
)

# 测试夹具专用 git 参数：固定身份、禁止签名交互、允许本地 file 传输（仅测试内使用）。
_GIT_PREFIX = [
    "-c",
    "user.name=skill-submodule-test",
    "-c",
    "user.email=skill-submodule-test@example.com",
    "-c",
    "commit.gpgsign=false",
    "-c",
    "protocol.file.allow=always",
    "-c",
    "core.autocrlf=false",
]


# 方法作用：在指定目录执行本地 git 命令，供测试夹具构造真实仓库。
# Args: cwd - 执行目录（必须存在）；args - git 子命令与参数；check - 是否要求返回码为 0。
# Returns: 标准输出（已去除首尾空白）。
def _run_git(cwd: Path, *args: str, check: bool = True) -> str:
    completed = subprocess.run(
        ["git", *_GIT_PREFIX, *args],
        cwd=str(cwd),
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        check=False,
    )
    if check and completed.returncode != 0:
        raise AssertionError(f"git {' '.join(args)} 失败: {completed.stderr.strip()}")
    return completed.stdout.strip()


# 方法作用：写入带 frontmatter 的技能清单，供社区与内置夹具共用。
# Args: manifest_path - SKILL.md 路径；name - 技能名；version - 版本；keyword - 触发关键词；tools_block - 工具声明块。
# Returns: 无返回值。
def _write_manifest(
    manifest_path: Path,
    name: str,
    *,
    version: str = "1.0.0",
    keyword: str = "社区",
    tools_block: str = "",
) -> None:
    manifest_path.parent.mkdir(parents=True, exist_ok=True)
    manifest_path.write_text(
        "---\n"
        f"name: {name}\n"
        f"version: {version}\n"
        f"description: {name} 技能\n"
        "triggers:\n"
        f"  keywords: [{keyword}]\n"
        f"{tools_block}"
        "---\n说明\n",
        encoding="utf-8",
    )


# 方法作用：写入内置目录下的最小技能，用于验证社区来源不影响其它来源。
# Args: builtin_dir - 内置目录；name - 技能名；version - 版本；keyword - 触发关键词。
# Returns: 技能目录路径。
def _write_builtin_skill(
    builtin_dir: Path,
    name: str,
    *,
    version: str = "1.0.0",
    keyword: str = "内置",
) -> Path:
    skill_dir = builtin_dir / name
    _write_manifest(skill_dir / "SKILL.md", name, version=version, keyword=keyword)
    return skill_dir


class _CommunityFixture:
    """父仓库 + 本地社区技能仓库 + 白名单 HTTPS URL 的构造夹具。"""

    DEFAULT_URL = "https://git.example.com/acme/community-skills.git"

    # 方法作用：创建父仓库骨架（含 skills/community 与受管目录）。
    # Args: root - 用例临时目录。
    # Returns: 无返回值。
    def __init__(self, root: Path) -> None:
        self.root = root
        self.parent = root / "parent"
        self.builtin = self.parent / "skills"
        self.community_root = self.builtin / "community"
        self.managed = self.parent / "data" / "skills"
        self.remote_root = root / "remotes"
        self.community_root.mkdir(parents=True)
        self.remote_root.mkdir(parents=True)
        _run_git(self.parent, "init", "-q")
        (self.parent / "README.md").write_text("parent\n", encoding="utf-8")
        _run_git(self.parent, "add", "-A")
        _run_git(self.parent, "commit", "-q", "-m", "init")

    # 方法作用：创建一个本地"远程"社区技能仓库并提交。
    # Args: leaf - 仓库目录名；name - 技能名；files - 额外的相对路径到内容映射；root_manifest - 是否使用根 SKILL.md。
    # Returns: 仓库路径。
    def make_skill_repo(
        self,
        leaf: str,
        name: str,
        *,
        files: dict[str, str] | None = None,
        root_manifest: bool = True,
    ) -> Path:
        repo = self.remote_root / leaf
        repo.mkdir(parents=True)
        if root_manifest:
            _write_manifest(repo / "SKILL.md", name)
        if files:
            for relative, content in files.items():
                target = repo / relative
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_text(content, encoding="utf-8")
        _run_git(repo, "init", "-q")
        _run_git(repo, "add", "-A")
        _run_git(repo, "commit", "-q", "-m", "skill")
        return repo

    # 方法作用：把本地仓库安装为子模块，并把 .gitmodules 与 origin 改写成白名单 HTTPS URL。
    # Args: leaf - 子模块目录名；repo - 本地仓库路径；url - 声明与远端 URL；target - 可选的安装相对路径。
    # Returns: gitlink 固定的 commit。
    def install(
        self,
        leaf: str,
        repo: Path,
        *,
        url: str | None = None,
        target: str | None = None,
    ) -> str:
        relative = target or f"skills/community/{leaf}"
        effective_url = url or self.DEFAULT_URL
        _run_git(
            self.parent,
            "submodule",
            "add",
            str(repo).replace("\\", "/"),
            relative,
        )
        _run_git(self.parent, "config", "-f", ".gitmodules", f"submodule.{relative}.url", effective_url)
        _run_git(self.parent / relative, "remote", "set-url", "origin", effective_url)
        self.commit_parent(f"install {leaf}")
        return _run_git(self.parent / relative, "rev-parse", "HEAD")

    # 方法作用：提交父仓库当前索引（含 .gitmodules 与 gitlink）。
    # Args: message - 提交信息；add_all - 是否先执行 git add -A。
    # Returns: 无返回值。
    def commit_parent(self, message: str, *, add_all: bool = True) -> None:
        if add_all:
            _run_git(self.parent, "add", "-A")
        _run_git(self.parent, "commit", "-q", "-m", message)

    # 方法作用：向 .gitmodules 追加一条声明（可越权或指向不存在的路径）。
    # Args: name - 小节名；path - 声明的相对路径；url - 声明的仓库 URL。
    # Returns: 无返回值。
    def declare_extra_submodule(self, name: str, *, path: str, url: str) -> None:
        gitmodules = self.parent / ".gitmodules"
        existing = gitmodules.read_text(encoding="utf-8") if gitmodules.exists() else ""
        gitmodules.write_text(
            f'{existing}[submodule "{name}"]\n\tpath = {path}\n\turl = {url}\n',
            encoding="utf-8",
        )
        _run_git(self.parent, "add", ".gitmodules")
        self.commit_parent(f"declare {name}", add_all=False)

    # 方法作用：在子模块工作树内提交一次新改动（制造版本漂移）。
    # Args: leaf - 子模块目录名；relative - 相对文件路径；content - 写入内容。
    # Returns: 无返回值。
    def commit_inside_submodule(self, leaf: str, relative: str, content: str) -> None:
        worktree = self.community_root / leaf
        target = worktree / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content, encoding="utf-8")
        _run_git(worktree, "add", "-A")
        _run_git(worktree, "commit", "-q", "-m", f"drift {relative}")

    # 方法作用：返回子模块工作树路径。
    # Args: leaf - 子模块目录名。
    # Returns: 工作树绝对路径。
    def worktree(self, leaf: str) -> Path:
        return self.community_root / leaf


@pytest.fixture
def community(tmp_path: Path) -> _CommunityFixture:
    """提供父仓库、社区子模块与白名单 URL 的本地 Git 夹具。"""
    return _CommunityFixture(tmp_path)


# 方法作用：在测试夹具中创建 Windows junction（无需管理员权限的目录链接）。
# Args: link - 链接路径；target - 目标目录路径。
# Returns: 创建成功返回 True，环境不支持时返回 False。
def _make_junction(link: Path, target: Path) -> bool:
    completed = subprocess.run(
        ["cmd", "/c", "mklink", "/J", str(link), str(target)],
        capture_output=True,
        text=True,
        check=False,
    )
    return completed.returncode == 0


# 方法作用：按用例需要构造 SkillManager（社区配置可开、可关或完全缺省）。
# Args: fixture - 社区夹具；enabled - True 启用 / False 显式关闭配置 / None 不注入配置；allowed - 白名单 URL 列表；
#       require_signatures - 是否强制签名；trusted_public_keys - 可信签发者公钥；timeout - git 超时秒数。
# Returns: 已注入社区配置的 SkillManager。
def _make_manager(
    fixture: _CommunityFixture,
    *,
    enabled: bool | None = True,
    allowed: list[str] | None = None,
    require_signatures: bool = False,
    trusted_public_keys: str | dict[str, str] = "",
    timeout: float = 5.0,
):
    from src.skill_manager import SkillManager
    from src.skill_submodules import CommunitySubmoduleConfig

    urls = [fixture.DEFAULT_URL] if allowed is None else list(allowed)
    config = None
    if enabled is True:
        config = CommunitySubmoduleConfig(
            enabled=True,
            allowed_repo_urls=tuple(urls),
            git_timeout_seconds=timeout,
        )
    elif enabled is False:
        config = CommunitySubmoduleConfig.from_settings(
            False, ";".join(urls), git_timeout_seconds=timeout,
        )
    return SkillManager(
        str(fixture.builtin),
        managed_dir=str(fixture.managed),
        trusted_public_keys=trusted_public_keys,
        require_signatures=require_signatures,
        community_config=config,
    )


# 方法作用：用临时 Ed25519 私钥签名技能包并写回签名信封。
# Args: skill_dir - 技能包目录；key_id - 签发者标识。
# Returns: (私钥对象, 公钥 Base64 字符串)。
def _sign_skill_package(skill_dir: Path, key_id: str = "release-key"):
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

    from src.skill_security import compute_skill_package_digest

    private_key = Ed25519PrivateKey.generate()
    digest = compute_skill_package_digest(skill_dir)
    envelope = {
        "algorithm": "ed25519",
        "key_id": key_id,
        "digest": digest,
        "signature": base64.b64encode(private_key.sign(bytes.fromhex(digest))).decode("ascii"),
    }
    (skill_dir / "SIGNATURE.json").write_text(json.dumps(envelope), encoding="utf-8")
    public_key = private_key.public_key().public_bytes(
        serialization.Encoding.Raw,
        serialization.PublicFormat.Raw,
    )
    return private_key, base64.b64encode(public_key).decode("ascii")


class _GitSubprocessProxy:
    """只替换 run 的 subprocess 代理，保留 TimeoutExpired 等真实属性。"""

    # 方法作用：记录每次 git 调用并委托真实执行，或按需抛出注入异常。
    # Args: recorder - 调用记录列表；error - 可选异常工厂（None 表示真实执行）。
    # Returns: 无返回值。
    def __init__(self, recorder: list | None = None, error: object | None = None) -> None:
        self.recorder = recorder if recorder is not None else []
        self.error = error

    # 方法作用：代理 subprocess.run，把调用参数记录后交给真实实现或抛出注入异常。
    # Args: args - 位置参数；kwargs - 关键字参数。
    # Returns: 真实 subprocess.run 的返回值。
    def run(self, *args, **kwargs):
        self.recorder.append((args, kwargs))
        if self.error is not None:
            raise self.error() if callable(self.error) else self.error
        return subprocess.run(*args, **kwargs)

    # 方法作用：把未覆写的属性（如 TimeoutExpired）透传给真实 subprocess 模块。
    # Args: name - 属性名。
    # Returns: 真实 subprocess 模块上的同名属性。
    def __getattr__(self, name: str):
        return getattr(subprocess, name)


class _ExplodingGitModule:
    """默认关闭时不得触碰 git：任何调用都会让用例失败。"""

    # 方法作用：初始化调用记录。
    # Args: 无。
    # Returns: 无返回值。
    def __init__(self) -> None:
        self.calls: list = []

    # 方法作用：拦截任意属性访问并返回会失败的调用体。
    # Args: name - 属性名。
    # Returns: 记录调用后抛错的函数。
    def __getattr__(self, name: str):
        # 方法作用：记录一次非法 git 调用并抛出断言错误。
        # Args: args - 位置参数；kwargs - 关键字参数。
        # Returns: 不返回，始终抛出 AssertionError。
        def _explode(*args, **kwargs):
            self.calls.append((name, args, kwargs))
            raise AssertionError("社区子模块关闭时不允许调用 git")
        return _explode


class TestCommunityDiscoveryDisabled:
    """覆盖 9.4.2 默认关闭：关闭状态零副作用、零 git 调用。"""

    # 方法作用：验证默认不注入社区配置时既不读 .gitmodules 也不调用 git。
    # Args: self - pytest 测试类实例；community - 社区夹具；monkeypatch - pytest 补丁工具。
    # Returns: 无返回值，断言失败时由 pytest 报告。
    async def test_community_discovery_disabled_by_default(self, community, monkeypatch) -> None:
        """默认关闭：已安装的合法子模块也不被发现，且完全不走 git。"""
        # Arrange
        repo = community.make_skill_repo("alpha", "community-alpha")
        community.install("alpha", repo)
        _write_builtin_skill(community.builtin, "local-report")
        exploding = _ExplodingGitModule()
        monkeypatch.setattr("src.skill_submodules.subprocess", exploding)
        manager = _make_manager(community, enabled=None)

        # Act
        await manager.discover()

        # Assert
        assert exploding.calls == []
        assert manager.get_skill("local-report", tenant_id=1, user_id=1) is not None
        assert manager.get_skill("community-alpha", tenant_id=1, user_id=1) is None

    # 方法作用：验证显式传入 enabled=False 的配置对象同样保持关闭且不校验白名单。
    # Args: self - pytest 测试类实例；community - 社区夹具。
    # Returns: 无返回值，断言失败时由 pytest 报告。
    async def test_explicitly_disabled_config_skips_whitelist_validation(self, community) -> None:
        """关闭状态下的白名单不参与校验，也不产生任何社区技能。"""
        # Arrange
        from src.skill_submodules import CommunitySubmoduleConfig

        repo = community.make_skill_repo("alpha", "community-alpha")
        community.install("alpha", repo)
        _write_builtin_skill(community.builtin, "local-report")

        # Act
        config = CommunitySubmoduleConfig.from_settings(False, "file:///tmp/not-https")
        manager = _make_manager(community, enabled=False)
        await manager.discover()

        # Assert
        assert config.enabled is False
        assert manager.community_config is not None
        assert manager.community_config.enabled is False
        assert manager.get_skill("local-report", tenant_id=1, user_id=1) is not None
        assert manager.get_skill("community-alpha", tenant_id=1, user_id=1) is None


class TestCommunityDiscoveryHappyPath:
    """覆盖 9.4.2 正常路径：gitlink 固定一致的子模块被发现为 system 技能。"""

    # 方法作用：验证根 SKILL.md 形式的社区子模块被发现且作用域与来源正确。
    # Args: self - pytest 测试类实例；community - 社区夹具。
    # Returns: 无返回值，断言失败时由 pytest 报告。
    async def test_community_submodule_skill_is_discovered_and_pinned(self, community) -> None:
        """HEAD 与父仓库 gitlink 完全一致时，技能以 system 作用域进入缓存。"""
        # Arrange
        repo = community.make_skill_repo("alpha", "community-alpha")
        pinned = community.install("alpha", repo)
        _write_builtin_skill(community.builtin, "local-report")
        manager = _make_manager(community)

        # Act
        await manager.discover()

        # Assert
        skill = manager.get_skill("community-alpha", tenant_id=1, user_id=1)
        assert skill is not None
        assert skill.scope == "system"
        assert (skill.tenant_id, skill.owner_user_id) == (0, 0)
        assert skill.resource_id == "system:0:0:community-alpha"
        assert skill.enabled is True
        assert Path(skill.source_path) == community.worktree("alpha")
        assert pinned == _run_git(community.worktree("alpha"), "rev-parse", "HEAD")
        # 其它来源不受影响
        assert manager.get_skill("local-report", tenant_id=1, user_id=1) is not None

    # 方法作用：验证一个子模块可承载一层子目录中的多个技能，且不做无界递归。
    # Args: self - pytest 测试类实例；community - 社区夹具。
    # Returns: 无返回值，断言失败时由 pytest 报告。
    async def test_community_collection_loads_one_level_manifests_only(self, community) -> None:
        """一层子目录清单被加载，更深层清单不得进入缓存。"""
        # Arrange
        repo = community.make_skill_repo(
            "collection",
            "unused-root",
            root_manifest=False,
            files={
                "forecast/SKILL.md": (
                    "---\nname: community-forecast\nversion: 1.0.0\n"
                    "triggers:\n  keywords: [预测]\n---\n说明\n"
                ),
                "detect/SKILL.md": (
                    "---\nname: community-detect\nversion: 1.0.0\n"
                    "triggers:\n  keywords: [检测]\n---\n说明\n"
                ),
                "deep/inner/SKILL.md": (
                    "---\nname: community-deep\nversion: 1.0.0\n"
                    "triggers:\n  keywords: [深层]\n---\n说明\n"
                ),
            },
        )
        community.install("collection", repo)
        manager = _make_manager(community)

        # Act
        await manager.discover()

        # Assert
        assert manager.get_skill("community-forecast", tenant_id=1, user_id=1) is not None
        assert manager.get_skill("community-detect", tenant_id=1, user_id=1) is not None
        assert manager.get_skill("community-deep", tenant_id=1, user_id=1) is None

    # 方法作用：验证社区技能的启停状态在刷新后仍然生效。
    # Args: self - pytest 测试类实例；community - 社区夹具。
    # Returns: 无返回值，断言失败时由 pytest 报告。
    async def test_community_skill_toggle_state_survives_refresh(self, community) -> None:
        """禁用社区技能后刷新，不得因重新扫描而自动启用。"""
        # Arrange
        repo = community.make_skill_repo("alpha", "community-alpha")
        community.install("alpha", repo)
        manager = _make_manager(community)
        await manager.discover()
        skill = manager.get_skill("community-alpha", tenant_id=1, user_id=1)
        assert skill is not None

        # Act
        manager.set_enabled(skill, False)
        await manager.discover()

        # Assert
        refreshed = manager.get_skill("community-alpha", tenant_id=1, user_id=1)
        assert refreshed is not None
        assert refreshed.enabled is False


class TestCommunityUrlWhitelist:
    """覆盖 9.4.2 白名单与 URL 形态：只接受显式列出的完整 HTTPS 仓库 URL。"""

    # 方法作用：验证声明 URL 必须与白名单完整一致，近似 URL 一律拒绝。
    # Args: self - pytest 测试类实例；community - 社区夹具。
    # Returns: 无返回值，断言失败时由 pytest 报告。
    async def test_allowed_url_must_match_whitelist_exactly(self, community) -> None:
        """缺少 .git 或换主机都不匹配，只有完整一致的白名单项才放行。"""
        # Arrange
        repo = community.make_skill_repo("alpha", "community-alpha")
        community.install("alpha", repo, url=community.DEFAULT_URL)

        # Act / Assert：三种白名单下的发现结果
        manager_no_git_suffix = _make_manager(
            community, allowed=["https://git.example.com/acme/community-skills"],
        )
        await manager_no_git_suffix.discover()
        assert manager_no_git_suffix.get_skill("community-alpha", tenant_id=1, user_id=1) is None

        manager_other_host = _make_manager(
            community, allowed=["https://other.example.com/acme/community-skills.git"],
        )
        await manager_other_host.discover()
        assert manager_other_host.get_skill("community-alpha", tenant_id=1, user_id=1) is None

        manager_exact = _make_manager(community, allowed=[community.DEFAULT_URL])
        await manager_exact.discover()
        assert manager_exact.get_skill("community-alpha", tenant_id=1, user_id=1) is not None

    # 方法作用：验证非 HTTPS、带用户信息、带查询或片段的 URL 形态被拒绝。
    # Args: self - pytest 测试类实例。
    # Returns: 无返回值，断言失败时由 pytest 报告。
    def test_repository_url_shape_rules(self) -> None:
        """file/ssh/ext/scp/http/userinfo/query/fragment/相对路径全部拒绝。"""
        # Arrange
        from src.skill_submodules import parse_allowed_repo_urls, validate_repository_url

        good = "https://git.example.com/acme/community-skills.git"
        rejected = [
            "http://git.example.com/acme/community-skills.git",
            "file:///tmp/community-skills",
            "ssh://git@git.example.com/acme/community-skills.git",
            "git://git.example.com/acme/community-skills.git",
            "ext::sh -c id",
            "git@git.example.com:acme/community-skills.git",
            "https://user:pass@git.example.com/acme/community-skills.git",
            "https://git.example.com/acme/community-skills.git?ref=main",
            "https://git.example.com/acme/community-skills.git#fragment",
            "../../local/community-skills",
            "C:/repos/community-skills",
            "",
        ]

        # Act / Assert
        assert validate_repository_url(good) == good
        for bad in rejected:
            with pytest.raises(ValueError):
                validate_repository_url(bad)
        with pytest.raises(ValueError):
            parse_allowed_repo_urls(f"{good}, file:///tmp/community-skills")

    # 方法作用：验证启用状态下非法白名单在构造期即失败关闭。
    # Args: self - pytest 测试类实例。
    # Returns: 无返回值，断言失败时由 pytest 报告。
    def test_enabled_config_rejects_malformed_whitelist(self) -> None:
        """显式启用时白名单项自身必须合法，配置错误不能拖到运行期。"""
        # Arrange
        from src.skill_submodules import CommunitySubmoduleConfig

        # Act / Assert
        with pytest.raises(ValueError):
            CommunitySubmoduleConfig.from_settings(True, "file:///tmp/community-skills")
        config = CommunitySubmoduleConfig.from_settings(
            True, "https://git.example.com/acme/community-skills.git",
        )
        assert config.enabled is True
        assert config.allowed_repo_urls == (
            "https://git.example.com/acme/community-skills.git",
        )


class TestCommunityPinEnforcement:
    """覆盖 9.4.2 固定校验：未初始化、gitlink、漂移、脏树、可执行文件、URL 替换、路径逃逸。"""

    # 方法作用：验证父仓库 HEAD 中没有 gitlink 时子模块被拒绝。
    # Args: self - pytest 测试类实例；community - 社区夹具。
    # Returns: 无返回值，断言失败时由 pytest 报告。
    async def test_gitlink_missing_is_rejected(self, community) -> None:
        """只有 .gitmodules 声明、父仓库未固定 commit 的子模块不得加载。"""
        # Arrange
        repo = community.make_skill_repo("alpha", "community-alpha")
        community.install("alpha", repo)
        _run_git(community.parent, "rm", "--cached", "-q", "skills/community/alpha")
        community.commit_parent("drop gitlink", add_all=False)
        _write_builtin_skill(community.builtin, "local-report")
        manager = _make_manager(community)

        # Act
        await manager.discover()

        # Assert
        assert manager.get_skill("community-alpha", tenant_id=1, user_id=1) is None
        assert manager.get_skill("local-report", tenant_id=1, user_id=1) is not None

    # 方法作用：验证子模块 HEAD 前移（版本漂移）时被拒绝。
    # Args: self - pytest 测试类实例；community - 社区夹具。
    # Returns: 无返回值，断言失败时由 pytest 报告。
    async def test_head_drift_is_rejected(self, community) -> None:
        """子模块 HEAD 与 gitlink 不一致即拒绝，哪怕工作树干净。"""
        # Arrange
        repo = community.make_skill_repo("alpha", "community-alpha")
        community.install("alpha", repo)
        community.commit_inside_submodule("alpha", "drift.txt", "drift\n")
        manager = _make_manager(community)

        # Act
        await manager.discover()

        # Assert
        assert manager.get_skill("community-alpha", tenant_id=1, user_id=1) is None

    # 方法作用：验证已跟踪文件被修改（脏工作树）时被拒绝。
    # Args: self - pytest 测试类实例；community - 社区夹具。
    # Returns: 无返回值，断言失败时由 pytest 报告。
    async def test_dirty_worktree_is_rejected(self, community) -> None:
        """本地改动未提交即视为不可信内容，拒绝加载。"""
        # Arrange
        repo = community.make_skill_repo("alpha", "community-alpha")
        community.install("alpha", repo)
        (community.worktree("alpha") / "SKILL.md").write_text(
            "---\nname: community-alpha\nversion: 9.9.9\n---\n被篡改\n",
            encoding="utf-8",
        )
        manager = _make_manager(community)

        # Act
        await manager.discover()

        # Assert
        assert manager.get_skill("community-alpha", tenant_id=1, user_id=1) is None

    # 方法作用：验证存在恶意 clean 过滤器时拒绝来源，且过滤器命令绝不执行。
    # Args: self - pytest 测试类实例；community - 社区夹具。
    # Returns: 无返回值，断言失败时由 pytest 报告。
    async def test_git_filter_configuration_is_rejected_without_execution(self, community) -> None:
        """先只读检查过滤器配置再拒绝来源；marker 文件证明过滤器未被触发。"""
        # Arrange：真实本地过滤器会写 marker，先用 git diff 证明夹具确实危险
        marker = community.root / "filter-marker.txt"
        repo = community.make_skill_repo(
            "alpha",
            "community-alpha",
            files={".gitattributes": "SKILL.md filter=malicious\n"},
        )
        community.install("alpha", repo)
        worktree = community.worktree("alpha")
        _run_git(
            worktree,
            "config",
            "filter.malicious.clean",
            f"sh -c 'echo pwned > \"{marker.as_posix()}\"'",
        )
        original = (worktree / "SKILL.md").read_bytes()
        (worktree / "SKILL.md").write_bytes(original + b"\n")
        _run_git(worktree, "diff", "--quiet", check=False)
        assert marker.exists(), "夹具失效：clean 过滤器没有被内容读取命令执行"
        marker.unlink()
        (worktree / "SKILL.md").write_bytes(original)  # 恢复为与提交完全一致的字节
        manager = _make_manager(community)

        # Act
        await manager.discover()

        # Assert
        assert manager.get_skill("community-alpha", tenant_id=1, user_id=1) is None
        assert not marker.exists(), "发现流程执行了 Git 过滤器"

    # 方法作用：验证未跟踪的可执行文件导致子模块被拒绝。
    # Args: self - pytest 测试类实例；community - 社区夹具。
    # Returns: 无返回值，断言失败时由 pytest 报告。
    async def test_untracked_executable_is_rejected(self, community) -> None:
        """未跟踪脚本可以绕过清单校验，必须拒绝整个子模块。"""
        # Arrange
        repo = community.make_skill_repo("alpha", "community-alpha")
        community.install("alpha", repo)
        (community.worktree("alpha") / "payload.sh").write_text(
            "#!/bin/sh\nid\n", encoding="utf-8",
        )
        manager = _make_manager(community)

        # Act
        await manager.discover()

        # Assert
        assert manager.get_skill("community-alpha", tenant_id=1, user_id=1) is None

    # 方法作用：验证未跟踪的字节码缓存属于未固定文件，必须拒绝整个子模块。
    # Args: self - pytest 测试类实例；community - 社区夹具。
    # Returns: 无返回值，断言失败时由 pytest 报告。
    async def test_untracked_bytecode_cache_is_rejected(self, community) -> None:
        """`.pyc` 等缓存不在提交内即视为未固定内容，安装者需先清理再启用。"""
        # Arrange
        repo = community.make_skill_repo("alpha", "community-alpha")
        community.install("alpha", repo)
        cache_dir = community.worktree("alpha") / "__pycache__"
        cache_dir.mkdir()
        (cache_dir / "tools.cpython-314.pyc").write_bytes(b"\x00\x01")
        manager = _make_manager(community)

        # Act
        await manager.discover()

        # Assert
        assert manager.get_skill("community-alpha", tenant_id=1, user_id=1) is None

    # 方法作用：验证 .gitignore 不能隐藏代码，ignored 的脚本与字节码一律拒绝。
    # Args: self - pytest 测试类实例；community - 社区夹具。
    # Returns: 无返回值，断言失败时由 pytest 报告。
    async def test_ignored_hidden_files_are_rejected(self, community) -> None:
        """只用 `git status` 会漏掉 ignored 文件，必须检查完整工作树清单。"""
        # Arrange
        repo = community.make_skill_repo(
            "alpha",
            "community-alpha",
            files={".gitignore": "payload.py\n*.pyc\n"},
        )
        community.install("alpha", repo)
        worktree = community.worktree("alpha")
        (worktree / "payload.py").write_text("print('hidden')\n", encoding="utf-8")
        (worktree / "tools.pyc").write_bytes(b"\x00\x01")
        manager = _make_manager(community)

        # Act
        await manager.discover()

        # Assert
        assert manager.get_skill("community-alpha", tenant_id=1, user_id=1) is None

    # 方法作用：验证工作树内的 junction 目录同样被拒绝。
    # Args: self - pytest 测试类实例；community - 社区夹具。
    # Returns: 无返回值，断言失败时由 pytest 报告。
    async def test_junction_inside_worktree_is_rejected(self, community) -> None:
        """junction 可把外部目录挂进已固定工作树，必须拒绝该子模块。"""
        # Arrange
        repo = community.make_skill_repo("alpha", "community-alpha")
        community.install("alpha", repo)
        outside = community.root / "outside" / "linked"
        _write_manifest(outside / "SKILL.md", "community-linked")
        linker = community.worktree("alpha") / "linked"
        if not _make_junction(linker, outside):
            pytest.skip("当前环境不支持创建 junction（需要 NTFS 与命令权限）")
        manager = _make_manager(community)

        # Act
        await manager.discover()

        # Assert
        assert manager.get_skill("community-alpha", tenant_id=1, user_id=1) is None
        assert manager.get_skill("community-linked", tenant_id=1, user_id=1) is None

    # 方法作用：验证声明路径存在但没有 .git 的未初始化子模块被拒绝。
    # Args: self - pytest 测试类实例；community - 社区夹具。
    # Returns: 无返回值，断言失败时由 pytest 报告。
    async def test_uninitialized_submodule_is_rejected(self, community) -> None:
        """目录存在但未 git submodule update --init 时不得加载。"""
        # Arrange
        (community.community_root / "ghost").mkdir()
        community.declare_extra_submodule(
            "ghost", path="skills/community/ghost", url=community.DEFAULT_URL,
        )
        _write_builtin_skill(community.builtin, "local-report")
        manager = _make_manager(community)

        # Act
        await manager.discover()

        # Assert
        assert manager.get_skill("ghost", tenant_id=1, user_id=1) is None
        assert manager.get_skill("local-report", tenant_id=1, user_id=1) is not None

    # 方法作用：验证子模块 origin 被换成别的仓库时被拒绝。
    # Args: self - pytest 测试类实例；community - 社区夹具。
    # Returns: 无返回值，断言失败时由 pytest 报告。
    async def test_remote_url_replacement_is_rejected(self, community) -> None:
        """远端与 .gitmodules 声明不一致说明来源被替换。"""
        # Arrange
        repo = community.make_skill_repo("alpha", "community-alpha")
        community.install("alpha", repo)
        _run_git(
            community.worktree("alpha"),
            "remote",
            "set-url",
            "origin",
            "https://git.example.com/acme/other-skills.git",
        )
        manager = _make_manager(community)

        # Act
        await manager.discover()

        # Assert
        assert manager.get_skill("community-alpha", tenant_id=1, user_id=1) is None

    # 方法作用：验证工作区被改写但未提交的 .gitmodules 不能替换固定配置。
    # Args: self - pytest 测试类实例；community - 社区夹具。
    # Returns: 无返回值，断言失败时由 pytest 报告。
    async def test_uncommitted_gitmodules_change_is_rejected(self, community) -> None:
        """实际内容必须与 HEAD 一致；未提交修改（哪怕只是注释）即跳过社区来源。"""
        # Arrange
        repo = community.make_skill_repo("alpha", "community-alpha")
        community.install("alpha", repo)
        gitmodules = community.parent / ".gitmodules"
        gitmodules.write_text(
            gitmodules.read_text(encoding="utf-8") + "\n# 未提交修改\n",
            encoding="utf-8",
        )
        manager = _make_manager(community)

        # Act
        await manager.discover()

        # Assert
        assert manager.get_skill("community-alpha", tenant_id=1, user_id=1) is None

    # 方法作用：验证 .gitmodules 声明越权路径时不读取社区根之外的技能。
    # Args: self - pytest 测试类实例；community - 社区夹具。
    # Returns: 无返回值，断言失败时由 pytest 报告。
    async def test_escaping_declared_paths_are_ignored(self, community) -> None:
        """`..`、绝对路径与多层路径都不得成为发现入口。"""
        # Arrange
        outside = community.root / "outside" / "escape"
        _write_manifest(outside / "SKILL.md", "community-escape")
        community.declare_extra_submodule(
            "escape", path="skills/community/../../outside/escape", url=community.DEFAULT_URL,
        )
        community.declare_extra_submodule(
            "absolute", path=str(outside).replace("\\", "/"), url=community.DEFAULT_URL,
        )
        community.declare_extra_submodule(
            "nested", path="skills/community/alpha/nested", url=community.DEFAULT_URL,
        )
        _write_builtin_skill(community.builtin, "local-report")
        manager = _make_manager(community)

        # Act
        await manager.discover()

        # Assert
        assert manager.get_skill("community-escape", tenant_id=1, user_id=1) is None
        assert manager.get_skill("local-report", tenant_id=1, user_id=1) is not None

    # 方法作用：验证子模块路径为符号链接或联接点时被拒绝。
    # Args: self - pytest 测试类实例；community - 社区夹具。
    # Returns: 无返回值，断言失败时由 pytest 报告。
    async def test_symlinked_submodule_path_is_rejected(self, community) -> None:
        """symlink/junction 可以把社区根指向任意目录，必须拒绝。"""
        # Arrange
        target = community.root / "outside" / "linked"
        _write_manifest(target / "SKILL.md", "community-linked")
        _run_git(target, "init", "-q")
        _run_git(target, "add", "-A")
        _run_git(target, "commit", "-q", "-m", "linked")
        linker = community.community_root / "linked"
        try:
            os.symlink(target, linker, target_is_directory=True)
        except (OSError, NotImplementedError):
            pytest.skip("当前环境不支持创建符号链接（需要开发者模式或管理员权限）")
        community.declare_extra_submodule(
            "linked", path="skills/community/linked", url=community.DEFAULT_URL,
        )
        manager = _make_manager(community)

        # Act
        await manager.discover()

        # Assert
        assert manager.get_skill("community-linked", tenant_id=1, user_id=1) is None

    # 方法作用：验证技能包内的符号链接文件被摘要校验拒绝。
    # Args: self - pytest 测试类实例；community - 社区夹具。
    # Returns: 无返回值，断言失败时由 pytest 报告。
    async def test_symlinked_package_file_is_rejected(self, community) -> None:
        """包内符号链接会被摘要校验拦截，不影响其它来源。"""
        # Arrange
        repo = community.make_skill_repo("alpha", "community-alpha")
        try:
            os.symlink(repo / "SKILL.md", repo / "linked.txt")
        except (OSError, NotImplementedError):
            pytest.skip("当前环境不支持创建符号链接（需要开发者模式或管理员权限）")
        _run_git(repo, "add", "-A")
        _run_git(repo, "commit", "-q", "-m", "symlink")
        community.install("alpha", repo)
        _write_builtin_skill(community.builtin, "local-report")
        manager = _make_manager(community)

        # Act
        await manager.discover()

        # Assert
        assert manager.get_skill("community-alpha", tenant_id=1, user_id=1) is None
        assert manager.get_skill("local-report", tenant_id=1, user_id=1) is not None


class TestCommunityPinBypassRegressions:
    """覆盖 9.4.2 独立验收复现的固定校验绕过：Git 索引标记与根目录链接。"""

    # 方法作用：验证 assume-unchanged / skip-worktree 标记不能掩盖已跟踪文件的实际改动。
    # Args: self - pytest 测试类实例；community - 社区夹具；flag - Git 索引标记；target - 被蒙蔽的对象。
    # Returns: 无返回值，断言失败时由 pytest 报告。
    @pytest.mark.parametrize("flag", ["--assume-unchanged", "--skip-worktree"])
    @pytest.mark.parametrize("target", ["SKILL.md", ".gitmodules"])
    async def test_index_hints_cannot_hide_modified_content(self, community, flag, target) -> None:
        """索引标记让 status 报告干净，但实际内容已改变，拒绝比信任 status 安全。"""
        # Arrange
        repo = community.make_skill_repo("alpha", "community-alpha")
        community.install("alpha", repo)
        repository = community.parent if target == ".gitmodules" else community.worktree("alpha")
        _run_git(repository, "update-index", flag, target)
        path = repository / target
        path.write_text(path.read_text(encoding="utf-8") + "\n# 未固定内容\n", encoding="utf-8")
        # 前提证据：标记确实蒙蔽了 status，旧实现会因此放行
        if target == ".gitmodules":
            status = _run_git(
                repository, "status", "--porcelain", "--untracked-files=all", "--", target,
            )
        else:
            status = _run_git(repository, "status", "--porcelain", "--untracked-files=no")
        assert status == "", "夹具失效：索引标记没有让 status 漏报改动"
        manager = _make_manager(community)

        # Act
        await manager.discover()

        # Assert
        assert manager.get_skill("community-alpha", tenant_id=1, user_id=1) is None

    # 方法作用：验证社区根目录本身是 junction 时被拒绝，链接事实不因 resolve 而丢失。
    # Args: self - pytest 测试类实例；community - 社区夹具。
    # Returns: 无返回值，断言失败时由 pytest 报告。
    async def test_community_root_junction_is_rejected(self, community) -> None:
        """junction 在 resolve 后呈现为普通目录，必须在解析前的原始路径上识别。"""
        from src.skill_submodules import CommunitySubmoduleConfig, CommunitySubmoduleScanner

        if os.name != "nt":
            pytest.skip("此用例专门验证 Windows junction")
        # Arrange
        repo = community.make_skill_repo("alpha", "community-alpha")
        community.install("alpha", repo)
        alias = community.parent / "community-junction"
        if not _make_junction(alias, community.community_root):
            pytest.skip("当前环境不支持创建 junction（需要 NTFS 与命令权限）")
        config = CommunitySubmoduleConfig(
            enabled=True,
            allowed_repo_urls=(community.DEFAULT_URL,),
            git_timeout_seconds=5.0,
        )
        try:
            # Act
            results = await CommunitySubmoduleScanner(alias, config).scan()

            # Assert
            assert results == []
        finally:
            if alias.is_junction():
                alias.rmdir()

    # 方法作用：验证社区根为符号链接时同样被拒绝；无符号链接权限的环境按设计跳过。
    # Args: self - pytest 测试类实例；community - 社区夹具。
    # Returns: 无返回值，断言失败时由 pytest 报告。
    async def test_community_root_symlink_is_rejected(self, community) -> None:
        """symlink 根解析前即拒绝，链接目标不得被当作固定来源。"""
        from src.skill_submodules import CommunitySubmoduleConfig, CommunitySubmoduleScanner

        # Arrange
        repo = community.make_skill_repo("alpha", "community-alpha")
        community.install("alpha", repo)
        alias = community.parent / "community-alias"
        try:
            os.symlink(community.community_root, alias, target_is_directory=True)
        except (OSError, NotImplementedError):
            pytest.skip("当前环境不支持创建符号链接（需要开发者模式或管理员权限）")
        config = CommunitySubmoduleConfig(
            enabled=True,
            allowed_repo_urls=(community.DEFAULT_URL,),
            git_timeout_seconds=5.0,
        )

        # Act
        results = await CommunitySubmoduleScanner(alias, config).scan()

        # Assert
        assert results == []


class TestCommunityTrustBoundary:
    """覆盖 9.4.2 信任边界：签名、伪装内置、重名不覆盖。"""

    # 方法作用：验证社区技能不享受内置豁免，必须通过签名门槛。
    # Args: self - pytest 测试类实例；community - 社区夹具。
    # Returns: 无返回值，断言失败时由 pytest 报告。
    async def test_unsigned_community_skill_fails_signature_gate(self, community) -> None:
        """require_signatures 开启时未签名社区技能被拒，内置技能照常加载。"""
        # Arrange
        repo = community.make_skill_repo("alpha", "community-alpha")
        community.install("alpha", repo)
        _write_builtin_skill(community.builtin, "local-report")
        manager = _make_manager(community, require_signatures=True)

        # Act
        await manager.discover()

        # Assert
        assert manager.get_skill("community-alpha", tenant_id=1, user_id=1) is None
        assert manager.get_skill("local-report", tenant_id=1, user_id=1) is not None

    # 方法作用：验证已签名且未被篡改的社区技能通过校验，篡改过的被摘要拦截。
    # Args: self - pytest 测试类实例；community - 社区夹具。
    # Returns: 无返回值，断言失败时由 pytest 报告。
    async def test_signed_community_skill_verifies_and_tamper_is_blocked(self, community) -> None:
        """签名信封与包摘要必须一致，签名后再改内容即拒绝。

        使用一层子目录形态的技能包对照根清单形态：包摘要排除技能根 `.git` 元数据，
        两种清单形态都应支持安装前签名。
        """
        # Arrange：签名前不再改动被签目录的内容
        signed_repo = community.make_skill_repo(
            "alpha",
            "unused-root-alpha",
            root_manifest=False,
            files={
                "pkg/SKILL.md": (
                    "---\nname: community-alpha\nversion: 1.0.0\n"
                    "triggers:\n  keywords: [社区]\n---\n说明\n"
                ),
                "pkg/tools.py": "VALUE = 1\n",
            },
        )
        _, public_key = _sign_skill_package(signed_repo / "pkg")
        _run_git(signed_repo, "add", "-A")
        _run_git(signed_repo, "commit", "-q", "-m", "sign")

        tampered_repo = community.make_skill_repo(
            "beta",
            "unused-root-beta",
            root_manifest=False,
            files={
                "pkg/SKILL.md": (
                    "---\nname: community-beta\nversion: 1.0.0\n"
                    "triggers:\n  keywords: [社区]\n---\n说明\n"
                ),
                "pkg/tools.py": "VALUE = 1\n",
            },
        )
        _sign_skill_package(tampered_repo / "pkg")
        (tampered_repo / "pkg" / "tools.py").write_text("VALUE = 2\n", encoding="utf-8")
        _run_git(tampered_repo, "add", "-A")
        _run_git(tampered_repo, "commit", "-q", "-m", "tamper")

        community.install("alpha", signed_repo)
        community.install("beta", tampered_repo)
        manager = _make_manager(
            community,
            require_signatures=True,
            trusted_public_keys={"release-key": public_key},
        )

        # Act
        await manager.discover()

        # Assert
        signed = manager.get_skill("community-alpha", tenant_id=1, user_id=1)
        assert signed is not None
        assert signed.signature_verified is True
        assert manager.get_skill("community-beta", tenant_id=1, user_id=1) is None

    # 方法作用：验证社区技能不被判定为内置，也不获得内置的可信执行路径。
    # Args: self - pytest 测试类实例；community - 社区夹具。
    # Returns: 无返回值，断言失败时由 pytest 报告。
    async def test_community_skill_never_counts_as_builtin(self, community) -> None:
        """位于 skills/community 下的技能 is_builtin=False，工具不做内置信任。"""
        # Arrange
        repo = community.make_skill_repo(
            "alpha",
            "community-alpha",
            files={
                "SKILL.md": (
                    "---\nname: community-alpha\nversion: 1.0.0\n"
                    "triggers:\n  keywords: [社区]\n"
                    "tools:\n  - name: community_check\n    description: 社区检查\n---\n说明\n"
                ),
            },
        )
        community.install("alpha", repo)
        builtin_dir = _write_builtin_skill(community.builtin, "local-report")
        (builtin_dir / "SKILL.md").write_text(
            "---\nname: local-report\nversion: 1.0.0\n"
            "triggers:\n  keywords: [内置]\n"
            "tools:\n  - name: builtin_check\n    description: 内置检查\n---\n说明\n",
            encoding="utf-8",
        )
        manager = _make_manager(community)
        await manager.discover()

        # Act
        community_skill = manager.get_skill("community-alpha", tenant_id=1, user_id=1)
        builtin_skill = manager.get_skill("local-report", tenant_id=1, user_id=1)
        community_tools = manager.get_active_tools([community_skill])
        builtin_tools = manager.get_active_tools([builtin_skill])

        # Assert
        assert manager.is_builtin("community-alpha", tenant_id=1, user_id=1) is False
        assert manager.is_builtin("local-report", tenant_id=1, user_id=1) is True
        assert community_tools[0]._trusted_builtin is False
        assert builtin_tools[0]._trusted_builtin is True

    # 方法作用：验证社区技能与系统同名技能冲突时不覆盖既有技能。
    # Args: self - pytest 测试类实例；community - 社区夹具。
    # Returns: 无返回值，断言失败时由 pytest 报告。
    async def test_community_skill_does_not_override_builtin_same_name(self, community) -> None:
        """同名冲突时保留内置技能，社区副本不得进入缓存。"""
        # Arrange
        _write_builtin_skill(community.builtin, "report-pack", version="1.0.0")
        repo = community.make_skill_repo("alpha", "report-pack")
        (repo / "SKILL.md").write_text(
            "---\nname: report-pack\nversion: 9.9.9\n"
            "triggers:\n  keywords: [报告]\n---\n社区覆盖\n",
            encoding="utf-8",
        )
        _run_git(repo, "add", "-A")
        _run_git(repo, "commit", "-q", "-m", "impersonate")
        community.install("alpha", repo)
        manager = _make_manager(community)

        # Act
        await manager.discover()

        # Assert
        skill = manager.get_skill("report-pack", tenant_id=1, user_id=1)
        assert skill is not None
        assert skill.version == "1.0.0"
        assert manager.is_builtin("report-pack", tenant_id=1, user_id=1) is True
        assert Path(skill.source_path) == community.builtin / "report-pack"
        assert all(
            community.community_root not in Path(item.source_path).parents
            for item in manager.skills.values()
        )


class TestCommunityRootManifestSignature:
    """覆盖 9.4.2 根清单形态：安装改变 `.git` 形态与路径，签名身份必须保持稳定。"""

    # 方法作用：验证根清单技能从普通仓库安装为子模块后签名仍然有效。
    # Args: self - pytest 测试类实例；community - 社区夹具。
    # Returns: 无返回值，断言失败时由 pytest 报告。
    async def test_root_manifest_signature_survives_submodule_install(self, community) -> None:
        """技能内容未变时，`.git` 目录到指针文件的形态变化不得让签名失效。"""
        # Arrange
        repository = community.make_skill_repo(
            "alpha", "community-alpha", files={"tools.py": "VALUE = 1\n"},
        )
        _, public_key = _sign_skill_package(repository)
        _run_git(repository, "add", "SIGNATURE.json")
        _run_git(repository, "commit", "-q", "-m", "添加测试签名")
        community.install("alpha", repository)
        manager = _make_manager(
            community,
            require_signatures=True,
            trusted_public_keys={"release-key": public_key},
        )

        # Act
        await manager.discover()

        # Assert
        skill = manager.get_skill("community-alpha", tenant_id=1, user_id=1)
        assert skill is not None
        assert skill.signature_verified is True

    # 方法作用：验证根清单技能的代码在提交后被篡改时仍被签名边界拒绝。
    # Args: self - pytest 测试类实例；community - 社区夹具。
    # Returns: 无返回值，断言失败时由 pytest 报告。
    async def test_committed_root_manifest_tamper_is_rejected(self, community) -> None:
        """篡改后提交满足固定校验，但摘要与签名信封不一致必须拒绝。"""
        # Arrange
        repository = community.make_skill_repo(
            "alpha", "community-alpha", files={"tools.py": "VALUE = 1\n"},
        )
        _, public_key = _sign_skill_package(repository)
        (repository / "tools.py").write_text("VALUE = 2\n", encoding="utf-8")
        _run_git(repository, "add", "tools.py", "SIGNATURE.json")
        _run_git(repository, "commit", "-q", "-m", "提交已篡改测试代码")
        community.install("alpha", repository)
        manager = _make_manager(
            community,
            require_signatures=True,
            trusted_public_keys={"release-key": public_key},
        )

        # Act
        await manager.discover()

        # Assert
        assert manager.get_skill("community-alpha", tenant_id=1, user_id=1) is None


class TestCommunityFailureIsolation:
    """覆盖 9.4.2 失败隔离：单个子模块、git 不可用或超时都不影响其它来源。"""

    # 方法作用：验证一个子模块校验失败不影响另一个子模块和其它来源。
    # Args: self - pytest 测试类实例；community - 社区夹具。
    # Returns: 无返回值，断言失败时由 pytest 报告。
    async def test_failure_of_one_submodule_does_not_affect_others(self, community) -> None:
        """好子模块、内置、租户技能都必须在坏子模块失败后照常加载。"""
        # Arrange
        good_repo = community.make_skill_repo("good", "community-good")
        community.install("good", good_repo)
        bad_repo = community.make_skill_repo("bad", "community-bad")
        community.install("bad", bad_repo)
        (community.worktree("bad") / "SKILL.md").write_text("被篡改\n", encoding="utf-8")
        _write_builtin_skill(community.builtin, "local-report")
        _write_manifest(
            community.managed / "tenant" / "1" / "tenant-report" / "SKILL.md",
            "tenant-report",
        )
        manager = _make_manager(community)

        # Act
        await manager.discover()

        # Assert
        assert manager.get_skill("community-good", tenant_id=1, user_id=1) is not None
        assert manager.get_skill("community-bad", tenant_id=1, user_id=1) is None
        assert manager.get_skill("local-report", tenant_id=1, user_id=1) is not None
        assert manager.get_skill("tenant-report", tenant_id=1, user_id=1) is not None

    # 方法作用：验证 .gitmodules 损坏时社区来源整体跳过且不影响其它来源。
    # Args: self - pytest 测试类实例；community - 社区夹具。
    # Returns: 无返回值，断言失败时由 pytest 报告。
    async def test_broken_gitmodules_keeps_other_sources(self, community) -> None:
        """声明文件不可解析时不得抛出到调用方。"""
        # Arrange
        _write_builtin_skill(community.builtin, "local-report")
        (community.parent / ".gitmodules").write_text("[[[ 破坏\n", encoding="utf-8")
        manager = _make_manager(community)

        # Act
        await manager.discover()

        # Assert
        assert manager.get_skill("local-report", tenant_id=1, user_id=1) is not None

    # 方法作用：验证父仓库存在 Git 过滤器配置时整个社区来源按 fail closed 跳过。
    # Args: self - pytest 测试类实例；community - 社区夹具。
    # Returns: 无返回值，断言失败时由 pytest 报告。
    async def test_parent_filter_config_skips_community_source(self, community) -> None:
        """父仓库工作树读取命令前同样先查过滤器配置，其它来源不受影响。"""
        # Arrange
        marker = community.root / "parent-filter-marker.txt"
        repo = community.make_skill_repo("alpha", "community-alpha")
        community.install("alpha", repo)
        _run_git(
            community.parent,
            "config",
            "filter.malicious.smudge",
            f"sh -c 'echo pwned > \"{marker.as_posix()}\"'",
        )
        _write_builtin_skill(community.builtin, "local-report")
        manager = _make_manager(community)

        # Act
        await manager.discover()

        # Assert
        assert manager.get_skill("community-alpha", tenant_id=1, user_id=1) is None
        assert manager.get_skill("local-report", tenant_id=1, user_id=1) is not None
        assert not marker.exists()

    # 方法作用：验证 git 不可用时社区来源跳过且不中断刷新。
    # Args: self - pytest 测试类实例；community - 社区夹具；monkeypatch - pytest 补丁工具。
    # Returns: 无返回值，断言失败时由 pytest 报告。
    async def test_git_unavailable_keeps_other_sources(self, community, monkeypatch) -> None:
        """缺少 git 可执行文件时只跳过社区来源。"""
        # Arrange
        repo = community.make_skill_repo("alpha", "community-alpha")
        community.install("alpha", repo)
        _write_builtin_skill(community.builtin, "local-report")
        proxy = _GitSubprocessProxy(error=FileNotFoundError("git 不存在"))
        monkeypatch.setattr("src.skill_submodules.subprocess", proxy)
        manager = _make_manager(community)

        # Act
        await manager.discover()

        # Assert
        assert manager.get_skill("community-alpha", tenant_id=1, user_id=1) is None
        assert manager.get_skill("local-report", tenant_id=1, user_id=1) is not None

    # 方法作用：验证 git 超时时社区来源跳过且不中断刷新。
    # Args: self - pytest 测试类实例；community - 社区夹具；monkeypatch - pytest 补丁工具。
    # Returns: 无返回值，断言失败时由 pytest 报告。
    async def test_git_timeout_keeps_other_sources(self, community, monkeypatch) -> None:
        """命令超时按失败处理，不影响其它来源。"""
        # Arrange
        repo = community.make_skill_repo("alpha", "community-alpha")
        community.install("alpha", repo)
        _write_builtin_skill(community.builtin, "local-report")
        proxy = _GitSubprocessProxy(
            error=lambda: subprocess.TimeoutExpired(cmd="git", timeout=5.0),
        )
        monkeypatch.setattr("src.skill_submodules.subprocess", proxy)
        manager = _make_manager(community)

        # Act
        await manager.discover()

        # Assert
        assert manager.get_skill("community-alpha", tenant_id=1, user_id=1) is None
        assert manager.get_skill("local-report", tenant_id=1, user_id=1) is not None

    # 方法作用：验证 git 调用的参数数组、有限超时、受控环境与只读子命令约束。
    # Args: self - pytest 测试类实例；community - 社区夹具；monkeypatch - pytest 补丁工具。
    # Returns: 无返回值，断言失败时由 pytest 报告。
    async def test_git_call_hardening_arguments(self, community, monkeypatch) -> None:
        """所有 git 调用都必须是数组、禁 shell、带超时与环境加固，且只读。"""
        # Arrange：先注入恶意继承环境，确认全部 GIT_* 被清除后重建
        repo = community.make_skill_repo("alpha", "community-alpha")
        community.install("alpha", repo)
        recorder: list = []
        monkeypatch.setattr("src.skill_submodules.subprocess", _GitSubprocessProxy(recorder))
        monkeypatch.setenv("GIT_DIR", str(community.parent / ".git"))
        monkeypatch.setenv("GIT_WORK_TREE", str(community.parent))
        monkeypatch.setenv("GIT_CONFIG_COUNT", "1")
        monkeypatch.setenv("GIT_CONFIG_KEY_0", "core.fsmonitor")
        monkeypatch.setenv("GIT_CONFIG_VALUE_0", "true")
        monkeypatch.setenv("GIT_CONFIG_PARAMETERS", "'core.hooksPath=/tmp/evil'")
        manager = _make_manager(community)

        # Act
        await manager.discover()

        # Assert
        assert recorder, "社区发现必须至少执行一次 git 调用"
        forbidden = {"clone", "fetch", "pull", "push", "submodule", "update", "checkout", "remote"}
        injected = (
            "GIT_DIR",
            "GIT_WORK_TREE",
            "GIT_CONFIG_COUNT",
            "GIT_CONFIG_KEY_0",
            "GIT_CONFIG_VALUE_0",
            "GIT_CONFIG_PARAMETERS",
        )
        for positional, keyword in recorder:
            command = positional[0]
            assert isinstance(command, list), "git 必须以参数数组调用"
            assert command[0] == "git"
            assert keyword.get("shell") is False
            timeout = keyword.get("timeout")
            assert isinstance(timeout, (int, float)) and 0 < timeout <= 300
            env = keyword.get("env") or {}
            assert env.get("GIT_TERMINAL_PROMPT") == "0"
            assert env.get("GIT_CONFIG_NOSYSTEM") == "1"
            assert env.get("GIT_CONFIG_GLOBAL") == os.devnull
            assert env.get("GIT_OPTIONAL_LOCKS") == "0"
            assert env.get("GIT_PAGER") == "cat"
            for name in injected:
                assert not any(key.upper() == name for key in env), f"继承的 {name} 未被清除"
            assert "-c" in command
            assert any(part.startswith("core.hooksPath=") for part in command)
            assert any(part.startswith("core.fsmonitor=") for part in command)
            assert any(part.startswith("core.attributesFile=") for part in command)
            assert any(part.startswith("credential.helper=") for part in command)
            # 跳过全部 -c 键值对后，剩余第一个 token 必须是允许的只读子命令
            index = 1
            while index + 1 < len(command) and command[index] == "-c":
                index += 2
            subcommand = command[index] if index < len(command) else ""
            assert subcommand not in forbidden
            assert subcommand in {"rev-parse", "ls-tree", "config", "status", "ls-files"}
            assert not (set(command[1:]) & forbidden), "命令参数中出现写操作或网络子命令"

    # 方法作用：验证社区子模块校验在独立线程执行，不阻塞事件循环。
    # Args: self - pytest 测试类实例；community - 社区夹具；monkeypatch - pytest 补丁工具。
    # Returns: 无返回值，断言失败时由 pytest 报告。
    async def test_discovery_does_not_block_event_loop(self, community, monkeypatch) -> None:
        """慢速校验期间事件循环必须持续调度其它协程。"""
        # Arrange
        from src.skill_submodules import CommunitySubmoduleScanner

        repo = community.make_skill_repo("alpha", "community-alpha")
        community.install("alpha", repo)
        manager = _make_manager(community)
        real_verify = CommunitySubmoduleScanner._verify_submodule

        # 方法作用：让真实校验前额外睡眠，放大同步阻塞的可观测性。
        # Args: self - 扫描器实例；declared - 声明项。
        # Returns: 真实校验结果。
        def slow_verify(self, declared):
            time.sleep(0.3)
            return real_verify(self, declared)

        monkeypatch.setattr(CommunitySubmoduleScanner, "_verify_submodule", slow_verify)

        # Act
        ticks = 0
        task = asyncio.create_task(manager.discover())
        while not task.done():
            ticks += 1
            await asyncio.sleep(0.01)
        await task

        # Assert
        assert ticks > 5, "事件循环在校验期间被阻塞"
        assert manager.get_skill("community-alpha", tenant_id=1, user_id=1) is not None


class TestCommunityIsolationBytecode:
    """覆盖 9.4.2 隔离执行约束：社区工具运行后不得在固定工作树留下未固定文件。"""

    _TOOLS_BODY = (
        "class _EchoTool:\n"
        "    async def ainvoke(self, payload):\n"
        "        return {'echo': sorted(payload)}\n"
        "def get_tool(name):\n"
        "    return _EchoTool() if name == 'community_echo' else None\n"
    )
    _MANIFEST_BODY = (
        "---\nname: community-alpha\nversion: 1.0.0\n"
        "triggers:\n  keywords: [社区]\n"
        "tools:\n  - name: community_echo\n    description: 社区回声\n---\n说明\n"
    )

    # 方法作用：验证真实隔离 Worker 执行社区工具后不产生字节码，刷新仍然通过。
    # Args: self - pytest 测试类实例；community - 社区夹具。
    # Returns: 无返回值，断言失败时由 pytest 报告。
    async def test_isolated_run_leaves_no_bytecode_and_refresh_passes(self, community) -> None:
        """隔离执行必须禁写 `.pyc`，否则下次刷新会因未固定文件禁用该来源。"""
        # Arrange
        repo = community.make_skill_repo(
            "alpha",
            "community-alpha",
            files={"SKILL.md": self._MANIFEST_BODY, "tools.py": self._TOOLS_BODY},
        )
        community.install("alpha", repo)
        manager = _make_manager(community)
        await manager.discover()
        skill = manager.get_skill("community-alpha", tenant_id=1, user_id=1)
        assert skill is not None

        # Act：经隔离 Worker 真实执行一次社区工具
        tool = manager.get_active_tools([skill])[0]
        result = await tool.ainvoke({})

        # Assert
        assert result == {"echo": []}
        worktree = community.worktree("alpha")
        assert not list(worktree.rglob("*.pyc")), "隔离执行在社区工作树写入了字节码"
        assert not list(worktree.rglob("__pycache__")), "隔离执行创建了 __pycache__"
        await manager.discover()
        assert manager.get_skill("community-alpha", tenant_id=1, user_id=1) is not None

    # 方法作用：验证隔离 Worker 启动参数与环境整体禁写字节码。
    # Args: self - pytest 测试类实例；community - 社区夹具；monkeypatch - pytest 补丁工具。
    # Returns: 无返回值，断言失败时由 pytest 报告。
    async def test_isolated_worker_spawn_disables_bytecode(self, community, monkeypatch) -> None:
        """`-B` 覆盖 main 之前的导入窗口，环境变量覆盖子进程内部导入路径。"""
        # Arrange
        from src.skill_runtime import SkillRuntime, _isolated_environment

        repo = community.make_skill_repo(
            "alpha",
            "community-alpha",
            files={"SKILL.md": self._MANIFEST_BODY, "tools.py": self._TOOLS_BODY},
        )
        community.install("alpha", repo)
        manager = _make_manager(community)
        await manager.discover()
        skill = manager.get_skill("community-alpha", tenant_id=1, user_id=1)
        assert skill is not None
        captured: list = []

        # 方法作用：记录 Worker 启动参数后立即中止，避免真实子进程。
        # Args: args - 位置参数；kwargs - 关键字参数。
        # Returns: 不返回，始终抛出 FileNotFoundError。
        async def fake_exec(*args, **kwargs):
            captured.append((args, kwargs))
            raise FileNotFoundError("测试中止")

        monkeypatch.setattr("src.skill_runtime.asyncio.create_subprocess_exec", fake_exec)

        # Act / Assert
        with pytest.raises(FileNotFoundError):
            await SkillRuntime().execute(skill, "community_echo", {}, trusted_builtin=False)
        command = captured[0][0]
        assert command[0] == sys.executable
        assert "-I" in command and "-B" in command, "Worker 启动必须带 -B 禁写字节码"
        assert captured[0][1]["env"].get("PYTHONDONTWRITEBYTECODE") == "1"
        assert _isolated_environment().get("PYTHONDONTWRITEBYTECODE") == "1"
