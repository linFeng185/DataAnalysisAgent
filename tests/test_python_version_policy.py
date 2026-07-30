"""Python 运行时精确版本策略契约测试。"""

from __future__ import annotations

import tomllib
from pathlib import Path


class TestPythonVersionPolicy:
    """覆盖项目 Python 3.14.0 版本锁定策略。"""

    # 方法作用：验证包元数据与 Ruff 均严格使用 Python 3.14.0 对应版本。
    # Args: self - pytest 测试类实例。
    # Returns: 无返回值，断言失败时由 pytest 报告。
    def test_project_metadata_locks_python_3_14_0(self) -> None:
        """项目元数据不得允许低于或高于 Python 3.14.0 的解释器。"""
        # Arrange
        project = tomllib.loads(Path("pyproject.toml").read_text(encoding="utf-8"))

        # Act
        required_python = project["project"]["requires-python"]
        ruff_target = project["tool"]["ruff"]["target-version"]

        # Assert
        assert required_python == "==3.14.0"
        assert ruff_target == "py314"

    # 方法作用：验证本地、CI 与容器入口使用同一个精确 Python 版本。
    # Args: self - pytest 测试类实例。
    # Returns: 无返回值，断言失败时由 pytest 报告。
    def test_runtime_entrypoints_lock_python_3_14_0(self) -> None:
        """开发、CI 与生产镜像必须统一使用 Python 3.14.0。"""
        # Arrange
        local_version = Path(".python-version").read_text(encoding="utf-8").strip()
        workflow = Path(".github/workflows/test.yml").read_text(encoding="utf-8")
        dockerfile = Path("Dockerfile").read_text(encoding="utf-8").lower()

        # Act / Assert
        assert local_version == "3.14.0"
        assert 'python-version: "3.14.0"' in workflow
        assert dockerfile.count("from python:3.14.0-slim") == 2

    # 方法作用：验证主要维护文档只声明精确 Python 3.14.0 运行版本。
    # Args: self - pytest 测试类实例。
    # Returns: 无返回值，断言失败时由 pytest 报告。
    def test_maintained_documents_declare_python_3_14_0(self) -> None:
        """项目说明、规范与功能清单不得继续声明旧版或宽松版本范围。"""
        # Arrange
        document_paths = (
            Path("README.md"),
            Path("AGENTS.md"),
            Path("CLAUDE.md"),
            Path("CODE_GUIDE.md"),
            Path("features/01-infrastructure.md"),
            Path("spec/05-tech-stack.md"),
            Path("spec/22-defect-remediation-implementation-plan.md"),
            Path("spec/27-container-deployment.md"),
        )

        # Act
        documents = {
            str(path): path.read_text(encoding="utf-8") for path in document_paths
        }

        # Assert
        for path, content in documents.items():
            assert "Python 3.14.0" in content, f"{path} 未声明 Python 3.14.0"
            assert "Python 3.12" not in content, f"{path} 仍包含旧 Python 版本"
            assert "Python 3.14+" not in content, f"{path} 使用了宽松 Python 版本"
