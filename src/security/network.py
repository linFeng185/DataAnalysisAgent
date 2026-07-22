"""数据库出站地址校验，阻断私网探测与 SSRF。"""

from __future__ import annotations

import ipaddress
import socket

from src.logging_config import get_logger


logger = get_logger(__name__)


# 方法作用：解析部署方声明的可信主机、IP 和 CIDR allowlist。
# Args: raw_allowlist - 逗号分隔的配置文本。
# Returns: 规范化后的非空 allowlist 条目元组。
def parse_host_allowlist(raw_allowlist: str) -> tuple[str, ...]:
    """解析数据库私网访问 allowlist。"""
    logger.debug("出站主机 allowlist 解析入口", configured=bool(raw_allowlist.strip()))
    try:
        result = tuple(
            dict.fromkeys(
                item.strip().casefold()
                for item in raw_allowlist.split(",")
                if item.strip()
            )
        )
    except Exception as exc:
        logger.error("出站主机 allowlist 解析失败", error=str(exc), exc_info=True)
        raise
    logger.info("出站主机 allowlist 解析完成", entry_count=len(result))
    return result


# 方法作用：判断主机名或解析 IP 是否被部署方 allowlist 显式授权。
# Args: host - 原始主机名；address - 已解析 IP；allowlist - 规范化配置条目。
# Returns: 任一精确主机、IP 或 CIDR 条目命中时返回 True。
def _is_allowlisted(
    host: str,
    address: ipaddress.IPv4Address | ipaddress.IPv6Address,
    allowlist: tuple[str, ...],
) -> bool:
    """匹配精确主机、IP 和 CIDR。"""
    logger.debug("出站地址 allowlist 匹配入口", host=host, address=str(address))
    normalized_host = host.strip().strip("[]").casefold()
    for item in allowlist:
        if item == normalized_host or item == str(address).casefold():
            logger.info("出站地址 allowlist 精确命中", host=host, address=str(address))
            return True
        if "/" not in item:
            continue
        try:
            if address in ipaddress.ip_network(item, strict=False):
                logger.info("出站地址 allowlist CIDR 命中", host=host, address=str(address))
                return True
        except ValueError:
            logger.warning("忽略无效出站 CIDR 配置", entry=item)
    logger.info("出站地址 allowlist 未命中", host=host, address=str(address))
    return False


# 方法作用：解析并校验数据库目标，默认只允许全局可路由 IP。
# Args: host - 数据库主机；port - 数据库端口；raw_allowlist - 私网可信目标配置。
# Returns: 本次解析得到的去重 IP 文本元组。
def validate_outbound_host(
    host: str,
    port: int,
    raw_allowlist: str = "",
) -> tuple[str, ...]:
    """在任何网络连接前拒绝未授权私网、回环和特殊地址。"""
    logger.debug("数据库出站地址校验入口", host=host, port=port)
    normalized_host = host.strip().strip("[]")
    if not normalized_host or not 1 <= int(port) <= 65_535:
        logger.error("数据库出站地址校验失败", host=host, port=port, reason="主机或端口无效")
        raise PermissionError("数据库出站地址无效")
    try:
        resolved = socket.getaddrinfo(
            normalized_host,
            int(port),
            type=socket.SOCK_STREAM,
        )
        addresses = tuple(
            dict.fromkeys(
                ipaddress.ip_address(str(item[4][0]).split("%", maxsplit=1)[0])
                for item in resolved
            )
        )
        if not addresses:
            raise OSError("主机没有可用地址")
        allowlist = parse_host_allowlist(raw_allowlist)
        blocked = [
            address
            for address in addresses
            if not address.is_global
            and not _is_allowlisted(normalized_host, address, allowlist)
        ]
        if blocked:
            logger.warning(
                "数据库出站地址已阻断",
                host=normalized_host,
                port=port,
                blocked_addresses=[str(address) for address in blocked],
            )
            raise PermissionError("数据库出站地址被安全策略拒绝")
    except PermissionError:
        raise
    except Exception as exc:
        logger.error(
            "数据库出站地址解析失败",
            host=normalized_host,
            port=port,
            error=str(exc),
            exc_info=True,
        )
        raise PermissionError("数据库出站地址无法安全解析") from exc
    result = tuple(str(address) for address in addresses)
    logger.info(
        "数据库出站地址校验完成",
        host=normalized_host,
        port=port,
        address_count=len(result),
    )
    return result
