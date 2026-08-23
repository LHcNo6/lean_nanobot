"""网络安全助手 —— SSRF 防护与内部 URL 识别。

对齐 nanobot `security/network.py` 的最小集（H7）：
- `_BLOCKED_NETWORKS`：RFC1918 / loopback / link-local / metadata / CGN 网段；
- ``is_loopback_host``：绑定目标是否为显式 loopback；
- ``validate_url_target`` / ``resolve_url_target``：scheme + 主机名 + 解析 IP
  校验，``allow_loopback`` 采用窄语义（仅字面 loopback 主机且全部解析地址
  都是 loopback 才放行）；
- ``contains_internal_url``：命令字符串中是否含内部 URL（shell 门禁用）。

与 nanobot 的差异（刻意简化）：
- 不含 httpx 传输层（``PinnedDNSAsyncTransport`` / proxy mounts / DNS pin），
  那属于真实 web 工具的产品层能力，留未来候选；
- 重定向后的 ``validate_resolved_url`` 一并省略（跟随重定向时再补）。
"""

from __future__ import annotations

import ipaddress
import re
import socket
from contextlib import suppress
from urllib.parse import urlparse

_BLOCKED_NETWORKS = [
    ipaddress.ip_network("0.0.0.0/8"),
    ipaddress.ip_network("10.0.0.0/8"),
    ipaddress.ip_network("100.64.0.0/10"),  # carrier-grade NAT
    ipaddress.ip_network("127.0.0.0/8"),
    ipaddress.ip_network("169.254.0.0/16"),  # link-local / cloud metadata
    ipaddress.ip_network("172.16.0.0/12"),
    ipaddress.ip_network("192.168.0.0/16"),
    ipaddress.ip_network("::1/128"),
    ipaddress.ip_network("fc00::/7"),  # unique local
    ipaddress.ip_network("fe80::/10"),  # link-local v6
]

_URL_RE = re.compile(r"https?://[^\s\"'`;|<>]+", re.IGNORECASE)
_allowed_networks: list[ipaddress.IPv4Network | ipaddress.IPv6Network] = []


def is_loopback_host(host: str) -> bool:
    """返回绑定目标是否被显式限制为 loopback（localhost / 127.0.0.1 / ::1）。"""
    normalized = host.strip().rstrip(".").lower()
    if normalized == "localhost":
        return True
    if normalized.startswith("[") and normalized.endswith("]"):
        normalized = normalized[1:-1]
    with suppress(ValueError):
        return ipaddress.ip_address(normalized).is_loopback
    return False


def configure_ssrf_whitelist(cidrs: list[str]) -> None:
    """允许指定 CIDR 网段绕过 SSRF 拦截（如 Tailscale 的 100.64.0.0/10）。"""
    global _allowed_networks
    nets = []
    for cidr in cidrs:
        with suppress(ValueError):
            nets.append(ipaddress.ip_network(cidr, strict=False))
    _allowed_networks = nets


def _normalize_addr(
    addr: ipaddress.IPv4Address | ipaddress.IPv6Address,
) -> ipaddress.IPv4Address | ipaddress.IPv6Address:
    """把 IPv6-mapped IPv4（``::ffff:127.0.0.1``）规整为 IPv4 形式。

    否则 ipaddress 会把它们当成 IPv6 地址，既不匹配 ``127.0.0.0/8`` 也
    不匹配 ``::1/128``，导致绕过拦截（对齐 nanobot）。
    """
    if isinstance(addr, ipaddress.IPv6Address) and addr.ipv4_mapped is not None:
        return addr.ipv4_mapped
    return addr


def _is_private(addr: ipaddress.IPv4Address | ipaddress.IPv6Address) -> bool:
    """返回地址是否命中拦截网段（白名单网段优先豁免）。"""
    normalized = _normalize_addr(addr)
    if _allowed_networks and any(normalized in net for net in _allowed_networks):
        return False
    return any(normalized in net for net in _BLOCKED_NETWORKS)


def resolve_url_target(url: str, *, allow_loopback: bool = False) -> tuple[bool, str, tuple[str, ...]]:
    """校验 URL 是否可安全抓取：scheme / hostname / 解析 IP。

    ``allow_loopback`` 刻意收窄：只允许字面 loopback 主机（localhost、
    127.0.0.0/8、::1），且**所有**解析地址都是 loopback；不放行 RFC1918、
    link-local、metadata 或碰巧解析到 loopback 的公网域名。

    Returns:
        (ok, error_message, resolved_ips)；ok 时 resolved_ips 为校验通过的地址。
    """
    try:
        p = urlparse(url)
    except Exception as e:
        return False, str(e), ()

    if p.scheme not in ("http", "https"):
        return False, f"Only http/https allowed, got '{p.scheme or 'none'}'", ()
    if not p.netloc:
        return False, "Missing domain", ()

    hostname = p.hostname
    if not hostname:
        return False, "Missing hostname", ()

    try:
        infos = socket.getaddrinfo(hostname, None, socket.AF_UNSPEC, socket.SOCK_STREAM)
    except socket.gaierror:
        return False, f"Cannot resolve hostname: {hostname}", ()

    addrs: list[ipaddress.IPv4Address | ipaddress.IPv6Address] = []
    for info in infos:
        try:
            addr = ipaddress.ip_address(info[4][0])
        except ValueError:
            continue
        addrs.append(addr)
    if allow_loopback and _is_allowed_loopback_target(hostname, addrs):
        return True, "", tuple(dict.fromkeys(str(_normalize_addr(addr)) for addr in addrs))
    for addr in addrs:
        if _is_private(addr):
            return False, f"Blocked: {hostname} resolves to private/internal address {addr}", ()

    return True, "", tuple(dict.fromkeys(str(_normalize_addr(addr)) for addr in addrs))


def validate_url_target(url: str, *, allow_loopback: bool = False) -> tuple[bool, str]:
    """校验 URL 是否可安全抓取（只关心 ok/error，忽略解析地址）。"""
    ok, error, _ = resolve_url_target(url, allow_loopback=allow_loopback)
    return ok, error


def contains_internal_url(command: str, *, allow_loopback: bool = False) -> bool:
    """命令字符串中含内部/私有地址 URL 时返回 True（shell 门禁用）。"""
    for m in _URL_RE.finditer(command):
        url = m.group(0)
        ok, _ = validate_url_target(url, allow_loopback=allow_loopback)
        if not ok:
            return True
    return False


def _is_allowed_loopback_target(
    hostname: str,
    addrs: list[ipaddress.IPv4Address | ipaddress.IPv6Address],
) -> bool:
    """判断主机名 + 解析地址集合是否为合法 loopback 目标。"""
    if not addrs or not all(_normalize_addr(addr).is_loopback for addr in addrs):
        return False
    normalized = hostname.rstrip(".").lower()
    if normalized == "localhost":
        return True
    with suppress(ValueError):
        return ipaddress.ip_address(hostname).is_loopback
    return False