"""配置模型约束与配置文件取值的一致性回归测试。

覆盖本次启动崩溃的根因层：Settings 字段的静态上下界（Field 的 ge/le）
一旦窄于 config/app.yaml（及其模板）里的合法取值，服务会在启动时直接抛
ValidationError 退出——用户看到的现象是「改了配置但好像没生效」，实际是
进程根本没起来。
"""

from __future__ import annotations

import logging
from pathlib import Path

import pytest
import yaml
from annotated_types import Le
from pydantic import ValidationError

from src.config import Settings

logger = logging.getLogger(__name__)

_PROJECT_ROOT = Path(__file__).resolve().parents[1]
_CONFIG_FILES: dict[str, Path] = {
    "app.yaml": _PROJECT_ROOT / "config" / "app.yaml",
    "app.example.yaml": _PROJECT_ROOT / "config" / "app.example.yaml",
}
# 前端额度表单可下发的分析单元上界，模型约束必须容得下它。
_FORM_CEILING = 100


class TestConfigFileConstraints:
    """覆盖报表配置面与 Settings 模型约束的一致性。"""

    # 方法作用：读取配置文件的顶层键值对。
    # Args: path - 配置文件路径。
    # Returns: 配置键值字典；文件不存在时返回空字典。
    @staticmethod
    def _load(path: Path) -> dict:
        if not path.exists():
            return {}
        return yaml.safe_load(path.read_text(encoding="utf-8")) or {}

    # 方法作用：验证配置文件里的取值全部落在模型约束内。
    # Args: self - 测试实例；name - 配置文件名。
    # Returns: 无返回值，越界时由断言报告字段名与允许区间。
    @pytest.mark.parametrize("name", sorted(_CONFIG_FILES))
    def test_config_values_satisfy_model_constraints(self, name: str) -> None:
        """复制模板后必须仍能构造 Settings，否则服务启动即退出。"""
        logger.debug("test_config_values_satisfy_model_constraints 入口", extra={"name": name})
        try:
            # Arrange
            values = self._load(_CONFIG_FILES[name])

            # Act
            try:
                Settings(**values)
            except ValidationError as exc:
                # Assert：把越界字段整理成人能读的提示，避免只看到 pydantic 长栈
                offenders = [
                    f"{'.'.join(str(part) for part in error['loc'])}={error['input']!r}（{error['msg']}）"
                    for error in exc.errors()
                ]
                pytest.fail(f"{name} 存在越界配置，服务启动会直接失败：{'; '.join(offenders)}")

            logger.info(
                "test_config_values_satisfy_model_constraints 完成",
                extra={"name": name, "key_count": len(values)},
            )
        except Exception as exc:
            logger.error(
                "test_config_values_satisfy_model_constraints 异常",
                extra={"name": name, "error": str(exc)},
                exc_info=True,
            )
            raise

    # 方法作用：验证分析单元上限的模型上界容得下前端表单可提交的值。
    # Args: self - 测试实例。
    # Returns: 无返回值，上界过窄时断言失败。
    def test_unit_ceiling_field_allows_form_range(self) -> None:
        """模型上界曾写死 12，用户把配置改成 36 后启动崩溃。"""
        logger.debug("test_unit_ceiling_field_allows_form_range 入口")
        try:
            # Arrange
            field = Settings.model_fields["report_max_units_per_run"]

            # Act
            ceilings = [meta.le for meta in field.metadata if isinstance(meta, Le)]

            # Assert
            assert ceilings, "report_max_units_per_run 缺少 le 上界约束"
            assert ceilings[0] >= _FORM_CEILING, (
                f"分析单元上界 {ceilings[0]} 过窄，前端表单可提交到 {_FORM_CEILING}，"
                "配置写入后服务会启动失败"
            )
            logger.info(
                "test_unit_ceiling_field_allows_form_range 完成",
                extra={"ceiling": ceilings[0]},
            )
        except Exception as exc:
            logger.error("test_unit_ceiling_field_allows_form_range 异常", extra={"error": str(exc)}, exc_info=True)
            raise

    # 方法作用：验证模块级单元数静态上界与配置字段上界保持一致。
    # Args: self - 测试实例。
    # Returns: 无返回值，两者脱钩时断言失败。
    def test_quota_static_max_matches_config_ceiling(self) -> None:
        """两处硬编码脱钩会让租户管理员配不出系统上限允许的值。"""
        logger.debug("test_quota_static_max_matches_config_ceiling 入口")
        try:
            # Arrange
            from src.reporting.models import _UNIT_COUNT_STATIC_MAX

            field = Settings.model_fields["report_max_units_per_run"]
            ceilings = [meta.le for meta in field.metadata if isinstance(meta, Le)]

            # Assert
            assert ceilings and ceilings[0] == _UNIT_COUNT_STATIC_MAX, (
                f"配额模型静态上界 {_UNIT_COUNT_STATIC_MAX} 与配置上界 {ceilings} 不一致"
            )
            logger.info(
                "test_quota_static_max_matches_config_ceiling 完成",
                extra={"static_max": _UNIT_COUNT_STATIC_MAX},
            )
        except Exception as exc:
            logger.error("test_quota_static_max_matches_config_ceiling 异常", extra={"error": str(exc)}, exc_info=True)
            raise
