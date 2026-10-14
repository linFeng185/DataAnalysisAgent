"""中英文固定公开提示的双语契约回归测试。

覆盖功能 35.1（SQL 默认时间补参与公开错误双语映射）与 35.2（统一响应出口
语言及节点串联回归），设计见 spec/60-bilingual-public-messages.md。
只替换确定性系统文案：模型给出的具体解释、SQL、表列名、数据与错误码语义不变。
"""

from __future__ import annotations

import logging
import re
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

# 匹配中日韩统一表意文字，用于断言英文固定文案不混入中文。
_CJK_PATTERN = re.compile(r"[一-鿿]")

# outcome 中已有静态映射的全部已知错误码；漏映射会退化为中文 fallback 被断言捕获。
_KNOWN_ERROR_CODES = [
    "VALIDATION_FAILED",
    "EXPLAIN_FAILED",
    "LLM_UNAVAILABLE",
    "SQL_EXECUTION_FAILED",
    "MCP_AGENT_FAILED",
    "MARKET_RESEARCH_EVIDENCE_MISSING",
    "MULTI_SOURCE_FAILED",
    "MULTI_SOURCE_PARTIAL",
    "STREAM_FAILED",
]

# 已知执行失败类型；未知类型的回退路径由独立用例覆盖。
_KNOWN_FAILURE_TYPES = [
    "rate_limit",
    "security",
    "sql_semantic",
    "transient",
    "configuration",
    "resource_limit",
    "llm_unavailable",
]


# 方法作用：隔离真实历史存储，避免测试落库并保留调用计数。
# Args: monkeypatch - pytest 补丁工具。
# Returns: 记录 add 调用的替身历史存储。
def _isolate_history_store(monkeypatch: Any) -> SimpleNamespace:
    """build_response 历史写入只依赖 add 协程，替身不影响被测链路。"""
    history_store = SimpleNamespace(add=AsyncMock())
    monkeypatch.setattr("src.memory.history_store.get_history_store", lambda: history_store)
    return history_store


class TestOutcomeBilingualMessages:
    """覆盖功能 35.1：公开错误与默认补参提示的双语映射。"""

    # 方法作用：验证不传语言参数时保持既有中文语义与旧调用签名兼容。
    # Args: self - 测试实例。
    # Returns: 无返回值，断言失败时由 pytest 报告。
    def test_default_calls_stay_chinese(self) -> None:
        """默认调用必须保持旧契约中的中文文案与默认 fallback。"""
        # Arrange
        from src.graph.outcome import public_error_message, public_failure_reason

        # Act / Assert
        assert public_error_message("VALIDATION_FAILED") == "查询未通过安全校验"
        assert public_error_message("PRIVATE_DETAIL") == "处理失败"
        assert public_failure_reason("SQL_EXECUTION_FAILED", "transient") == "数据源暂时不可用或查询超时"
        assert public_failure_reason("SQL_EXECUTION_FAILED", "configuration") == "数据源配置、认证或连接失败"

    # 方法作用：验证全部已知错误码在 en-US 下都有对应英文映射而非中文 fallback。
    # Args: self - 测试实例。
    # Returns: 无返回值，断言失败时由 pytest 报告。
    def test_english_error_codes_mirror_chinese(self) -> None:
        """漏映射的错误码会退化为中文 fallback，被无中文断言捕获。"""
        # Arrange
        from src.graph.outcome import public_error_message

        # Act / Assert
        for code in _KNOWN_ERROR_CODES:
            english = public_error_message(code, language="en-US")
            assert english, code
            assert not _CJK_PATTERN.search(english), code

    # 方法作用：验证全部已知失败类型在 en-US 下都有对应英文映射。
    # Args: self - 测试实例。
    # Returns: 无返回值，断言失败时由 pytest 报告。
    def test_english_failure_types_mirror_chinese(self) -> None:
        """LLM 不可用类型复用错误码映射，其余类型各有独立英文文案。"""
        # Arrange
        from src.graph.outcome import public_error_message, public_failure_reason

        # Act / Assert
        for failure_type in _KNOWN_FAILURE_TYPES:
            english = public_failure_reason("SQL_EXECUTION_FAILED", failure_type, language="en-US")
            assert english, failure_type
            assert not _CJK_PATTERN.search(english), failure_type
        llm_english = public_failure_reason("LLM_UNAVAILABLE", "llm_unavailable", language="en-US")
        assert llm_english == public_error_message("LLM_UNAVAILABLE", language="en-US")

    # 方法作用：验证未知错误码继续使用调用方提供的 fallback，不做任何翻译。
    # Args: self - 测试实例。
    # Returns: 无返回值，断言失败时由 pytest 报告。
    def test_unknown_error_keeps_caller_fallback(self) -> None:
        """未知错误不得被硬编码英文替换，调用方 fallback 原样返回。"""
        # Arrange
        from src.graph.outcome import public_error_message, public_failure_reason

        # Act / Assert
        assert public_error_message("PRIVATE_DETAIL", fallback="处理失败", language="en-US") == "处理失败"
        assert public_error_message("PRIVATE_DETAIL", fallback="Custom fallback", language="en-US") == "Custom fallback"
        # 未知失败类型回退到同语言错误码映射，而不是退回中文默认
        unknown_type = public_failure_reason("SQL_EXECUTION_FAILED", "no_such_type", language="en-US")
        assert unknown_type == public_error_message("SQL_EXECUTION_FAILED", language="en-US")
        assert not _CJK_PATTERN.search(unknown_type)

    # 方法作用：验证非中英语言暂用中文默认，不强行输出英文。
    # Args: self - 测试实例。
    # Returns: 无返回值，断言失败时由 pytest 报告。
    def test_non_english_languages_fall_back_to_chinese(self) -> None:
        """ja-JP 等语言尚未本地化，按 spec/60 保留既有中文默认。"""
        # Arrange
        from src.graph.outcome import (
            public_error_message,
            public_failure_reason,
            public_prompt_message,
        )

        # Act / Assert
        assert public_error_message("VALIDATION_FAILED", language="ja-JP") == "查询未通过安全校验"
        assert public_failure_reason("SQL_EXECUTION_FAILED", "transient", language="ja-JP") == "数据源暂时不可用或查询超时"
        assert public_prompt_message("needs_input", "ja-JP") == "请补充具体业务目标或目标表后重试。"
        assert public_error_message("VALIDATION_FAILED", language="") == "查询未通过安全校验"

    # 方法作用：验证语言匹配只命中规范化后的 en 或 en- 前缀，enm 等独立代码保持中文默认。
    # Args: self - 测试实例。
    # Returns: 无返回值，断言失败时由 pytest 报告。
    def test_english_match_requires_en_or_en_dash_prefix(self) -> None:
        """enm 是独立语言代码，共享 en 字面前缀但不能套用英文固定文案。"""
        # Arrange
        from src.graph.outcome import (
            public_error_message,
            public_failure_reason,
            public_prompt_message,
        )
        chinese_error = public_error_message("VALIDATION_FAILED")
        chinese_reason = public_failure_reason("SQL_EXECUTION_FAILED", "transient")
        chinese_prompt = public_prompt_message("time_range")

        # Act / Assert：en 与 en-* 在大小写、空白归一化后命中英文
        for language in ("en", "en-US", "EN-us", " en-US "):
            assert public_error_message("VALIDATION_FAILED", language=language) == (
                "The query did not pass the security validation"
            )
            assert not _CJK_PATTERN.search(public_failure_reason("SQL_EXECUTION_FAILED", "transient", language=language))
            assert not _CJK_PATTERN.search(public_prompt_message("time_range", language=language))

        # 三个公开 helper 对 enm 等非 en/en-* 值必须逐字保持中文默认
        for language in ("enm", "english", "en_US"):
            assert public_error_message("VALIDATION_FAILED", language=language) == chinese_error
            assert public_failure_reason("SQL_EXECUTION_FAILED", "transient", language=language) == chinese_reason
            assert public_prompt_message("time_range", language=language) == chinese_prompt

    # 方法作用：验证补参固定提示默认中文且按 en/en-* 前缀切换英文。
    # Args: self - 测试实例。
    # Returns: 无返回值，断言失败时由 pytest 报告。
    def test_prompt_messages_default_chinese_and_english(self) -> None:
        """生成节点与响应出口的时间补参默认值不同，中文语义各自保持。"""
        # Arrange
        from src.graph.outcome import public_prompt_message

        # Act / Assert
        assert public_prompt_message("time_range") == "请指定查询的时间范围（最近一周/一月/一年/两年/三年/五年 或 全部数据）"
        assert public_prompt_message("time_range_response") == "请指定查询的时间范围（最近一周/一月/一年/两年/三年/五年）"
        assert public_prompt_message("needs_input") == "请补充具体业务目标或目标表后重试。"
        for language in ("en", "en-US", "en-GB", "EN-us"):
            for kind in ("time_range", "time_range_response", "needs_input"):
                english = public_prompt_message(kind, language)
                assert english and not _CJK_PATTERN.search(english), (kind, language)

    # 方法作用：验证高频重复调用公开 helper 输出稳定且不产生任何日志。
    # Args: self - 测试实例；caplog - pytest 日志捕获工具。
    # Returns: 无返回值，断言失败时由 pytest 报告。
    def test_repeated_helper_calls_are_stable_and_silent(self, caplog: pytest.LogCaptureFixture) -> None:
        """公开消息 helper 处于请求主路径，禁止新增日志与状态漂移。"""
        # Arrange
        from src.graph import outcome

        # Act
        with caplog.at_level(logging.DEBUG):
            first = outcome.public_error_message("VALIDATION_FAILED", language="en-US")
            for _ in range(200):
                assert outcome.public_error_message("VALIDATION_FAILED", language="en-US") == first
                assert outcome.public_failure_reason("SQL_EXECUTION_FAILED", "transient", language="en-US")
                assert outcome.public_prompt_message("needs_input", "en-US")

        # Assert
        assert not [record for record in caplog.records if record.name.startswith("src.graph.outcome")]


class TestBilingualNodeChain:
    """覆盖功能 35.2：真实节点串联、语言兜底与响应契约不变量。"""

    # 方法作用：验证英文时间补参经真实生成与响应节点全程保持英文且状态一致。
    # Args: self - 测试实例；monkeypatch - 补丁工具。
    # Returns: 无返回值，断言失败时由 pytest 报告。
    async def test_english_time_prompt_crosses_real_nodes(self, monkeypatch: Any) -> None:
        """结构化时间补参不调用模型，出口 summary 与生成节点解释逐字一致。"""
        # Arrange
        from src.graph.contracts import build_task_plan
        from src.graph.nodes.build_response import build_response_node
        from src.graph.nodes.generate_sql import generate_sql_node

        _isolate_history_store(monkeypatch)
        state = {
            "user_query": "Show total sales",
            "task_plan": build_task_plan("query", query="Show total sales").model_dump(),
        }
        state["task_plan"]["needs_time_range"] = True

        # Act
        generated = await generate_sql_node(state, {})
        result = await build_response_node({**state, **generated})

        # Assert
        assert generated["generated_sql"] == ""
        assert generated["needs_time_range"] is True
        assert generated["input_explanation"] == generated["time_range_explanation"]
        assert generated["time_range_explanation"] and not _CJK_PATTERN.search(generated["time_range_explanation"])
        final = result["final_response"]
        assert final["status"] == "needs_input"
        assert final["display_language"] == "en-US"
        assert final["analysis"]["summary"] == generated["time_range_explanation"]

    # 方法作用：验证中文请求的补参与默认提示保持既有中文语义。
    # Args: self - 测试实例；monkeypatch - 补丁工具。
    # Returns: 无返回值，断言失败时由 pytest 报告。
    async def test_chinese_request_keeps_chinese_defaults(self, monkeypatch: Any) -> None:
        """中文链路不得被双语改动反向污染。"""
        # Arrange
        from src.graph.contracts import build_task_plan
        from src.graph.nodes.build_response import build_response_node
        from src.graph.nodes.generate_sql import generate_sql_node

        _isolate_history_store(monkeypatch)
        state = {
            "user_query": "查询订单总额走势",
            "task_plan": build_task_plan("query", query="查询订单总额走势").model_dump(),
        }
        state["task_plan"]["needs_time_range"] = True

        # Act
        generated = await generate_sql_node(state, {})
        result = await build_response_node({**state, **generated})

        # Assert
        assert generated["time_range_explanation"].startswith("请指定查询的时间范围")
        final = result["final_response"]
        assert final["display_language"] == "zh-CN"
        assert final["analysis"]["summary"] == generated["time_range_explanation"]

    # 方法作用：验证无自然语言请求时按界面语言输出补参，不落回中文硬编码。
    # Args: self - 测试实例；monkeypatch - 补丁工具。
    # Returns: 无返回值，断言失败时由 pytest 报告。
    async def test_interface_language_fallback_without_text(self, monkeypatch: Any) -> None:
        """纯数字请求无法判断语言，界面偏好 en-US 必须生效。"""
        # Arrange
        from src.graph.nodes.build_response import build_response_node

        _isolate_history_store(monkeypatch)

        # Act
        result = await build_response_node({
            "user_query": "987654",
            "needs_time_range": True,
            "display_language": "en-US",
        })

        # Assert
        final = result["final_response"]
        assert final["display_language"] == "en-US"
        assert final["analysis"]["summary"] and not _CJK_PATTERN.search(final["analysis"]["summary"])

    # 方法作用：验证无文字请求按最近历史轮次的语言输出补参。
    # Args: self - 测试实例；monkeypatch - 补丁工具。
    # Returns: 无返回值，断言失败时由 pytest 报告。
    async def test_history_language_fallback(self, monkeypatch: Any) -> None:
        """短追问或参数补充请求要延续上一轮已确认的语言。"""
        # Arrange
        from src.graph.nodes.build_response import build_response_node

        _isolate_history_store(monkeypatch)

        # Act
        result = await build_response_node({
            "user_query": "987654",
            "needs_time_range": True,
            "conversation_history": [{"turn_id": 1, "user_query": "Show total sales"}],
        })

        # Assert
        final = result["final_response"]
        assert final["display_language"] == "en-US"
        assert final["analysis"]["summary"] and not _CJK_PATTERN.search(final["analysis"]["summary"])

    # 方法作用：验证上游给出的具体解释原文不被语言切换替换或翻译。
    # Args: self - 测试实例；monkeypatch - 补丁工具。
    # Returns: 无返回值，断言失败时由 pytest 报告。
    async def test_model_explanation_preserved_verbatim(self, monkeypatch: Any) -> None:
        """具体证据缺口说明是用户补齐参数的唯一线索，必须逐字保留。"""
        # Arrange
        from src.graph.nodes.build_response import build_response_node

        _isolate_history_store(monkeypatch)
        explanation = "无法生成该查询：缺少「地区」字段。"

        # Act
        result = await build_response_node({
            "user_query": "Show total sales",
            "needs_input": True,
            "input_explanation": explanation,
        })

        # Assert
        final = result["final_response"]
        assert final["display_language"] == "en-US"
        assert final["error_message"] == explanation
        assert final["failure_reason"] == explanation
        assert final["analysis"]["summary"] == explanation

    # 方法作用：验证模型未给出解释时的默认输入提示按展示语言输出。
    # Args: self - 测试实例；monkeypatch - 补丁工具。
    # Returns: 无返回值，断言失败时由 pytest 报告。
    async def test_generate_sql_default_input_prompt_follows_language(self, monkeypatch: Any) -> None:
        """模型返回空 SQL 且无解释时，补参提示必须是英文默认文案。"""
        # Arrange
        import src.graph.nodes.generate_sql as generate_sql_module
        from src.graph.contracts import build_task_plan

        monkeypatch.setattr(generate_sql_module, "is_llm_available", lambda: True)

        async def _empty_generate(**kwargs: Any) -> tuple[str, str, str]:
            del kwargs
            return "", "", ""

        monkeypatch.setattr(generate_sql_module, "_llm_generate", _empty_generate)
        state = {
            "user_query": "Show total sales",
            "task_plan": build_task_plan("query", query="Show total sales").model_dump(),
        }

        # Act
        result = await generate_sql_module.generate_sql_node(state, {})

        # Assert
        assert result["needs_input"] is True
        assert result["input_explanation"] and not _CJK_PATTERN.search(result["input_explanation"])

    # 方法作用：验证英文已知错误只切换文案，错误码、状态与成功标志保持原语义。
    # Args: self - 测试实例；monkeypatch - 补丁工具。
    # Returns: 无返回值，断言失败时由 pytest 报告。
    async def test_error_status_and_code_unchanged_in_english(self, monkeypatch: Any) -> None:
        """安全校验失败在英文下仍是 VALIDATION_FAILED / failed。"""
        # Arrange
        from src.graph.nodes.build_response import build_response_node

        _isolate_history_store(monkeypatch)

        # Act
        result = await build_response_node({
            "user_query": "Show total sales",
            "validation_errors": ["secret-connection-password"],
        })

        # Assert
        final = result["final_response"]
        assert final["error_code"] == "VALIDATION_FAILED"
        assert final["status"] == "failed"
        assert final["success"] is False
        assert final["display_language"] == "en-US"
        assert final["error_message"] and not _CJK_PATTERN.search(final["error_message"])

    # 方法作用：验证执行失败原因按语言输出且公共响应不携带执行异常正文。
    # Args: self - 测试实例；monkeypatch - 补丁工具。
    # Returns: 无返回值，断言失败时由 pytest 报告。
    async def test_execution_failure_reason_is_english_and_redacted(self, monkeypatch: Any) -> None:
        """连接细节只能留在服务端日志，公开原因只保留安全分类。"""
        # Arrange
        from src.graph.nodes.build_response import build_response_node

        _isolate_history_store(monkeypatch)

        # Act
        result = await build_response_node({
            "user_query": "Show total sales",
            "execution_error": "secret-connection-password",
            "execution_error_type": "transient",
        })

        # Assert
        final = result["final_response"]
        assert final["error_code"] == "SQL_EXECUTION_FAILED"
        assert final["status"] == "failed"
        assert final["error_message"] and not _CJK_PATTERN.search(final["error_message"])
        assert final["failure_reason"] == final["error_message"]
        assert "secret-connection-password" not in final["error_message"]
        assert "secret-connection-password" not in final["failure_reason"]

    # 方法作用：验证补参与响应日志只保留长度等元信息，不回显解释正文。
    # Args: self - 测试实例；monkeypatch - 补丁工具。
    # Returns: 无返回值，断言失败时由 pytest 报告。
    async def test_prompt_logs_keep_only_metadata(self, monkeypatch: Any) -> None:
        """解释正文可能含业务数据，日志只能记录字符数等元信息。"""
        # Arrange
        import src.graph.nodes.build_response as build_response_module

        _isolate_history_store(monkeypatch)
        captured_logger = MagicMock()
        monkeypatch.setattr(build_response_module, "logger", captured_logger)
        marker = "SECRET-EXPLANATION-BODY"

        # Act
        result = await build_response_module.build_response_node({
            "user_query": "Show total sales",
            "needs_time_range": True,
            "time_range_explanation": f"具体解释-{marker}",
        })

        # Assert
        assert result["final_response"]["analysis"]["summary"] == f"具体解释-{marker}"
        for call in captured_logger.info.call_args_list:
            assert marker not in str(call)
