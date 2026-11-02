"""出站抓取的 URL 安全闸门。

运行时会去抓 RSS/HN 里出现的原文链接（找事件时间、精读正文），
这些 URL 来自外部内容，不能无条件相信。这里只做最基本的 SSRF 防护：
仅允许 http(s)、拒绝 localhost/内网/链路本地/保留地址。

说明：域名不做本地解析（代理场景下解析发生在上游），因此挡不住
「域名解析到内网 IP」的 DNS rebinding，个人自托管够用；若要更严，
可在部署层再加出网策略。
"""

from __future__ import annotations

from ipaddress import ip_address
from urllib.parse import urlsplit

BLOCKED_HOSTNAMES = {
    "localhost",
    "localhost.localdomain",
    "ip6-localhost",
    "ip6-loopback",
}
BLOCKED_SUFFIXES = (".local", ".internal", ".localhost", ".home.arpa")


def is_public_http_url(url: str) -> bool:
    try:
        parts = urlsplit(str(url or "").strip())
    except ValueError:
        return False
    if parts.scheme.lower() not in ("http", "https"):
        return False
    host = (parts.hostname or "").lower().rstrip(".")
    if not host or host in BLOCKED_HOSTNAMES or host.endswith(BLOCKED_SUFFIXES):
        return False
    try:
        address = ip_address(host)
    except ValueError:
        # 普通域名：交给部署层的出网策略兜底
        return True
    return not (
        address.is_private
        or address.is_loopback
        or address.is_link_local
        or address.is_reserved
        or address.is_multicast
        or address.is_unspecified
    )
