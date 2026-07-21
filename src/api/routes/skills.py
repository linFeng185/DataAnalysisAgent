"""Skills 管理路由。"""

from __future__ import annotations

import io
import html
import json
import os
import time
import uuid
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import APIRouter, Body, File, HTTPException, Query, UploadFile

from src.api.schemas import (
    ChatRequest, ChatResponse, ColumnCommentRequest,
    DataSourceCreateRequest, DataSourceInfo, HealthResponse, KnowledgeTagCreateRequest,
    KnowledgeTagStatusRequest, MCPServerCreate, TableInfo,
)
from src.exceptions import DataSourceNotFoundError
from src.llm.client import is_llm_available
from src.logging_config import get_logger
from src.api.routes._helpers import _app, _authorize_extension_scope, _registry

logger = get_logger(__name__)
router = APIRouter()
_started_at = time.time()

# ---- Skills 管理 ----


@router.get("/skills")
# 方法作用：列出当前身份可见的 Skill 资源。
# Args: skill_scope - 可选 system/tenant/private 过滤范围。
# Returns: Skill 列表和总数。
async def list_skills(skill_scope: str | None = None):
    """列出当前身份可见的系统、租户和个人 Skills。"""
    logger.debug("Skill 列表入口", skill_scope=skill_scope or "")
    try:
        from src.api.auth import get_current_tenant_id, get_current_user_id
        from src.knowledge.governance import normalize_knowledge_scope
        from src.skill_manager import get_skill_manager
        mgr = get_skill_manager()
        skills = []
        normalized_scope = None
        if skill_scope:
            try:
                normalized_scope = normalize_knowledge_scope(skill_scope).value
            except ValueError as exc:
                raise HTTPException(400, str(exc)) from exc
        tenant_id = get_current_tenant_id()
        user_id = get_current_user_id()
        for s in mgr.get_visible_skills(tenant_id, user_id):
            if normalized_scope and s.scope != normalized_scope:
                continue
            triggers = s.triggers or {}
            tools = s.tools or []
            deps = s.depends_on or {}
            skills.append({
                "name": s.name,
                "version": s.version,
                "enabled": s.enabled,
                "description": s.description or "",
                "triggers": triggers.get("keywords", []),
                "intents": triggers.get("intents", []),
                "tools": [t.get("name", "") for t in tools],
                "dependencies": deps.get("python_packages", []),
                "is_builtin": mgr.is_builtin(
                    s.name, tenant_id=tenant_id, user_id=user_id, scope=s.scope,
                ),
                "scope": s.scope,
                "tenant_id": s.tenant_id,
                "owner_user_id": s.owner_user_id,
            })
        result = {"skills": skills, "total": len(skills)}
        logger.info("Skill 列表完成", total=len(skills), tenant_id=tenant_id, user_id=user_id)
        return result
    except HTTPException:
        raise
    except Exception as e:
        logger.error("Skills 列表加载失败", error=str(e), exc_info=True)
        return {"skills": [], "total": 0}


# 方法作用：按配置限制读取 Skill 上传内容并检查单文件与累计大小。
# Args: files - 上传文件；max_bytes - 单文件上限；max_files - 文件数上限；max_total_bytes - 累计上限。
# Returns: 以 UploadFile 对象标识为键的原始内容映射。
async def _read_skill_uploads(
    files: list[UploadFile],
    max_bytes: int,
    max_files: int,
    max_total_bytes: int,
) -> dict[int, bytes]:
    logger.debug("读取 Skill 上传入口", file_count=len(files), max_files=max_files)
    if len(files) > max_files:
        logger.warning("Skill 上传文件数超限", file_count=len(files), limit=max_files)
        raise HTTPException(413, f"单次最多上传 {max_files} 个文件")
    prepared: dict[int, bytes] = {}
    total_bytes = 0
    for uploaded_file in files:
        if not uploaded_file.filename:
            continue
        content = await uploaded_file.read(max_bytes + 1)
        if len(content) > max_bytes:
            logger.warning(
                "Skill 上传单文件超限",
                filename=uploaded_file.filename,
                size=len(content),
                limit=max_bytes,
            )
            raise HTTPException(413, f"文件 '{uploaded_file.filename}' 超过大小限制")
        total_bytes += len(content)
        if total_bytes > max_total_bytes:
            logger.warning("Skill 上传累计大小超限", total_bytes=total_bytes, limit=max_total_bytes)
            raise HTTPException(413, "上传文件累计大小超过限制")
        prepared[id(uploaded_file)] = content
    logger.info("读取 Skill 上传完成", file_count=len(prepared), total_bytes=total_bytes)
    return prepared


# 方法作用：解析已写入的 Skill 清单并增量注入 SkillManager 缓存。
# Args: imported - 已导入清单；skills_dir - 目标目录；scope - 作用域；tenant_id - 租户；user_id - 用户。
# Returns: 缓存注入失败明细。
def _cache_uploaded_skills(
    imported: list[dict],
    skills_dir: str,
    scope: str,
    tenant_id: int,
    user_id: int,
) -> list[dict]:
    from pathlib import Path
    from src.skill_manager import get_skill_manager

    logger.debug("缓存上传 Skill 入口", imported=len(imported), scope=scope)
    errors: list[dict] = []
    manager = get_skill_manager()
    for item in imported:
        try:
            manifest_path = os.path.join(skills_dir, item["name"], "SKILL.md")
            if os.path.isfile(manifest_path):
                skill = manager._parse_skill_manifest(  # noqa: SLF001
                    Path(manifest_path),
                    scope=scope,
                    tenant_id=tenant_id if scope != "system" else 0,
                    owner_user_id=user_id if scope == "private" else 0,
                )
                manager.add_skill(skill)
        except Exception as exc:
            logger.error("Skill 上传缓存注入失败", name=item["name"], exc_info=True)
            errors.append({"file": item["file"], "error": str(exc)})
    logger.info("缓存上传 Skill 完成", errors=len(errors))
    return errors


# 方法作用：解析并安全写入单个上传的 SKILL.md 清单。
# Args: uploaded_file - 上传文件；prepared - 内容映射；skills_dir - 目标目录；scope - 作用域。
# Returns: 导入记录、是否跳过和错误记录三元组。
def _write_uploaded_skill(
    uploaded_file: UploadFile,
    prepared: dict[int, bytes],
    skills_dir: str,
    scope: str,
) -> tuple[dict | None, bool, dict | None]:
    import re
    import yaml

    logger.debug("写入单个 Skill 入口", filename=uploaded_file.filename or "", scope=scope)
    if not uploaded_file.filename:
        logger.info("写入单个 Skill 完成", skipped=True, reason="空文件名")
        return None, False, None
    if os.path.basename(uploaded_file.filename).lower() != "skill.md":
        logger.info("写入单个 Skill 完成", skipped=True, reason="非 SKILL.md")
        return None, True, None
    try:
        content = prepared[id(uploaded_file)].decode("utf-8")
    except Exception as exc:
        logger.warning("写入单个 Skill 解码失败", filename=uploaded_file.filename)
        return None, False, {"file": uploaded_file.filename, "error": str(exc)}
    match = re.match(r"^---\s*\n(.*?)\n---", content, re.DOTALL)
    skill_name = None
    if match:
        try:
            metadata = yaml.safe_load(match.group(1)) or {}
            skill_name = metadata.get("name", "")
        except Exception as exc:
            logger.warning(
                "Skill frontmatter 解析失败，回退目录名",
                filename=uploaded_file.filename,
                error=str(exc),
                exc_info=True,
            )
    if not skill_name:
        parent = os.path.basename(os.path.dirname(uploaded_file.filename))
        if parent and parent != ".":
            skill_name = parent
        else:
            error = {"file": uploaded_file.filename, "error": "无法从 frontmatter 或路径提取 skill name"}
            logger.warning("写入单个 Skill 失败", filename=uploaded_file.filename, reason="缺少名称")
            return None, False, error
    root = os.path.realpath(skills_dir)
    dest_dir = os.path.realpath(os.path.join(root, os.path.basename(skill_name)))
    if os.path.commonpath([dest_dir, root]) != root:
        logger.warning("Skill 上传路径拒绝", skill_name=skill_name)
        raise HTTPException(403, "禁止访问")
    os.makedirs(dest_dir, exist_ok=True)
    with open(os.path.join(dest_dir, "SKILL.md"), "w", encoding="utf-8") as file_handle:
        file_handle.write(content)
    result = {"name": skill_name, "file": uploaded_file.filename, "scope": scope}
    logger.info("写入单个 Skill 完成", skipped=False, skill_name=skill_name)
    return result, False, None


@router.post("/skills/upload")
# 方法作用：把 Skill 清单上传到当前身份有权写入的受管目录。
# Args: files - 上传文件；skill_scope - 目标作用域，默认 private。
# Returns: 导入、跳过和失败明细。
async def upload_skills(
    files: list[UploadFile] = File(...),
    skill_scope: str = "private",
):
    """按当前认证身份批量上传 SKILL.md 到受管作用域目录。

    前端选择文件夹时，通过 webkitdirectory 传入整个文件夹的所有文件，
    后端递归过滤 SKILL.md（大小写不敏感），写入 skills/<name>/SKILL.md。
    """
    from src.api.auth import (
        get_current_role, get_current_tenant_id, get_current_user_id,
    )
    from src.config import get_settings
    from src.knowledge.governance import can_write_knowledge_scope, normalize_knowledge_scope
    from src.skill_manager import get_skill_manager

    logger.debug("Skill 上传入口", skill_scope=skill_scope, file_count=len(files))
    try:
        normalized_scope = normalize_knowledge_scope(skill_scope).value
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
    role = get_current_role()
    tenant_id = get_current_tenant_id()
    user_id = get_current_user_id()
    settings = get_settings()
    if not can_write_knowledge_scope(
        normalized_scope,
        role=role,
        user_id=user_id,
        multi_tenant=settings.multi_tenant,
    ):
        logger.warning(
            "Skill 上传权限拒绝",
            skill_scope=normalized_scope,
            role=role,
            tenant_id=tenant_id,
            user_id=user_id,
        )
        raise HTTPException(403, f"当前角色无权写入 {normalized_scope} Skill")
    max_upload_bytes = max(1, int(getattr(settings, "max_upload_bytes", 20 * 1024 * 1024)))
    max_upload_files = max(1, int(getattr(settings, "max_upload_files", 20)))
    max_upload_total_bytes = max(
        max_upload_bytes,
        int(getattr(settings, "max_upload_total_bytes", 100 * 1024 * 1024)),
    )
    prepared_contents = await _read_skill_uploads(
        files,
        max_upload_bytes,
        max_upload_files,
        max_upload_total_bytes,
    )
    mgr = get_skill_manager()
    skills_dir = str(mgr.get_upload_dir(
        normalized_scope, tenant_id=tenant_id, user_id=user_id,
    ))
    os.makedirs(skills_dir, exist_ok=True)
    imported: list[dict] = []
    skipped: list[str] = []
    errors: list[dict] = []

    for uploaded_file in files:
        imported_item, was_skipped, error = _write_uploaded_skill(
            uploaded_file,
            prepared_contents,
            skills_dir,
            normalized_scope,
        )
        if imported_item:
            imported.append(imported_item)
        if was_skipped and uploaded_file.filename:
            skipped.append(uploaded_file.filename)
        if error:
            errors.append(error)

    errors.extend(
        _cache_uploaded_skills(
            imported,
            skills_dir,
            normalized_scope,
            tenant_id,
            user_id,
        ),
    )

    result = {
        "imported": imported,
        "skipped": skipped,
        "errors": errors,
        "total": len(imported),
    }
    logger.info(
        "Skill 上传完成",
        skill_scope=normalized_scope,
        tenant_id=tenant_id,
        user_id=user_id,
        imported=len(imported),
        errors=len(errors),
    )
    return result

# ---- Skill 管理操作 ----


@router.post("/skills/refresh")
# 方法作用：重新扫描全部受信任 Skill 目录并返回当前身份可见数量。
# Args: 无。
# Returns: 刷新状态和可见 Skill 数量。
async def refresh_skills():
    """全量重新扫描 Skill 目录（用户手动触发）。"""
    try:
        from src.skill_manager import get_skill_manager
        await get_skill_manager().discover()
        from src.api.auth import get_current_tenant_id, get_current_user_id
        visible = get_skill_manager().get_visible_skills(
            get_current_tenant_id(), get_current_user_id(),
        )
        result = {"status": "ok", "total": len(visible)}
        logger.info("Skill 刷新完成", total=len(visible))
        return result
    except Exception as e:
        logger.error("Skill 刷新失败", error=str(e), exc_info=True)
        raise HTTPException(500, str(e))


@router.get("/skills/{skill_name}/content")
# 方法作用：读取当前身份可见 Skill 的原始 SKILL.md。
# Args: skill_name - Skill 名称；skill_scope - 可选精确作用域。
# Returns: Skill 名称、作用域和文件内容。
async def get_skill_content(skill_name: str, skill_scope: str | None = None):
    """获取 SKILL.md 文件的原始内容（直接从缓存 source_path 读取）。"""
    from src.skill_manager import get_skill_manager
    from src.api.auth import get_current_tenant_id, get_current_user_id
    mgr = get_skill_manager()
    skill = mgr.get_skill(
        skill_name, scope=skill_scope,
        tenant_id=get_current_tenant_id(), user_id=get_current_user_id(),
    )
    if not skill:
        raise HTTPException(404, f"Skill '{skill_name}' 未找到")
    md_path = os.path.join(skill.source_path, "SKILL.md")
    if not os.path.isfile(md_path):
        raise HTTPException(404, f"SKILL.md 文件不存在: {md_path}")
    with open(md_path, "r", encoding="utf-8", errors="replace") as f:
        result = {"name": skill_name, "scope": skill.scope, "content": f.read()}
    logger.info("读取 Skill 内容完成", name=skill_name, scope=skill.scope)
    return result


@router.put("/skills/{skill_name}/toggle")
# 方法作用：启用或停用当前身份有权管理的 Skill。
# Args: skill_name - Skill 名称；enabled - 目标状态；skill_scope - 可选精确作用域。
# Returns: 修改后的 Skill 状态。
async def toggle_skill(
    skill_name: str, enabled: bool = Query(...), skill_scope: str | None = None,
):
    """启用或禁用一个 Skill。"""
    try:
        from src.skill_manager import get_skill_manager
        from src.api.auth import (
            get_current_role, get_current_tenant_id, get_current_user_id,
        )
        from src.knowledge.governance import can_manage_knowledge_resource
        mgr = get_skill_manager()
        tenant_id = get_current_tenant_id()
        user_id = get_current_user_id()
        skill = mgr.get_skill(
            skill_name, scope=skill_scope, tenant_id=tenant_id, user_id=user_id,
        )
        if not skill:
            raise HTTPException(404, f"Skill '{skill_name}' 未找到")
        if not can_manage_knowledge_resource(
            skill.scope, role=get_current_role(), current_tenant_id=tenant_id,
            resource_tenant_id=skill.tenant_id, current_user_id=user_id,
            owner_user_id=skill.owner_user_id,
        ):
            raise HTTPException(403, "无权修改该 Skill")
        skill.enabled = enabled
        logger.info("Skill 启停完成", name=skill_name, scope=skill.scope, enabled=enabled)
        return {
            "status": "ok", "name": skill_name, "scope": skill.scope,
            "enabled": enabled,
        }
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(500, str(e))


@router.delete("/skills/{skill_name}")
# 方法作用：删除当前身份有权管理的非内置 Skill 目录和缓存。
# Args: skill_name - Skill 名称；skill_scope - 可选精确作用域。
# Returns: 删除状态、名称和作用域。
async def delete_skill(skill_name: str, skill_scope: str | None = None):
    """删除一个 Skill（移除磁盘目录）。内置 Skill 不可删除。"""
    import shutil as _shutil
    from src.api.auth import get_current_role, get_current_tenant_id, get_current_user_id
    from src.knowledge.governance import can_manage_knowledge_resource
    from src.skill_manager import get_skill_manager
    mgr = get_skill_manager()
    tenant_id = get_current_tenant_id()
    user_id = get_current_user_id()
    skill = mgr.get_skill(
        skill_name, scope=skill_scope, tenant_id=tenant_id, user_id=user_id,
    )
    if not skill:
        raise HTTPException(404, f"Skill '{skill_name}' 目录不存在")
    if mgr.is_builtin(
        skill_name, tenant_id=tenant_id, user_id=user_id, scope=skill.scope,
    ):
        raise HTTPException(403, f"内置 Skill '{skill_name}' 不可删除")
    if not can_manage_knowledge_resource(
        skill.scope, role=get_current_role(), current_tenant_id=tenant_id,
        resource_tenant_id=skill.tenant_id, current_user_id=user_id,
        owner_user_id=skill.owner_user_id,
    ):
        raise HTTPException(403, "无权删除该 Skill")
    skill_dir = skill.source_path
    real = os.path.realpath(os.path.normpath(skill_dir))
    # 安全检查：必须在任一 allowed 目录下
    managed_root = os.path.realpath(os.path.normpath(str(mgr.managed_dir)))
    ok = os.path.commonpath([real, managed_root]) == managed_root
    if not ok:
        raise HTTPException(403, "路径非法")
    _shutil.rmtree(skill_dir)
    # 直接从缓存移除，不扫描整个文件夹
    get_skill_manager().remove_skill(
        skill_name, scope=skill.scope, tenant_id=tenant_id, user_id=user_id,
    )
    logger.info("Skill 删除完成", name=skill_name, scope=skill.scope)
    return {
        "status": "ok", "name": skill_name, "scope": skill.scope,
        "deleted": True,
    }
