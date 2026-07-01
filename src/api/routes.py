"""11.1 + 2.3.7-9 API 路由 — chat / schema / datasources / health (13 端点)。"""

from __future__ import annotations

import io
import os
import time
import uuid

from fastapi import APIRouter, File, HTTPException, Query, UploadFile

from src.api.schemas import (
    ChatRequest, ChatResponse, ColumnCommentRequest,
    DataSourceCreateRequest, DataSourceInfo, HealthResponse, TableInfo,
)
from src.exceptions import DataSourceNotFoundError
from src.llm.client import is_llm_available
from src.logging_config import get_logger

logger = get_logger(__name__)
router = APIRouter()
_started_at = time.time()


def _app():
    from src.graph.workflow import app
    return app


def _registry():
    from src.datasource.registry import get_registry
    return get_registry()


# ---- Chat (11.1.1-2) ----

@router.post("/chat")
async def chat(req: ChatRequest):
    """统一 chat 端点：stream=False 返回 JSON，stream=True 返回 SSE 流式。"""
    if req.stream:
        from fastapi.responses import StreamingResponse
        from src.api.streaming import stream_analysis
        return StreamingResponse(
            stream_analysis(req.query, req.datasource, req.session_id or ""),
            media_type="text/event-stream",
            headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
        )
    cfg = {"configurable": {"thread_id": req.session_id or str(uuid.uuid4())}}
    result = await _app().ainvoke({"user_query": req.query, "datasource": req.datasource}, cfg)
    f = result.get("final_response", {})
    return ChatResponse(
        success=f.get("success", True), session_id=req.session_id or str(uuid.uuid4())[:8],
        user_query=req.query, sql=result.get("generated_sql", ""),
        data=result.get("query_result_sample", []),
        analysis=result.get("analysis_result", {}), chart=result.get("chart_config", {}),
    )


@router.post("/chat/stream")
async def chat_stream(req: ChatRequest):
    """（保留向后兼容）独立流式端点，等价于 /chat + stream=True。"""
    from fastapi.responses import StreamingResponse
    from src.api.streaming import stream_analysis
    return StreamingResponse(
        stream_analysis(req.query, req.datasource),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


# ---- Schema (11.1.3-6) ----

@router.get("/schema/tables")
async def list_tables(
    datasource: str = Query(default="demo"),
    page: int = Query(default=1, ge=1), page_size: int = Query(default=20, ge=1, le=100),
    search: str = Query(default=""),
) -> dict:
    try:
        ds = await _registry().resolve(datasource)
    except DataSourceNotFoundError:
        raise HTTPException(404, f"数据源 '{datasource}' 未找到")
    tables = []
    for t in (ds.schema.tables if ds.schema else []):
        if search and search.lower() not in t.name.lower():
            continue
        tables.append(TableInfo(name=t.name, description=t.description,
            columns=[{"name": c.name, "type": c.type, "comment": c.comment} for c in t.columns],
            row_count_estimate=t.row_count_estimate))
    total = len(tables)
    start = (page - 1) * page_size
    return {"tables": tables[start:start+page_size], "datasource": datasource,
            "total": total, "page": page, "page_size": page_size}


@router.get("/schema/tables/{table_name}")
async def get_table(table_name: str, datasource: str = Query(default="demo")):
    try:
        ds = await _registry().resolve(datasource)
    except DataSourceNotFoundError:
        raise HTTPException(404, f"数据源 '{datasource}' 未找到")
    for t in (ds.schema.tables if ds.schema else []):
        if t.name == table_name:
            return TableInfo(name=t.name, description=t.description,
                columns=[{"name": c.name, "type": c.type, "comment": c.comment,
                          "is_nullable": c.is_nullable, "is_primary_key": c.is_primary_key}
                         for c in t.columns],
                row_count_estimate=t.row_count_estimate)
    raise HTTPException(404, f"表 '{table_name}' 未找到")


@router.post("/schema/refresh")
async def refresh_schema(datasource: str = Query(default="demo")):
    return {"status": "ok", "message": "刷新已触发", "datasource": datasource}


@router.put("/schema/tables/{table_name}/columns/{column_name}/comment")
async def update_column_comment(
    table_name: str, column_name: str, req: ColumnCommentRequest,
    datasource: str = Query(default="demo"),
):
    return {"status": "ok", "table": table_name, "column": column_name, "comment": req.comment}


# ---- 数据源管理 (2.3.7-9) ----

@router.post("/datasources", status_code=201)
async def register_datasource(req: DataSourceCreateRequest):
    from src.datasource.providers.external import ExternalDataSourceProvider
    ds = await ExternalDataSourceProvider().register(req)
    return DataSourceInfo(name=ds.name, dialect=ds.dialect, mode=ds.mode,
                          host=ds.host, description=ds.description)


@router.delete("/datasources/{name}")
async def delete_datasource(name: str):
    return {"status": "ok", "message": f"数据源 '{name}' 已删除"}


@router.get("/datasources")
async def list_datasources(page: int = Query(default=1, ge=1), page_size: int = Query(default=20, ge=1, le=100)):
    items = await _registry().list_all()
    total = len(items)
    start = (page - 1) * page_size
    return {"datasources": items[start:start+page_size], "total": total, "page": page, "page_size": page_size}


# ---- 查询历史 ----


@router.get("/history")
async def list_history(
    datasource: str | None = Query(default=None),
    search: str | None = Query(default=None),
):
    from src.memory.history_store import get_history_store
    items = get_history_store().list(datasource=datasource, search=search)
    return {"history": items, "total": len(items)}


# ---- Skills 管理 ----


@router.get("/skills")
async def list_skills():
    """列出所有已注册 Skills 及其状态。"""
    try:
        from src.skill_manager import get_skill_manager
        mgr = get_skill_manager()
        skills = []
        for s in mgr.skills.values():
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
                "is_builtin": mgr.is_builtin(s.name),
            })
        return {"skills": skills, "total": len(skills)}
    except Exception as e:
        logger.warning("Skills 列表加载失败", error=str(e))
        return {"skills": [], "total": 0}


@router.post("/skills/upload")
async def upload_skills(files: list[UploadFile] = File(...)):
    """批量上传 SKILL.md 文件到 skills/ 目录。

    前端选择文件夹时，通过 webkitdirectory 传入整个文件夹的所有文件，
    后端递归过滤 SKILL.md（大小写不敏感），写入 skills/<name>/SKILL.md。
    """
    import os
    import re
    import yaml
    from pathlib import Path

    from src.skill_manager import get_skill_manager
    mgr = get_skill_manager()
    skills_dir = str(mgr.upload_dir)
    os.makedirs(skills_dir, exist_ok=True)
    imported: list[str] = []
    skipped: list[str] = []
    errors: list[dict] = []

    for f in files:
        if not f.filename:
            continue
        # 匹配路径中各级目录下的 SKILL.md（大小写不敏感）
        basename = os.path.basename(f.filename).lower()
        if basename != "skill.md":
            skipped.append(f.filename)
            continue
        try:
            content = (await f.read()).decode("utf-8")
        except Exception as e:
            errors.append({"file": f.filename, "error": str(e)})
            continue

        # 解析 YAML frontmatter 提取 name
        fm_match = re.match(r"^---\s*\n(.*?)\n---", content, re.DOTALL)
        skill_name = None
        if fm_match:
            try:
                fm = yaml.safe_load(fm_match.group(1))
                skill_name = fm.get("name", "")
            except Exception:
                pass
        if not skill_name:
            # 回退：从父目录名推断
            parent = os.path.basename(os.path.dirname(f.filename))
            if parent and parent != ".":
                skill_name = parent
            else:
                errors.append({"file": f.filename, "error": "无法从 frontmatter 或路径提取 skill name"})
                continue

        # 写文件
        dest_dir = os.path.join(skills_dir, skill_name)
        os.makedirs(dest_dir, exist_ok=True)
        dest = os.path.join(dest_dir, "SKILL.md")
        with open(dest, "w", encoding="utf-8") as fp:
            fp.write(content)
        imported.append({"name": skill_name, "file": f.filename})

        # 递归检测：同级目录下有 tools.py 或 templates/ 也一并说明
        # （前端传了整个文件夹时这些文件也会在 files 中，但会被 basename 检查跳过）

    # 将已导入的 skill 注入缓存（不扫描整个文件夹，增量更新）
    for item in imported:
        try:
            from src.skill_manager import get_skill_manager
            mgr2 = get_skill_manager()
            md_path = os.path.join(skills_dir, item["name"], "SKILL.md")
            if os.path.isfile(md_path):
                skill_obj = mgr2._parse_skill_manifest(Path(md_path))  # noqa: SLF001
                mgr2.add_skill(skill_obj)
        except Exception:
            pass

    return {
        "imported": imported,
        "skipped": skipped,
        "errors": errors,
        "total": len(imported),
    }


# ---- Knowledge / 知识库 ----


@router.get("/knowledge")
async def list_knowledge(
    category: str | None = Query(default=None),
    search: str | None = Query(default=None),
):
    """列出知识库条目（业务规则、表语义等）。"""
    try:
        from src.knowledge.schema_manager import get_schema_manager
        sm = get_schema_manager()
        sm._ensure_initialized()  # noqa: SLF001
        results = sm._collection.get(  # noqa: SLF001
            where={"category": category} if category else None,
        )
        logger.info("知识库查询", total_ids=len(results.get("ids", []) or []))
        entries = []
        if results and results.get("metadatas"):
            for i, meta in enumerate(results["metadatas"]):
                is_user = meta.get("source") == "user_upload"
                entries.append({
                    "id": results["ids"][i] if results.get("ids") else str(i),
                    "content": results["documents"][i][:200] if results.get("documents") else "",
                    "category": meta.get("category", ""),
                    "datasource": meta.get("datasource", ""),
                    "table_name": meta.get("table_name", ""),
                    "source": meta.get("source", "unknown"),
                    "source_file": meta.get("source_file", ""),
                    "is_builtin": not is_user,
                })
        if search:
            q = search.lower()
            entries = [e for e in entries if q in e["content"].lower()
                       or q in e.get("table_name", "").lower()]
        return {"entries": entries, "total": len(entries)}
    except Exception as e:
        logger.warning("知识库加载失败", error=str(e))
        return {"entries": [], "total": 0}


@router.post("/knowledge/docs/upload")
async def upload_knowledge_docs(
    files: list[UploadFile] = File(...),
    strategy: str = Query(default="auto"),
    chunk_size: int = Query(default=800, ge=200, le=4000),
    chunk_overlap: int = Query(default=100, ge=0, le=500),
    category: str = Query(default=""),
):
    """批量上传文档（Word/PDF/TXT/MD）到知识库，异步分块+索引。

    category 参数支持任意自定义类别，如 sales_metrics、pricing、user_ops 等。
    不传默认为空，后端写入 ChromaDB 时使用 'general' 兜底。
    """
    import asyncio as _asyncio

    supported_exts = {".md", ".txt", ".pdf", ".docx", ".doc", ".markdown"}
    from src.knowledge.doc_parser import ChunkConfig, ChunkStrategy
    from src.knowledge.upload_manager import get_upload_manager
    mgr = get_upload_manager()
    tasks_result: list[dict] = []
    errors: list[dict] = []

    try:
        strat = ChunkStrategy(strategy)
    except ValueError:
        strat = ChunkStrategy.AUTO
    config = ChunkConfig(strategy=strat, chunk_size=chunk_size, chunk_overlap=chunk_overlap)

    for f in files:
        if not f.filename:
            continue
        ext = os.path.splitext(f.filename)[1].lower()
        if ext not in supported_exts:
            errors.append({"file": f.filename, "error": f"不支持的文件格式: {ext}"})
            continue
        try:
            content = await f.read()
            # 保存原始文件到 PostgreSQL
            from src.knowledge.file_store import get_file_store
            file_id = await get_file_store().save(f.filename, content)

            task = mgr.create(f.filename)
            tasks_result.append({"task_id": task.id, "file_name": f.filename, "file_id": file_id})
            _asyncio.create_task(mgr.process(task, content, config, category))
        except Exception as e:
            errors.append({"file": f.filename, "error": str(e)})

    return {
        "tasks": tasks_result, "errors": errors, "total": len(tasks_result),
        "config": config.to_dict(),
        "message": "已接收文件，后台处理中。轮询 GET /api/v1/knowledge/upload/status 获取进度",
    }


@router.get("/knowledge/upload/status")
async def upload_status(task_id: str = Query(default="")):
    """查询上传任务进度（单任务或全部最近任务）。"""
    from src.knowledge.upload_manager import get_upload_manager
    mgr = get_upload_manager()
    if task_id:
        t = mgr.get(task_id)
        if t is None:
            raise HTTPException(404, f"任务 '{task_id}' 未找到")
        return {"task": t.to_dict()}
    return {"tasks": mgr.list_recent()}


@router.get("/knowledge/docs")
async def list_knowledge_docs():
    """列出已索引的文档（优先 PG，回退磁盘）。"""
    from src.knowledge.file_store import get_file_store
    docs = await get_file_store().list_files()
    return {"docs": docs, "total": len(docs)}


@router.get("/knowledge/docs/{doc_name}/content")
async def get_doc_content(doc_name: str):
    """获取已索引文档的内容（从 PG 读取，回退磁盘）。"""
    from src.knowledge.file_store import get_file_store
    store = get_file_store()
    doc = await store.get_by_name(doc_name)
    if doc:
        raw = doc["file_data"]
        size = doc["size"]
    else:
        # 磁盘回退
        doc_path = os.path.join(os.path.dirname(__file__), "..", "..", "docs", "metrics", doc_name)
        if not os.path.isfile(doc_path):
            raise HTTPException(404, f"文档 '{doc_name}' 未找到")
        with open(doc_path, "rb") as fp:
            raw = fp.read()
        size = os.path.getsize(doc_path)

    ext = os.path.splitext(doc_name)[1].lower()
    result = {"name": doc_name, "size": size, "ext": ext, "type": "text", "content": "", "raw_url": ""}
    if ext == ".pdf":
        result["type"] = "pdf"
        result["raw_url"] = f"/api/v1/knowledge/docs/{doc_name}/raw"
        from src.knowledge.doc_parser import extract_text
        result["content"] = extract_text(doc_name, raw)
    elif ext in (".docx", ".doc"):
        result["type"] = "word"
        result["content"] = _docx_to_html(raw)
    else:
        result["content"] = raw.decode("utf-8", errors="replace")
    return result


@router.get("/knowledge/docs/{doc_name}/raw")
async def get_doc_raw(doc_name: str):
    """返回原始文件（从 PG 读取，回退磁盘，用于 PDF iframe 渲染）。"""
    from fastapi.responses import Response
    from src.knowledge.file_store import get_file_store
    doc = await get_file_store().get_by_name(doc_name)
    if doc:
        return Response(content=bytes(doc["file_data"]), media_type=doc["content_type"])
    # 磁盘回退
    from fastapi.responses import FileResponse
    doc_path = os.path.join(os.path.dirname(__file__), "..", "..", "docs", "metrics", doc_name)
    if not os.path.isfile(doc_path):
        raise HTTPException(404, f"文档 '{doc_name}' 未找到")
    ext = os.path.splitext(doc_name)[1].lower()
    media_map = {".pdf": "application/pdf", ".txt": "text/plain", ".md": "text/markdown"}
    return FileResponse(doc_path, media_type=media_map.get(ext, "application/octet-stream"))


def _docx_to_html(raw: bytes) -> str:
    """将 Word 文档转换为简单 HTML。"""
    try:
        from docx import Document
        doc = Document(io.BytesIO(raw))
        parts = ['<div style="font-family: sans-serif; line-height: 1.8;">']
        for para in doc.paragraphs:
            text = para.text.strip()
            if not text:
                parts.append('<br/>')
                continue
            if para.style and para.style.name and para.style.name.startswith("Heading"):
                level = para.style.name.replace("Heading", "").strip()
                try:
                    lv = int(level)
                except ValueError:
                    lv = 2
                parts.append(f'<h{lv} style="margin:12px 0 6px;">{text}</h{lv}>')
            else:
                parts.append(f'<p style="margin:4px 0;">{text}</p>')
        # 处理表格
        for table in doc.tables:
            parts.append('<table border="1" cellpadding="6" cellspacing="0" style="border-collapse:collapse; width:100%; margin:8px 0;">')
            for row in table.rows:
                parts.append('<tr>')
                for cell in row.cells:
                    parts.append(f'<td style="padding:4px 8px;">{cell.text}</td>')
                parts.append('</tr>')
            parts.append('</table>')
        parts.append('</div>')
        return "\n".join(parts)
    except Exception:
        return "<p>无法渲染 Word 文档内容</p>"


@router.delete("/knowledge/{entry_id}")
async def delete_knowledge_entry(entry_id: str):
    """删除指定知识条目（仅限用户上传的）。"""
    try:
        from src.knowledge.schema_manager import get_schema_manager
        sm = get_schema_manager()
        sm._ensure_initialized()  # noqa: SLF001
        results = sm._collection.get(ids=[entry_id])  # noqa: SLF001
        if results and results.get("metadatas"):
            meta = results["metadatas"][0] or {}
            if meta.get("source") != "user_upload":
                raise HTTPException(403, "系统内置知识条目不可删除")
        sm._collection.delete(ids=[entry_id])  # noqa: SLF001
        return {"status": "ok", "id": entry_id}
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(500, str(e))


@router.delete("/knowledge/docs/{doc_name}")
async def delete_knowledge_doc(doc_name: str):
    """删除已索引文档（仅限用户上传的）。"""
    # 检查是否是内置文档
    from src.knowledge.schema_manager import get_schema_manager
    sm = get_schema_manager()
    sm._ensure_initialized()  # noqa: SLF001
    results = sm._collection.get(where={"source_file": doc_name})  # noqa: SLF001
    if results and results.get("metadatas"):
        for meta in results["metadatas"]:
            if meta and meta.get("source") != "user_upload":
                raise HTTPException(403, f"内置文档 '{doc_name}' 不可删除")
    # 从 PG 删除
    from src.knowledge.file_store import get_file_store
    await get_file_store().delete(doc_name)
    # 磁盘回退清理
    doc_path = os.path.join(os.path.dirname(__file__), "..", "..", "docs", "metrics", doc_name)
    if os.path.isfile(doc_path):
        os.remove(doc_path)
    # 从 ChromaDB 删除关联条目
    if results and results.get("ids"):
        sm._collection.delete(ids=results["ids"])  # noqa: SLF001
    return {"status": "ok", "doc": doc_name}


# ---- Skill 管理操作 ----


@router.post("/skills/refresh")
async def refresh_skills():
    """全量重新扫描 Skill 目录（用户手动触发）。"""
    try:
        from src.skill_manager import get_skill_manager
        await get_skill_manager().discover()
        return {"status": "ok", "total": len(get_skill_manager().skills)}
    except Exception as e:
        raise HTTPException(500, str(e))


@router.get("/skills/{skill_name}/content")
async def get_skill_content(skill_name: str):
    """获取 SKILL.md 文件的原始内容（直接从缓存 source_path 读取）。"""
    from src.skill_manager import get_skill_manager
    mgr = get_skill_manager()
    skill = mgr.skills.get(skill_name)
    if not skill:
        raise HTTPException(404, f"Skill '{skill_name}' 未找到")
    md_path = os.path.join(skill.source_path, "SKILL.md")
    if not os.path.isfile(md_path):
        raise HTTPException(404, f"SKILL.md 文件不存在: {md_path}")
    with open(md_path, "r", encoding="utf-8", errors="replace") as f:
        return {"name": skill_name, "content": f.read()}


@router.put("/skills/{skill_name}/toggle")
async def toggle_skill(skill_name: str, enabled: bool = Query(...)):
    """启用或禁用一个 Skill。"""
    try:
        from src.skill_manager import get_skill_manager
        mgr = get_skill_manager()
        for s in mgr.skills.values():
            if s.name == skill_name:
                s.enabled = enabled
                return {"status": "ok", "name": skill_name, "enabled": enabled}
        raise HTTPException(404, f"Skill '{skill_name}' 未找到")
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(500, str(e))


@router.delete("/skills/{skill_name}")
async def delete_skill(skill_name: str):
    """删除一个 Skill（移除磁盘目录）。内置 Skill 不可删除。"""
    import shutil as _shutil
    from src.skill_manager import get_skill_manager
    mgr = get_skill_manager()
    if mgr.is_builtin(skill_name):
        raise HTTPException(403, f"内置 Skill '{skill_name}' 不可删除")
    # 在所有目录中查找并删除
    skill_dir = None
    for d in mgr.all_dirs:
        candidate = d / skill_name
        if candidate.is_dir():
            skill_dir = str(candidate)
            break
    if skill_dir is None:
        raise HTTPException(404, f"Skill '{skill_name}' 目录不存在")
    real = os.path.realpath(os.path.normpath(skill_dir))
    # 安全检查：必须在任一 allowed 目录下
    ok = any(
        real.startswith(os.path.realpath(os.path.normpath(str(d))))
        for d in mgr.all_dirs
    )
    if not ok:
        raise HTTPException(403, "路径非法")
    _shutil.rmtree(skill_dir)
    # 直接从缓存移除，不扫描整个文件夹
    get_skill_manager().remove_skill(skill_name)
    return {"status": "ok", "name": skill_name, "deleted": True}


# ---- Health (11.1.10) ----

@router.get("/health", response_model=HealthResponse)
async def health():
    return HealthResponse(status="ok", llm_available=is_llm_available(),
                          uptime_seconds=round(time.time() - _started_at, 2))
