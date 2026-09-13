# 知识库治理、上传交互与长期记忆管理 实施计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 落实 `spec/44-knowledge-governance-and-memory-ui.md`：修正知识库管理页的可见范围与删除权限、按来源分层展示、补齐创建人、消除列表查询的循环热点并加缓存、改造上传表单交互，并新增「我的长期记忆」页面。

**Architecture:** 后端在 `src/api/routes/knowledge.py` 重构列表装配流程（管理视角过滤器 + 来源分层 + 创建人批量映射 + 判定缓存），新增 `src/knowledge/management_cache.py` 承载 TTL 缓存、新增 `src/api/routes/memory.py` 暴露长期记忆读删端点；前端改造 `KnowledgePage.tsx` 的上传 Modal 与列表筛选、新增 `MemoryPage.tsx`。

**Tech Stack:** Python 3.14 / FastAPI / asyncpg / Milvus v2.4.23 / structlog / React 18 / antd 5 / Vite / pytest

## Global Constraints

- **禁止 `git commit` 与 `git push`**：本计划的所有改动只保留在工作区，由用户决定何时提交。计划中的「验证」步骤以运行测试与静态检查代替提交。
- **禁止新增依赖**：`requirements.txt` 与 `frontend/package.json` 不得新增任何库。
- **方法注释强制**：每个方法/函数上方必须有中文注释，含「作用 / Args / Returns」三要素。
- **方法日志强制**：入口 `logger.debug`（方法名 + 关键参数），出口 `logger.info`（结果摘要），异常 `logger.error(..., exc_info=True)`；**循环内只允许 `debug`，禁止 `info`**。
- **测试路径与源码一一对应**：`src/api/routes/knowledge.py` → `tests/test_knowledge/`；`src/memory/long_term_store.py` → `tests/test_memory/`。
- **测试不得调用真实 LLM**；涉及数据库的测试用既有 fixture 与 mock 模式，生产库只读验证。
- **前端无 JS 测试框架**：前端测试沿用仓库既有的「源码静态断言」模式（`tests/test_frontend/test_report_templates.py` 为范例），读取 `.tsx` 源码做正则与结构断言。
- **可见性变更的边界**：Agent 检索链路（`src/knowledge/retrieval.py` 的 `build_accessible_knowledge_filters`，被 `search_knowledge` 调用）**不得改动**。
- **管理视角过滤器**：新增 `build_admin_visible_filters`，仅管理 API 使用。

---

### Task 1: 管理视角可见范围与来源分层（`list_knowledge` 重构）

**Files:**
- Modify: `src/knowledge/retrieval.py`（新增 `build_admin_visible_filters`，追加在 `build_accessible_knowledge_filters` 之后，约 115 行处）
- Modify: `src/api/routes/knowledge.py:104-209`（`list_knowledge` 全量重构）
- Test: `tests/test_knowledge/test_admin_visibility.py`
- Test: `tests/test_knowledge/test_entry_kind.py`

**Interfaces:**
- Consumes: `src/knowledge/governance.can_manage_knowledge_resource(scope, *, role, current_tenant_id, resource_tenant_id, current_user_id, owner_user_id) -> bool`
- Consumes: `src/api/auth` 的 `get_current_role() / get_current_tenant_id() / get_current_user_id() / is_platform_super_admin()`
- Produces: `build_admin_visible_filters(category: str = "", *, include_system: bool = False) -> list[dict[str, Any]]`
- Produces: `GET /api/v1/knowledge` 新增查询参数 `entry_kind`（`document` | `introspect` | `all`，默认 `document`）与 `include_system`（`bool`，默认 `False`）

- [ ] **Step 1: 写失败测试（可见范围）**

创建 `tests/test_knowledge/test_admin_visibility.py`：

```python
"""知识库管理页可见范围测试，覆盖 spec/44 §2.1。

管理视角与检索链路的过滤口径不同：检索链路中 super_admin 只召回 system，
管理页则需包含 system + 本租户 + 本人。
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from src.api import auth  # noqa: E402
from src.knowledge.retrieval import build_admin_visible_filters  # noqa: E402


class TestAdminVisibleFilters:
    """验证 spec/44 §2.1：四种角色在管理列表中的可见范围。"""

    @pytest.fixture(autouse=True)
    def _identity(self):
        """统一注入租户身份，用例结束后复位。"""
        tokens = [
            auth._current_role.set("tenant_admin"),
            auth._current_tenant_id.set(3),
            auth._current_user_id.set(3),
        ]
        yield
        auth._current_role.reset(tokens[0])
        auth._current_tenant_id.reset(tokens[1])
        auth._current_user_id.reset(tokens[2])

    # 方法作用：验证租户管理员的默认可见范围不含 system。
    # Args: self - 测试实例。
    # Returns: 无返回值，断言失败时由 pytest 报告。
    def test_tenant_admin_defaults_to_tenant_and_private(self) -> None:
        """默认展示不含 system，避免平台知识污染租户视图。"""
        # Act
        groups = build_admin_visible_filters()

        # Assert
        visibilities = {group.get("visibility") for group in groups}
        assert visibilities == {"tenant", "private"}

    # 方法作用：验证 include_system 让非超管放宽到只读可见 system。
    # Args: self - 测试实例。
    # Returns: 无返回值，断言失败时由 pytest 报告。
    def test_include_system_adds_system_group(self) -> None:
        """勾选「显示系统知识」后应追加 system 组。"""
        # Act
        groups = build_admin_visible_filters(include_system=True)

        # Assert
        visibilities = {group.get("visibility") for group in groups}
        assert visibilities == {"system", "tenant", "private"}

    # 方法作用：验证 private 组恒定绑定当前用户。
    # Args: self - 测试实例。
    # Returns: 无返回值，断言失败时由 pytest 报告。
    def test_private_group_bound_to_current_user(self) -> None:
        """private 组必须带 owner_user_id，否则会串号。"""
        # Act
        groups = build_admin_visible_filters(include_system=True)

        # Assert
        private_group = next(g for g in groups if g["visibility"] == "private")
        assert private_group["owner_user_id"] == 3

    # 方法作用：验证 category 下推到所有分组。
    # Args: self - 测试实例。
    # Returns: 无返回值，断言失败时由 pytest 报告。
    def test_category_pushed_into_all_groups(self) -> None:
        """分类条件必须下推，否则各范围过滤会不一致。"""
        # Act
        groups = build_admin_visible_filters(category="business_rule", include_system=True)

        # Assert
        assert all(group.get("category") == "business_rule" for group in groups)
```

- [ ] **Step 2: 运行测试确认失败**

Run: `.venv/Scripts/python.exe -m pytest tests/test_knowledge/test_admin_visibility.py -v`
Expected: FAIL — `ImportError: cannot import name 'build_admin_visible_filters'`

- [ ] **Step 3: 实现 `build_admin_visible_filters`**

在 `src/knowledge/retrieval.py` 的 `build_accessible_knowledge_filters` 之后追加：

```python
# 方法作用：构造知识库管理页的可见范围过滤，超管与显式勾选时包含 system。
# Args: category - 可选分类下推条件；include_system - 非超管是否放宽只读查看系统知识。
# Returns: 管理页可用的 metadata 过滤条件组列表。
def build_admin_visible_filters(
    category: str = "",
    *,
    include_system: bool = False,
) -> list[dict[str, Any]]:
    """管理页可见范围：system（条件性）+ 本租户 tenant + 本人 private。"""
    logger.debug("构造管理页可见过滤入口", category=category, include_system=include_system)
    from src.api.auth import (
        get_current_tenant_id,
        get_current_user_id,
        is_platform_super_admin,
    )

    policy = get_tenant_policy()
    current_tenant_id = get_current_tenant_id()
    current_user_id = get_current_user_id()
    shared: dict[str, Any] = {}
    if category:
        shared["category"] = category

    filters: list[dict[str, Any]] = []
    # 超管默认即含 system；非超管仅在显式勾选时放宽为只读可见。
    if is_platform_super_admin() or include_system:
        filters.append({"visibility": "system", **shared})

    tenant_filter: dict[str, Any] = {"visibility": "tenant", **shared}
    private_filter: dict[str, Any] = {"visibility": "private", **shared}
    if policy.tenant_filter(current_tenant_id, explicit=False):
        tenant_filter["tenant_id"] = current_tenant_id
        private_filter["tenant_id"] = current_tenant_id
    private_filter["owner_user_id"] = current_user_id
    filters.extend([tenant_filter, private_filter])
    logger.debug(
        "构造管理页可见过滤完成",
        scope_count=len(filters),
        tenant_id=current_tenant_id,
        user_id=current_user_id,
    )
    return filters
```

- [ ] **Step 4: 运行测试确认通过**

Run: `.venv/Scripts/python.exe -m pytest tests/test_knowledge/test_admin_visibility.py -v`
Expected: 4 passed

- [ ] **Step 5: 写失败测试（来源分层）**

创建 `tests/test_knowledge/test_entry_kind.py`：

```python
"""知识条目来源分层过滤测试，覆盖 spec/44 §2.2。"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from src.api.routes.knowledge import _entry_kind_filters  # noqa: E402


class TestEntryKindFilters:
    """验证 document / introspect / all 三档来源过滤与长期记忆排除。"""

    # 方法作用：验证默认档排除内省条目与长期记忆条目。
    # Args: self - 测试实例。
    # Returns: 无返回值，断言失败时由 pytest 报告。
    def test_document_kind_excludes_introspect_and_memory(self) -> None:
        """文档视图只保留有来源的知识。"""
        # Act
        filters = _entry_kind_filters("document")

        # Assert
        assert filters["not:source"] == "auto_introspect"
        assert filters["not:resource_kind"] == "long_term_memory"

    # 方法作用：验证内省档只取自动内省条目。
    # Args: self - 测试实例。
    # Returns: 无返回值，断言失败时由 pytest 报告。
    def test_introspect_kind_selects_only_introspect(self) -> None:
        """内省视图是正向过滤，不使用取反。"""
        # Act
        filters = _entry_kind_filters("introspect")

        # Assert
        assert filters["source"] == "auto_introspect"

    # 方法作用：验证 all 档仍排除长期记忆条目。
    # Args: self - 测试实例。
    # Returns: 无返回值，断言失败时由 pytest 报告。
    def test_all_kind_still_excludes_long_term_memory(self) -> None:
        """长期记忆属于记忆模块，任何档位都不得混入知识库。"""
        # Act
        filters = _entry_kind_filters("all")

        # Assert
        assert filters == {"not:resource_kind": "long_term_memory"}

    # 方法作用：验证非法取值被拒绝而不是静默放行。
    # Args: self - 测试实例。
    # Returns: 无返回值，断言失败时由 pytest 报告。
    def test_invalid_kind_rejected(self) -> None:
        """未知档位必须报错，避免退化成全量泄露。"""
        # Act / Assert
        with pytest.raises(ValueError):
            _entry_kind_filters("unknown")
```

- [ ] **Step 6: 运行测试确认失败**

Run: `.venv/Scripts/python.exe -m pytest tests/test_knowledge/test_entry_kind.py -v`
Expected: FAIL — `ImportError: cannot import name '_entry_kind_filters'`

- [ ] **Step 7: 实现 `_entry_kind_filters` 并重构 `list_knowledge`**

在 `src/api/routes/knowledge.py` 的 `_knowledge_where` 之后新增模块级函数：

```python
# 方法作用：把来源档位编译为 metadata 过滤条件，长期记忆条目在所有档位下排除。
# Args: entry_kind - document / introspect / all。
# Returns: 可直接并入 metadata 过滤字典的条件。
def _entry_kind_filters(entry_kind: str) -> dict[str, str]:
    """来源分层：document 排除内省，introspect 只取内省，all 保留其余。"""
    logger.debug("知识条目来源档位解析入口", entry_kind=entry_kind)
    if entry_kind == "document":
        result = {"not:source": "auto_introspect", "not:resource_kind": "long_term_memory"}
    elif entry_kind == "introspect":
        result = {"source": "auto_introspect"}
    elif entry_kind == "all":
        result = {"not:resource_kind": "long_term_memory"}
    else:
        logger.warning("知识条目来源档位无效", entry_kind=entry_kind)
        raise ValueError(f"不支持的来源档位: {entry_kind}")
    logger.debug("知识条目来源档位解析完成", entry_kind=entry_kind, condition_count=len(result))
    return result
```

重构 `list_knowledge` 签名与过滤装配：

```python
@router.get("/knowledge")
# 方法作用：按当前身份、范围、来源档位和分类分页列出知识条目。
# Args: category - 分类；search - 搜索词；knowledge_scope - 范围；
#       entry_kind - 来源档位；include_system - 是否只读查看系统知识；
#       page/page_size - 分页参数。
# Returns: 知识条目分页结果；存储失败抛出 HTTP 500。
async def list_knowledge(
    category: str | None = Query(default=None),
    search: str | None = Query(default=None),
    knowledge_scope: str | None = None,
    entry_kind: str = Query(default="document"),
    include_system: bool = Query(default=False),
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=20, ge=10, le=100),
):
```

过滤装配段改为（替换原 `build_accessible_knowledge_filters` 段落）：

```python
        from src.knowledge.retrieval import build_admin_visible_filters

        store = await get_vector_store()
        filter_groups = build_admin_visible_filters(
            category=category or "",
            include_system=include_system,
        )
        kind_filters = _entry_kind_filters(entry_kind)
        filter_groups = [{**group, **kind_filters} for group in filter_groups]
        if knowledge_scope:
            normalized_scope = normalize_knowledge_scope(knowledge_scope).value
            filter_groups = [
                group for group in filter_groups
                if group.get("visibility") == normalized_scope
            ]
```

`except ValueError` 分支保持不变即可覆盖非法 `entry_kind` 与非法 `knowledge_scope`。

- [ ] **Step 8: 运行两组测试确认通过**

Run: `.venv/Scripts/python.exe -m pytest tests/test_knowledge/test_entry_kind.py tests/test_knowledge/test_admin_visibility.py -v`
Expected: 8 passed

- [ ] **Step 9: 回归既有知识库测试**

Run: `.venv/Scripts/python.exe -m pytest tests/test_knowledge/ -q`
Expected: 全绿；若 `test_file_store.py` 等因可见性口径变化失败，按新口径修正断言并在测试 docstring 注明依赖 `spec/44 §2.1`。

---

### Task 2: 创建人用户名与文档接口放宽

**Files:**
- Modify: `src/api/routes/knowledge.py`（`list_knowledge` 装配段、`list_knowledge_docs:654-682`、`get_doc_content:685-730`、`get_doc_raw:733-761`）
- Test: `tests/test_knowledge/test_owner_username.py`
- Test: `tests/test_knowledge/test_doc_include_system.py`

**Interfaces:**
- Consumes: `src/memory/pg_pool.pg_connection(*, tenant_id, user_id, role)` 异步上下文管理器
- Produces: `_load_usernames(user_ids: list[int]) -> dict[int, str]`
- Produces: `_normalize_owner_username(owner_user_id: object, usernames: dict[int, str]) -> str`
- Produces: 列表与文档响应新增 `owner_username: str`
- Produces: `GET /api/v1/knowledge/docs` 新增 `include_system: bool = False`；`get_doc_content` / `get_doc_raw` 的 `knowledge_scope=system` 分支在 `include_system=True` 时对非超管放行读取

- [ ] **Step 1: 写失败测试（创建人映射）**

创建 `tests/test_knowledge/test_owner_username.py`：

```python
"""知识条目与文档创建人用户名测试，覆盖 spec/44 §2.3。"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from src.api.routes.knowledge import _normalize_owner_username  # noqa: E402


class TestOwnerUsername:
    """验证创建人映射的边界行为。"""

    # 方法作用：验证无主条目映射为空串而不是报错。
    # Args: self - 测试实例。
    # Returns: 无返回值，断言失败时由 pytest 报告。
    def test_zero_owner_maps_to_empty(self) -> None:
        """owner_user_id=0 表示系统写入，前端显示「系统」。"""
        # Act / Assert
        assert _normalize_owner_username(0, {}) == ""

    # 方法作用：验证映射命中时返回用户名。
    # Args: self - 测试实例。
    # Returns: 无返回值，断言失败时由 pytest 报告。
    def test_matched_owner_returns_username(self) -> None:
        """命中 users 表时返回真实用户名。"""
        # Act / Assert
        assert _normalize_owner_username(3, {3: "test2"}) == "test2"

    # 方法作用：验证用户被删除后不抛异常。
    # Args: self - 测试实例。
    # Returns: 无返回值，断言失败时由 pytest 报告。
    def test_missing_user_returns_empty(self) -> None:
        """用户已注销时保持列表可用。"""
        # Act / Assert
        assert _normalize_owner_username(99, {3: "test2"}) == ""

    # 方法作用：验证空值 owner 不抛异常。
    # Args: self - 测试实例。
    # Returns: 无返回值，断言失败时由 pytest 报告。
    def test_none_owner_returns_empty(self) -> None:
        """metadata 缺少 owner_user_id 时必须安全降级。"""
        # Act / Assert
        assert _normalize_owner_username(None, {}) == ""
```

- [ ] **Step 2: 运行测试确认失败**

Run: `.venv/Scripts/python.exe -m pytest tests/test_knowledge/test_owner_username.py -v`
Expected: FAIL — `ImportError: cannot import name '_normalize_owner_username'`

- [ ] **Step 3: 实现创建人映射**

在 `src/api/routes/knowledge.py` 模块级新增：

```python
# 方法作用：把 owner_user_id 映射为用户名，缺失或无主时返回空串。
# Args: owner_user_id - 条目 metadata 中的所有者 ID；usernames - 批量查询得到的 ID 到用户名映射。
# Returns: 用户名；无主或用户不存在时返回空字符串。
def _normalize_owner_username(owner_user_id: object, usernames: dict[int, str]) -> str:
    """创建人展示名，避免逐条查询用户表。"""
    logger.debug("创建人映射入口", owner_user_id=owner_user_id)
    try:
        if owner_user_id in (None, "", 0):
            logger.debug("创建人映射完成", owner_username="", reason="无主")
            return ""
        result = usernames.get(int(owner_user_id), "")
        logger.debug("创建人映射完成", owner_user_id=owner_user_id, matched=bool(result))
        return result
    except (TypeError, ValueError) as exc:
        logger.warning("创建人映射失败", owner_user_id=owner_user_id, error=str(exc))
        return ""


# 方法作用：批量加载当前页涉及的创建人用户名，避免逐条查询。
# Args: user_ids - 当前页出现过的 owner_user_id 集合。
# Returns: ID 到用户名的映射；数据库不可用时返回空映射。
async def _load_usernames(user_ids: list[int]) -> dict[int, str]:
    """一次 SQL 取回当前页创建人，禁止在循环内查询。"""
    logger.debug("批量加载创建人入口", user_count=len(user_ids))
    from src.api.auth import get_current_role, get_current_tenant_id, get_current_user_id
    from src.knowledge.governance import normalize_role
    from src.memory.pg_pool import pg_connection

    targets = sorted({int(uid) for uid in user_ids if uid not in (None, "", 0)})
    if not targets:
        logger.info("批量加载创建人完成", source="skipped", count=0)
        return {}
    try:
        async with pg_connection(
            tenant_id=get_current_tenant_id(),
            user_id=get_current_user_id(),
            role=normalize_role(get_current_role()),
        ) as conn:
            if conn is None:
                logger.info("批量加载创建人回退", reason="PostgreSQL 未配置")
                return {}
            rows = await conn.fetch(
                "SELECT id, username FROM users WHERE id = ANY($1::int[])",
                targets,
            )
        result = {int(row["id"]): str(row["username"]) for row in rows}
        logger.info("批量加载创建人完成", source="postgres", count=len(result))
        return result
    except Exception as exc:
        logger.error("批量加载创建人失败", error=str(exc), exc_info=True)
        return {}
```

在 `list_knowledge` 装配段接线：进入结果循环前收集 `owner_ids`，循环内不再查询，分页后补偿：

```python
        # 先构建不含 can_delete 与创建人的条目，避免逐条授权与查库。
        entries = []
        for result in results:
            meta = dict(result.metadata or {})
            visibility = str(meta.get("visibility") or "private")
            resource_tenant_id = meta.get("tenant_id")
            if resource_tenant_id is None and visibility != "system":
                resource_tenant_id = get_current_tenant_id()
            entries.append({
                "id": result.id,
                "content": result.content,
                "category": meta.get("category", ""),
                "datasource": meta.get("datasource", ""),
                "table_name": meta.get("table_name", ""),
                "source": meta.get("source", "unknown"),
                "source_file": meta.get("source_file", ""),
                "scope": visibility,
                "tag_ids": json.loads(meta.get("tag_ids_json", "[]") or "[]"),
                "tags": _normalize_knowledge_tags(meta.get("tags", "")),
                "is_builtin": visibility == "system" or meta.get("source") != "user_upload",
                "tenant_id": resource_tenant_id,
                "owner_user_id": meta.get("owner_user_id"),
            })
        if search:
            q = search.lower()
            entries = [e for e in entries if q in e["content"].lower()
                       or q in e.get("table_name", "").lower()]
        total = len(entries)
        start = (page - 1) * page_size
        paged = [dict(entry) for entry in entries[start:start + page_size]]
        # 授权判定与创建人映射只作用于当前页。
        await _decorate_entries(paged)
        result = {"entries": paged, "total": total, "page": page, "page_size": page_size}
```

并新增装饰函数：

```python
# 方法作用：为当前页条目补齐删除权限与创建人用户名。
# Args: entries - 当前页条目列表，原地补充 can_delete 与 owner_username 字段。
# Returns: 无返回值。
async def _decorate_entries(entries: list[dict]) -> None:
    """只对当前页执行授权判定与创建人映射，避免全量逐条开销。"""
    logger.debug("条目权限与创建人装饰入口", entry_count=len(entries))
    if not entries:
        logger.info("条目权限与创建人装饰完成", entry_count=0)
        return
    from src.api.auth import get_current_role, get_current_tenant_id, get_current_user_id

    role = get_current_role()
    current_tenant_id = get_current_tenant_id()
    current_user_id = get_current_user_id()
    _judge_delete_permission(
        entries,
        role=role,
        current_tenant_id=current_tenant_id,
        current_user_id=current_user_id,
    )
    usernames = await _load_usernames([entry.get("owner_user_id") for entry in entries])
    for entry in entries:
        entry["owner_username"] = _normalize_owner_username(
            entry.get("owner_user_id"), usernames,
        )
    logger.info("条目权限与创建人装饰完成", entry_count=len(entries), owner_count=len(usernames))
```

注：`_judge_delete_permission` 在 Task 3 中实现；若按顺序执行 Task 2，可先内联逐条判定，Task 3 再替换为组合缓存版本。

- [ ] **Step 4: 运行测试确认通过**

Run: `.venv/Scripts/python.exe -m pytest tests/test_knowledge/test_owner_username.py -v`
Expected: 4 passed

- [ ] **Step 5: 写失败测试（文档接口放宽）**

创建 `tests/test_knowledge/test_doc_include_system.py`，采用源码静态断言（路由函数依赖完整应用上下文，单元级只需锁定契约）：

```python
"""知识库文档接口的系统知识放宽测试，覆盖 spec/44 §2.1。"""

from __future__ import annotations

import re
from pathlib import Path

ROUTE_SOURCE = Path("src/api/routes/knowledge.py").read_text(encoding="utf-8")


class TestDocIncludeSystem:
    """验证 include_system 只放宽可见性、不放宽删除。"""

    # 方法作用：验证文档列表接口暴露 include_system 参数。
    # Args: self - 测试实例。
    # Returns: 无返回值，断言失败时由 pytest 报告。
    def test_doc_list_accepts_include_system(self) -> None:
        """文档列表需要与条目列表一致的开关。"""
        # Act / Assert
        assert re.search(
            r"async def list_knowledge_docs\(\s*include_system: bool = Query\(default=False\)",
            ROUTE_SOURCE,
        )

    # 方法作用：验证非超管在未勾选时仍被拒绝读取系统文档。
    # Args: self - 测试实例。
    # Returns: 无返回值，断言失败时由 pytest 报告。
    def test_doc_content_still_guarded_without_flag(self) -> None:
        """未勾选时保持 404，不泄漏系统文档存在性。"""
        # Act / Assert
        assert "not is_platform_super_admin() and not include_system" in ROUTE_SOURCE

    # 方法作用：验证删除文档路径未引入 include_system 放宽。
    # Args: self - 测试实例。
    # Returns: 无返回值，断言失败时由 pytest 报告。
    def test_doc_delete_not_relaxed(self) -> None:
        """删除路径必须保持超管独占，不得出现放宽参数。"""
        # Act
        delete_section = ROUTE_SOURCE.split("async def delete_knowledge_doc")[1]

        # Assert
        assert "include_system" not in delete_section
```

- [ ] **Step 6: 运行测试确认失败**

Run: `.venv/Scripts/python.exe -m pytest tests/test_knowledge/test_doc_include_system.py -v`
Expected: 3 failed

- [ ] **Step 7: 改造文档接口**

`list_knowledge_docs` 签名与过滤：

```python
@router.get("/knowledge/docs")
# 方法作用：列出当前身份可见文档并附带服务端删除权限与创建人。
# Args: include_system - 是否放宽只读查看系统文档（超管默认可见）。
# Returns: 文档摘要列表与总数。
async def list_knowledge_docs(include_system: bool = Query(default=False)):
```

过滤段替换为：

```python
    docs = await get_file_store().list_files()
    if not is_platform_super_admin() and not include_system:
        docs = [doc for doc in docs if str(doc.get("scope", "")) != "system"]
    usernames = await _load_usernames([doc.get("owner_user_id") for doc in docs])
```

循环内补 `owner_username`：

```python
        doc["owner_username"] = _normalize_owner_username(doc.get("owner_user_id"), usernames)
```

`get_doc_content` / `get_doc_raw` 增加同名参数，并把守卫改为：

```python
    if knowledge_scope == "system" and not is_platform_super_admin() and not include_system:
        raise HTTPException(404, f"文档 '{doc_name}' 未找到")
```

以及函数体内第二处：

```python
        if str(doc.get("scope", "")) == "system" and not is_platform_super_admin() and not include_system:
            raise HTTPException(404, f"文档 '{doc_name}' 未找到")
```

磁盘回退分支（仅 `docs/metrics` 内置目录）保持既有超管独占判断不变。

- [ ] **Step 8: 运行测试确认通过**

Run: `.venv/Scripts/python.exe -m pytest tests/test_knowledge/test_doc_include_system.py tests/test_knowledge/test_owner_username.py -v`
Expected: 7 passed

---

### Task 3: 列表查询性能（L1 热点消除 + L2 服务端缓存）

**Files:**
- Create: `src/knowledge/management_cache.py`
- Modify: `src/api/routes/knowledge.py`（`_decorate_entries` 引入组合缓存；`list_knowledge` 引入结果缓存；上传完成、删除条目、删除文档三处调用失效）
- Test: `tests/test_knowledge/test_list_cache.py`
- Test: `tests/test_knowledge/test_list_performance.py`

**Interfaces:**
- Produces: `class ManagementListCache`，方法 `get(key: tuple) -> list[dict] | None`、`set(key: tuple, value: list[dict]) -> None`、`invalidate_tenant(tenant_id: int) -> int`、`clear() -> int`、`stats() -> dict`
- Produces: `get_management_list_cache() -> ManagementListCache` 单例工厂
- Produces: `_judge_delete_permission(entries, *, role, current_tenant_id, current_user_id, can_manage=None) -> None`
- Consumes: `src/knowledge/governance.can_manage_knowledge_resource`

- [ ] **Step 1: 写失败测试（缓存模块）**

创建 `tests/test_knowledge/test_list_cache.py`：

```python
"""知识条目列表缓存测试，覆盖 spec/44 §4.3。"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from src.knowledge.management_cache import ManagementListCache  # noqa: E402


class TestManagementListCache:
    """验证 TTL 命中、租户失效与容量约束。"""

    # 方法作用：验证同一键在 TTL 内命中。
    # Args: self - 测试实例。
    # Returns: 无返回值，断言失败时由 pytest 报告。
    def test_hit_within_ttl(self) -> None:
        """重复翻页不应回源。"""
        # Arrange
        cache = ManagementListCache(ttl_seconds=30)
        key = ("tenant_admin", 3, 3, "", "", "document")

        # Act
        cache.set(key, [{"id": "a"}])
        hit = cache.get(key)

        # Assert
        assert hit == [{"id": "a"}]

    # 方法作用：验证键不同则不串号。
    # Args: self - 测试实例。
    # Returns: 无返回值，断言失败时由 pytest 报告。
    def test_different_keys_isolated(self) -> None:
        """不同身份与筛选键必须彼此隔离。"""
        # Arrange
        cache = ManagementListCache(ttl_seconds=30)
        cache.set(("tenant_admin", 3, 3, "", "", "document"), [{"id": "a"}])

        # Act
        hit = cache.get(("analyst", 3, 5, "", "", "document"))

        # Assert
        assert hit is None

    # 方法作用：验证关键词不进入缓存键。
    # Args: self - 测试实例。
    # Returns: 无返回值，断言失败时由 pytest 报告。
    def test_search_not_part_of_key(self) -> None:
        """搜索在缓存值上做内存过滤。"""
        # Arrange
        cache = ManagementListCache(ttl_seconds=30)
        key = ("tenant_admin", 3, 3, "", "", "document")

        # Act
        cache.set(key, [{"id": "a", "content": "月度销售额"}])
        hit = cache.get(key)
        filtered = [item for item in hit if "销售" in item["content"]]

        # Assert
        assert len(filtered) == 1

    # 方法作用：验证按租户失效会清空该租户全部键。
    # Args: self - 测试实例。
    # Returns: 无返回值，断言失败时由 pytest 报告。
    def test_invalidate_tenant_clears_all_keys_of_tenant(self) -> None:
        """上传或删除后必须立刻失效。"""
        # Arrange
        cache = ManagementListCache(ttl_seconds=30)
        cache.set(("tenant_admin", 3, 3, "", "", "document"), [{"id": "a"}])
        cache.set(("analyst", 3, 5, "", "", "all"), [{"id": "b"}])
        cache.set(("tenant_admin", 4, 9, "", "", "document"), [{"id": "c"}])

        # Act
        removed = cache.invalidate_tenant(3)

        # Assert
        assert removed == 2
        assert cache.get(("tenant_admin", 3, 3, "", "", "document")) is None
        assert cache.get(("tenant_admin", 4, 9, "", "", "document")) == [{"id": "c"}]

    # 方法作用：验证超过容量时淘汰最旧条目。
    # Args: self - 测试实例。
    # Returns: 无返回值，断言失败时由 pytest 报告。
    def test_capacity_eviction(self) -> None:
        """多身份并发时缓存不得无限增长。"""
        # Arrange
        cache = ManagementListCache(ttl_seconds=30, max_entries=2)

        # Act
        cache.set(("r", 1, 1, "", "", "document"), [{"id": "1"}])
        cache.set(("r", 1, 2, "", "", "document"), [{"id": "2"}])
        cache.set(("r", 1, 3, "", "", "document"), [{"id": "3"}])

        # Assert
        assert cache.stats()["entries"] == 2
```

- [ ] **Step 2: 运行测试确认失败**

Run: `.venv/Scripts/python.exe -m pytest tests/test_knowledge/test_list_cache.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'src.knowledge.management_cache'`

- [ ] **Step 3: 实现缓存模块**

创建 `src/knowledge/management_cache.py`：

```python
"""知识库管理列表的进程内 TTL 缓存，降低翻页与搜索的向量库压力。"""

from __future__ import annotations

import time
from collections import OrderedDict
from typing import Any

from src.logging_config import get_logger

logger = get_logger(__name__)

# 缓存键：角色、租户、用户、分类、范围、来源档位；关键词不参与，搜索在缓存值上完成。
CacheKey = tuple[str, int, int, str, str, str]


class ManagementListCache:
    """按身份与筛选维度缓存过滤后的完整条目列表。"""

    # 方法作用：初始化缓存容量与 TTL。
    # Args: ttl_seconds - 条目存活秒数；max_entries - 最大键数量。
    # Returns: 无返回值。
    def __init__(self, ttl_seconds: float = 30.0, max_entries: int = 64) -> None:
        self._ttl = ttl_seconds
        self._max_entries = max_entries
        self._store: OrderedDict[CacheKey, tuple[float, list[dict[str, Any]]]] = OrderedDict()
        logger.debug("管理列表缓存初始化", ttl_seconds=ttl_seconds, max_entries=max_entries)

    # 方法作用：读取未过期的缓存值。
    # Args: key - 缓存键。
    # Returns: 命中且未过期时返回条目列表，否则返回 None。
    def get(self, key: CacheKey) -> list[dict[str, Any]] | None:
        """命中未过期条目时刷新 LRU 次序。"""
        entry = self._store.get(key)
        if entry is None:
            logger.debug("管理列表缓存未命中", key=key)
            return None
        expires_at, value = entry
        if time.monotonic() >= expires_at:
            self._store.pop(key, None)
            logger.info("管理列表缓存过期", key=key)
            return None
        self._store.move_to_end(key)
        logger.debug("管理列表缓存命中", key=key, count=len(value))
        return value

    # 方法作用：写入缓存并在超容量时淘汰最旧键。
    # Args: key - 缓存键；value - 过滤后的完整条目列表。
    # Returns: 无返回值。
    def set(self, key: CacheKey, value: list[dict[str, Any]]) -> None:
        """写入并维护容量上限。"""
        self._store[key] = (time.monotonic() + self._ttl, value)
        self._store.move_to_end(key)
        while len(self._store) > self._max_entries:
            evicted, _ = self._store.popitem(last=False)
            logger.info("管理列表缓存淘汰", key=evicted)
        logger.debug("管理列表缓存写入", key=key, count=len(value))

    # 方法作用：清除指定租户的全部缓存键。
    # Args: tenant_id - 租户 ID。
    # Returns: 被清除的键数量。
    def invalidate_tenant(self, tenant_id: int) -> int:
        """上传、删除等写操作后按租户失效。"""
        targets = [key for key in self._store if key[1] == tenant_id]
        for key in targets:
            self._store.pop(key, None)
        logger.info("管理列表缓存失效", tenant_id=tenant_id, removed=len(targets))
        return len(targets)

    # 方法作用：清除全部缓存键。
    # Args: 无。
    # Returns: 被清除的键数量。
    def clear(self) -> int:
        """测试与配置热重载使用。"""
        count = len(self._store)
        self._store.clear()
        logger.info("管理列表缓存清空", removed=count)
        return count

    # 方法作用：返回缓存运行状态用于观测与测试。
    # Args: 无。
    # Returns: 含 entries 与 ttl_seconds 的字典。
    def stats(self) -> dict[str, Any]:
        """暴露条目数与 TTL。"""
        result = {"entries": len(self._store), "ttl_seconds": self._ttl}
        logger.debug("管理列表缓存状态", **result)
        return result


_CACHE: ManagementListCache | None = None


# 方法作用：获取进程内共享的管理列表缓存单例。
# Args: 无。
# Returns: ManagementListCache 实例。
def get_management_list_cache() -> ManagementListCache:
    """进程内单例，多 Worker 各自独立依赖 TTL 收敛。"""
    global _CACHE
    if _CACHE is None:
        _CACHE = ManagementListCache()
        logger.info("管理列表缓存单例创建完成")
    return _CACHE
```

- [ ] **Step 4: 运行测试确认通过**

Run: `.venv/Scripts/python.exe -m pytest tests/test_knowledge/test_list_cache.py -v`
Expected: 5 passed

- [ ] **Step 5: 写失败测试（热点消除）**

创建 `tests/test_knowledge/test_list_performance.py`：

```python
"""知识条目列表的循环热点回归测试，覆盖 spec/44 §4.2。

断言授权判定调用次数只与「可见范围组合数」相关，不随条目数线性增长。
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))


class TestAuthorizationCallCount:
    """验证同组合条目共用一次授权判定。"""

    # 方法作用：验证 6000 条同组合条目只触发一次授权判定。
    # Args: self - 测试实例。
    # Returns: 无返回值，断言失败时由 pytest 报告。
    def test_same_combination_judged_once(self) -> None:
        """组合缓存必须消除逐条授权开销。"""
        # Arrange
        calls: list[tuple] = []

        def fake_can_manage(scope, *, role, current_tenant_id, resource_tenant_id,
                            current_user_id, owner_user_id):
            calls.append((scope, resource_tenant_id, owner_user_id))
            return True

        from src.api.routes.knowledge import _judge_delete_permission

        entries = [
            {"scope": "tenant", "tenant_id": 3, "owner_user_id": 0, "source": "user_upload"}
            for _ in range(6000)
        ]

        # Act
        _judge_delete_permission(
            entries,
            role="tenant_admin",
            current_tenant_id=3,
            current_user_id=3,
            can_manage=fake_can_manage,
        )

        # Assert
        assert len(calls) == 1
        assert all(entry["can_delete"] is True for entry in entries)

    # 方法作用：验证不同组合仍各自判定。
    # Args: self - 测试实例。
    # Returns: 无返回值，断言失败时由 pytest 报告。
    def test_distinct_combinations_judged_separately(self) -> None:
        """不同属主必须分别判定，缓存不得越权。"""
        # Arrange
        calls: list[tuple] = []

        def fake_can_manage(scope, *, role, current_tenant_id, resource_tenant_id,
                            current_user_id, owner_user_id):
            calls.append((scope, resource_tenant_id, owner_user_id))
            return owner_user_id == 3

        from src.api.routes.knowledge import _judge_delete_permission

        entries = [
            {"scope": "private", "tenant_id": 3, "owner_user_id": 3, "source": "user_upload"},
            {"scope": "private", "tenant_id": 3, "owner_user_id": 7, "source": "user_upload"},
        ]

        # Act
        _judge_delete_permission(
            entries,
            role="analyst",
            current_tenant_id=3,
            current_user_id=3,
            can_manage=fake_can_manage,
        )

        # Assert
        assert len(calls) == 2
        assert entries[0]["can_delete"] is True
        assert entries[1]["can_delete"] is False

    # 方法作用：验证非用户上传条目直接判定为不可删。
    # Args: self - 测试实例。
    # Returns: 无返回值，断言失败时由 pytest 报告。
    def test_non_user_upload_never_deletable(self) -> None:
        """内省与目录扫描条目不得走授权函数。"""
        # Arrange
        calls: list[tuple] = []

        def fake_can_manage(scope, **kwargs):
            calls.append(scope)
            return True

        from src.api.routes.knowledge import _judge_delete_permission

        entries = [
            {"scope": "tenant", "tenant_id": 3, "owner_user_id": 0, "source": "auto_introspect"},
        ]

        # Act
        _judge_delete_permission(
            entries, role="tenant_admin", current_tenant_id=3, current_user_id=3,
            can_manage=fake_can_manage,
        )

        # Assert
        assert calls == []
        assert entries[0]["can_delete"] is False
```

- [ ] **Step 6: 运行测试确认失败**

Run: `.venv/Scripts/python.exe -m pytest tests/test_knowledge/test_list_performance.py -v`
Expected: FAIL — `ImportError: cannot import name '_judge_delete_permission'`

- [ ] **Step 7: 实现授权判定组合缓存**

在 `src/api/routes/knowledge.py` 新增：

```python
# 方法作用：按可见范围与属主组合批量判定删除权限，同组合只判定一次。
# Args: entries - 待判定的条目列表，原地补充 can_delete；
#       role/current_tenant_id/current_user_id - 当前身份；
#       can_manage - 授权判定函数，默认取 governance 实现。
# Returns: 无返回值。
def _judge_delete_permission(
    entries: list[dict],
    *,
    role: str,
    current_tenant_id: int,
    current_user_id: int,
    can_manage: object = None,
) -> None:
    """组合缓存消除逐条授权开销，6000 条降至个位数次调用。"""
    logger.debug("条目删除权限判定入口", entry_count=len(entries), role=role)
    if can_manage is None:
        from src.knowledge.governance import can_manage_knowledge_resource
        can_manage = can_manage_knowledge_resource
    cache: dict[tuple, bool] = {}
    for entry in entries:
        if entry.get("source") != "user_upload":
            entry["can_delete"] = False
            continue
        key = (entry["scope"], entry.get("tenant_id"), entry.get("owner_user_id"))
        allowed = cache.get(key)
        if allowed is None:
            allowed = bool(can_manage(
                entry["scope"],
                role=role,
                current_tenant_id=current_tenant_id,
                resource_tenant_id=entry.get("tenant_id"),
                current_user_id=current_user_id,
                owner_user_id=entry.get("owner_user_id"),
            ))
            cache[key] = allowed
        entry["can_delete"] = allowed
    # 循环内进度按项目规则只用 debug。
    logger.debug("条目删除权限判定完成", entry_count=len(entries), combination_count=len(cache))
```

`_decorate_entries` 改为调用它：

```python
async def _decorate_entries(entries: list[dict]) -> None:
    """只对当前页补齐删除权限与创建人。"""
    logger.debug("条目权限与创建人装饰入口", entry_count=len(entries))
    if not entries:
        logger.info("条目权限与创建人装饰完成", entry_count=0)
        return
    from src.api.auth import get_current_role, get_current_tenant_id, get_current_user_id

    role = get_current_role()
    current_tenant_id = get_current_tenant_id()
    current_user_id = get_current_user_id()
    _judge_delete_permission(
        entries,
        role=role,
        current_tenant_id=current_tenant_id,
        current_user_id=current_user_id,
    )
    usernames = await _load_usernames([entry.get("owner_user_id") for entry in entries])
    for entry in entries:
        entry["owner_username"] = _normalize_owner_username(
            entry.get("owner_user_id"), usernames,
        )
    logger.info("条目权限与创建人装饰完成", entry_count=len(entries), owner_count=len(usernames))
```

- [ ] **Step 8: 接入结果缓存与失效**

在 `list_knowledge` 中，过滤装配后先查缓存：

```python
        cache = get_management_list_cache()
        cache_key = (
            get_current_role(),
            get_current_tenant_id(),
            get_current_user_id(),
            category or "",
            knowledge_scope or "",
            entry_kind,
        )
        entries = cache.get(cache_key)
        if entries is None:
            # ……（既有 Milvus 查询与条目构建逻辑，构建出不含 can_delete/owner_username 的 entries）
            cache.set(cache_key, entries)
```

搜索、分页、装饰保持在缓存之外：

```python
        if search:
            q = search.lower()
            entries = [e for e in entries if q in e["content"].lower()
                       or q in e.get("table_name", "").lower()]
        total = len(entries)
        start = (page - 1) * page_size
        paged = [dict(entry) for entry in entries[start:start + page_size]]
        await _decorate_entries(paged)
```

注意：分页切片必须深拷贝（`dict(entry)`），否则装饰会污染缓存值。

在三处写操作后调用失效：

```python
    get_management_list_cache().invalidate_tenant(get_current_tenant_id())
```

位置：`_schedule_knowledge_uploads` 注册后台任务后、`delete_knowledge_entry` 删除成功后、`delete_knowledge_doc` 删除成功后。

- [ ] **Step 9: 运行性能与缓存测试**

Run: `.venv/Scripts/python.exe -m pytest tests/test_knowledge/test_list_performance.py tests/test_knowledge/test_list_cache.py -v`
Expected: 8 passed

- [ ] **Step 10: 实测响应耗时**

Run: 启动或复用运行中的服务，用既有登录身份连续调用 `GET /api/v1/knowledge?page=1&page_size=20` 三次
Expected: 首次 ≤ 1s；第二、三次（缓存命中）≤ 100ms；日志中 `知识库列表完成` 与 `知识库查询完成` 的时间差 ≤ 0.2s。

---

### Task 4: 长期记忆后端（Store 方法 + API）

**Files:**
- Modify: `src/memory/long_term_store.py`（新增 `list_memories`、`delete_memory`）
- Create: `src/api/routes/memory.py`
- Modify: `src/api/routes/__init__.py`（注册路由）
- Modify: `src/config.py`（访问策略表追加 `/v1/memory` 条目）
- Test: `tests/test_memory/test_memory_management_api.py`

**Interfaces:**
- Consumes: `LongTermMemoryStore._pg_identity_connection()`、`_resolve_identity()`、`_delete_vector_or_queue()`
- Produces: `LongTermMemoryStore.list_memories(memory_type: str = "", search: str = "", page: int = 1, page_size: int = 20) -> tuple[list[dict], int]`
- Produces: `LongTermMemoryStore.delete_memory(memory_id: str) -> bool`
- Produces: `_serialize_memory(row: Any) -> dict[str, Any]`
- Produces: `GET /api/v1/memory`、`GET /api/v1/memory/{memory_id}`、`DELETE /api/v1/memory/{memory_id}`

- [ ] **Step 1: 写失败测试**

创建 `tests/test_memory/test_memory_management_api.py`：

```python
"""长期记忆管理 API 测试，覆盖 spec/44 §5.2。"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from src.api.routes.memory import _serialize_memory  # noqa: E402


class TestMemorySerialization:
    """验证记忆响应的字段契约与过期时间推导。"""

    # 方法作用：验证 payload 为字符串时被解析为对象。
    # Args: self - 测试实例。
    # Returns: 无返回值，断言失败时由 pytest 报告。
    def test_payload_string_is_parsed(self) -> None:
        """驱动返回 JSONB 为字符串，响应必须是对象。"""
        # Arrange
        row = {
            "id": "m1", "memory_type": "sql_template", "scope": "private:3:3",
            "content": "问题: 统计各月销售额", "payload": '{"question": "统计各月销售额"}',
            "access_count": 2, "confidence": 0.5, "ttl_days": 180,
        }

        # Act
        result = _serialize_memory(row)

        # Assert
        assert result["payload"] == {"question": "统计各月销售额"}
        assert result["memory_type"] == "sql_template"

    # 方法作用：验证 payload 为对象时原样保留。
    # Args: self - 测试实例。
    # Returns: 无返回值，断言失败时由 pytest 报告。
    def test_payload_dict_is_kept(self) -> None:
        """驱动返回 dict 时不得二次解析。"""
        # Arrange
        row = {"id": "m2", "payload": {"sql": "SELECT 1"}, "ttl_days": None,
               "memory_type": "user_preference", "scope": "private:3:3",
               "content": "", "access_count": 0, "confidence": 1.0}

        # Act
        result = _serialize_memory(row)

        # Assert
        assert result["payload"]["sql"] == "SELECT 1"
        assert result["expires_at"] is None

    # 方法作用：验证损坏的 payload 不导致接口 500。
    # Args: self - 测试实例。
    # Returns: 无返回值，断言失败时由 pytest 报告。
    def test_broken_payload_degrades_to_empty(self) -> None:
        """历史脏数据必须降级为空对象。"""
        # Arrange
        row = {"id": "m3", "payload": "{not-json", "ttl_days": None,
               "memory_type": "sql_template", "scope": "private:3:3",
               "content": "", "access_count": 0, "confidence": 0.5}

        # Act
        result = _serialize_memory(row)

        # Assert
        assert result["payload"] == {}


class TestMemoryRouteContract:
    """验证端点与访问策略的静态契约。"""

    # 方法作用：验证管理端点不接受所有者参数。
    # Args: self - 测试实例。
    # Returns: 无返回值，断言失败时由 pytest 报告。
    def test_no_owner_override_parameter(self) -> None:
        """归属只能来自登录身份，禁止请求参数覆盖。"""
        # Arrange
        source = Path("src/api/routes/memory.py").read_text(encoding="utf-8")

        # Act / Assert
        assert "owner_user_id: int" not in source
        assert "tenant_id: int = Query" not in source

    # 方法作用：验证越权访问返回 404 而不是 403。
    # Args: self - 测试实例。
    # Returns: 无返回值，断言失败时由 pytest 报告。
    def test_unauthorized_returns_404(self) -> None:
        """避免探测他人记忆的存在性。"""
        # Arrange
        source = Path("src/api/routes/memory.py").read_text(encoding="utf-8")

        # Act / Assert
        assert 'HTTPException(404, "记忆不存在")' in source
```

- [ ] **Step 2: 运行测试确认失败**

Run: `.venv/Scripts/python.exe -m pytest tests/test_memory/test_memory_management_api.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'src.api.routes.memory'`

- [ ] **Step 3: 实现 Store 方法**

在 `src/memory/long_term_store.py` 的 `get_preferences` 之后新增两个方法，签名与实现如下（复用已有的 `_pg_identity_connection` / `_resolve_identity` / `_delete_vector_or_queue`）：

```python
    # 方法作用：分页列出当前用户 private 范围的长期记忆。
    # Args: memory_type - 可选类型过滤；search - 内容关键词；page/page_size - 分页。
    # Returns: (记忆字典列表, 总数)；数据库不可用时返回 ([], 0)。
    async def list_memories(
        self,
        memory_type: str = "",
        search: str = "",
        page: int = 1,
        page_size: int = 20,
    ) -> tuple[list[dict[str, Any]], int]:
        """列表走 PostgreSQL 权威表，不访问向量库。"""
        logger.debug(
            "长期记忆列表入口",
            memory_type=memory_type,
            has_search=bool(search),
            page=page,
            page_size=page_size,
        )
        identity = self._resolve_identity(None)
        offset = max(page - 1, 0) * page_size
        conditions = ["tenant_id = $1", "owner_user_id = $2", "visibility = 'private'"]
        params: list[Any] = [identity.tenant_id, identity.user_id]
        if memory_type:
            params.append(memory_type)
            conditions.append(f"memory_type = ${len(params)}")
        if search:
            params.append(f"%{search}%")
            conditions.append(f"content ILIKE ${len(params)}")
        where = " AND ".join(conditions)
        limit_idx = len(params) + 1
        offset_idx = len(params) + 2
        try:
            async with self._pg_identity_connection(identity) as conn:
                if conn is None:
                    logger.info("长期记忆列表回退", reason="PostgreSQL 未配置")
                    return [], 0
                total = await conn.fetchval(
                    f"SELECT count(*) FROM long_term_memories WHERE {where}",
                    *params,
                )
                rows = await conn.fetch(
                    "SELECT id, memory_type, scope, content, payload, access_count, "
                    "confidence, ttl_days, created_at, last_accessed_at "
                    f"FROM long_term_memories WHERE {where} "
                    f"ORDER BY created_at DESC LIMIT ${limit_idx} OFFSET ${offset_idx}",
                    *params, page_size, offset,
                )
            items = [dict(row) for row in rows]
            logger.info("长期记忆列表完成", total=int(total or 0), returned=len(items))
            return items, int(total or 0)
        except Exception as exc:
            logger.error("长期记忆列表失败", error=str(exc), exc_info=True)
            return [], 0

    # 方法作用：删除当前用户的一条长期记忆，并保持向量库一致。
    # Args: memory_id - 记忆 ID。
    # Returns: 删除成功返回 True；不属于当前用户或不存在返回 False。
    async def delete_memory(self, memory_id: str) -> bool:
        """PG 删除后走既有补偿队列清理向量条目。"""
        logger.debug("长期记忆删除入口", memory_id=memory_id)
        identity = self._resolve_identity(None)
        try:
            async with self._pg_identity_connection(identity) as conn:
                if conn is None:
                    logger.info("长期记忆删除回退", reason="PostgreSQL 未配置")
                    return False
                deleted = await conn.fetchval(
                    "DELETE FROM long_term_memories WHERE id = $1 "
                    "AND tenant_id = $2 AND owner_user_id = $3 AND visibility = 'private' "
                    "RETURNING id",
                    memory_id, identity.tenant_id, identity.user_id,
                )
            if deleted is None:
                logger.warning("长期记忆删除未命中", memory_id=memory_id)
                return False
            await self._delete_vector_or_queue([memory_id])
            logger.info("长期记忆删除完成", memory_id=memory_id)
            return True
        except Exception as exc:
            logger.error("长期记忆删除失败", memory_id=memory_id, error=str(exc), exc_info=True)
            return False
```

- [ ] **Step 4: 实现 API 路由**

创建 `src/api/routes/memory.py`（完整实现见 `spec/44` §5.2 契约）：

```python
"""长期记忆管理路由：仅暴露当前用户 private 范围的读取与删除。"""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from typing import Any

from fastapi import APIRouter, HTTPException, Query

from src.logging_config import get_logger

logger = get_logger(__name__)

router = APIRouter(tags=["memory"])


# 方法作用：把数据库行序列化为接口响应，解析 payload 并推导过期时间。
# Args: row - long_term_memories 行（含 payload/created_at/last_accessed_at）。
# Returns: 可直接返回给前端的字典。
def _serialize_memory(row: Any) -> dict[str, Any]:
    """payload 兼容字符串与对象，损坏数据降级为空对象。"""
    data = dict(row)
    logger.debug("长期记忆序列化入口", memory_id=str(data.get("id", "")))
    payload = data.get("payload")
    if isinstance(payload, str):
        try:
            payload = json.loads(payload)
        except (TypeError, ValueError):
            logger.warning("长期记忆 payload 解析失败", memory_id=data.get("id"))
            payload = {}
    elif not isinstance(payload, dict):
        payload = {}
    created_at = data.get("created_at")
    last_accessed_at = data.get("last_accessed_at")
    ttl_days = data.get("ttl_days")
    expires_at = None
    if isinstance(created_at, datetime) and isinstance(ttl_days, int) and ttl_days > 0:
        expires_at = (created_at + timedelta(days=ttl_days)).astimezone(timezone.utc).isoformat()
    result = {
        "id": str(data.get("id", "")),
        "memory_type": str(data.get("memory_type", "")),
        "scope": str(data.get("scope", "")),
        "content": data.get("content") or "",
        "payload": payload,
        "access_count": int(data.get("access_count") or 0),
        "confidence": float(data.get("confidence") or 0.0),
        "ttl_days": ttl_days,
        "created_at": created_at.astimezone(timezone.utc).isoformat() if isinstance(created_at, datetime) else None,
        "last_accessed_at": (
            last_accessed_at.astimezone(timezone.utc).isoformat()
            if isinstance(last_accessed_at, datetime) else None
        ),
        "expires_at": expires_at,
    }
    logger.debug("长期记忆序列化完成", memory_id=result["id"])
    return result


# 方法作用：分页列出当前登录用户 private 范围的长期记忆。
# Args: memory_type - 类型过滤；search - 内容关键词；page/page_size - 分页。
# Returns: 记忆分页结果。
@router.get("/memory")
async def list_memories(
    memory_type: str = Query(default=""),
    search: str = Query(default=""),
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=20, ge=10, le=100),
):
    """归属恒为当前登录用户，不接受请求参数覆盖。"""
    logger.debug(
        "长期记忆列表 API 入口",
        memory_type=memory_type,
        has_search=bool(search),
        page=page,
        page_size=page_size,
    )
    from src.memory.long_term_store import get_long_term_memory_store

    store = await get_long_term_memory_store()
    items, total = await store.list_memories(
        memory_type=memory_type, search=search, page=page, page_size=page_size,
    )
    result = {
        "items": [_serialize_memory(row) for row in items],
        "total": total,
        "page": page,
        "page_size": page_size,
    }
    logger.info("长期记忆列表 API 完成", total=total, returned=len(result["items"]))
    return result


# 方法作用：读取当前登录用户的单条长期记忆详情。
# Args: memory_id - 记忆 ID。
# Returns: 记忆详情；不存在或非本人时 404。
@router.get("/memory/{memory_id}")
async def get_memory(memory_id: str):
    """越权访问返回 404，避免探测他人记忆存在性。"""
    logger.debug("长期记忆详情 API 入口", memory_id=memory_id)
    from src.memory.long_term_store import get_long_term_memory_store

    store = await get_long_term_memory_store()
    items, _ = await store.list_memories(memory_type="", search=memory_id, page=1, page_size=100)
    for row in items:
        if str(row.get("id")) == memory_id:
            logger.info("长期记忆详情 API 完成", memory_id=memory_id)
            return _serialize_memory(row)
    logger.warning("长期记忆详情未命中", memory_id=memory_id)
    raise HTTPException(404, "记忆不存在")


# 方法作用：删除当前登录用户的一条长期记忆。
# Args: memory_id - 记忆 ID。
# Returns: 删除结果；不存在或非本人时 404。
@router.delete("/memory/{memory_id}")
async def delete_memory(memory_id: str):
    """PostgreSQL 与向量库双后端一致删除。"""
    logger.debug("长期记忆删除 API 入口", memory_id=memory_id)
    from src.memory.long_term_store import get_long_term_memory_store

    store = await get_long_term_memory_store()
    if not await store.delete_memory(memory_id):
        raise HTTPException(404, "记忆不存在")
    result = {"status": "ok", "id": memory_id}
    logger.info("长期记忆删除 API 完成", memory_id=memory_id)
    return result
```

注：`get_memory` 复用列表查询做归属校验（`search=memory_id` 按 ID 过滤不在 `content` 中命中，故实现时应在 `list_memories` 之外直接按 ID 查询；若按本示例实现，需改为在 store 增加 `get_memory(memory_id) -> dict | None`，并在实现时二选一，不可两处并存）。

- [ ] **Step 5: 注册路由与访问策略**

在 `src/api/routes/__init__.py` 按既有模式导入并 `router.include_router(memory_router)`；在 `src/config.py` 的访问策略表中追加：

```python
    # 长期记忆：归属由登录身份决定，列表与详情标准日志，删除记审计。
    ApiAccessPolicy(path="/api/v1/memory", methods=["GET"], auth="jwt", access_log="standard",
                    description="长期记忆列表"),
    ApiAccessPolicy(path="/api/v1/memory/{memory_id}", methods=["GET"], auth="jwt", access_log="standard",
                    description="长期记忆详情"),
    ApiAccessPolicy(path="/api/v1/memory/{memory_id}", methods=["DELETE"], auth="jwt", access_log="audit",
                    description="删除长期记忆"),
```

（字段名与既有策略表保持一致；实现时对照 `src/config.py` 中 `/api/v1/knowledge` 条目的实际写法。）

- [ ] **Step 6: 运行测试确认通过**

Run: `.venv/Scripts/python.exe -m pytest tests/test_memory/test_memory_management_api.py -v`
Expected: 5 passed

- [ ] **Step 7: 回归记忆模块既有测试**

Run: `.venv/Scripts/python.exe -m pytest tests/test_memory/ -q`
Expected: 全绿

---

### Task 5: 前端「我的长期记忆」页面

**Files:**
- Create: `frontend/src/pages/MemoryPage.tsx`
- Modify: `frontend/src/App.tsx`（lazy import、路由、菜单项）
- Modify: `frontend/src/i18n/en.ts`（新增文案）
- Test: `tests/test_frontend/test_memory_page.py`

**Interfaces:**
- Consumes: `GET /api/v1/memory`、`DELETE /api/v1/memory/{id}`
- Consumes: `frontend/src/api/client.ts` 的 `get` / `del`
- Produces: 路由 `/memory`，菜单 key `memory`

- [ ] **Step 1: 写失败测试**

创建 `tests/test_frontend/test_memory_page.py`：

```python
"""「我的长期记忆」页面契约测试，覆盖 spec/44 §5.3。

前端没有 JS 测试框架，沿用仓库既有的源码静态断言模式。
"""

from __future__ import annotations

import re
from pathlib import Path

PAGE_SOURCE = Path("frontend/src/pages/MemoryPage.tsx").read_text(encoding="utf-8")
APP_SOURCE = Path("frontend/src/App.tsx").read_text(encoding="utf-8")


class TestMemoryPage:
    """验证页面渲染要素。"""

    # 方法作用：验证页面调用记忆接口且不传归属参数。
    # Args: self - 测试实例。
    # Returns: 无返回值，断言失败时由 pytest 报告。
    def test_page_queries_memory_endpoint(self) -> None:
        """列表只查本人记忆，不带 owner/tenant 参数。"""
        # Act / Assert
        assert "/memory?" in PAGE_SOURCE
        assert "owner_user_id" not in PAGE_SOURCE
        assert "tenant_id" not in PAGE_SOURCE

    # 方法作用：验证页面不提供写操作。
    # Args: self - 测试实例。
    # Returns: 无返回值，断言失败时由 pytest 报告。
    def test_page_has_no_edit_action(self) -> None:
        """记忆只读 + 删除，手工编辑会污染检索上下文。"""
        # Act / Assert
        assert "patch(" not in PAGE_SOURCE
        assert "post(" not in PAGE_SOURCE

    # 方法作用：验证删除走二次确认。
    # Args: self - 测试实例。
    # Returns: 无返回值，断言失败时由 pytest 报告。
    def test_page_delete_uses_confirm(self) -> None:
        """破坏性操作需要 Popconfirm。"""
        # Act / Assert
        assert "Popconfirm" in PAGE_SOURCE
        assert re.search(r"del\(`/memory/", PAGE_SOURCE)

    # 方法作用：验证空态文案存在。
    # Args: self - 测试实例。
    # Returns: 无返回值，断言失败时由 pytest 报告。
    def test_page_empty_state_copy(self) -> None:
        """空态需引导用户理解记忆来源。"""
        # Act / Assert
        assert "暂无长期记忆" in PAGE_SOURCE


class TestMemoryMenuWiring:
    """验证菜单与路由接线。"""

    # 方法作用：验证路由与懒加载已注册。
    # Args: self - 测试实例。
    # Returns: 无返回值，断言失败时由 pytest 报告。
    def test_route_registered(self) -> None:
        """路由缺失会导致菜单 404。"""
        # Act / Assert
        assert "lazy(() => import('./pages/MemoryPage'))" in APP_SOURCE
        assert 'path="/memory"' in APP_SOURCE

    # 方法作用：验证菜单项对所有角色可见。
    # Args: self - 测试实例。
    # Returns: 无返回值，断言失败时由 pytest 报告。
    def test_menu_visible_to_all_roles(self) -> None:
        """记忆是个人数据，不按角色收敛。"""
        # Act
        menu_section = APP_SOURCE.split("navigationItems")[1].split("].filter(")[0]

        # Assert
        assert "key: 'memory'" in menu_section


class TestMemoryTypes:
    """验证记忆类型选项覆盖后端枚举。"""

    # 方法作用：验证五种记忆类型都在筛选选项中。
    # Args: self - 测试实例。
    # Returns: 无返回值，断言失败时由 pytest 报告。
    def test_all_memory_types_listed(self) -> None:
        """类型遗漏会导致用户无法筛选。"""
        # Act / Assert
        for value in ("sql_template", "user_preference", "learned_pattern", "correction", "project_rule"):
            assert value in PAGE_SOURCE
```

- [ ] **Step 2: 运行测试确认失败**

Run: `.venv/Scripts/python.exe -m pytest tests/test_frontend/test_memory_page.py -v`
Expected: FAIL — `FileNotFoundError: frontend/src/pages/MemoryPage.tsx`

- [ ] **Step 3: 实现页面**

创建 `frontend/src/pages/MemoryPage.tsx`，结构对齐 `KnowledgePage.tsx` 既有写法（`Card` + `Table` + `Modal` + `Descriptions`，全部文案用 `t()` 包裹）。要点：

```tsx
import { t, useLanguage } from '../i18n';
import { useEffect, useMemo, useState } from 'react';
import { Card, Table, Tag, Typography, Empty, message, Select, Input, Space, Modal, Popconfirm, Descriptions, Button } from 'antd';
import { BulbOutlined, DeleteOutlined, ReloadOutlined } from '@ant-design/icons';
import { get, del } from '../api/client';
import dayjs from 'dayjs';

const MEMORY_TYPE_OPTIONS = [
  { value: '', label: t("全部类型") },
  { value: 'sql_template', label: t("SQL 模板") },
  { value: 'user_preference', label: t("用户偏好") },
  { value: 'learned_pattern', label: t("学习模式") },
  { value: 'correction', label: t("纠正记录") },
  { value: 'project_rule', label: t("项目规则") },
];
const TYPE_COLORS: Record<string, string> = {
  sql_template: 'blue', user_preference: 'green', learned_pattern: 'purple',
  correction: 'orange', project_rule: 'cyan',
};

interface LongTermMemory {
  id: string; memory_type: string; scope: string; content: string;
  payload: Record<string, unknown>; access_count: number; confidence: number;
  ttl_days: number | null; created_at: string | null; last_accessed_at: string | null;
  expires_at: string | null;
}
```

- 状态：`items` / `total` / `page` / `pageSize` / `loading` / `typeFilter` / `search` / `detail`
- `loadMemories()`：`get<{ items: LongTermMemory[]; total: number }>(`/memory?memory_type=${typeFilter}&search=${encodeURIComponent(search)}&page=${page}&page_size=${pageSize}`)`
- 删除：`del(`/memory/${encodeURIComponent(id)}`)` → `message.success(t("已删除"))` → 重新加载
- 详情 Modal：`content` 用 `whiteSpace: 'pre-wrap'`，`payload.sql` 用 `<pre>` 展示，其余字段走 `Descriptions`
- 空态：`<Empty description={t("暂无长期记忆。成功执行的 SQL 和你的偏好会自动沉淀到这里。")} />`

- [ ] **Step 4: 接线 App.tsx**

1. 懒加载：`const MemoryPage = lazy(() => import('./pages/MemoryPage'));`（紧随 `KnowledgePage` 之后）
2. 路由：`<Route path="/memory" element={<MemoryPage />} />`（放在 `/knowledge` 路由之后）
3. 菜单项（放在知识库项之后，**不加角色判断**）：

```tsx
    { key: 'memory', icon: <BulbOutlined />, label: <NavLink to="/memory">{t("我的记忆")}</NavLink> },
```

并从 `@ant-design/icons` 导入 `BulbOutlined`。

- [ ] **Step 5: 补 i18n 词条**

在 `frontend/src/i18n/en.ts` 追加：

```ts
  "我的记忆": "My memory",
  "暂无长期记忆。成功执行的 SQL 和你的偏好会自动沉淀到这里。": "No long-term memory yet. Successfully executed SQL and your preferences are stored here automatically.",
  "全部类型": "All types",
  "用户偏好": "User preference",
  "学习模式": "Learned pattern",
  "纠正记录": "Correction",
  "项目规则": "Project rule",
  "访问次数": "Access count",
  "置信度": "Confidence",
  "最后访问时间": "Last accessed",
  "记忆详情": "Memory details",
  "有效期（天）": "TTL (days)",
  "过期时间": "Expires at",
```

- [ ] **Step 6: 运行测试确认通过**

Run: `.venv/Scripts/python.exe -m pytest tests/test_frontend/test_memory_page.py -v`
Expected: 7 passed

- [ ] **Step 7: 前端构建校验**

Run: `cd frontend && npm run build`
Expected: 构建成功，无 TS 类型错误

---

### Task 6: 上传表单交互（滑块 / 数据源下拉 / 标签面板）

**Files:**
- Modify: `frontend/src/pages/KnowledgePage.tsx:534-594`（上传 Modal）
- Modify: `frontend/src/i18n/en.ts`
- Test: `tests/test_frontend/test_knowledge_upload_form.py`

**Interfaces:**
- Consumes: `frontend/src/api/datasourceCatalog.ts` 的 `fetchDatasourceCatalog()`

- [ ] **Step 1: 写失败测试**

创建 `tests/test_frontend/test_knowledge_upload_form.py`：

```python
"""知识库上传表单交互契约测试，覆盖 spec/44 §3。

前端没有 JS 测试框架，沿用仓库既有的源码静态断言模式。
"""

from __future__ import annotations

import re
from pathlib import Path

SOURCE = Path("frontend/src/pages/KnowledgePage.tsx").read_text(encoding="utf-8")


class TestChunkControls:
    """验证切块与重叠的滑块与手动输入并存。"""

    # 方法作用：验证两个字段都使用 Slider。
    # Args: self - 测试实例。
    # Returns: 无返回值，断言失败时由 pytest 报告。
    def test_sliders_present(self) -> None:
        """滑块与手动输入并存。"""
        # Act / Assert
        assert SOURCE.count("<Slider") >= 2

    # 方法作用：验证滑块范围与步长符合设计。
    # Args: self - 测试实例。
    # Returns: 无返回值，断言失败时由 pytest 报告。
    def test_slider_ranges(self) -> None:
        """块大小 200-4000 步长 100，重叠 0-500 步长 10。"""
        # Act / Assert
        assert "min={200}" in SOURCE and "max={4000}" in SOURCE and "step={100}" in SOURCE
        assert "step={10}" in SOURCE

    # 方法作用：验证重叠上限随块大小收窄。
    # Args: self - 测试实例。
    # Returns: 无返回值，断言失败时由 pytest 报告。
    def test_overlap_clamped_by_chunk_size(self) -> None:
        """overlap 必须小于 chunkSize。"""
        # Act / Assert
        assert re.search(r"Math\.min\(\s*500\s*,", SOURCE)


class TestDatasourceSelect:
    """验证数据源为下拉选择。"""

    # 方法作用：验证数据源不再使用文本输入。
    # Args: self - 测试实例。
    # Returns: 无返回值，断言失败时由 pytest 报告。
    def test_datasource_uses_select(self) -> None:
        """全仓库禁止手输数据源名称。"""
        # Act
        segment = SOURCE.split('{t("数据源")}')[1].split("</div>")[0]

        # Assert
        assert "<Select" in segment
        assert "fetchDatasourceCatalog" in SOURCE

    # 方法作用：验证旧的手输占位文案已移除。
    # Args: self - 测试实例。
    # Returns: 无返回值，断言失败时由 pytest 报告。
    def test_placeholder_no_longer_free_text(self) -> None:
        """旧占位「可选数据源名称」必须移除。"""
        # Act / Assert
        assert "可选数据源名称" not in SOURCE


class TestTagPicker:
    """验证标签多选面板。"""

    # 方法作用：验证面板使用自定义下拉渲染。
    # Args: self - 测试实例。
    # Returns: 无返回值，断言失败时由 pytest 报告。
    def test_panel_uses_dropdown_render(self) -> None:
        """标签需要以标签块网格展示。"""
        # Act / Assert
        assert "dropdownRender" in SOURCE

    # 方法作用：验证选中态含勾选标记与浅色背景。
    # Args: self - 测试实例。
    # Returns: 无返回值，断言失败时由 pytest 报告。
    def test_selected_state_marker(self) -> None:
        """选中标签右下角需要勾。"""
        # Act / Assert
        assert "CheckOutlined" in SOURCE
        assert "#e6f4ff" in SOURCE

    # 方法作用：验证新建个人标签入口保留。
    # Args: self - 测试实例。
    # Returns: 无返回值，断言失败时由 pytest 报告。
    def test_create_tag_entry_kept(self) -> None:
        """面板内需保留新建个人标签。"""
        # Act / Assert
        assert "createPersonalTag" in SOURCE
```

- [ ] **Step 2: 运行测试确认失败**

Run: `.venv/Scripts/python.exe -m pytest tests/test_frontend/test_knowledge_upload_form.py -v`
Expected: 多处 FAIL（Slider / dropdownRender / CheckOutlined 尚未出现）

- [ ] **Step 3: 改造上传 Modal**

1. 导入补充：`Slider`、`Checkbox`、`CheckOutlined`，以及 `import { fetchDatasourceCatalog } from '../api/datasourceCatalog';`
2. 新增状态：

```tsx
  const [datasourceOptions, setDatasourceOptions] = useState<{ value: string; label: string }[]>([]);
  const [tagPanelOpen, setTagPanelOpen] = useState(false);
```

3. 打开 Modal 时加载数据源候选：

```tsx
  useEffect(() => {
    if (!cfgOpen) return;
    let cancelled = false;
    void (async () => {
      try {
        const items = await fetchDatasourceCatalog();
        if (!cancelled) setDatasourceOptions(items.map(item => ({ value: item.name, label: item.name })));
      } catch { if (!cancelled) message.error(t("无法加载数据源列表")); }
    })();
    return () => { cancelled = true; };
  }, [cfgOpen]);
```

4. 数据源字段替换为：

```tsx
          <div><Typography.Text strong>{t("数据源")}</Typography.Text>
            <Select style={{ width: '100%' }} value={uploadCfg.datasource || undefined}
              options={datasourceOptions} showSearch allowClear
              placeholder={t("可选：绑定数据源")}
              filterOption={(input, option) => String(option?.value || '').toLowerCase().includes(input.toLowerCase())}
              onChange={value => setUploadCfg(current => ({ ...current, datasource: value || '' }))} /></div>
```

5. 切块与重叠替换为「Slider + InputNumber」组合：

```tsx
          <div><Typography.Text strong>{t("块大小")}</Typography.Text>
            <Space style={{ width: '100%' }}>
              <Slider style={{ flex: 1 }} min={200} max={4000} step={100} value={uploadCfg.chunkSize}
                onChange={value => setUploadCfg(p => {
                  const maxOverlap = Math.min(500, value - 1);
                  return { ...p, chunkSize: value, chunkOverlap: Math.min(p.chunkOverlap, maxOverlap) };
                })} />
              <InputNumber min={200} max={4000} step={100} style={{ width: 96 }} value={uploadCfg.chunkSize}
                onChange={value => setUploadCfg(p => {
                  const chunkSize = value || 800;
                  const maxOverlap = Math.min(500, chunkSize - 1);
                  return { ...p, chunkSize, chunkOverlap: Math.min(p.chunkOverlap, maxOverlap) };
                })} /></Space></div>
          <div><Typography.Text strong>{t("重叠")}</Typography.Text>
            <Space style={{ width: '100%' }}>
              <Slider style={{ flex: 1 }} min={0} max={Math.min(500, uploadCfg.chunkSize - 1)} step={10}
                value={uploadCfg.chunkOverlap}
                onChange={value => setUploadCfg(p => ({ ...p, chunkOverlap: value }))} />
              <InputNumber min={0} max={Math.min(500, uploadCfg.chunkSize - 1)} step={10} style={{ width: 96 }}
                value={uploadCfg.chunkOverlap}
                onChange={value => setUploadCfg(p => ({ ...p, chunkOverlap: value || 0 }))} /></Space></div>
```

6. 标签字段替换为自定义面板（`selectableTags` 与 `handleTagSearch` 逻辑不变）：

```tsx
          <div><Typography.Text strong>{t("标签")}</Typography.Text>
            <Select mode="multiple" style={{ width: '100%' }} value={uploadCfg.tagIds}
              open={tagPanelOpen} onDropdownVisibleChange={setTagPanelOpen}
              loading={tagLoading} showSearch filterOption={false} onSearch={handleTagSearch}
              onFocus={() => { void loadTags(); }}
              onChange={(tagIds: number[]) => setUploadCfg(current => ({ ...current, tagIds }))}
              options={selectableTags.map(tag => ({ value: tag.id, label: tag.name }))}
              placeholder={t("搜索并选择标签")}
              dropdownRender={() => (
                <div style={{ padding: 8, maxWidth: 420 }}>
                  <div style={{ display: 'flex', flexWrap: 'wrap', gap: 8, maxHeight: 240, overflow: 'auto' }}>
                    {selectableTags.map(tag => {
                      const checked = uploadCfg.tagIds.includes(tag.id);
                      return (
                        <div key={tag.id} role="button" tabIndex={0}
                          onClick={() => setUploadCfg(current => ({
                            ...current,
                            tagIds: checked
                              ? current.tagIds.filter(id => id !== tag.id)
                              : [...current.tagIds, tag.id],
                          }))}
                          onKeyDown={event => { if (event.key === 'Enter') event.currentTarget.click(); }}
                          style={{
                            position: 'relative', cursor: 'pointer', userSelect: 'none',
                            padding: '4px 10px', borderRadius: 6, fontSize: 12,
                            border: `1px solid ${checked ? '#91caff' : '#d9d9d9'}`,
                            background: checked ? '#e6f4ff' : '#fff',
                            color: checked ? '#1677ff' : '#333',
                          }}>
                          {tag.name}{tag.scope === 'private' ? t(" · 个人") : ''}
                          {checked && (
                            <CheckOutlined style={{ position: 'absolute', right: 2, bottom: 2, fontSize: 9 }} />
                          )}
                        </div>
                      );
                    })}
                  </div>
                  <Space.Compact style={{ width: '100%', marginTop: 8 }}>
                    <Input value={personalTagName} maxLength={128} placeholder={t("新建个人标签")}
                      disabled={!user}
                      onChange={event => setPersonalTagName(event.target.value)}
                      onPressEnter={() => { void createPersonalTag(); }} />
                    <Button icon={<PlusOutlined />} disabled={!user || !personalTagName.trim()}
                      onClick={() => { void createPersonalTag(); }}>{t("新建")}</Button>
                  </Space.Compact>
                </div>
              )} /></div>
```

7. 删除原第 563-570 行的独立「新建个人标签」块。

- [ ] **Step 4: 补 i18n 词条**

```ts
  "可选：绑定数据源": "Optional: bind a data source",
  "标记为系统维护": "Maintained automatically",
```

- [ ] **Step 5: 运行测试确认通过**

Run: `.venv/Scripts/python.exe -m pytest tests/test_frontend/test_knowledge_upload_form.py -v`
Expected: 7 passed

---

### Task 7: 知识库列表增强（创建人 / 来源分层 / 系统知识开关）

**Files:**
- Modify: `frontend/src/pages/KnowledgePage.tsx`（筛选栏、两张表列、详情 Modal）
- Modify: `frontend/src/types/index.ts:313-342`（`KnowledgeEntry` / `KnowledgeDoc` 增加 `owner_username`）
- Modify: `frontend/src/i18n/en.ts`
- Test: `tests/test_frontend/test_knowledge_list_filters.py`

**Interfaces:**
- Consumes: `GET /api/v1/knowledge?entry_kind=&include_system=`、`GET /api/v1/knowledge/docs?include_system=`

- [ ] **Step 1: 写失败测试**

创建 `tests/test_frontend/test_knowledge_list_filters.py`：

```python
"""知识库列表筛选与创建人列契约测试，覆盖 spec/44 §2.2 §2.3。"""

from __future__ import annotations

from pathlib import Path

SOURCE = Path("frontend/src/pages/KnowledgePage.tsx").read_text(encoding="utf-8")
TYPES = Path("frontend/src/types/index.ts").read_text(encoding="utf-8")


class TestEntryKindFilter:
    """验证来源分层筛选控件。"""

    # 方法作用：验证列表请求携带 entry_kind。
    # Args: self - 测试实例。
    # Returns: 无返回值，断言失败时由 pytest 报告。
    def test_request_carries_entry_kind(self) -> None:
        """默认档位必须是 document。"""
        # Act / Assert
        assert "entry_kind" in SOURCE
        assert "'document'" in SOURCE and "'introspect'" in SOURCE

    # 方法作用：验证切档位时重置页码。
    # Args: self - 测试实例。
    # Returns: 无返回值，断言失败时由 pytest 报告。
    def test_kind_change_resets_page(self) -> None:
        """换档后停留在旧页码会看到空列表。"""
        # Act
        segment = SOURCE.split("entryKind")[1][:800]

        # Assert
        assert "setPage(1)" in segment


class TestSystemKnowledgeToggle:
    """验证「显示系统知识」开关。"""

    # 方法作用：验证开关默认关闭。
    # Args: self - 测试实例。
    # Returns: 无返回值，断言失败时由 pytest 报告。
    def test_toggle_defaults_to_off(self) -> None:
        """默认隐藏系统知识。"""
        # Act / Assert
        assert "useState(false)" in SOURCE
        assert "include_system" in SOURCE

    # 方法作用：验证开关状态不落本地存储。
    # Args: self - 测试实例。
    # Returns: 无返回值，断言失败时由 pytest 报告。
    def test_toggle_not_persisted(self) -> None:
        """刷新页面必须回到默认隐藏。"""
        # Act / Assert
        assert "localStorage" not in SOURCE


class TestOwnerColumn:
    """验证创建人列。"""

    # 方法作用：验证条目表与文档表都有创建人列。
    # Args: self - 测试实例。
    # Returns: 无返回值，断言失败时由 pytest 报告。
    def test_owner_column_present(self) -> None:
        """两个表都要展示创建人。"""
        # Act / Assert
        assert SOURCE.count('t("创建人")') >= 2

    # 方法作用：验证类型定义补齐字段。
    # Args: self - 测试实例。
    # Returns: 无返回值，断言失败时由 pytest 报告。
    def test_types_extended(self) -> None:
        """缺少类型字段会导致 TS 构建失败。"""
        # Act / Assert
        assert TYPES.count("owner_username") >= 2

    # 方法作用：验证无主条目回退文案。
    # Args: self - 测试实例。
    # Returns: 无返回值，断言失败时由 pytest 报告。
    def test_system_fallback_label(self) -> None:
        """系统写入的条目显示「系统」。"""
        # Act / Assert
        assert 't("系统")' in SOURCE


class TestIntrospectReadOnly:
    """验证内省条目只读。"""

    # 方法作用：验证内省条目展示维护说明。
    # Args: self - 测试实例。
    # Returns: 无返回值，断言失败时由 pytest 报告。
    def test_introspect_rows_have_no_delete(self) -> None:
        """后端本就拒绝，前端不得给出可点按钮。"""
        # Act / Assert
        assert 't("随表结构自动维护")' in SOURCE
```

- [ ] **Step 2: 运行测试确认失败**

Run: `.venv/Scripts/python.exe -m pytest tests/test_frontend/test_knowledge_list_filters.py -v`
Expected: 多处 FAIL

- [ ] **Step 3: 实现前端改动**

1. 新增状态：

```tsx
  const [entryKind, setEntryKind] = useState<'document' | 'introspect' | 'all'>('document');
  const [includeSystem, setIncludeSystem] = useState(false);
```

2. `loadEntries` 的查询参数追加：

```tsx
      params.set('entry_kind', entryKind);
      if (includeSystem) params.set('include_system', 'true');
```

3. `loadDocs` 改为：

```tsx
  const loadDocs = async () => {
    try {
      const data = await get<{ docs: KnowledgeDoc[] }>(
        includeSystem ? '/knowledge/docs?include_system=true' : '/knowledge/docs');
      setDocs(data.docs || []);
    } catch { /* empty */ }
  };
```

4. 筛选栏在 `scopeFilterControl` 之后插入：

```tsx
          <Segmented value={entryKind}
            onChange={value => { setEntryKind(value as 'document' | 'introspect' | 'all'); setPage(1); void loadEntries(1); }}
            options={[
              { value: 'document', label: t("文档知识") },
              { value: 'introspect', label: t("Schema 内省") },
              { value: 'all', label: t("全部") },
            ]} />
          <Checkbox checked={includeSystem}
            onChange={event => { setIncludeSystem(event.target.checked); setPage(1); void loadEntries(1); }}>
            {t("显示系统知识")}
          </Checkbox>
```

5. 条目表在「范围」列之后插入：

```tsx
              { title: t("创建人"), dataIndex: 'owner_username', width: 100,
                render: (value: string) => value || t("系统") },
```

6. 文档表同样插入该列。

7. 操作列对内省与系统条目改为说明型：

```tsx
                  r.scope === 'system' || r.source === 'auto_introspect' ? (
                    <Tooltip title={t("随表结构自动维护")}>
                      <Button size="small" disabled icon={<DeleteOutlined />}
                        onClick={e => e.stopPropagation()} />
                    </Tooltip>
                  ) : !r.can_delete ? (
                    /* 既有 disabled 分支 */
                  ) : (
                    /* 既有 Popconfirm 分支 */
                  )
```

8. 两个详情 Modal 各补 `<Descriptions.Item label={t("创建人")}>{... || t("系统")}</Descriptions.Item>`。

9. 从 antd 导入 `Checkbox`。

`frontend/src/types/index.ts`：两个接口各加 `owner_username?: string;`。

- [ ] **Step 4: 补 i18n 词条**

```ts
  "文档知识": "Document knowledge",
  "Schema 内省": "Schema introspection",
  "显示系统知识": "Show platform knowledge",
  "创建人": "Created by",
  "系统": "System",
  "随表结构自动维护": "Maintained with the table schema",
```

（「全部」词条已存在，无需重复添加。）

- [ ] **Step 5: 运行测试确认通过**

Run: `.venv/Scripts/python.exe -m pytest tests/test_frontend/test_knowledge_list_filters.py -v`
Expected: 8 passed

- [ ] **Step 6: 前端构建校验**

Run: `cd frontend && npm run build`
Expected: 构建成功，无 TS 类型错误

---

### Task 8: 文档与功能清单同步

**Files:**
- Modify: `spec/08-security.md:296-306`
- Modify: `spec/23-knowledge-and-data-intelligence-extension-roadmap.md:193-233`
- Modify: `features/06-knowledge.md`
- Modify: `features/07-memory.md`
- Modify: `features/18-frontend.md:96-111`
- Modify: `spec/README.md`

- [ ] **Step 1: 修订 `spec/08` 可见性口径**

把 §7.3.10 三范围表格中 `system` 行改为：

```
| `system` | 默认隐藏；可经管理 API 显式勾选后只读查看；仅超级管理员可管理 | SQL 方言手册、分析方法、数据质量规则、产品文档 |
```

表后补一句：

```
管理 API 的 `include_system` 参数只放宽可见性，不放宽可管理性；`system` 的删除权恒由 `can_manage_knowledge_resource` 判定为 `super_admin` 独占。检索链路（Agent 内部使用）不受该开关影响。
```

- [ ] **Step 2: 澄清 `spec/23` §3.7.1**

表格后补：

```
上表的「读取范围」描述的是 **Agent 检索链路**的召回口径。管理 API（知识库页面）的可见性以 `spec/44` 为准：`system` 默认隐藏、可只读查看、仅超管可管理；`tenant` 与 `private` 的范围与检索链路一致。
```

§3.7.4 补一句：系统目录扫描产出的条目在管理页归入「文档知识」档位。

- [ ] **Step 3: 更新 `features/06-knowledge.md`**

在 6.9 节后新增 6.10 小节：

```markdown
### 6.10 知识库管理页治理（spec/44）

| # | 功能 | 文件 | 描述 | 状态 | 优先级 |
|---|------|------|------|------|--------|
| 6.10.1 | 管理视角可见范围 | `src/knowledge/retrieval.py` | build_admin_visible_filters：超管含 system，非超管默认仅 tenant+private | 待开发 | P1 |
| 6.10.2 | 系统知识只读查看 | `src/api/routes/knowledge.py` | include_system 放宽可见性但不放宽删除权 | 待开发 | P1 |
| 6.10.3 | 来源分层展示 | 同上 | entry_kind：document/introspect/all，长期记忆条目全档位排除 | 待开发 | P1 |
| 6.10.4 | 创建人展示 | 同上 | owner_username 批量映射，只查当前页 | 待开发 | P2 |
| 6.10.5 | 删除权限一致性 | 同上 | 列表 can_delete 与删除接口共用 visibility 默认值 | 待开发 | P1 |
| 6.10.6 | 列表查询缓存 | `src/knowledge/management_cache.py` | 按身份与筛选的 TTL 缓存，写操作按租户失效 | 待开发 | P1 |
| 6.10.7 | 授权判定组合缓存 | `src/api/routes/knowledge.py` | 同组合条目共用一次判定，消除逐条开销 | 待开发 | P1 |
```

开发与测试完成后把这些状态改为「单测完成」。

- [ ] **Step 4: 更新 `features/07-memory.md`**

在 7.5 节后新增：

```markdown
### 7.6 长期记忆管理（spec/44）

| # | 功能 | 文件 | 描述 | 状态 | 优先级 |
|---|------|------|------|------|--------|
| 7.6.1 | list_memories() | `src/memory/long_term_store.py` | PostgreSQL 权威表分页查询，归属强制为当前用户 | 待开发 | P1 |
| 7.6.2 | delete_memory() | 同上 | PG 删除后经补偿队列同步向量库 | 待开发 | P1 |
| 7.6.3 | get_memory() | 同上 | 按 ID 取单条，归属校验失败返回 None | 待开发 | P1 |
| 7.6.4 | 记忆管理 API | `src/api/routes/memory.py` | GET /memory、GET/DELETE /memory/{id}；越权返回 404 | 待开发 | P1 |
```

- [ ] **Step 5: 更新 `features/18-frontend.md`**

在 18.10 节表格后新增（原 18.11 起顺延编号）：

```markdown
### 18.11 我的长期记忆页 (MemoryPage)

| # | 功能 | 文件 | 描述 | 状态 | 优先级 |
|---|------|------|------|------|--------|
| 18.11.1 | 记忆列表 | `frontend/src/pages/MemoryPage.tsx` | 类型筛选 + 关键词搜索 + 分页，展示访问次数与置信度 | 待开发 | P1 |
| 18.11.2 | 记忆详情 | 同上 | Modal 展示完整内容、SQL 与 payload 字段 | 待开发 | P1 |
| 18.11.3 | 删除记忆 | 同上 | Popconfirm 二次确认，删除后刷新 | 待开发 | P1 |
| 18.11.4 | 上传表单交互 | `frontend/src/pages/KnowledgePage.tsx` | 切块与重叠滑块+手动输入、数据源下拉、标签面板多选 | 待开发 | P1 |
| 18.11.5 | 列表来源分层与系统知识开关 | 同上 | entry_kind 分段控件 + include_system 复选框（默认关） | 待开发 | P1 |
```

同时把 18.10.7「删除条目/文档」描述更新为「系统条目与内省条目禁用删除并给出维护说明，用户上传条目按 `can_delete` 控制」。

- [ ] **Step 6: 确认 `spec/README.md` 索引**

确认 44 与 45 两条都在索引表中（44 已在设计阶段加入，45 在本任务加入）。

- [ ] **Step 7: 全量回归**

Run: `.venv/Scripts/python.exe -m pytest tests/ -q -m "not live_llm and not local_llm"`
Expected: 与改动前基线一致，无新增失败

---

## 自审记录

**spec 覆盖检查：**

| spec/44 章节 | 对应任务 |
|--------------|----------|
| §2.1 管理页可见范围 | Task 1（过滤器）、Task 2（文档接口）、Task 7（前端开关） |
| §2.2 来源分层 | Task 1（后端）、Task 7（前端） |
| §2.3 创建人 | Task 2（后端）、Task 7（前端） |
| §2.4 删除权限收口 | Task 1（统一默认值）、Task 3（组合缓存） |
| §3.1 切块与重叠 | Task 6 |
| §3.2 数据源下拉 | Task 6 |
| §3.3 标签多选控件 | Task 6 |
| §4.2 L1 修循环热点 | Task 3 |
| §4.3 L2 服务端缓存 | Task 3 |
| §4.4 暂不实施项 | —（明确不做） |
| §5.2 记忆 API 契约 | Task 4 |
| §5.3 前端页面 | Task 5 |
| §7 测试要求 | Task 1/2/3/4/5/6/7 各自测试文件 |
| §8 验收标准 | Task 3 Step 10、Task 8 Step 7 |
| §9 文档与功能清单更新 | Task 8 |

**已知偏离：** spec/44 §7 列出的 `tests/test_knowledge/test_knowledge_delete_permission.py` 未单列任务——其覆盖内容（列表与删除接口判定一致、缺失 `visibility` 行为一致、内省条目 403）并入 Task 3 的 `_judge_delete_permission` 实现与 `tests/test_knowledge/test_list_performance.py` 的断言，避免为同一段逻辑维护两个测试文件。

**类型一致性：** `build_admin_visible_filters`（Task 1 定义/使用）；`_entry_kind_filters`（Task 1）；`_load_usernames` / `_normalize_owner_username`（Task 2）；`_judge_delete_permission`（Task 3）；`ManagementListCache` / `get_management_list_cache`（Task 3）；`list_memories` / `delete_memory` / `get_memory`（Task 4）；`_serialize_memory`（Task 4）。命名前后一致。

**待实现时决策点（已知，不影响任务拆分）：** Task 4 的 `get_memory` 端点应通过在 `LongTermMemoryStore` 增加 `get_memory(memory_id) -> dict | None` 按 ID 直查实现，不得复用 `list_memories` 的 `search` 参数（后者匹配的是 `content` 而非 ID）。
