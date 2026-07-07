"""Batch 2: 路由 + 意图分类 + llm_direct_answer 测试。"""

import pytest


# ── route_by_intent ──

def test_route_file_analysis():
    from src.graph.workflow import route_by_intent
    assert route_by_intent({"intent": "file_analysis"}) == "mcp_agent"

def test_route_metadata():
    from src.graph.workflow import route_by_intent
    assert route_by_intent({"intent": "metadata"}) == "llm_direct_answer"

def test_route_chat():
    from src.graph.workflow import route_by_intent
    assert route_by_intent({"intent": "chat"}) == "llm_direct_answer"

def test_route_sql_default():
    from src.graph.workflow import route_by_intent
    for i in ("query", "aggregation", "trend", "attribution", ""):
        assert route_by_intent({"intent": i}) == "retrieve_schema", f"intent={i}"


# ── classify_intent ──

async def _intent(query: str) -> str:
    from src.graph.nodes.classify_intent import classify_intent_node
    r = await classify_intent_node({"user_query": query, "relevant_tables": []})
    return r["intent"]

@pytest.mark.asyncio
async def test_intent_metadata():
    for q in ["有哪些表", "orders 有哪些字段", "什么是 schema", "DATE_FORMAT怎么用"]:
        assert await _intent(q) == "metadata", f"'{q}'"

@pytest.mark.asyncio
async def test_intent_chat():
    for q in ["你好", "你能做什么", "谢谢"]:
        assert await _intent(q) == "chat", f"'{q}'"

@pytest.mark.asyncio
async def test_intent_query():
    for q in ["查一下销售额", "统计订单数", "列出所有用户"]:
        i = await _intent(q)
        assert i in ("query", "aggregation", "attribution", "trend"), f"'{q}'→{i}"


# ── llm_direct_answer ──

@pytest.mark.asyncio
async def test_llm_answer_fallback():
    from src.graph.nodes.llm_answer import llm_direct_answer_node
    r = await llm_direct_answer_node({"intent": "chat", "user_query": "你好",
        "long_term_memories_text": ""})
    fr = r["final_response"]
    assert fr["source"] == "llm_direct"
    assert fr["success"]
    assert len(fr["analysis"]["summary"]) > 5

@pytest.mark.asyncio
async def test_llm_answer_metadata():
    from src.graph.nodes.llm_answer import llm_direct_answer_node
    r = await llm_direct_answer_node({"intent": "metadata", "user_query": "有哪些表",
        "long_term_memories_text": "orders: 订单表; users: 用户表"})
    assert "orders" in r["final_response"]["analysis"]["summary"]
