"""离线图评测与显式 LangSmith 批量评测入口。"""

from __future__ import annotations

import argparse
import os
from pathlib import Path
from typing import Any, Awaitable, Callable

from tests.evaluators.graph_regression import evaluate_graph_benchmark, load_graph_benchmark


async def run_langsmith_evaluation(
    target: Callable[[dict[str, Any]], Awaitable[dict[str, Any]]],
    *,
    dataset_name: str,
    experiment_prefix: str = "data-analysis-agent",
) -> Any:
    """仅在显式环境开关下运行 LangSmith Dataset 生产评测。"""
    if os.getenv("RUN_LANGSMITH_EVALS") != "1":
        raise RuntimeError("LangSmith 评测需要 RUN_LANGSMITH_EVALS=1")
    from langsmith import aevaluate

    return await aevaluate(
        target,
        data=dataset_name,
        evaluators=[_langsmith_graph_evaluator],
        max_concurrency=4,
        experiment_prefix=experiment_prefix,
        metadata={"evaluation": "graph-regression", "dataset": dataset_name},
    )


def _langsmith_graph_evaluator(run: Any, example: Any) -> dict[str, Any]:
    """按响应成功状态和统一 Artifact 存在性评分。"""
    outputs = getattr(run, "outputs", {}) or {}
    response = outputs.get("final_response", outputs) if isinstance(outputs, dict) else {}
    score = float(bool(response.get("success")) and bool(response.get("artifact")))
    return {"key": "graph_contract", "score": score}


def run_offline(path: str | Path) -> dict[str, Any]:
    """运行无网络、无模型依赖的固定图回归评测。"""
    return evaluate_graph_benchmark(load_graph_benchmark(path))


def main() -> int:
    """命令行运行离线评测并以退出码反馈阈值。"""
    parser = argparse.ArgumentParser(description="运行 DataAnalysisAgent 离线评测")
    parser.add_argument(
        "--benchmark",
        default="tests/fixtures/graph_benchmark.json",
        help="图回归 JSON 文件",
    )
    parser.add_argument("--offline", action="store_true", help="运行离线固定评测")
    args = parser.parse_args()
    if not args.offline:
        parser.error("当前命令行仅允许显式 --offline；LangSmith 请通过 Python API 调用")
    result = run_offline(args.benchmark)
    print(
        f"图回归: {result['passed']}/{result['case_count']}，"
        f"通过率 {result['pass_rate']:.2%}"
    )
    return 0 if result["pass_rate"] == 1.0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
