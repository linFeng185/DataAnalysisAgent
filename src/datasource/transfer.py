"""应用内数据源 JSON 迁移契约与凭证过滤。"""

from __future__ import annotations

import re
import secrets
from collections.abc import Callable
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from src.api.schemas import DataSourceCreateRequest

MAX_IMPORT_BYTES = 1024 * 1024
_MAX_NAME_LENGTH = 64
_SUFFIX_ALPHABET = "0123456789abcdefghijklmnopqrstuvwxyz"
_SUFFIX_LENGTH = 6
_MAX_RENAME_ATTEMPTS = 3
_SENSITIVE_KEY = re.compile(r"password|passwd|pwd|secret|token|credential|authorization|cookie|private.?key|api.?key|access.?key|connection.?string|dsn|url|uri", re.I)


# 方法作用：生成重名改名用的随机后缀，避免依赖需要并发保护的递增序号。
# Args: 无。
# Returns: 6 位 base36 小写随机串。
def random_suffix() -> str:
    """递增序号需先查询现有名称求最大值，并发导入会选中同一序号。"""
    return "".join(secrets.choice(_SUFFIX_ALPHABET) for _ in range(_SUFFIX_LENGTH))


# 方法作用：返回未被占用的名称，冲突时追加随机后缀并重试。
# Args: name - 原始名称；taken - 判断候选名是否已被占用的回调；max_attempts - 最大尝试次数。
# Returns: 未冲突时返回原名，否则返回截断后带后缀的新名称。
# Raises: DataSourceConflictError - 超过尝试次数仍无可用名称。
def resolve_available_name(
    name: str,
    taken: Callable[[str], bool],
    *,
    max_attempts: int = _MAX_RENAME_ATTEMPTS,
) -> str:
    """名称与后缀合计不超过 64 字符，后缀固定为下划线加 6 位 base36。"""
    if not taken(name):
        return name
    suffix_width = _SUFFIX_LENGTH + 1
    for _ in range(max_attempts):
        candidate = f"{name[: _MAX_NAME_LENGTH - suffix_width]}_{random_suffix()}"
        if not taken(candidate):
            return candidate
    raise DataSourceConflictError("同名数据源过多，未能生成可用后缀")


class DataSourceConflictError(ValueError):
    """同租户名称已存在，不能以导入覆盖配置。"""


class ImportDataSource(DataSourceCreateRequest):
    """拒绝归属覆盖、未知字段及无效端口的导入条目。"""

    model_config = ConfigDict(extra="forbid", strict=True)
    name: str = Field(min_length=1, max_length=64, pattern=r"^\S(?:.*\S)?$")
    port: int = Field(default=0, ge=0, le=65535)


class DataSourceBundle(BaseModel):
    """一次最多导入一百个数据源，全部校验后才允许写入。"""

    model_config = ConfigDict(extra="forbid", strict=True)
    version: Literal[1]
    datasources: list[ImportDataSource] = Field(min_length=1, max_length=100)

    # 方法作用：阻止文件内同名配置造成隐式覆盖。
    # Args: self - 已解析的导入文件。
    # Returns: 已确认名称唯一的导入文件。
    @model_validator(mode="after")
    def validate_unique_names(self) -> DataSourceBundle:
        """文件内重复名称整批拒绝。"""
        names = [item.name for item in self.datasources]
        if len(names) != len(set(names)):
            raise ValueError("导入文件包含重复数据源名称")
        return self


# 方法作用：递归剔除扩展字段中的凭证键及可能携带认证信息的连接串。
# Args: value - 待导出的 JSON 值。
# Returns: 不包含已识别敏感字段的 JSON 值。
def safe_extra_params(value: Any) -> Any:
    """保留插件非敏感扩展配置，不导出凭证或连接 URL。"""
    if isinstance(value, dict):
        return {key: safe_extra_params(item) for key, item in value.items() if not _SENSITIVE_KEY.search(str(key))}
    if isinstance(value, list):
        return [safe_extra_params(item) for item in value]
    if isinstance(value, str) and ("://" in value or re.search(r"(?:password|token|secret)\s*=", value, re.I)):
        return ""
    return value


# 方法作用：仅导出应用创建请求允许的配置字段，不访问或解密密码。
# Args: source - Provider 中的数据源配置。
# Returns: 可重新导入的无凭证配置。
def export_config(source: object) -> dict[str, Any]:
    """导出不包括密码、所属身份、权限、同步状态或运行时对象。"""
    fields = ("name", "dialect", "host", "port", "database", "username", "version", "description")
    result = {field: getattr(source, field, 0 if field == "port" else "") for field in fields}
    result["extra_params"] = safe_extra_params(getattr(source, "extra_params", {}) or {})
    return result
