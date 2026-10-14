"""9.4.2 Git 子模块支持 — 社区 Skill 的只读发现、固定校验与信任边界。

依据: spec/57-skill-submodule-discovery.md；上层渠道见 spec/03-core-components.md §3.10.4。
本模块只发现运维已用 `git submodule` 显式安装、且由父仓库 gitlink 固定 commit 的本地
社区 Skill；运行期绝不执行 clone/fetch/pull/submodule update，不联网、不写父仓库。

信任顺序：父仓库 gitlink（受信）→ HTTPS 白名单 URL（需显式配置）→ 子模块工作树内容
（不可信，须通过过滤器与索引标记预检、固定版本、干净度与全文件跟踪校验）。
git status 输出或路径清单单独都不能证明内容已固定：assume-unchanged 与 skip-worktree
标记会隐藏实际改动，父 `.gitmodules` 与子模块工作树存在此类标记时一律拒绝该来源。
"""

from __future__ import annotations

import asyncio
import configparser
import os
import re
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Sequence
from urllib.parse import urlsplit

from src.logging_config import get_logger

logger = get_logger(__name__)

# 社区子模块内允许的清单层级：根 SKILL.md 或恰好一层子目录的 SKILL.md
_SUBMODULE_SECTION_PATTERN = re.compile(r'^submodule\s+"(?P<name>[^"]*)"$')
_WINDOWS_DRIVE_PATTERN = re.compile(r"^[A-Za-z]:")
_FILTER_CONFIG_PATTERN = r"^filter\..*\.(clean|smudge|process)$"
_GITLINK_MODE = "160000"


# 方法作用：表示一次受控拒绝，来源校验失败但扫描流程继续。
# Args: reason - 不含 URL 与文件内容的拒绝原因。
# Returns: 无返回值。
class _CommunityRejection(Exception):
    """受控拒绝：携带不含 URL 与文件内容的拒绝原因。"""


# 方法作用：表示 git 不可用、超时或调用失败。
# Args: reason - git 失败原因。
# Returns: 无返回值。
class _GitFailure(Exception):
    """git 调用失败：由扫描边界统一降级为跳过社区来源。"""


@dataclass(frozen=True)
class CommunitySubmoduleConfig:
    """社区 Git 子模块发现配置：开关、HTTPS 白名单与 git 超时。"""

    enabled: bool = False
    allowed_repo_urls: tuple[str, ...] = ()
    git_timeout_seconds: float = 15.0

    # 方法作用：从 Settings 字段构造配置，启用时立即校验白名单（fail closed）。
    # Args: enabled - 是否启用；allowed_repo_urls - 逗号分隔字符串或 URL 序列；git_timeout_seconds - git 超时秒数。
    # Returns: 社区子模块配置实例；启用且白名单非法时抛出 ValueError。
    @classmethod
    def from_settings(
        cls,
        enabled: bool,
        allowed_repo_urls: str | Sequence[str] | None,
        *,
        git_timeout_seconds: float = 15.0,
    ) -> "CommunitySubmoduleConfig":
        """关闭状态不读取也不校验白名单，配置错误在启用时即刻暴露。"""
        if not enabled:
            logger.debug("社区子模块发现关闭，跳过白名单校验")
            return cls(
                enabled=False,
                allowed_repo_urls=(),
                git_timeout_seconds=float(git_timeout_seconds),
            )
        urls = parse_allowed_repo_urls(allowed_repo_urls)
        logger.debug("社区子模块配置构造完成", whitelist_size=len(urls))
        return cls(
            enabled=True,
            allowed_repo_urls=urls,
            git_timeout_seconds=float(git_timeout_seconds),
        )


@dataclass(frozen=True)
class CommunitySubmodule:
    """通过全部固定校验的社区子模块及其待加载清单。"""

    name: str
    root: Path
    revision: str
    manifest_paths: tuple[Path, ...]


@dataclass(frozen=True)
class _DeclaredSubmodule:
    """已通过路径与白名单筛选的 .gitmodules 声明。"""

    name: str
    path: str
    leaf: str
    url: str
    root: Path


# 方法作用：校验并规范化仓库 URL，只接受无凭证、无查询、无片段的完整 HTTPS 地址。
# Args: value - 待校验的仓库 URL。
# Returns: 去除首尾空白与末尾斜杠后的合法 URL；非法时抛出 ValueError。
def validate_repository_url(value: str) -> str:
    """白名单与声明 URL 共用同一套形态规则，杜绝近似匹配。"""
    candidate = str(value or "").strip()
    if not candidate:
        raise ValueError("社区 Skill 仓库 URL 不能为空")
    if any(character.isspace() for character in candidate):
        raise ValueError("社区 Skill 仓库 URL 不能包含空白字符")
    if "\\" in candidate:
        raise ValueError("社区 Skill 仓库 URL 不能包含反斜杠")
    normalized = candidate.rstrip("/")
    if not normalized:
        raise ValueError("社区 Skill 仓库 URL 不能为空")
    parsed = urlsplit(normalized)
    scheme = parsed.scheme.lower()
    if scheme != "https":
        raise ValueError(f"社区 Skill 仓库 URL 仅允许 https（实际 scheme={scheme or '无'}）")
    if not parsed.netloc or not parsed.hostname:
        raise ValueError("社区 Skill 仓库 URL 必须包含主机名")
    if parsed.username is not None or parsed.password is not None or "@" in parsed.netloc:
        raise ValueError("社区 Skill 仓库 URL 禁止携带 userinfo")
    if parsed.query:
        raise ValueError("社区 Skill 仓库 URL 禁止携带 query")
    if parsed.fragment:
        raise ValueError("社区 Skill 仓库 URL 禁止携带 fragment")
    return normalized


# 方法作用：解析并逐条严格校验白名单配置。
# Args: raw - 逗号分隔字符串、URL 序列或 None。
# Returns: 去空后逐条验证的 URL 元组；任一条非法时抛出 ValueError。
def parse_allowed_repo_urls(raw: str | Sequence[str] | None) -> tuple[str, ...]:
    """每条白名单项自身必须合法，配置错误在构造期暴露。"""
    if raw is None:
        return ()
    entries = raw.split(",") if isinstance(raw, str) else [str(item) for item in raw]
    result: list[str] = []
    for entry in entries:
        candidate = str(entry).strip()
        if not candidate:
            continue
        result.append(validate_repository_url(candidate))
    logger.debug("社区子模块白名单解析完成", entry_count=len(result))
    return tuple(result)


# 方法作用：构造固定的 -c 覆盖参数，屏蔽 hook、fsmonitor、属性文件与凭证助手。
# Args: 无。
# Returns: git 命令前置的 -c 键值参数列表。
def _fixed_git_overrides() -> list[str]:
    devnull = os.devnull
    return [
        "-c", f"core.hooksPath={devnull}",
        "-c", "core.fsmonitor=false",
        "-c", "core.untrackedCache=false",
        "-c", f"core.attributesFile={devnull}",
        "-c", "core.pager=cat",
        "-c", "credential.helper=",
        "-c", "core.quotePath=false",
    ]


# 方法作用：构造受控 git 环境，移除全部继承 GIT_* 注入后写入安全白名单项。
# Args: 无。
# Returns: 仅含安全项的进程环境映射。
def _hardened_git_environment() -> dict[str, str]:
    environment = {
        name: value
        for name, value in os.environ.items()
        if not name.upper().startswith("GIT_")
    }
    environment.update(
        {
            "GIT_TERMINAL_PROMPT": "0",
            "GIT_ASKPASS": "",
            "SSH_ASKPASS": "",
            "GIT_CONFIG_NOSYSTEM": "1",
            "GIT_CONFIG_GLOBAL": os.devnull,
            "GIT_OPTIONAL_LOCKS": "0",
            "GIT_PAGER": "cat",
        }
    )
    return environment


# 方法作用：判断路径是否位于指定根目录内部（含自身）。
# Args: path - 已解析的目标路径；root - 已解析的根目录。
# Returns: 位于根目录内部时返回 True。
def _is_within(path: Path, root: Path) -> bool:
    return path == root or root in path.parents


class CommunitySubmoduleScanner:
    """只读发现社区 Skill 子模块，并在加载前完成全部固定校验。

    扫描永不抛出：单个子模块失败只跳过自身，扫描级失败只跳过社区来源。
    """

    # 方法作用：绑定社区根目录与配置，并固化白名单比较形式。
    # Args: community_root - 社区子模块根目录；config - 社区子模块配置。
    # Returns: 无返回值。
    def __init__(self, community_root: str | Path, config: CommunitySubmoduleConfig) -> None:
        logger.debug(
            "社区子模块扫描器初始化入口",
            root=str(community_root),
            enabled=config.enabled,
        )
        self.community_root = Path(community_root)
        self.config = config
        self._resolved_community_root = self.community_root.resolve()
        self._allowed_repo_urls = tuple(
            str(url).strip().rstrip("/")
            for url in config.allowed_repo_urls
            if str(url).strip()
        )
        logger.debug(
            "社区子模块扫描器初始化完成",
            whitelist_size=len(self._allowed_repo_urls),
        )

    # 方法作用：扫描社区子模块清单，永不抛出异常。
    # Args: 无。
    # Returns: 通过全部固定校验的社区子模块列表；关闭或失败时返回空列表。
    async def scan(self) -> list[CommunitySubmodule]:
        """关闭状态零副作用；全部文件与 git 校验在线程中执行，不阻塞事件循环。"""
        if not self.config.enabled:
            logger.debug("社区子模块扫描关闭，直接返回")
            return []
        logger.debug("社区子模块扫描入口", root=str(self.community_root))
        try:
            result = await asyncio.to_thread(self._scan_sync)
        except Exception:
            logger.error("社区子模块扫描异常，跳过社区来源", exc_info=True)
            return []
        logger.debug("社区子模块扫描完成", accepted=len(result))
        return result

    # 方法作用：在线程内执行完整的同步扫描流程。
    # Args: 无。
    # Returns: 通过校验的社区子模块列表。
    def _scan_sync(self) -> list[CommunitySubmodule]:
        # 必须先在未经解析的原始路径上识别链接：resolve() 会跟随 symlink/junction，
        # 在解析后的实体目录上重查会把「根目录本身是链接」这一事实抹掉
        if self.community_root.is_symlink() or self.community_root.is_junction():
            logger.error("社区 Skill 根目录是链接，拒绝社区来源")
            return []
        community_root = self._resolved_community_root
        if not community_root.is_dir():
            logger.debug("社区 Skill 根目录不存在，跳过社区来源")
            return []
        try:
            parent_repo = self._locate_parent_repo()
            if parent_repo is None:
                return []
            if self._has_filter_drivers(parent_repo):
                logger.warning("父仓库存在 Git 过滤器配置，跳过社区来源")
                return []
            if not self._gitmodules_is_pinned(parent_repo):
                return []
        except _GitFailure as exc:
            logger.warning("Git 不可用或调用失败，跳过社区来源", reason=str(exc))
            return []
        declarations = self._read_declarations(parent_repo)
        accepted: list[CommunitySubmodule] = []
        rejected = 0
        for declared in declarations:
            try:
                accepted.append(self._verify_submodule(declared))
            except _CommunityRejection as exc:
                rejected += 1
                logger.warning(
                    "社区子模块被拒绝",
                    submodule=declared.leaf,
                    reason=str(exc),
                )
            except _GitFailure as exc:
                rejected += 1
                logger.warning(
                    "社区子模块 Git 调用失败",
                    submodule=declared.leaf,
                    reason=str(exc),
                )
            except Exception:
                rejected += 1
                logger.error("社区子模块校验异常", submodule=declared.leaf, exc_info=True)
        logger.info("社区子模块扫描完成", accepted=len(accepted), rejected=rejected)
        return accepted

    # 方法作用：从社区根向上定位父仓库，并以 rev-parse 确认仓库根。
    # Args: 无。
    # Returns: 父仓库根目录；不在 Git 仓库内时返回 None。
    def _locate_parent_repo(self) -> Path | None:
        current = self._resolved_community_root
        while True:
            git_entry = current / ".git"
            if git_entry.exists() or git_entry.is_symlink():
                break
            if current.parent == current:
                logger.debug("社区根不在 Git 仓库内，跳过社区来源")
                return None
            current = current.parent
        toplevel = self._run_git(current, "rev-parse", "--show-toplevel").strip()
        if not toplevel:
            logger.debug("git 未返回仓库根，跳过社区来源")
            return None
        result = Path(toplevel).resolve()
        if not result.is_dir():
            logger.debug("git 返回的仓库根不存在，跳过社区来源")
            return None
        return result

    # 方法作用：只读检查有效 Git 配置中是否存在非空 clean/smudge/process 过滤器。
    # Args: repo - 待检查仓库的工作目录。
    # Returns: 存在任一非空过滤器时返回 True。
    def _has_filter_drivers(self, repo: Path) -> bool:
        output = self._run_git(
            repo,
            "config",
            "--null",
            "--get-regexp",
            _FILTER_CONFIG_PATTERN,
            no_value_ok=True,
        )
        for entry in output.split("\0"):
            if not entry.strip():
                continue
            _, separator, value = entry.partition("\n")
            if separator and value.strip():
                return True
        return False

    # 方法作用：只读检查索引中的 assume-unchanged / skip-worktree 标记，二者都会让 git status 漏报实际改动。
    # Args: repo - 待检查仓库的工作目录；pathspec - 可选路径限定（如父仓库只查 .gitmodules）。
    # Returns: 存在任一隐藏标记时返回 True；git 调用失败按 _GitFailure 上抛。
    def _has_index_hints(self, repo: Path, pathspec: str | None = None) -> bool:
        """索引标记不能代表内容固定：status 报干净不代表工作树与提交一致，标记存在即拒绝该来源。"""
        args = ["ls-files", "-v", "-z"]
        if pathspec is not None:
            args += ["--", pathspec]
        output = self._run_git(repo, *args)
        for entry in output.split("\0"):
            if not entry:
                continue
            # `-v` 输出为 "<tag> <path>"：S 为 skip-worktree，小写 tag 为 assume-unchanged
            tag = entry[:1]
            if tag == "S" or tag.islower():
                return True
        return False

    # 方法作用：确认父仓库 .gitmodules 的实际内容与 HEAD 固定内容一致。
    # Args: parent_repo - 父仓库根目录。
    # Returns: 已固定且内容一致时返回 True。
    def _gitmodules_is_pinned(self, parent_repo: Path) -> bool:
        if not (parent_repo / ".gitmodules").is_file():
            logger.debug("父仓库没有 .gitmodules，跳过社区来源")
            return False
        tree = self._run_git(parent_repo, "ls-tree", "HEAD", "--", ".gitmodules")
        entries = [line for line in tree.splitlines() if line.strip()]
        if len(entries) != 1:
            logger.warning(".gitmodules 未固定于 HEAD，跳过社区来源")
            return False
        metadata, _, tree_path = entries[0].partition("\t")
        parts = metadata.split()
        if len(parts) != 3 or parts[1] != "blob" or tree_path.strip() != ".gitmodules":
            logger.warning(".gitmodules 在 HEAD 中不是普通文件，跳过社区来源")
            return False
        # 索引标记会让 status 漏报实际改动，必须在信任 status 之前拒绝
        if self._has_index_hints(parent_repo, ".gitmodules"):
            logger.warning(".gitmodules 存在 assume-unchanged/skip-worktree 标记，跳过社区来源")
            return False
        status = self._run_git(
            parent_repo,
            "status",
            "--porcelain",
            "--untracked-files=all",
            "--",
            ".gitmodules",
        )
        if status.strip():
            logger.warning(".gitmodules 实际内容与 HEAD 不一致，跳过社区来源")
            return False
        return True

    # 方法作用：解析已固定的 .gitmodules，筛出社区根下一层且 URL 通过白名单的声明。
    # Args: parent_repo - 父仓库根目录。
    # Returns: 待固定校验的声明记录列表。
    def _read_declarations(self, parent_repo: Path) -> list[_DeclaredSubmodule]:
        gitmodules = parent_repo / ".gitmodules"
        if not gitmodules.is_file():
            return []
        try:
            community_rel = self._resolved_community_root.relative_to(parent_repo).as_posix()
        except ValueError:
            logger.warning("社区根不在父仓库内，跳过社区来源")
            return []
        parser = configparser.ConfigParser()
        try:
            parser.read_string(gitmodules.read_text(encoding="utf-8"))
        except (configparser.Error, UnicodeError, OSError):
            logger.warning("父仓库 .gitmodules 无法解析，跳过社区来源")
            return []
        declarations: list[_DeclaredSubmodule] = []
        for section in parser.sections():
            matched = _SUBMODULE_SECTION_PATTERN.match(section.strip())
            if matched is None:
                continue
            raw_path = str(parser.get(section, "path", fallback="") or "").strip()
            leaf = self._single_level_leaf(raw_path, community_rel)
            if leaf is None:
                continue
            raw_url = str(parser.get(section, "url", fallback="") or "").strip()
            try:
                url = validate_repository_url(raw_url)
            except ValueError:
                logger.warning(
                    "社区子模块声明被忽略",
                    submodule=leaf,
                    reason="URL 形态非法",
                )
                continue
            if url not in self._allowed_repo_urls:
                logger.warning(
                    "社区子模块声明被忽略",
                    submodule=leaf,
                    reason="URL 不在白名单",
                )
                continue
            declarations.append(
                _DeclaredSubmodule(
                    name=matched.group("name"),
                    path=raw_path.rstrip("/"),
                    leaf=leaf,
                    url=url,
                    root=self._resolved_community_root / leaf,
                )
            )
        return declarations

    # 方法作用：把 .gitmodules 的 path 解析为社区根下的一层目录名。
    # Args: raw_path - 声明的原始路径；community_rel - 社区根相对父仓库根的 POSIX 路径。
    # Returns: 合法的一层目录名；越权、多层或不匹配时返回 None。
    def _single_level_leaf(self, raw_path: str, community_rel: str) -> str | None:
        if not raw_path or "\\" in raw_path:
            return None
        if raw_path.startswith("/") or _WINDOWS_DRIVE_PATTERN.match(raw_path):
            return None
        segments = raw_path.rstrip("/").split("/")
        if any(segment in {"", ".", ".."} for segment in segments):
            return None
        prefix = community_rel.split("/")
        if len(segments) != len(prefix) + 1 or segments[:-1] != prefix:
            return None
        return segments[-1]

    # 方法作用：按序执行单个社区子模块的全部固定校验，任一步失败即拒绝。
    # Args: declared - 已通过声明层筛选的子模块记录。
    # Returns: 通过固定校验的 CommunitySubmodule。
    def _verify_submodule(self, declared: _DeclaredSubmodule) -> CommunitySubmodule:
        root = declared.root
        if root.is_symlink() or root.is_junction():
            raise _CommunityRejection("安装路径是链接")
        if not root.is_dir():
            raise _CommunityRejection("安装目录不存在")
        resolved = root.resolve()
        if resolved == self._resolved_community_root or not _is_within(
            resolved, self._resolved_community_root,
        ):
            raise _CommunityRejection("路径逃逸社区根目录")

        git_entry = resolved / ".git"
        if git_entry.is_symlink() or git_entry.is_junction():
            raise _CommunityRejection(".git 是链接")
        if not git_entry.exists():
            raise _CommunityRejection("子模块未初始化")
        if git_entry.is_file():
            self._check_gitdir_ownership(resolved, git_entry)
        elif not git_entry.is_dir():
            raise _CommunityRejection(".git 形态非法")

        parent_repo = self._locate_parent_repo()
        if parent_repo is None:
            raise _CommunityRejection("父仓库不可用")
        gitlink_commit = self._read_gitlink(parent_repo, declared)

        head = self._run_git(resolved, "rev-parse", "HEAD").strip()
        if head != gitlink_commit:
            raise _CommunityRejection("HEAD 与父仓库 gitlink 不一致")

        origin = self._run_git(
            resolved, "config", "--get", "remote.origin.url", no_value_ok=True,
        ).strip().rstrip("/")
        if not origin or origin != declared.url:
            raise _CommunityRejection("远程 URL 与固定声明不一致")

        if self._has_filter_drivers(resolved):
            raise _CommunityRejection("存在 Git 过滤器配置")
        # 索引标记会隐藏已跟踪文件的实际改动，标记存在即视为无法证明内容固定
        if self._has_index_hints(resolved):
            raise _CommunityRejection("工作树存在 assume-unchanged/skip-worktree 索引标记")
        status = self._run_git(
            resolved, "status", "--porcelain", "--untracked-files=no",
        )
        if status.strip():
            raise _CommunityRejection("工作树不干净")

        self._check_worktree_is_pinned(resolved)

        manifest_paths = self._locate_manifests(resolved)
        if not manifest_paths:
            logger.debug("社区子模块没有可加载清单", submodule=declared.leaf)
        return CommunitySubmodule(
            name=declared.leaf,
            root=resolved,
            revision=gitlink_commit,
            manifest_paths=manifest_paths,
        )

    # 方法作用：确认子模块 gitdir 归属父仓库 modules 目录，阻止 gitdir 指针逃逸。
    # Args: root - 子模块工作树根目录；git_entry - 子模块 .git 文件路径。
    # Returns: 归属合法时无返回值；否则抛出受控拒绝。
    def _check_gitdir_ownership(self, root: Path, git_entry: Path) -> None:
        pointer = git_entry.read_text(encoding="utf-8", errors="replace").strip()
        if not pointer.startswith("gitdir:"):
            raise _CommunityRejection("子模块 .git 指针格式非法")
        target = pointer[len("gitdir:"):].strip()
        if not target:
            raise _CommunityRejection("子模块 .git 指针为空")
        gitdir = Path(target)
        gitdir = gitdir.resolve() if gitdir.is_absolute() else (root / gitdir).resolve()
        parent_repo = self._locate_parent_repo()
        if parent_repo is None:
            raise _CommunityRejection("父仓库不可用")
        parent_git_dir = Path(
            self._run_git(parent_repo, "rev-parse", "--absolute-git-dir").strip()
        ).resolve()
        modules_root = parent_git_dir / "modules"
        if modules_root not in gitdir.parents:
            raise _CommunityRejection("子模块 gitdir 不在父仓库 modules 目录下")

    # 方法作用：读取父仓库 HEAD 中该子模块的 gitlink 记录。
    # Args: parent_repo - 父仓库根目录；declared - 声明记录（提供固定相对路径）。
    # Returns: gitlink 固定的 commit。
    def _read_gitlink(self, parent_repo: Path, declared: _DeclaredSubmodule) -> str:
        tree = self._run_git(parent_repo, "ls-tree", "HEAD", "--", declared.path)
        lines = [line for line in tree.splitlines() if line.strip()]
        if len(lines) != 1:
            raise _CommunityRejection("父仓库 HEAD 中没有 gitlink")
        metadata, _, tree_path = lines[0].partition("\t")
        parts = metadata.split()
        if (
            len(parts) != 3
            or parts[0] != _GITLINK_MODE
            or parts[1] != "commit"
            or tree_path.strip() != declared.path
        ):
            raise _CommunityRejection("gitlink 记录非法")
        return parts[2]

    # 方法作用：校验工作树全部文件都被 gitlink 所指提交跟踪，且不含任何链接。
    # Args: root - 子模块工作树根目录。
    # Returns: 通过校验时无返回值；否则抛出受控拒绝。
    def _check_worktree_is_pinned(self, root: Path) -> None:
        tracked_raw = self._run_git(root, "ls-files", "-z")
        tracked = {entry for entry in tracked_raw.split("\0") if entry}
        worktree_files, reason = self._collect_worktree_files(root)
        if reason:
            raise _CommunityRejection(reason)
        untracked = sorted(worktree_files - tracked)
        if untracked:
            raise _CommunityRejection(
                f"存在 {len(untracked)} 个未固定文件，例如 {untracked[0]}"
            )
        missing = sorted(tracked - worktree_files)
        if missing:
            raise _CommunityRejection(
                f"缺少 {len(missing)} 个已固定文件，例如 {missing[0]}"
            )

    # 方法作用：遍历工作树收集全部文件相对路径，并拒绝任何 symlink/junction。
    # Args: root - 子模块工作树根目录。
    # Returns: (POSIX 相对路径集合, 拒绝原因)；无链接时原因为空字符串。
    def _collect_worktree_files(self, root: Path) -> tuple[set[str], str]:
        files: set[str] = set()
        for dirpath, dirnames, filenames in os.walk(root, followlinks=False):
            directory = Path(dirpath)
            if directory == root and ".git" in dirnames:
                dirnames.remove(".git")
            for name in dirnames:
                if (directory / name).is_symlink() or (directory / name).is_junction():
                    return set(), "工作树包含链接目录"
            for name in filenames:
                child = directory / name
                if child.is_symlink():
                    return set(), "工作树包含链接文件"
                if directory == root and name == ".git":
                    continue
                files.add(child.relative_to(root).as_posix())
        return files, ""

    # 方法作用：定位子模块内允许的清单：根 SKILL.md 或恰好一层子目录的 SKILL.md。
    # Args: root - 子模块工作树根目录。
    # Returns: 清单路径元组；清单或父目录是链接时抛出受控拒绝。
    def _locate_manifests(self, root: Path) -> tuple[Path, ...]:
        root_manifest = root / "SKILL.md"
        if root_manifest.is_symlink():
            raise _CommunityRejection("根清单是链接")
        if root_manifest.is_file():
            return (root_manifest,)
        manifests: list[Path] = []
        for child in sorted(root.iterdir()):
            if not child.is_dir():
                continue
            if child.is_symlink() or child.is_junction():
                raise _CommunityRejection("一层子目录是链接")
            candidate = child / "SKILL.md"
            if candidate.is_symlink():
                raise _CommunityRejection("清单是链接")
            if candidate.is_file():
                resolved = candidate.resolve()
                if not _is_within(resolved, root.resolve()):
                    raise _CommunityRejection("清单逃逸子模块根目录")
                manifests.append(candidate)
        return tuple(manifests)

    # 方法作用：以加固环境执行一次只读 git 子命令，绝不拼接 shell 字符串。
    # Args: cwd - 命令工作目录；args - git 子命令与参数；no_value_ok - 是否允许"无匹配值"退出码 1。
    # Returns: 标准输出文本；no_value_ok 且无匹配时返回空字符串。
    def _run_git(self, cwd: Path, *args: str, no_value_ok: bool = False) -> str:
        command = ["git", *_fixed_git_overrides(), *args]
        timeout = max(0.5, min(float(self.config.git_timeout_seconds), 300.0))
        try:
            completed = subprocess.run(
                command,
                cwd=str(cwd),
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="surrogateescape",
                timeout=timeout,
                shell=False,
                env=_hardened_git_environment(),
                check=False,
            )
        except FileNotFoundError as exc:
            raise _GitFailure("git 不可用") from exc
        except subprocess.TimeoutExpired as exc:
            raise _GitFailure("git 调用超时") from exc
        except OSError as exc:
            raise _GitFailure("git 进程无法启动") from exc
        if completed.returncode == 1 and no_value_ok:
            return ""
        if completed.returncode != 0:
            raise _GitFailure(f"git 子命令失败，退出码 {completed.returncode}")
        return completed.stdout
