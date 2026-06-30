"""9.1 Skill 引擎 — Skill 发现、加载、激活与生命周期管理。

依据: SPEC §3.10 Skills 技能系统
"""

from __future__ import annotations

import importlib.util
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

from src.logging_config import get_logger

logger = get_logger(__name__)


# ── 9.1.1 Skill 数据模型 ─────────────────────────────

@dataclass
class Skill:
    """Skill 清单 — 自包含的能力扩展包。"""
    name: str
    version: str
    description: str
    triggers: dict          # keywords, intents, tables
    depends_on: dict         # mcp_servers, skills, python_packages
    tools: list[dict]
    system_prompt_override: str
    output_schema_extension: dict = field(default_factory=dict)
    source_path: str = ""
    enabled: bool = True


# ── 9.1.2 SkillManager ──────────────────────────────

class SkillManager:
    """管理 Skill 的发现、加载、激活与生命周期。

    支持多目录扫描：内置目录始终加载，额外目录作为补充。
    同名 Skill 以先发现的为准（内置优先）。
    """

    def __init__(self, builtin_dir: str = "skills", extra_dirs: str = ""):
        self.builtin_dir = Path(builtin_dir)
        self.extra_dirs = [Path(d.strip()) for d in extra_dirs.split(";") if d.strip()]
        self.all_dirs: list[Path] = []
        self.skills: dict[str, Skill] = {}

    async def discover(self) -> None:
        """全量重新扫描所有目录 — 清空旧数据，重新解析所有 SKILL.md。

        同名 skill 以先发现的为准（内置目录优先）。
        首次调用时加载，后续由用户手动触发刷新。
        """
        self.all_dirs = [self.builtin_dir] + self.extra_dirs
        fresh: dict[str, Skill] = {}
        for d in self.all_dirs:
            if not d.exists():
                logger.info("Skill 目录不存在", path=str(d))
                continue
            for skill_md in sorted(d.glob("*/SKILL.md")):
                try:
                    skill = self._parse_skill_manifest(skill_md)
                    if skill.name in fresh:
                        continue
                    missing = self._check_dependencies(skill)
                    if missing:
                        logger.warning("Skill 缺少依赖", skill=skill.name, missing=missing)
                        skill.enabled = False
                    fresh[skill.name] = skill
                    logger.info("Skill 已加载", name=skill.name, version=skill.version,
                                enabled=skill.enabled, dir=str(d))
                except Exception as e:
                    logger.error("Skill 解析失败", path=str(skill_md), error=str(e))
        self.skills = fresh
        logger.info("Skill 扫描完成", total=len(self.skills))

    def add_skill(self, skill: Skill) -> None:
        """直接注入缓存（上传后使用，无需重新扫描）。"""
        self.skills[skill.name] = skill
        logger.info("Skill 已注入缓存", name=skill.name)

    def is_builtin(self, name: str) -> bool:
        """判断 skill 是否来自内置目录。"""
        skill = self.skills.get(name)
        if not skill:
            return False
        source = Path(skill.source_path).resolve()
        builtin = self.builtin_dir.resolve()
        return str(source).startswith(str(builtin))

    def remove_skill(self, name: str) -> bool:
        """从缓存中移除。"""
        if name in self.skills:
            del self.skills[name]
            logger.info("Skill 已从缓存移除", name=name)
            return True
        return False

    @property
    def upload_dir(self) -> Path:
        """上传写入目录 — 优先第一个额外目录，否则内置目录。"""
        for d in self.extra_dirs:
            if d.exists():
                return d
        return self.builtin_dir

    # ── 9.1.6 匹配 ──────────────────────────────────

    def match_skills(
        self, user_query: str, intent: str, tables: list[str] | None = None,
    ) -> list[Skill]:
        """关键词 + 意图 + 表名三重 OR 匹配。"""
        tables = tables or []
        query_lower = user_query.lower()
        activated: list[Skill] = []
        for skill in self.skills.values():
            if not skill.enabled:
                continue
            triggers = skill.triggers or {}
            if any(kw.lower() in query_lower for kw in triggers.get("keywords", [])):
                activated.append(skill)
            elif intent in triggers.get("intents", []):
                activated.append(skill)
            elif set(tables) & set(triggers.get("tables", [])):
                activated.append(skill)
        if activated:
            logger.info("Skill 激活", names=[s.name for s in activated])
        return activated

    # ── 9.1.7 获取工具 ──────────────────────────────

    def get_active_tools(self, activated_skills: list[Skill]) -> list:
        """获取激活 Skills 的所有 BaseTool。"""
        tools: list = []
        for skill in activated_skills:
            try:
                mod = self._load_skill_module(skill)
                if mod and hasattr(mod, "get_tool"):
                    for td in (skill.tools or []):
                        t = mod.get_tool(td["name"])
                        if t:
                            tools.append(t)
            except Exception as e:
                logger.warning("Skill 工具加载失败", skill=skill.name, error=str(e))
        return tools

    # ── 9.1.8 构建 Prompt ───────────────────────────

    def build_skill_prompt(self, activated_skills: list[Skill]) -> str:
        """将激活的 Skill 指令追加到 System Prompt。"""
        if not activated_skills:
            return ""
        parts = ["\n## 激活的技能\n"]
        for s in activated_skills:
            parts.append(f"### {s.name}")
            parts.append(s.system_prompt_override)
            parts.append("")
        return "\n".join(parts)

    # ── 9.1.4 解析清单 ───────────────────────────────

    def _parse_skill_manifest(self, skill_md_path: Path) -> Skill:
        content = skill_md_path.read_text(encoding="utf-8")
        parts = content.split("---", 2)
        if len(parts) < 3:
            raise ValueError(f"缺少 YAML frontmatter: {skill_md_path}")
        manifest = yaml.safe_load(parts[1])
        return Skill(
            name=manifest.get("name", skill_md_path.parent.name),
            version=str(manifest.get("version", "1.0.0")),
            description=manifest.get("description", ""),
            triggers=manifest.get("triggers", {}),
            depends_on=manifest.get("depends_on", {}),
            tools=manifest.get("tools", []),
            system_prompt_override=parts[2].strip(),
            output_schema_extension=manifest.get("output_schema_extension", {}),
            source_path=str(skill_md_path.parent),
        )

    # ── 9.1.5 依赖检查 ───────────────────────────────

    @staticmethod
    def _check_dependencies(skill: Skill) -> list[str]:
        missing: list[str] = []
        for pkg in (skill.depends_on.get("python_packages", []) or []):
            try:
                __import__(pkg)
            except ImportError:
                missing.append(pkg)
        return missing

    # ── 9.1.9 动态加载 ───────────────────────────────

    @staticmethod
    def _load_skill_module(skill: Skill):
        tools_path = Path(skill.source_path) / "tools.py"
        if not tools_path.exists():
            return None
        module_name = f"skills.{skill.name}.tools"
        spec = importlib.util.spec_from_file_location(module_name, tools_path)
        if spec is None or spec.loader is None:
            return None
        module = importlib.util.module_from_spec(spec)
        sys.modules[module_name] = module
        spec.loader.exec_module(module)
        return module


# ── 全局单例 ──────────────────────────────────────

_skill_manager: SkillManager | None = None


def get_skill_manager(builtin_dir: str = "skills", extra_dirs: str = "") -> SkillManager:
    global _skill_manager
    if _skill_manager is None:
        _skill_manager = SkillManager(builtin_dir, extra_dirs)
    return _skill_manager
