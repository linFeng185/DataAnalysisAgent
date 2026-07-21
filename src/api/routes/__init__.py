"""按领域组合 FastAPI API 路由并保留原模块导出。"""

from __future__ import annotations

from fastapi import APIRouter

from src.api.schemas import ColumnCommentRequest, DataSourceCreateRequest
from src.api.routes._helpers import _app, _authorize_extension_scope, _registry
from src.api.routes.chat import (
    _enforce_chat_request_quota,
    _requested_chat_datasources,
    _resolve_chat_access,
    chat,
    chat_stream,
    router as chat_router,
)
from src.api.routes.datasource import (
    delete_datasource,
    list_datasources,
    register_datasource,
    router as datasource_router,
)
from src.api.routes.knowledge import (
    _docx_to_html,
    _knowledge_where,
    create_global_knowledge_tag,
    create_knowledge_tag,
    delete_knowledge_doc,
    delete_knowledge_entry,
    get_doc_content,
    get_doc_raw,
    list_knowledge,
    list_knowledge_docs,
    promote_knowledge_tag,
    router as knowledge_router,
    search_knowledge_tags,
    test_knowledge_search,
    update_knowledge_tag_status,
    upload_knowledge_docs,
    upload_status,
)
from src.api.routes.management import (
    forecast_asset,
    health,
    list_models,
    profile_structured_asset,
    query_structured_asset,
    router as management_router,
    test_model,
)
from src.api.routes.mcp import (
    _connect_scoped_mcp_db,
    _mcp_owner_fields,
    _validate_managed_mcp_request,
    create_mcp_server,
    delete_mcp_server,
    list_mcp_servers,
    router as mcp_router,
    test_mcp_server,
)
from src.api.routes.schema import (
    _schema_manager,
    get_table,
    list_tables,
    refresh_schema,
    router as schema_router,
    update_column_comment,
)
from src.api.routes.session import (
    _load_checkpoint_tuple,
    _load_latest_state,
    _load_session_turns,
    _merge_rich_result,
    delete_session,
    get_session,
    list_history,
    list_session_turns,
    list_sessions,
    router as session_router,
)
from src.api.routes.skills import (
    delete_skill,
    get_skill_content,
    list_skills,
    refresh_skills,
    router as skills_router,
    toggle_skill,
    upload_skills,
)


router = APIRouter()
for domain_router in (
    chat_router,
    schema_router,
    datasource_router,
    mcp_router,
    session_router,
    management_router,
    skills_router,
    knowledge_router,
):
    router.include_router(domain_router)


__all__ = [
    "router",
    "chat",
    "chat_stream",
    "list_tables",
    "get_table",
    "refresh_schema",
    "update_column_comment",
    "register_datasource",
    "delete_datasource",
    "list_datasources",
    "list_mcp_servers",
    "create_mcp_server",
    "delete_mcp_server",
    "test_mcp_server",
    "list_history",
    "list_sessions",
    "get_session",
    "list_session_turns",
    "delete_session",
    "list_models",
    "test_model",
    "list_skills",
    "upload_skills",
    "refresh_skills",
    "get_skill_content",
    "toggle_skill",
    "delete_skill",
    "profile_structured_asset",
    "query_structured_asset",
    "forecast_asset",
    "list_knowledge",
    "upload_knowledge_docs",
    "upload_status",
    "search_knowledge_tags",
    "create_knowledge_tag",
    "create_global_knowledge_tag",
    "promote_knowledge_tag",
    "update_knowledge_tag_status",
    "test_knowledge_search",
    "list_knowledge_docs",
    "get_doc_content",
    "get_doc_raw",
    "delete_knowledge_entry",
    "delete_knowledge_doc",
    "health",
]
