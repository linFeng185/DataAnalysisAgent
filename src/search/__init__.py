"""受控联网搜索包：对外导出模型与核心服务入口（spec/58）。"""

from src.search.models import SearchHit, SearchResponse, is_explicit_web_search
from src.search.service import search_public_web

__all__ = ["SearchHit", "SearchResponse", "is_explicit_web_search", "search_public_web"]
