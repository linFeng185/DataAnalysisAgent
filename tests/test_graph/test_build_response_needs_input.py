"""build_response「需要补充输入」分支的响应契约测试。

覆盖缺陷：该分支只把模型的证据缺口说明写进 analysis.summary，没有写
error_message。报表面板读的是 final_response.error_message，取不到就退化成
默认文案「查询失败」，用户看不到「缺哪张表、缺哪个字段」这条唯一可据以补齐参数的线索。
"""

from __future__ import annotations

import pytest

from src.graph.nodes.build_response import build_response_node

# 模型给出的证据缺口说明，是用户补齐参数时唯一可用的线索。
_GAP_EXPLANATION = "无法生成该查询，存在以下证据缺口：缺少「地区」字段。"
_DEFAULT_EXPLANATION = "请补充具体业务目标或目标表后重试。"


class TestGenericPromptResponse:
    """覆盖 18.8.1：需要补充输入的响应必须回传真实原因。"""

    # 方法作用：证据缺口说明必须同时出现在 error_message 与 failure_reason。
    # Args: self - 测试实例。
    # Returns: 断言通过时无返回值。
    @pytest.mark.asyncio
    async def test_gap_explanation_surfaces_as_error_message(self) -> None:
        result = await build_response_node({
            "user_query": "按地区统计订单净销售额",
            "needs_input": True,
            "input_explanation": _GAP_EXPLANATION,
        })

        response = result["final_response"]
        assert response["status"] == "needs_input"
        assert response["error_message"] == _GAP_EXPLANATION
        assert response["failure_reason"] == _GAP_EXPLANATION
        assert response["analysis"]["summary"] == _GAP_EXPLANATION

    # 方法作用：缺少说明时回落到默认文案，不允许留下空字符串。
    # Args: self - 测试实例。
    # Returns: 断言通过时无返回值。
    @pytest.mark.asyncio
    async def test_missing_explanation_falls_back_to_default(self) -> None:
        result = await build_response_node({
            "user_query": "按地区统计订单净销售额",
            "needs_input": True,
        })

        response = result["final_response"]
        assert response["error_message"] == _DEFAULT_EXPLANATION

    # 方法作用：时间范围缺失走独立分支，不能被本改动波及。
    # Args: self - 测试实例。
    # Returns: 断言通过时无返回值。
    @pytest.mark.asyncio
    async def test_time_range_prompt_branch_unaffected(self) -> None:
        result = await build_response_node({
            "user_query": "订单走势",
            "needs_time_range": True,
            "time_range_explanation": "请指定查询的时间范围（最近一周/全部数据）",
        })

        response = result["final_response"]
        assert response["needs_time_range"] is True
        assert response["analysis"]["summary"].startswith("请指定查询的时间范围")


class TestMultiSourceFailureDetail:
    """覆盖多源全部失败时的原因汇总：模型证据缺口不能被写成数据源故障。"""

    # 方法作用：各源的 user_error 必须出现在 error_message 里，且同一原因只保留一份。
    # Args: self - 测试实例。
    # Returns: 断言通过时无返回值。
    @pytest.mark.asyncio
    async def test_user_error_is_surfaced(self) -> None:
        gap = "缺少「地区」字段：retail_orders 无 region_code 列"
        result = await build_response_node({
            "user_query": "广告归因与订单净销售额对比",
            "multi_source_results": [
                {"datasource": "mysql", "success": False, "user_error": gap},
                {"datasource": "ch", "success": False, "user_error": gap},
            ],
        })

        response = result["final_response"]
        assert response["status"] == "failed"
        assert "缺少「地区」字段" in response["error_message"]
        # 同一原因来自两个源时只保留一份
        assert response["error_message"].count("缺少「地区」字段") == 1

    # 方法作用：内部 error 不得回传给用户——它可能含连接串、权限等细节。
    # Args: self - 测试实例。
    # Returns: 断言通过时无返回值。
    @pytest.mark.asyncio
    async def test_internal_error_not_surfaced(self) -> None:
        result = await build_response_node({
            "user_query": "广告归因",
            "multi_source_results": [
                {"datasource": "mysql", "success": False, "error": "ORA-01722: invalid number"},
            ],
        })

        assert "ORA-01722" not in result["final_response"]["error_message"]

    # 方法作用：各源都没带用户可见原因时回退到通用文案，不能给出空错误。
    # Args: self - 测试实例。
    # Returns: 断言通过时无返回值。
    @pytest.mark.asyncio
    async def test_missing_error_falls_back_to_default(self) -> None:
        result = await build_response_node({
            "user_query": "广告归因",
            "multi_source_results": [{"datasource": "mysql", "success": False}],
        })

        assert result["final_response"]["error_message"]
