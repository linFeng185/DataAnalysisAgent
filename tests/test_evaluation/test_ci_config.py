"""持续集成与 Ruff 配置契约测试。"""

from __future__ import annotations

import tomllib
from pathlib import Path


class TestContinuousIntegrationConfig:
    """覆盖功能 1.1.8、15.6 的本地与 CI 静态检查一致性。"""

    # 方法作用：验证 CI 执行的质量门禁与本地配置保持一致。
    # Args: self - pytest 测试类实例。
    # Returns: 无返回值，断言失败时由 pytest 报告。
    def test_ci_runs_ruff_offline_evaluation_and_non_live_tests(self) -> None:
        """CI 必须执行 Ruff、离线评测并排除显式外部验收用例。"""
        # Arrange
        workflow = Path(".github/workflows/test.yml").read_text(encoding="utf-8")

        # Act
        project = tomllib.loads(Path("pyproject.toml").read_text(encoding="utf-8"))
        ruff = project["tool"]["ruff"]
        lint = ruff["lint"]

        # Assert
        assert ruff["target-version"] == "py314"
        assert 'python-version: "3.14.0"' in workflow
        assert {"E4", "E7", "E9", "F", "B", "ASYNC"}.issubset(set(lint["select"]))
        assert "ruff check src tests" in workflow
        assert "tests.evaluators.run_eval --offline" in workflow
        assert "not live_llm and not live_datasource" in workflow
