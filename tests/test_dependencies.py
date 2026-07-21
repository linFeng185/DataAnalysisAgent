"""项目依赖单一来源回归测试。"""

from __future__ import annotations

import logging
import re
import tomllib
from pathlib import Path


logger = logging.getLogger(__name__)
ROOT = Path(__file__).resolve().parents[1]


# 方法作用：从依赖规格中提取规范化包名。
# Args: requirement - PEP 508 依赖规格。
# Returns: 小写规范化包名。
def _package_name(requirement: str) -> str:
    """忽略 extras 和版本约束，仅比较依赖归属。"""
    logger.debug("_package_name 入口", extra={"requirement": requirement})
    result = re.split(r"[<>=!~\[]", requirement, maxsplit=1)[0].strip().lower()
    logger.info("_package_name 完成", extra={"package": result})
    return result


class TestDependencies:
    """覆盖功能 1.1.1、1.1.5：pyproject 是唯一人工维护来源。"""

    # 方法作用：验证 requirements 中没有 pyproject 未声明的依赖或重复项。
    # Args: self - pytest 测试类实例。
    # Returns: 无返回值，断言失败时由 pytest 报告。
    def test_requirements_are_generated_from_pyproject(self) -> None:
        """部署依赖必须能追溯到 project 或 optional-dependencies。"""
        logger.debug("test_requirements_are_generated_from_pyproject 入口")
        pyproject = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))
        project = pyproject["project"]
        declared = list(project["dependencies"])
        for group in project.get("optional-dependencies", {}).values():
            declared.extend(group)
        declared_names = {_package_name(item) for item in declared}

        lines = (ROOT / "requirements.txt").read_text(encoding="utf-8").splitlines()
        requirements = [
            line.strip()
            for line in lines
            if line.strip() and not line.lstrip().startswith("#")
        ]
        names = [_package_name(item) for item in requirements]

        assert lines[0] == "# 由 pyproject.toml 生成，请勿手工维护"
        assert len(names) == len(set(names))
        assert set(names).issubset(declared_names)
        logger.info("test_requirements_are_generated_from_pyproject 完成")
