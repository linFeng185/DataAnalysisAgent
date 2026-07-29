"""受信任 Skill Registry 目录和包下载客户端。"""

from __future__ import annotations

import asyncio
import hashlib
import json
import re
import urllib.request
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from urllib.parse import urljoin, urlparse

from src.logging_config import get_logger

logger = get_logger(__name__)
_SAFE_SKILL_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")
_SHA256 = re.compile(r"^[0-9a-f]{64}$")


@dataclass(frozen=True)
class RegistrySkillPackage:
    """Registry 中一个已审核 Skill 版本的不可变元数据。"""

    name: str
    version: str
    description: str
    download_url: str
    sha256: str
    api_version: str
    status: str

    # 方法作用：校验 Registry JSON 并转换为强类型包元数据。
    # Args: cls - 包类型；value - Registry 返回的单项字典。
    # Returns: 已完成名称、版本和校验和校验的包。
    @classmethod
    def from_dict(cls, value: dict) -> "RegistrySkillPackage":
        name = str(value.get("name", "") or "").strip()
        version = str(value.get("version", "") or "").strip()
        checksum = str(value.get("sha256", "") or "").strip().lower()
        if not _SAFE_SKILL_NAME.fullmatch(name):
            raise ValueError("Registry Skill 名称无效")
        if not version or len(version) > 64:
            raise ValueError("Registry Skill 版本无效")
        if not _SHA256.fullmatch(checksum):
            raise ValueError("Registry Skill SHA-256 无效")
        api_version = str(value.get("api_version", "") or "").strip()
        if api_version != "data-agent/v1":
            raise ValueError(f"不兼容的 Skill API 版本: {api_version}")
        return cls(
            name=name,
            version=version,
            description=str(value.get("description", "") or "")[:500],
            download_url=str(value.get("download_url", "") or "").strip(),
            sha256=checksum,
            api_version=api_version,
            status=str(value.get("status", "") or "").strip().lower(),
        )


class SkillRegistryClient:
    """读取已审核目录并校验下载来源、大小和 SHA-256。"""

    # 方法作用：初始化 Registry 地址、受信主机和可注入传输层。
    # Args: self - 客户端；base_url - Registry 根地址；trusted_hosts - 包下载主机白名单；max_package_bytes - 包上限；transport - 测试传输函数。
    # Returns: 无返回值。
    def __init__(
        self,
        base_url: str,
        *,
        trusted_hosts: list[str] | None = None,
        max_package_bytes: int = 10 * 1024 * 1024,
        transport: Callable[[str, int], Awaitable[bytes]] | None = None,
    ) -> None:
        normalized = str(base_url or "").strip().rstrip("/")
        if not normalized:
            raise ValueError("Skill Registry URL 未配置")
        base_host = self._validate_url(normalized, set(trusted_hosts or []), allow_base=True)
        self._base_url = normalized
        self._trusted_hosts = {base_host, *(host.strip().lower() for host in trusted_hosts or [] if host.strip())}
        self._max_package_bytes = max(1024, int(max_package_bytes))
        self._transport = transport

    # 方法作用：获取目录并只返回审核通过的兼容 Skill 版本。
    # Args: self - 客户端。
    # Returns: 按名称和版本排序的 approved 包列表。
    async def list_packages(self) -> list[RegistrySkillPackage]:
        url = urljoin(self._base_url + "/", "v1/skills")
        raw = await self._fetch(url, 1024 * 1024)
        try:
            payload = json.loads(raw.decode("utf-8"))
            items = payload.get("skills", []) if isinstance(payload, dict) else []
            if not isinstance(items, list):
                raise ValueError("Registry skills 必须是数组")
            packages = []
            for item in items:
                if not isinstance(item, dict):
                    continue
                if str(item.get("status", "")).strip().lower() != "approved":
                    continue
                packages.append(RegistrySkillPackage.from_dict(item))
        except (UnicodeDecodeError, json.JSONDecodeError, ValueError) as exc:
            logger.error("Skill Registry 目录解析失败", error=str(exc), exc_info=True)
            raise ValueError("Skill Registry 目录格式无效") from exc
        result = sorted(packages, key=lambda package: (package.name, package.version))
        logger.info("Skill Registry 目录加载完成", package_count=len(result))
        return result

    # 方法作用：下载并验证单个已审核 Skill 包的来源、大小和 SHA-256。
    # Args: self - 客户端；package - Registry 包元数据。
    # Returns: 校验通过的 ZIP 二进制。
    async def download_package(self, package: RegistrySkillPackage) -> bytes:
        self._validate_url(package.download_url, self._trusted_hosts)
        payload = await self._fetch(package.download_url, self._max_package_bytes)
        checksum = hashlib.sha256(payload).hexdigest()
        if checksum != package.sha256:
            logger.error(
                "Skill Registry 包校验失败",
                skill=package.name,
                version=package.version,
                expected=package.sha256[:12],
                actual=checksum[:12],
            )
            raise ValueError("Skill 包 SHA-256 校验失败")
        logger.info(
            "Skill Registry 包下载完成",
            skill=package.name,
            version=package.version,
            size=len(payload),
        )
        return payload

    # 方法作用：调用注入传输层或线程化 urllib 获取受限大小响应。
    # Args: self - 客户端；url - 请求 URL；max_bytes - 响应上限。
    # Returns: 响应二进制。
    async def _fetch(self, url: str, max_bytes: int) -> bytes:
        self._validate_url(url, self._trusted_hosts)
        if self._transport is not None:
            payload = await self._transport(url, max_bytes)
            if len(payload) > max_bytes:
                raise ValueError("Skill Registry 响应超过大小限制")
            return payload
        return await asyncio.to_thread(
            self._fetch_sync,
            url,
            max_bytes,
            self._trusted_hosts,
        )

    # 方法作用：同步下载并验证重定向后的最终主机仍在白名单。
    # Args: url - 请求 URL；max_bytes - 响应上限；trusted_hosts - 主机白名单。
    # Returns: 响应二进制。
    @staticmethod
    def _fetch_sync(url: str, max_bytes: int, trusted_hosts: set[str]) -> bytes:
        request = urllib.request.Request(url, headers={"Accept": "application/json, application/zip"})
        with urllib.request.urlopen(request, timeout=15) as response:  # noqa: S310
            SkillRegistryClient._validate_url(response.geturl(), trusted_hosts)
            content_length = int(response.headers.get("Content-Length", 0) or 0)
            if content_length > max_bytes:
                raise ValueError("Skill Registry 响应超过大小限制")
            payload = response.read(max_bytes + 1)
        if len(payload) > max_bytes:
            raise ValueError("Skill Registry 响应超过大小限制")
        return payload

    # 方法作用：校验 URL 协议和主机白名单，开发机仅允许本地 HTTP。
    # Args: url - 待校验 URL；trusted_hosts - 主机白名单；allow_base - 是否允许用当前主机建立初始白名单。
    # Returns: 规范化主机名。
    @staticmethod
    def _validate_url(url: str, trusted_hosts: set[str], allow_base: bool = False) -> str:
        parsed = urlparse(url)
        host = str(parsed.hostname or "").lower()
        local_host = host in {"localhost", "127.0.0.1", "::1"}
        if parsed.scheme != "https" and not (parsed.scheme == "http" and local_host):
            raise ValueError("Skill Registry 必须使用 HTTPS")
        if not host:
            raise ValueError("Skill Registry URL 缺少主机")
        normalized_trusted = {item.strip().lower() for item in trusted_hosts if item.strip()}
        if not allow_base and host not in normalized_trusted:
            raise ValueError("Skill Registry 下载主机不在白名单")
        return host


# 方法作用：从当前 AppContext 配置创建或复用 Skill Registry 客户端。
# Args: 无。
# Returns: 配置完成的 SkillRegistryClient。
def get_skill_registry_client() -> SkillRegistryClient:
    from functools import partial

    from src.app_context import get_app_context

    context = get_app_context()
    settings = context.settings
    trusted_hosts = [
        item.strip()
        for item in str(getattr(settings, "skill_registry_trusted_hosts", "") or "").split(",")
        if item.strip()
    ]
    return context.get_or_create(
        "skill_registry_client",
        partial(
            SkillRegistryClient,
            settings.skill_registry_url,
            trusted_hosts=trusted_hosts,
            max_package_bytes=settings.skill_registry_max_package_bytes,
        ),
    )
