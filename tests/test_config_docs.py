"""配置模板文档守卫：每个参数必须带用途、默认值与生效范围注释。

覆盖两个对外模板文件：
- config/app.example.yaml：顶层键逐项注释；api_access.bootstrap_policies 的
  子项属于结构化数据（每项固定七字段），按结构豁免逐条注释。
- .env.example：每个未被注释的 KEY 上方必须有注释行，注释需说明用途与默认值。

为什么需要这个测试：模板一旦漏注释，新人只能靠读 src/config.py 猜语义，
而「默认值」和「是否热生效」这两类信息在代码里分散在多个模块，最容易漏改。
"""

from __future__ import annotations

import logging
import re
from pathlib import Path

import pytest
import yaml

logger = logging.getLogger(__name__)

_PROJECT_ROOT = Path(__file__).resolve().parents[1]
_APP_EXAMPLE = _PROJECT_ROOT / "config" / "app.example.yaml"
_ENV_EXAMPLE = _PROJECT_ROOT / ".env.example"
# 顶层键：行首无缩进，形如 `key:`；api_access 等嵌套子项因有缩进天然不入集合。
_YAML_TOP_LEVEL_KEY = re.compile(r"^([A-Za-z_][A-Za-z0-9_]*):")
# 环境变量：行首无缩进，形如 `KEY=value`。
_ENV_KEY = re.compile(r"^([A-Za-z_][A-Za-z0-9_]*)=(.*)$")
# 三要素关键字：注释必须同时说明用途与默认值，避免只写半句。
_REQUIRED_COMMENT_MARKERS = ("用途", "默认值")


# 方法作用：读取文件所有行，供逐行定位注释使用。
# Args: path - 目标文件路径。
# Returns: 去掉换行符的文本行列表。
def _read_lines(path: Path) -> list[str]:
    logger.debug("配置模板读取入口", extra={"path": str(path)})
    lines = path.read_text(encoding="utf-8").splitlines()
    logger.info("配置模板读取完成", extra={"path": str(path), "line_count": len(lines)})
    return lines


# 方法作用：取得指定行上方最近的一条非空行，用于判断注释是否紧邻参数。
# Args: lines - 文件全文行；index - 参数所在行下标。
# Returns: 最近非空行文本；上方无非空行时返回空字符串。
def _comment_above(lines: list[str], index: int) -> str:
    for position in range(index - 1, -1, -1):
        if lines[position].strip():
            return lines[position]
    return ""


# 方法作用：判断一行是否为质量达标的参数注释。
# Args: line - 待判定行文本。
# Returns: 是注释且同时写明用途与默认值时返回 True。
def _is_documented_comment(line: str) -> bool:
    stripped = line.strip()
    if not stripped.startswith("#"):
        return False
    body = stripped.lstrip("#").strip()
    return all(marker in body for marker in _REQUIRED_COMMENT_MARKERS)


# 方法作用：按正则收集文件中符合行首格式的键及其行号。
# Args: lines - 文件全文行；pattern - 键匹配正则。
# Returns: 键名到行号的映射。
def _key_line_numbers(lines: list[str], pattern: re.Pattern[str]) -> dict[str, int]:
    return {
        match.group(1): index
        for index, line in enumerate(lines)
        if (match := pattern.match(line))
    }


class TestAppExampleYamlDocs:
    """覆盖 config/app.example.yaml 的逐项注释要求。"""

    # 方法作用：验证 YAML 每个顶层键上方都紧邻一条注释行。
    # Args: self - pytest 测试类实例。
    # Returns: 无返回值，漏注释时断言失败并列出键名。
    def test_app_example_yaml_top_level_keys_have_comments(self) -> None:
        """顶层键缺少注释时，模板复制后无法判断字段含义。"""
        logger.debug("test_app_example_yaml_top_level_keys_have_comments 入口")
        try:
            # Arrange
            lines = _read_lines(_APP_EXAMPLE)
            parsed_keys = set(yaml.safe_load("\n".join(lines)) or {})
            located = _key_line_numbers(lines, _YAML_TOP_LEVEL_KEY)

            # Act
            missing = [
                key
                for key, index in located.items()
                if not _comment_above(lines, index).strip().startswith("#")
            ]

            # Assert
            assert located.keys() == parsed_keys, (
                "行级扫描与 YAML 解析出的顶层键不一致，注释守卫会误判："
                f"仅扫描到={sorted(set(located) - parsed_keys)}，仅解析到={sorted(parsed_keys - set(located))}"
            )
            assert missing == [], f"以下顶层键上方缺少注释行：{sorted(missing)}"
            logger.info(
                "test_app_example_yaml_top_level_keys_have_comments 完成",
                extra={"key_count": len(located)},
            )
        except Exception as exc:
            logger.error(
                "test_app_example_yaml_top_level_keys_have_comments 异常",
                extra={"error": str(exc)},
                exc_info=True,
            )
            raise

    # 方法作用：验证 YAML 顶层键上方注释同时写明用途与默认值。
    # Args: self - pytest 测试类实例。
    # Returns: 无返回值，注释要素不全时断言失败。
    def test_app_example_yaml_comments_cover_purpose_and_default(self) -> None:
        """只写字段名的「注释」对使用者没有价值，必须带用途与默认值。"""
        logger.debug("test_app_example_yaml_comments_cover_purpose_and_default 入口")
        try:
            # Arrange
            lines = _read_lines(_APP_EXAMPLE)
            located = _key_line_numbers(lines, _YAML_TOP_LEVEL_KEY)

            # Act
            incomplete = [
                key
                for key, index in located.items()
                if not _is_documented_comment(_comment_above(lines, index))
            ]

            # Assert
            assert incomplete == [], (
                f"以下顶层键的注释未写明用途/默认值：{sorted(incomplete)}"
            )
            logger.info(
                "test_app_example_yaml_comments_cover_purpose_and_default 完成",
                extra={"key_count": len(located)},
            )
        except Exception as exc:
            logger.error(
                "test_app_example_yaml_comments_cover_purpose_and_default 异常",
                extra={"error": str(exc)},
                exc_info=True,
            )
            raise

    # 方法作用：验证结构化子项的豁免边界，避免守卫误伤启动策略列表。
    # Args: self - pytest 测试类实例。
    # Returns: 无返回值，策略子项被当成顶层键时断言失败。
    def test_app_example_yaml_bootstrap_policies_items_are_exempt(self) -> None:
        """bootstrap_policies 每项固定七字段，属于结构数据而非可调参数。"""
        logger.debug("test_app_example_yaml_bootstrap_policies_items_are_exempt 入口")
        try:
            # Arrange
            lines = _read_lines(_APP_EXAMPLE)
            located = _key_line_numbers(lines, _YAML_TOP_LEVEL_KEY)

            # Act：缩进的子项不应出现在行首键集合中
            indented = {
                line.strip().split(":", 1)[0]
                for line in lines
                if line.startswith((" ", "\t")) and ":" in line
            }
            api_access = (yaml.safe_load("\n".join(lines)) or {}).get("api_access") or {}
            policy_ids = {str(item.get("id")) for item in api_access.get("bootstrap_policies", [])}

            # Assert
            assert policy_ids, "示例未声明任何启动策略，豁免场景已失效"
            assert "bootstrap_policies" not in located, (
                "嵌套的 api_access.bootstrap_policies 被误判为顶层键，豁免规则失效"
            )
            assert policy_ids & set(located) == set(), (
                f"启动策略子项被误判为顶层键：{sorted(policy_ids & set(located))}"
            )
            assert indented & set(located) == set(), (
                f"缩进子项被误判为顶层键：{sorted(indented & set(located))}"
            )
            logger.info(
                "test_app_example_yaml_bootstrap_policies_items_are_exempt 完成",
                extra={"policy_count": len(policy_ids), "indented_key_count": len(indented)},
            )
        except Exception as exc:
            logger.error(
                "test_app_example_yaml_bootstrap_policies_items_are_exempt 异常",
                extra={"error": str(exc)},
                exc_info=True,
            )
            raise


class TestEnvExampleDocs:
    """覆盖 .env.example 的逐项注释要求。"""

    # 方法作用：验证环境变量模板中每个活动 KEY 上方都紧邻一条注释行。
    # Args: self - pytest 测试类实例。
    # Returns: 无返回值，漏注释时断言失败并列出键名。
    def test_env_example_active_keys_have_comments(self) -> None:
        """模板被 install.sh 直接复制为 .env，任何无注释键都会带进真实配置。"""
        logger.debug("test_env_example_active_keys_have_comments 入口")
        try:
            # Arrange
            lines = _read_lines(_ENV_EXAMPLE)
            active = _key_line_numbers(lines, _ENV_KEY)

            # Act
            missing = [
                key
                for key, index in active.items()
                if not _comment_above(lines, index).strip().startswith("#")
            ]

            # Assert
            assert active, ".env.example 未解析出任何活动键，正则或文件结构已变化"
            assert missing == [], f"以下活动键上方缺少注释行：{sorted(missing)}"
            logger.info(
                "test_env_example_active_keys_have_comments 完成",
                extra={"active_key_count": len(active)},
            )
        except Exception as exc:
            logger.error(
                "test_env_example_active_keys_have_comments 异常",
                extra={"error": str(exc)},
                exc_info=True,
            )
            raise

    # 方法作用：验证环境变量模板注释写明用途与默认值。
    # Args: self - pytest 测试类实例。
    # Returns: 无返回值，注释要素不全时断言失败。
    def test_env_example_comments_cover_purpose_and_default(self) -> None:
        """默认值写错会让使用者把模板值误当成代码默认值。"""
        logger.debug("test_env_example_comments_cover_purpose_and_default 入口")
        try:
            # Arrange
            lines = _read_lines(_ENV_EXAMPLE)
            active = _key_line_numbers(lines, _ENV_KEY)

            # Act
            incomplete = [
                key
                for key, index in active.items()
                if not _is_documented_comment(_comment_above(lines, index))
            ]

            # Assert
            assert incomplete == [], (
                f"以下活动键的注释未写明用途/默认值：{sorted(incomplete)}"
            )
            logger.info(
                "test_env_example_comments_cover_purpose_and_default 完成",
                extra={"active_key_count": len(active)},
            )
        except Exception as exc:
            logger.error(
                "test_env_example_comments_cover_purpose_and_default 异常",
                extra={"error": str(exc)},
                exc_info=True,
            )
            raise


if __name__ == "__main__":
    pytest.main([__file__, "-q"])
