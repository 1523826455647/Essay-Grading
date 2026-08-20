"""服务端外联请求的安全防护。

用于对动态 URL 做协议/主机/IP 边界校验，防范 SSRF（服务端请求伪造）
与 DNS rebinding / TOCTOU 风险。仅用于流量出口是受信配置源
（如硬编码新闻源、官方回调域名）的抓取/回调场景。

防护要点：
- 仅允许 http/https，命中公开域名白名单（含子域名）；
- 发送前解析一次并阻断私网/环回/链路本地等保留网段；
- **HTTP 连接固定到校验通过的 IP**（不再二次解析，防 DNS rebinding），
  并通过 Host 头/SNI 呈现原主机；
- HTTPS 目标为固定白名单域名（当前仅微信回调），校验后按主机名连接；
- 默认不跟随重定向（外部重定向目标需重新过校验）。
"""
import ipaddress
import logging
import socket
from urllib.parse import urlparse

import requests
from requests.adapters import HTTPAdapter

logger = logging.getLogger(__name__)

# 业务里所有抓取源（新闻媒体）、回调（微信）都应加入此处。
PUBLIC_HOST_ALLOWLIST = {
    "people.com.cn",
    "qstheory.cn",
    "xinhuanet.com",               # 新华网评（好词好句抓取）
    "api.weixin.qq.com",           # 微信小程序登录回调
}

# 明确拒绝的内网/保留网段地址
_BLOCKED_NETWORKS = [
    ipaddress.ip_network("0.0.0.0/8"),
    ipaddress.ip_network("10.0.0.0/8"),
    ipaddress.ip_network("100.64.0.0/10"),
    ipaddress.ip_network("127.0.0.0/8"),
    ipaddress.ip_network("169.254.0.0/16"),
    ipaddress.ip_network("172.16.0.0/12"),
    ipaddress.ip_network("192.0.0.0/24"),
    ipaddress.ip_network("192.168.0.0/16"),
    ipaddress.ip_network("198.18.0.0/15"),
    ipaddress.ip_network("224.0.0.0/4"),
    ipaddress.ip_network("240.0.0.0/4"),
    ipaddress.ip_network("::/128"),
    ipaddress.ip_network("::1/128"),
    ipaddress.ip_network("fc00::/7"),
    ipaddress.ip_network("fe80::/10"),
]


def _ip_is_internal(ip_str: str) -> bool:
    try:
        ip = ipaddress.ip_address(ip_str)
    except ValueError:
        return True
    return any(ip in block for block in _BLOCKED_NETWORKS)


def validate_http_url(url: str, allowlist: set | None = None) -> tuple[str, str, str]:
    """校验外联 URL，返回 (归一化 url, host, 固定 ip)。校验失败抛 ValueError。"""
    if not url or not isinstance(url, str):
        raise ValueError("URL 无效")
    allowed = allowlist or PUBLIC_HOST_ALLOWLIST
    parsed = urlparse(url)
    if parsed.scheme not in ("http", "https"):
        raise ValueError(f"不允许的协议：{parsed.scheme}")
    host = (parsed.hostname or "").lower().strip(".")
    if not host:
        raise ValueError("URL 缺少主机名")
    host_ok = any(host == d or host.endswith("." + d) for d in allowed if d)
    if not host_ok:
        raise ValueError(f"主机不在白名单内：{host}")

    try:
        infos = socket.getaddrinfo(
            host,
            parsed.port or (443 if parsed.scheme == "https" else 80),
        )
    except socket.gaierror as e:
        raise ValueError(f"主机解析失败：{host}") from e
    for info in infos:
        if _ip_is_internal(info[4][0]):
            raise ValueError(f"目标 IP 命中保留/内网网段：{info[4][0]}")
    pin_ip = infos[0][4][0]  # 固定使用解析到的第一个地址
    return url, host, pin_ip


class _PinnedHostAdapter(HTTPAdapter):
    """HTTP 场景：发送时把 URL 主机改写为已校验 IP，并用 Host 头呈现原主机。"""

    def __init__(self, host: str, ip: str, **kwargs):
        self._host = host
        self._ip = ip
        super().__init__(**kwargs)

    def send(self, request, **kwargs):
        netloc = request.url.split("://", 1)[-1].split("/", 1)[0]
        request.url = request.url.replace(f"//{netloc}", f"//{self._ip}", 1)
        request.headers["Host"] = self._host
        return super().send(request, **kwargs)


def _safe_session(url: str, allowlist: set | None = None) -> tuple[requests.Session, str, str]:
    _, host, ip = validate_http_url(url, allowlist)
    session = requests.Session()
    # HTTP 用固定 IP 连接（防 DNS rebound）；HTTPS 走默认（主机为固定白名单域名）
    session.mount(f"http://{host}", _PinnedHostAdapter(host, ip))
    return session, url, host


def safe_get(url: str, **kwargs) -> requests.Response:
    """带 SSRF/DNS-rebinding 边界的受控 get。仅应调用受信白名单内的 URL。"""
    session, real_url, _host = _safe_session(url)
    try:
        kwargs.setdefault("timeout", 15)
        kwargs.setdefault("allow_redirects", False)
        return session.get(real_url, **kwargs)
    finally:
        session.close()


def safe_http_post(url: str, **kwargs) -> requests.Response:
    """带 SSRF/DNS-rebinding 边界的受控 post。仅应调用受信白名单内的 URL。"""
    session, real_url, _host = _safe_session(url)
    try:
        kwargs.setdefault("timeout", 15)
        kwargs.setdefault("allow_redirects", False)
        return session.post(real_url, **kwargs)
    finally:
        session.close()