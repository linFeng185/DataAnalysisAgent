# 16. 架构基础层 — 第 1 批改造

> 包含 VectorStore 抽象、LLM Provider 抽象、用户权限体系。
> 三项共同构成系统的横切基础层，必须在任何新功能之前完成。
> 本文件为开发文档，实现时必须严格参照。

---

## 16.1 VectorStore 抽象层

### 16.1.1 目标

将项目中 43 处直接调用 `chromadb.Collection` API 的代码统一到抽象接口，
未来切换为 pgvector / Milvus 只需新增一个实现类 + 改一行配置。

### 16.1.2 接口定义

```python
# src/memory/vector_store.py

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any

@dataclass
class VectorEntry:
    """向量库中的一条记录。"""
    id: str
    content: str
    metadata: dict[str, Any] = field(default_factory=dict)
    embedding: list[float] | None = None

@dataclass
class VectorSearchResult:
    """一次检索的结果。"""
    id: str
    content: str
    metadata: dict[str, Any]
    score: float   # 0~1，越大越相关

class VectorStore(ABC):
    """向量存储抽象接口。"""

    @abstractmethod
    async def search(
        self, query: str, top_k: int = 5,
        filters: dict[str, str] | None = None,
    ) -> list[VectorSearchResult]:
        """语义向量搜索。filters 为 metadata 精确过滤条件。"""
        ...

    @abstractmethod
    async def get_by_id(self, entry_id: str) -> VectorEntry | None:
        """按 ID 精确获取单条记录。"""
        ...

    @abstractmethod
    async def get_by_filter(
        self, filters: dict[str, str], limit: int = 100,
    ) -> list[VectorEntry]:
        """按 metadata 精确过滤，不涉及向量相似度。"""
        ...

    @abstractmethod
    async def upsert(self, entries: list[VectorEntry]) -> int:
        """批量插入或更新。返回实际写入数。"""
        ...

    @abstractmethod
    async def delete_by_ids(self, ids: list[str]) -> int:
        """按 ID 批量删除。返回实际删除数。"""
        ...

    @abstractmethod
    async def delete_by_filter(self, filters: dict[str, str]) -> int:
        """按 metadata 过滤批量删除。"""
        ...

    @abstractmethod
    async def count(self, filters: dict[str, str] | None = None) -> int:
        """条目总数。"""
        ...

    @abstractmethod
    async def health_check(self) -> bool:
        """连通性检查。"""
        ...
```

### 16.1.3 ChromaDB 适配器（第一个实现）

```python
# src/memory/vector_store_chroma.py

class ChromaVectorStore(VectorStore):
    """封装现有 chromadb.Collection，兼容已有数据。"""

    def __init__(self, collection, embedding_fn=None):
        self._col = collection
        self._embed = embedding_fn

    async def search(self, query, top_k=5, filters=None):
        where = self._build_where(filters)
        if self._embed:
            emb = self._embed(query)
            raw = self._col.query(query_embeddings=[emb], n_results=top_k, where=where)
        else:
            raw = self._col.query(query_texts=[query], n_results=top_k, where=where)
        return self._parse_results(raw)

    async def upsert(self, entries):
        ids, docs, metas, embs = [], [], [], []
        for e in entries:
            ids.append(e.id)
            docs.append(e.content)
            metas.append(e.metadata)
            if e.embedding:
                embs.append(e.embedding)
        kwargs = {"ids": ids, "documents": docs, "metadatas": metas}
        if embs:
            kwargs["embeddings"] = embs
        self._col.upsert(**kwargs)
        return len(ids)

    # get_by_id, get_by_filter, delete_by_ids, delete_by_filter, count, health_check
    # 均直接委托给 self._col 对应方法
```

### 16.1.4 工厂函数 + 配置切换

```python
# src/memory/vector_store.py

from src.config import get_settings

async def get_vector_store() -> VectorStore:
    s = get_settings()
    store_type = s.vector_store_type

    if store_type == "pgvector":
        from src.memory.vector_store_pg import PgVectorStore
        return await PgVectorStore.create(s.database_url)

    if store_type == "milvus":
        from src.memory.vector_store_milvus import MilvusVectorStore
        return await MilvusVectorStore.create(s.milvus_uri)

    # 默认 ChromaDB
    from src.memory.vector_store_chroma import ChromaVectorStore
    from src.knowledge.schema_manager import get_schema_manager
    sm = get_schema_manager()
    sm._ensure_initialized()
    return ChromaVectorStore(sm._collection, sm._embedding_fn)
```

### 16.1.5 现有调用点迁移规则

| 当前调用 | 迁移后 |
|----------|--------|
| `sm._collection.query(query_texts=[q], n_results=n, where=w)` | `store.search(q, top_k=n, filters=w)` |
| `sm._collection.get(where=w)` | `store.get_by_filter(filters=w)` |
| `sm._collection.get(ids=[id])` | `store.get_by_id(id)` |
| `sm._collection.add(ids=ids, documents=docs, metadatas=metas)` | `store.upsert([VectorEntry(id=i, content=d, metadata=m)])` |
| `sm._collection.delete(ids=ids)` | `store.delete_by_ids(ids)` |
| `sm._collection.count()` | `store.count()` |

**影响 8 个文件**：`schema_manager.py`、`business_rules.py`、`upload_manager.py`、`long_term_store.py`、`retrieve_schema.py`、`routes.py`。

### 16.1.6 配置项

```python
# config.py 新增
vector_store_type: str = "chroma"  # chroma | pgvector | milvus
milvus_uri: str = ""
```

---

## 16.2 LLM Provider 抽象层 + 多模型适配

### 16.2.1 目标

当前 `src/llm/adapters/` 已有 `ModelAdapter` 基类和 `SupportedFeatures`。
需扩展为完整的 Provider 模式：统一 `agenerate()` 接口 + 模型能力查询 + 运行时切换。

### 16.2.2 扩展现有 SupportedFeatures

```python
# src/llm/adapters/base.py — 扩展

@dataclass
class SupportedFeatures:
    streaming: bool = True
    reasoning: bool = False
    reasoning_content_in_response: bool = False
    function_calling: bool = True
    json_mode: bool = True
    max_tokens_limit: int = 16384
    context_window: int = 128000       # 新增
    vision: bool = False               # 新增
    default_temperature: float = 0.0   # 新增
```

### 16.2.3 Provider 接口

```python
# src/llm/provider.py

from abc import ABC, abstractmethod

@dataclass
class LLMResponse:
    content: str
    reasoning: str = ""
    finish_reason: str = "stop"
    usage: dict | None = None

@dataclass
class LLMStreamChunk:
    content: str = ""
    reasoning: str = ""

class LLMProvider(ABC):
    @property
    @abstractmethod
    def capabilities(self) -> SupportedFeatures: ...

    @abstractmethod
    async def agenerate(
        self,
        messages: list[dict],
        temperature: float | None = None,
        max_tokens: int | None = None,
        stream: bool = False,
    ) -> LLMResponse: ...

    @abstractmethod
    async def astream(
        self,
        messages: list[dict],
        temperature: float | None = None,
        max_tokens: int | None = None,
    ):
        """流式调用，yield LLMStreamChunk。"""
        ...
```

### 16.2.4 模型注册表

```python
# src/llm/model_registry.py

@dataclass
class ModelInfo:
    model_id: str
    provider: str
    display_name: str
    capabilities: SupportedFeatures
    cost_per_1k_tokens: float = 0.0

class ModelRegistry:
    def __init__(self):
        self._models: dict[str, ModelInfo] = {}

    def register(self, info: ModelInfo):
        self._models[info.model_id] = info

    def list_all(self) -> list[ModelInfo]:
        return list(self._models.values())

    def get(self, model_id: str) -> ModelInfo | None:
        return self._models.get(model_id)

    def get_by_capability(self, **caps) -> list[ModelInfo]:
        """按能力筛选：vision=True, context_window>=100000"""
        return [
            m for m in self._models.values()
            if all(
                getattr(m.capabilities, k) == v if isinstance(v, bool)
                else getattr(m.capabilities, k) >= v
                for k, v in caps.items()
            )
        ]
```

### 16.2.5 OpenAI-Compatible Provider

```python
# src/llm/provider_openai.py

class OpenAIProvider(LLMProvider):
    """适配现有 ReasoningChatOpenAI + ModelAdapter 体系，不做破坏性变更。"""

    def __init__(self, model_id: str, base_url: str, api_key: str):
        self._model_id = model_id
        self._base_url = base_url
        self._api_key = api_key
        self._adapter = get_adapter(model_id)
        self._capabilities = self._adapter.supported_features

    @property
    def capabilities(self):
        return self._capabilities

    async def agenerate(self, messages, temperature=None, max_tokens=None, stream=False):
        s = get_settings()
        llm = ReasoningChatOpenAI(
            model=self._model_id,
            temperature=temperature or s.llm_temperature,
            max_tokens=max_tokens or s.llm_max_tokens,
            api_key=self._api_key or None,
            base_url=self._base_url or None,
            timeout=s.llm_timeout,
            streaming=stream,
            **(self._adapter.get_chat_openai_kwargs()),
        )
        lc_msgs = [self._to_lc_msg(m) for m in messages]
        resp = await llm.ainvoke(lc_msgs)
        return LLMResponse(
            content=resp.content or "",
            reasoning=self._adapter.parse_response(resp).reasoning_content,
        )
```

### 16.2.6 运行时切换 + 降级链

```python
# config.py
llm_fallback_chain: str = ""  # "gpt-4o,claude-sonnet-4-6"

# client.py
def get_provider(model_id: str | None = None) -> LLMProvider:
    s = get_settings()
    mid = model_id or s.llm_model
    info = get_registry().get(mid)
    if not info:
        raise ValueError(f"未知模型: {mid}")
    if info.provider == "openai":
        return OpenAIProvider(mid, s.openai_base_url, s.openai_api_key)
    if info.provider == "anthropic":
        return AnthropicProvider(mid, s.anthropic_api_key or "")
    raise ValueError(f"不支持的 Provider: {info.provider}")
```

### 16.2.7 前端 API

```
GET /api/v1/models
  → {"models": [{id, name, context_window, vision, reasoning}], "default": "deepseek-v4-pro"}

POST /api/v1/models/test
  请求: {"model_id": "gpt-4o", "api_key": "sk-xxx"}
  → {"ok": true, "latency_ms": 234}
```

---

## 16.3 用户体系与权限隔离

### 16.3.1 目标

支持用户登录（JWT），每个请求自动识别 `{user_id, tenant_id, role}`。
所有数据访问按用户/租户过滤，PG RLS 兜底保证不泄露。

### 16.3.2 PG 表结构

```sql
CREATE TABLE tenants (
    id SERIAL PRIMARY KEY, name VARCHAR(128) NOT NULL,
    created_at TIMESTAMPTZ DEFAULT NOW()
);
CREATE TABLE users (
    id SERIAL PRIMARY KEY, username VARCHAR(64) NOT NULL,
    password_hash VARCHAR(256) NOT NULL,
    role VARCHAR(16) NOT NULL DEFAULT 'analyst',
    tenant_id INT NOT NULL REFERENCES tenants(id) DEFAULT 1,
    created_at TIMESTAMPTZ DEFAULT NOW(),
    UNIQUE(username, tenant_id)
);
CREATE TABLE datasource_permissions (
    id SERIAL PRIMARY KEY, datasource_name VARCHAR(64) NOT NULL,
    tenant_id INT NOT NULL REFERENCES tenants(id),
    owner_user_id INT NOT NULL REFERENCES users(id),
    visibility VARCHAR(16) DEFAULT 'private',
    access_level VARCHAR(16) DEFAULT 'read',
    allowed_columns TEXT[] DEFAULT '{}',
    row_filter_sql TEXT DEFAULT '',
    created_at TIMESTAMPTZ DEFAULT NOW(),
    UNIQUE(datasource_name, tenant_id)
);
CREATE TABLE query_audit_log (
    id SERIAL PRIMARY KEY, user_id INT NOT NULL, tenant_id INT NOT NULL,
    datasource VARCHAR(64), sql_hash VARCHAR(64),
    row_count INT DEFAULT 0, duration_ms INT DEFAULT 0,
    success BOOLEAN DEFAULT TRUE, error_message TEXT DEFAULT '',
    created_at TIMESTAMPTZ DEFAULT NOW()
);
```

### 16.3.3 RLS 策略

```sql
ALTER TABLE sessions ENABLE ROW LEVEL SECURITY;
ALTER TABLE query_history ENABLE ROW LEVEL SECURITY;

CREATE POLICY tenant_isolation ON sessions
    FOR ALL USING (tenant_id =
        NULLIF(current_setting('app.current_tenant_id', true), '')::int);
CREATE POLICY user_isolation ON sessions
    FOR ALL USING (user_id =
        NULLIF(current_setting('app.current_user_id', true), '')::int);
```

### 16.3.4 JWT 中间件

```python
# src/api/auth.py

from contextvars import ContextVar
from fastapi import Request, HTTPException
from starlette.middleware.base import BaseHTTPMiddleware
import jwt, os

_current_user_id: ContextVar[int] = ContextVar("current_user_id", default=0)
_current_tenant_id: ContextVar[int] = ContextVar("current_tenant_id", default=1)
_current_role: ContextVar[str] = ContextVar("current_role", default="anonymous")

JWT_SECRET = os.getenv("JWT_SECRET", "dev-secret-change-in-production")
PUBLIC_PATHS = {"/api/v1/health", "/api/v1/auth/login", "/api/v1/auth/register"}

class AuthMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next):
        if request.url.path in PUBLIC_PATHS:
            return await call_next(request)
        token = request.headers.get("Authorization", "").replace("Bearer ", "")
        if not token and get_settings().multi_tenant:
            raise HTTPException(401, "未提供认证令牌")
        if token:
            try:
                payload = jwt.decode(token, JWT_SECRET, algorithms=["HS256"])
                _current_user_id.set(payload["user_id"])
                _current_tenant_id.set(payload["tenant_id"])
                _current_role.set(payload["role"])
            except jwt.PyJWTError:
                raise HTTPException(401, "令牌无效或已过期")
        return await call_next(request)
```

### 16.3.5 API 变更

| 端点 | 变更 |
|------|------|
| `POST /auth/register` | 新增 |
| `POST /auth/login` | 新增，返回 `{access_token, refresh_token}` |
| `POST /auth/refresh` | 新增 |
| `GET /datasources` | 按 `tenant_id` + 权限过滤 |
| `GET /sessions` | 加 `user_id` 过滤 |
| `GET /history` | 加 `user_id` 过滤 |
| `POST /chat` | 校验数据源权限 → 注入 RLS |
| `GET /knowledge` | ChromaDB 条目加 `tenant_id` 过滤 |
| `GET /skills` | 多租户时过滤租户不可见 Skill |

### 16.3.6 多租户开关

```python
# config.py
multi_tenant: bool = False  # MULTI_TENANT=true 开启
```

| multi_tenant | 行为 |
|-------------|------|
| `false` | 所有用户 `tenant_id=1`，不强制登录，现有功能不变 |
| `true` | 必须登录，JWT 校验，RLS 生效 |

---

## 16.4 实现检查清单

### VectorStore 抽象
- [ ] `src/memory/vector_store.py`：VectorEntry, VectorSearchResult, VectorStore ABC
- [ ] `src/memory/vector_store_chroma.py`：ChromaVectorStore
- [ ] `get_vector_store()` 工厂 + 单例
- [ ] `config.py`：vector_store_type
- [ ] 迁移 6 个调用文件
- [ ] 单元测试

### LLM Provider 抽象
- [ ] `src/llm/adapters/base.py`：扩展 SupportedFeatures
- [ ] `src/llm/provider.py`：LLMProvider ABC
- [ ] `src/llm/model_registry.py`：ModelRegistry
- [ ] `src/llm/provider_openai.py`：OpenAIProvider
- [ ] `get_provider()` + 降级链
- [ ] `GET /models` 端点
- [ ] 前端模型选择下拉框
- [ ] 单元测试

### 权限体系
- [ ] PG 表 + RLS
- [ ] JWT 中间件 + ContextVar
- [ ] login/register/refresh 端点
- [ ] TenantAwarePool 连接池
- [ ] 现有 API 加过滤
- [ ] ChromaDB 条目加 tenant_id
- [ ] SkillManager 租户目录扫描
- [ ] 前端登录页 + AuthContext
- [ ] 单租户兼容性测试

---

## 16.5 五轮自辩修正

### 第 1 轮：接口完整性

**发现 1.1 — `_build_where` 和 `_parse_results` 未定义**

`ChromaVectorStore.search()` 调用了 `self._build_where(filters)` 和 `self._parse_results(raw)` 但这两个方法未在文档中定义。
**修正**：在 ChromaVectorStore 中增加这两个私有方法：

```python
def _build_where(self, filters: dict[str, str] | None) -> dict | None:
    if not filters:
        return None
    result = {}
    for k, v in filters.items():
        if k.startswith("not:"):
            result[k[4:]] = {"$ne": v}
        else:
            result[k] = v
    return result

def _parse_results(self, raw: dict) -> list[VectorSearchResult]:
    ids = raw.get("ids", [[]])[0]
    docs = raw.get("documents", [[]])[0]
    metas = raw.get("metadatas", [[]])[0]
    dists = raw.get("distances", [[]])[0]
    results = []
    for i in range(len(ids)):
        score = 1.0 - min(dists[i], 1.0) if i < len(dists) else 0.0
        results.append(VectorSearchResult(
            id=ids[i],
            content=docs[i] if i < len(docs) else "",
            metadata=metas[i] if i < len(metas) else {},
            score=round(score, 4),
        ))
    return results
```

**发现 1.2 — `AnthropicProvider` 仅有引用无实现**

`get_provider()` 中引用了 `AnthropicProvider` 但文档未给出其实现。
**修正**：增加 AnthropicProvider 骨架：

```python
# src/llm/provider_anthropic.py
class AnthropicProvider(LLMProvider):
    def __init__(self, model_id: str, api_key: str):
        self._model_id = model_id
        self._api_key = api_key
        self._capabilities = SupportedFeatures(
            streaming=True, reasoning=True, function_calling=True,
            json_mode=True, max_tokens_limit=8192,
            context_window=200000, vision=True,
        )

    @property
    def capabilities(self):
        return self._capabilities

    async def agenerate(self, messages, temperature=None, max_tokens=None, stream=False):
        from langchain_anthropic import ChatAnthropic
        llm = ChatAnthropic(
            model=self._model_id,
            temperature=temperature or get_settings().llm_temperature,
            max_tokens=max_tokens or get_settings().llm_max_tokens,
            api_key=self._api_key,
            streaming=stream,
        )
        lc_msgs = [self._to_lc_msg(m) for m in messages]
        resp = await llm.ainvoke(lc_msgs)
        return LLMResponse(content=resp.content or "")
```

**发现 1.3 — `_to_lc_msg` 未定义**

OpenAIProvider 和 AnthropicProvider 都使用 `self._to_lc_msg(m)` 将 dict 转为 LangChain Message，但未定义。
**修正**：在 LLMProvider 基类中增加：

```python
@staticmethod
def _to_lc_msg(m: dict):
    from langchain_core.messages import SystemMessage, HumanMessage, AIMessage
    role, content = m.get("role", "user"), m.get("content", "")
    if role == "system": return SystemMessage(content=content)
    if role == "assistant": return AIMessage(content=content)
    return HumanMessage(content=content)
```

**发现 1.4 — 密码哈希算法未指定**

`users.password_hash` 未指定哈希算法。
**修正**：使用 `bcrypt` 通过 `passlib` 库，`password_hash = pwd_context.hash(password)`。

**发现 1.5 — JWT Token 有效期未定义**

**修正**：`access_token` 24h，`refresh_token` 7d。

---

### 第 2 轮：错误处理

**发现 2.1 — upsert 部分失败**

ChromaDB `collection.upsert()` 是原子操作，不会部分失败。但如果 `len(embeddings) != len(ids)` 会报错。
**修正**：在 `upsert()` 中校验：

```python
if embs and len(embs) != len(ids):
    raise ValueError(f"embeddings 数量 ({len(embs)}) 与 entries ({len(ids)}) 不匹配")
```

**发现 2.2 — RLS NULL 绕过风险**

`NULLIF(current_setting('app.current_tenant_id', true), '')::int` 中，如果 `current_setting` 返回空字符串，`NULLIF` 返回 NULL，`NULL::int` 是 NULL，RLS 的 `tenant_id = NULL` 始终为 false → 所有行被拒绝（安全，不会泄露）。
**验证通过**：这是安全的兜底行为。

**发现 2.3 — 连接池 RESET 失败**

`TenantAwarePool.release()` 中如果 `RESET` 失败，连接可能带着上一个请求的 `current_user_id` 归还池。
**修正**：用 try/finally 保证 RESET 始终执行：

```python
async def release(self, conn):
    try:
        await conn.execute("RESET app.current_user_id")
        await conn.execute("RESET app.current_tenant_id")
    finally:
        await self._pool.release(conn)
```

**发现 2.4 — `get_provider` 未处理 API key 为空**

如果 `openai_api_key` 为空字符串，OpenAIProvider 创建时不会报错，到第一次 `agenerate()` 才失败。
**修正**：在 `get_provider()` 中提前校验：

```python
if info.provider == "openai" and not s.openai_api_key:
    raise ValueError(f"模型 {mid} 需要 OPENAI_API_KEY，当前未配置")
```

---

### 第 3 轮：迁移兼容

**发现 3.1 — 现有 `get_llm()` 需保持兼容**

37 处调用 `get_llm()` 的代码不能一次性全部改为 `get_provider().agenerate()`。
**修正**：`get_llm()` 保留，内部改为委托 `get_provider()`：

```python
def get_llm(**kwargs) -> BaseChatModel:
    """保留旧接口，内部委托 Provider。逐步迁移调用方到 get_provider()。"""
    # 兼容旧代码，返回 LangChain BaseChatModel
    provider = get_provider()
    return _LangChainWrapper(provider)  # 包装 Provider → BaseChatModel
```

**发现 3.2 — 现有 ChromaDB 数据无 `tenant_id`**

数据库中所有现有 ChromaDB 条目都没有 `tenant_id` metadata。
**修正**：在单租户模式（`multi_tenant=false`）下，所有 `where` 条件不添加 `tenant_id` 过滤。仅在多租户模式下添加 `{"tenant_id": str(current_tenant_id)}`。

**发现 3.3 — `get_cheap_llm()` 路径丢失**

`context_builder._summarize_turns_llm()` 已改用原生 `aiohttp`（之前修复压缩泄露时改的）。这个路径也需要支持 Provider 切换。
**修正**：在 `config.py` 增加 `context_summary_model`，`context_builder` 读此配置选模型。

---

### 第 4 轮：性能边界

**发现 4.1 — OpenAIProvider 每次 agenerate 都创建新 ChatOpenAI 实例**

LangChain 的 `ChatOpenAI` 内部有 HTTP 连接池，但每次新建实例会丢失复用。
**修正**：缓存 `ChatOpenAI` 实例在 Provider 级别：

```python
class OpenAIProvider(LLMProvider):
    def __init__(self, ...):
        self._llm = None  # 延迟创建

    def _get_llm(self, temperature, max_tokens, stream):
        if self._llm is None:
            self._llm = ReasoningChatOpenAI(...)
        self._llm.temperature = temperature or ...
        self._llm.max_tokens = max_tokens or ...
        self._llm.streaming = stream
        return self._llm
```

**发现 4.2 — 语义搜索每次翻查整个 collection**

ChromaDB `collection.query()` 无内置缓存，大量知识库条目时（>10 万）搜索延迟显著。
**修正**：`ChromaVectorStore.search()` 增加 `top_k` 上限保护：`top_k = min(top_k, 50)`，防止一次性拉取过多结果。

**发现 4.3 — RLS 策略性能**

PG RLS 策略本质是给每个查询追加 WHERE 条件，在有索引的 `tenant_id` 和 `user_id` 列上开销可忽略（<1ms）。
**验证通过**：确保 `sessions.tenant_id`、`query_history.user_id` 有索引即可。

---

### 第 5 轮：遗漏与边界

**发现 5.1 — Token 黑名单/登出未设计**

JWT 本身无状态，用户登出后 Token 仍有效直到过期。
**修正**：如有登出需求，后续通过 Redis 维护 Token 黑名单（`BLACKLIST:{jti}` → TTL = token剩余有效期）。MVP 阶段不做，在第 5 批锦上添花中实现。

**发现 5.2 — 数据迁移脚本**

现有 ChromaDB entries 和 sessions 表无 `tenant_id` 列。
**修正**：启动时执行迁移：
```sql
UPDATE sessions SET user_id = 0 WHERE user_id IS NULL;
ALTER TABLE sessions ADD COLUMN IF NOT EXISTS user_id INT DEFAULT 0;
ALTER TABLE sessions ADD COLUMN IF NOT EXISTS tenant_id INT DEFAULT 1;
```
ChromaDB entries 在单租户模式下不需要 tenant_id。仅在切换到 `multi_tenant=true` 时，通过一次性脚本遍历所有 entries 补 `tenant_id=1`。

**发现 5.3 — 多实例 ContextVar**

ContextVar 是协程级隔离，不是进程级。多个 uvicorn worker 各自有独立的 ContextVar，天然隔离。
多个并发请求在同一进程内通过 asyncio 协程隔离，ContextVar 正确隔离。
**验证通过**：不需额外处理。

**发现 5.4 — ChromaDB 并发写**

ChromaDB PersistentClient 不支持并发写（SQLite 后端单写锁）。
**修正**：不在此次修改范围内。后续如果多用户高并发上传文档，迁移 pgvector（利用 PG 的行级锁）。
`ChromaVectorStore.upsert()` 内部用 `asyncio.Lock` 保护：

```python
class ChromaVectorStore(VectorStore):
    def __init__(self, ...):
        self._write_lock = asyncio.Lock()

    async def upsert(self, entries):
        async with self._write_lock:
            self._col.upsert(...)
```

**发现 5.5 — ContextVar 在 background task 中的行为**

`asyncio.create_task` 创建的 background task 继承创建时的 ContextVar 值。如果 background task 在请求结束后运行，ContextVar 值可能已被清除。
**修正**：在 background task 入口显式传递 `user_id` / `tenant_id` 参数，不依赖 ContextVar。

```python
# 正确：显式传递
asyncio.create_task(log_audit(user_id, tenant_id, ...))

# 错误：依赖 ContextVar（可能已过期）
asyncio.create_task(log_audit(get_current_user_id(), ...))
```

---

## 16.6 修正汇总

| 轮次 | 发现数 | 关键修正 |
|------|--------|---------|
| 1 接口 | 5 | 补全 `_build_where/_parse_results/_to_lc_msg`，增加 AnthropicProvider，密码用 bcrypt |
| 2 错误 | 4 | upsert 校验，连接池 RESET try/finally，API key 空值提前报错 |
| 3 迁移 | 3 | `get_llm()` 保留兼容，单租户不查 tenant_id，context_summary_model 配置 |
| 4 性能 | 3 | ChatOpenAI 实例缓存，top_k ≤ 50，RLS 索引确认 |
| 5 边界 | 5 | Token 黑名单延后，数据迁移脚本，ChromaDB 写锁，background task 传参 |

---

## 16.7 第 6-10 轮自辩（深度实现细节）

### 第 6 轮：数据流与状态管理

**发现 6.1 — VectorStore 单例与 ChromaDB 生命周期冲突**

当前 `SchemaManager._collection` 由 `SchemaManager` 管理（`_ensure_initialized()` 延迟创建）。改为 `get_vector_store()` 单例后，若 `SchemaManager` 被销毁重建（如 uvicorn hot reload），`ChromaVectorStore` 内部引用变成悬空指针。

**修正**：

```python
_store: VectorStore | None = None

async def get_vector_store() -> VectorStore:
    global _store
    if _store is not None:
        try:
            if await _store.health_check():
                return _store
        except Exception:
            _store = None
    _store = await _create_store()
    return _store
```

**发现 6.2 — Provider 缓存导致 streaming 状态污染**

第 4 轮建议缓存 `ChatOpenAI`。但 `streaming` 属性在并发请求中会互相覆盖。`generate_sql` 需 `streaming=True`（SSE），`analyze_result` 也可能 stream，而压缩调用需 `streaming=False`。

**修正**：流式调用每次创建新实例，非流式可复用：

```python
class OpenAIProvider(LLMProvider):
    def _get_llm(self, temperature, max_tokens, stream):
        if stream:
            return ReasoningChatOpenAI(model=self._model_id, temperature=temperature,
                max_tokens=max_tokens, streaming=True, ...)
        if self._cached_llm is None:
            self._cached_llm = ReasoningChatOpenAI(model=self._model_id, streaming=False, ...)
        self._cached_llm.temperature = temperature
        self._cached_llm.max_tokens = max_tokens
        return self._cached_llm
```

**发现 6.3 — Auth ContextVar 在 LangGraph 中传播确认**

LangGraph 每个节点是独立 async 函数，共享同一 asyncio 协程。`ContextVar` 在协程内保持——从 `AuthMiddleware` 设置的 `current_user_id` 在整个请求生命周期内有效。

**验证通过**。但需在 `main.py` lifespan 中加验证。

**发现 6.4 — 连接池 SET 参数方案修正**

SQLAlchemy `Pool.connect` 事件只在**新连接**时触发一次，连接复用时不再触发 → 上一个请求的 tenant 参数残留。

**最终方案**：放弃事件机制，改为在 `execute_sql_node` 执行前显式 `SET`：

```python
async with ds.engine.connect() as conn:
    await conn.execute(sa.text(f"SET app.current_user_id = '{get_current_user_id()}'"))
    await conn.execute(sa.text(f"SET app.current_tenant_id = '{get_current_tenant_id()}'"))
    result = await conn.execute(sa.text(sql))
```

---

### 第 7 轮：跨切面关注

**发现 7.1 — 配置一致性校验**

用户可能设 `vector_store_type="pgvector"` 但未装 pgvector，或 `MULTI_TENANT=true` 但 JWT_SECRET 为默认值。

**修正**：`main.py` 启动时增加 `validate_config()`，输出 warning 但允许启动（不阻塞开发环境）。

**发现 7.2 — 错误码体系**

当前各层返回不一致：`execute_sql` 返回中文字符串，`AuthMiddleware` 返回 HTTPException。前端无法统一处理。

**修正**：定义 `ErrorCode` 枚举：

```python
class ErrorCode(StrEnum):
    AUTH_MISSING = "AUTH_MISSING"
    AUTH_EXPIRED = "AUTH_EXPIRED"
    DATASOURCE_NOT_FOUND = "DATASOURCE_NOT_FOUND"
    DATASOURCE_NO_PERMISSION = "DATASOURCE_NO_PERMISSION"
    SQL_EXECUTION_FAILED = "SQL_EXECUTION_FAILED"
    VECTOR_STORE_UNAVAILABLE = "VECTOR_STORE_UNAVAILABLE"
    LLM_PROVIDER_ERROR = "LLM_PROVIDER_ERROR"
    CONFIG_INVALID = "CONFIG_INVALID"
```

**发现 7.3 — 关键路径延迟日志**

VectorStore.search()、LLMProvider.agenerate() 是每个请求必走的热路径，需要 structlog 延迟日志便于排查。

**修正**：方法入口加 `_start = time.monotonic()`，出口 `logger.debug(... elapsed_ms=...)`。

---

### 第 8 轮：测试策略

**发现 8.1 — FakeVectorStore（内存实现，用于所有 6 个调用方的单元测试）**

**发现 8.2 — FakeLLMProvider（返回预设响应，用于所有调用方的单元测试）**

**发现 8.3 — RLS 测试需 PG 实例**

RLS 策略依赖 `current_setting` + Policy，不能 Mock。需 testcontainers-python 启动 PG 容器验证。

**发现 8.4 — 实现顺序 = 测试顺序**

1. VectorStore 抽象 + FakeVectorStore → 迁移 → 测试
2. LLM Provider + FakeLLMProvider → 迁移 → 测试
3. Auth Tables + Middleware → API 测试 → 集成测试

---

### 第 9 轮：回滚与部署

**发现 9.1 — 配置开关实现零代码回滚**

```python
vector_store_abstract_enabled: bool = False  # 设为 False 回退到直接调 ChromaDB
```

**发现 9.2 — SQL 迁移脚本独立于代码部署**

四张新表 + 两张现有表加列的全部 SQL 放入 `migrations/001_batch1.sql`，所有操作幂等（`IF NOT EXISTS` / `ON CONFLICT DO NOTHING`）。

**发现 9.3 — ChromaDB 数据迁移延后**

现有 54 个 ChromaDB 条目无 `tenant_id`，单租户模式无需迁移。仅在首次开 `multi_tenant=true` 时执行一次性脚本。

---

### 第 10 轮：后续批次衔接

**发现 10.1 — VectorStore 抽象直接支撑第 2 批知识库增强**

`store.upsert()` 接收用户 DDL 文档 → `store.search()` 匹配表结构 → 无需改动。

**发现 10.2 — Provider 能力查询直接支撑第 2 批 llm_direct_answer**

`capabilities.context_window` 决定注入多少知识库内容。

**发现 10.3 — refresh token 延后到第 5 批**

第 1 批仅实现 login + register。前端 401 → 跳登录页。

**发现 10.4 — 第 2 批全部条件满足，可立即开工**

| 第 2 批功能 | 依赖第 1 批 | 条件 |
|------------|-----------|------|
| 4.1.10 llm_direct_answer | Provider | ✅ |
| 19.16 知识库增强 | VectorStore | ✅ |
| 19.13 非查询式回答 | 两者 | ✅ |

---

### 第 6-10 轮修正汇总

| 轮次 | 发现数 | 关键修正 |
|------|--------|---------|
| 6 数据流 | 4 | lazy init+health_check，流式不缓存，放弃连接池事件改显式 SET，ContextVar 传播确认 |
| 7 跨切面 | 3 | 配置预检 validator，ErrorCode 枚举，关键路径延迟日志 |
| 8 测试 | 4 | FakeVectorStore + FakeLLMProvider，RLS 用 testcontainers，实现顺序=测试顺序 |
| 9 部署 | 3 | 配置开关零代码回滚，独立 SQL 迁移脚本，ChromaDB 迁移延后 |
| 10 衔接 | 4 | 第 2 批全部条件满足，refresh token 延后，动态模型测试补全 |
