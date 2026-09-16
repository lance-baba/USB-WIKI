"""SSRF 防护：把「允许抓取什么」收敛到唯一的网络出口。

## 策略：白名单（公网 global），而不是危险网段黑名单

只允许**解析结果是公网地址**的目标。判定用标准库 ``ipaddress``：

    is_global and not is_multicast        （IPv4-mapped IPv6 先还原成 IPv4）

**为什么不枚举 127/8、10/8、192.168/16 …**：枚举必然漏。实测就撞到两个：
``224.0.0.1`` 与 ``ff02::1`` 的 ``is_global`` 居然是 **True** ——
只按 ``is_global`` 放行会把组播地址放进去。而 CGNAT（100.64/10）、
169.254/16、192.0.0/24、198.18/15 这些也得靠标准库而不是手写清单。

**IPv4-mapped IPv6 必须还原**：``::ffff:127.0.0.1`` 直接判会得出误导结论，
还原成 ``127.0.0.1`` 才正确。

## TOCTOU / DNS rebinding：把已验证的 IP「钉住」

「先解析一次判断是公网，再用 hostname 发请求」是不够的 —— 建连时**还会再解析一次**。
攻击者让自己的域名第一次解析成公网、第二次解析成 127.0.0.1 即可绕过
（经典 DNS rebinding）。

本模块的做法：

    解析 → 校验**全部**返回地址 → 选定其中一个公网 IP → **直接连这个 IP**
        · HTTP ：仍发送原始 Host 头
        · HTTPS：SNI 与证书校验仍用原始 hostname

即「校验的 IP」与「连接的 IP」是同一个，中间**不存在第二次解析**。
见 :class:`_PinnedHTTPConnection` / :class:`_PinnedHTTPSConnection`。

## 多地址策略：保守

hostname 同时解析出公网与私网地址时 **直接拒绝**，不「挑公网那个继续」——
否则解析顺序变化就会让行为漂移（今天能抓、明天不能，或反之）。

## redirect：完全手动

不用任何库的自动跟随（``allow_redirects=False`` 也不够，那只是 requests 的开关）。
每一跳都：``urljoin`` 出绝对 URL → **重新做完整校验** → 再连。
最多 5 跳，并检测环。``公网 URL -> 302 -> 127.0.0.1`` 会被拦下。

## 代理的例外（已知边界，如实写清）

若配置了 HTTP(S)_PROXY，请求实际上由**代理**建立连接，我们无法钉住 IP。
此时仍然完成 scheme / hostname / 解析结果校验，但「连接时的 IP」由代理决定。
这是有意的取舍：代理是用户自己配置的可信基础设施，且代理侧不会去访问
受害者本机的 localhost。:func:`proxy_active` 会告诉你当前是否处于这种状态。
"""

from __future__ import annotations

import http.client
import ipaddress
import urllib.error
import urllib.request
import os
import socket
import ssl
from dataclasses import dataclass, field
from urllib.parse import urljoin, urlsplit, urlunsplit

from .log_util import get_logger

log = get_logger()

# 只允许这两种协议。其余（file / ftp / gopher / data / javascript …）一律拒绝。
ALLOWED_SCHEMES = ("http", "https")

MAX_REDIRECTS = 5
DEFAULT_UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) WikiUSB/1.3"

# 拒绝原因码（不含敏感信息，可安全回给用户）
R_SCHEME = "SCHEME_NOT_ALLOWED"
R_MALFORMED = "MALFORMED_URL"
R_USERINFO = "USERINFO_NOT_ALLOWED"
R_PORT = "INVALID_PORT"
R_NO_HOST = "MISSING_HOST"
R_DNS = "DNS_RESOLUTION_FAILED"
R_REQUEST = "REQUEST_FAILED"
R_NOT_PUBLIC = "TARGET_NOT_PUBLIC"
R_MIXED = "MIXED_PUBLIC_PRIVATE"
R_TOO_MANY_REDIRECTS = "TOO_MANY_REDIRECTS"
R_REDIRECT_LOOP = "REDIRECT_LOOP"
R_NO_LOCATION = "REDIRECT_WITHOUT_LOCATION"

REASON_TEXT = {
    R_SCHEME: "只支持 http / https 协议",
    R_MALFORMED: "URL 格式不合法",
    R_USERINFO: "URL 不允许携带用户名或密码",
    R_PORT: "端口不合法",
    R_NO_HOST: "URL 缺少主机名",
    R_DNS: "域名无法解析",
    R_REQUEST: "请求失败",
    R_NOT_PUBLIC: "该地址不是公网地址，已拒绝访问（如需抓取内网请显式开启 allow_private_network）",
    R_MIXED: "该域名同时解析出公网与内网地址，出于安全考虑已拒绝",
    R_TOO_MANY_REDIRECTS: f"重定向次数超过 {MAX_REDIRECTS} 次",
    R_REDIRECT_LOOP: "检测到重定向环路",
    R_NO_LOCATION: "重定向响应缺少 Location",
}


# 哪些原因码属于「安全策略拒绝」——调用方据此把「被安全拦住」与
# 「网络失败」区分开：前者要记 warning 让用户知道，后者只是普通失败。
SECURITY_REASONS = frozenset({
    R_SCHEME, R_MALFORMED, R_USERINFO, R_PORT, R_NO_HOST,
    R_NOT_PUBLIC, R_MIXED, R_TOO_MANY_REDIRECTS, R_REDIRECT_LOOP, R_NO_LOCATION,
})


class SSRFBlocked(Exception):
    """目标被安全策略拒绝。``reason`` 是稳定的机器可读码。"""

    def __init__(self, reason: str, detail: str = "") -> None:
        self.reason = reason
        self.detail = detail
        super().__init__(f"{REASON_TEXT.get(reason, '已拒绝')}" + (f"（{detail}）" if detail else ""))


# --------------------------------------------------------------------------
# 地址判定
# --------------------------------------------------------------------------
def effective_ip(ip):
    """把 IPv4-mapped IPv6（``::ffff:a.b.c.d``）还原成 IPv4，其余原样返回。"""
    mapped = getattr(ip, "ipv4_mapped", None)
    return mapped if mapped is not None else ip


def is_public_ip(ip) -> bool:
    """是否为可放行的**公网**地址。

    实测坑：``224.0.0.1`` / ``ff02::1`` 的 ``is_global`` 是 **True**，
    所以必须额外排除组播 —— 只信 ``is_global`` 会把组播放进去。
    """
    a = effective_ip(ip)
    if a.is_multicast:
        return False
    return bool(a.is_global)


def parse_ip(text: str):
    """把字面量解析成 ip 对象；不是字面量返回 None。"""
    try:
        return ipaddress.ip_address((text or "").strip())
    except ValueError:
        return None


# --------------------------------------------------------------------------
# URL 校验
# --------------------------------------------------------------------------
@dataclass
class ValidatedURL:
    url: str
    scheme: str
    host: str
    port: int
    hostname: str              # 原始 hostname（用于 Host 头 / SNI / 证书校验）
    ips: list = field(default_factory=list)


def _default_resolver(host: str, port: int):
    """默认解析器。测试可注入替身，避免依赖公共 DNS（CI 必须确定性）。"""
    infos = socket.getaddrinfo(host, port, proto=socket.IPPROTO_TCP)
    out = []
    for fam, _t, _p, _c, sa in infos:
        try:
            out.append(ipaddress.ip_address(sa[0]))
        except ValueError:
            continue
    return out


def check_url_syntax(url: str) -> ValidatedURL:
    """只做 URL 层面的检查（协议 / userinfo / hostname / 端口），不涉及 DNS。"""
    raw = (url or "").strip()
    if not raw:
        raise SSRFBlocked(R_MALFORMED, "空 URL")
    try:
        parts = urlsplit(raw)
    except ValueError as exc:
        raise SSRFBlocked(R_MALFORMED, str(exc)) from exc

    scheme = (parts.scheme or "").lower()
    if scheme not in ALLOWED_SCHEMES:
        raise SSRFBlocked(R_SCHEME, scheme or "(无)")

    # user:pass@host：既是钓鱼面，也常被用来混淆解析器，直接拒绝
    if parts.username or parts.password:
        raise SSRFBlocked(R_USERINFO)

    host = (parts.hostname or "").strip()
    if not host:
        raise SSRFBlocked(R_NO_HOST)
    host = host.lower().rstrip(".")          # hostname 规范化（FQDN 尾点）

    try:
        port = parts.port
    except ValueError as exc:
        raise SSRFBlocked(R_PORT, str(exc)) from exc
    if port is None:
        port = 443 if scheme == "https" else 80
    if not (1 <= port <= 65535):
        raise SSRFBlocked(R_PORT, str(port))

    # IPv6 字面量：urlsplit 会把 [] 去掉，这里重建一个规范的 host
    literal = parse_ip(host)
    if literal is not None and literal.version == 6:
        host_for_url = f"[{host}]"
    else:
        host_for_url = host
    netloc = host_for_url if port is None else (
        host_for_url if port == (443 if scheme == "https" else 80) else f"{host_for_url}:{port}"
    )
    return ValidatedURL(
        url=urlunsplit((scheme, netloc, parts.path or "/", parts.query, "")),
        scheme=scheme, host=parts.netloc, port=port, hostname=host,
    )


def resolve_and_validate(
    url: str,
    *,
    allow_private_network: bool = False,
    resolver=None,
) -> ValidatedURL:
    """完整校验：URL 语法 + DNS 解析 + 地址策略。

    ⚠ ``allow_private_network=True`` **只**放宽「地址必须是公网」这一条，
    协议限制、URL 合法性、重定向限制一律不变。
    """
    v = check_url_syntax(url)
    literal = parse_ip(v.hostname)

    if literal is not None:
        ips = [literal]
    elif allow_private_network and _is_localhostish(v.hostname):
        # 显式开启后，仍允许 localhost / *.localhost 这类名称
        ips = [ipaddress.ip_address("127.0.0.1")]
    else:
        resolver = resolver or _default_resolver
        try:
            ips = resolver(v.hostname, v.port)
        except (socket.gaierror, OSError, UnicodeError) as exc:
            raise SSRFBlocked(R_DNS, f"{v.hostname}: {exc}") from exc
        if not ips:
            raise SSRFBlocked(R_DNS, v.hostname)

    # 去重后逐类判定
    uniq = []
    for ip in ips:
        ip = effective_ip(ip)
        if ip not in uniq:
            uniq.append(ip)

    if allow_private_network:
        v.ips = uniq
        return v

    publics = [ip for ip in uniq if is_public_ip(ip)]
    if not publics:
        raise SSRFBlocked(R_NOT_PUBLIC, f"{v.hostname} → {', '.join(str(i) for i in uniq[:4])}")
    if len(publics) != len(uniq):
        # 同时解析出公网 + 私网 → 拒绝，不「挑公网那个继续」。
        # 否则解析顺序一变，行为就漂移（今天能抓、明天不能）。
        raise SSRFBlocked(
            R_MIXED,
            f"{v.hostname} → {', '.join(str(i) for i in uniq[:4])}",
        )
    v.ips = uniq
    return v


def _is_localhostish(host: str) -> bool:
    h = (host or "").lower()
    return h == "localhost" or h.endswith(".localhost")


def proxy_active() -> bool:
    """是否配置了 HTTP 代理（决定能否「钉住 IP」，见模块文档）。"""
    keys = ("HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "http_proxy", "https_proxy", "all_proxy")
    return any((os.environ.get(k) or "").strip() for k in keys)


# --------------------------------------------------------------------------
# 钉住 IP 的连接
# --------------------------------------------------------------------------
class _PinnedHTTPConnection(http.client.HTTPConnection):
    """连到**已验证的那个 IP**，但 Host 头仍用原 hostname。

    关键是覆盖 ``connect()`` —— 不再让底层按 hostname 解析，
    因此校验与连接用的是同一个 IP，不存在 TOCTOU 窗口。
    """

    def __init__(self, host: str, port: int, pinned_ip, timeout: float) -> None:
        super().__init__(host, port, timeout=timeout)
        self._pinned_ip = str(pinned_ip)

    def connect(self) -> None:  # noqa: D102
        self.sock = socket.create_connection((self._pinned_ip, self.port), self.timeout)


class _PinnedHTTPSConnection(http.client.HTTPSConnection):
    """同 :class:`_PinnedHTTPConnection`，但保留 SNI 与证书校验用原 hostname。"""

    def __init__(self, host: str, port: int, pinned_ip, timeout: float, context) -> None:
        super().__init__(host, port, timeout=timeout, context=context)
        self._pinned_ip = str(pinned_ip)

    def connect(self) -> None:  # noqa: D102
        sock = socket.create_connection((self._pinned_ip, self.port), self.timeout)
        # server_hostname 用 hostname 而非 IP：TLS 握手与证书校验仍针对域名
        self.sock = self._context.wrap_socket(sock, server_hostname=self.host)


@dataclass
class FetchResult:
    ok: bool
    status: int = 0
    body: bytes = b""
    headers: dict = field(default_factory=dict)
    final_url: str = ""
    hops: int = 0
    reason: str = ""
    message: str = ""
    proxy: bool = False


class _NoRedirectHandler(urllib.request.HTTPRedirectHandler):
    """让 urllib **不要**自动跟随重定向。

    ⚠ 这是修一个真实漏洞：代理分支原先直接用 ``urllib.request.urlopen``，
    而它会自己跟随 302 —— 于是「每一跳重新校验」形同虚设，
    ``公网 URL --302--> 127.0.0.1`` 在配了代理的机器上会被跟进去。
    现在 3xx 会以 ``HTTPError`` 抛出，由 safe_fetch 逐跳重新校验。
    """

    def redirect_request(self, req, fp, code, msg, headers, newurl):  # noqa: D102
        return None


_OPENER_NO_REDIRECT = urllib.request.build_opener(
    _NoRedirectHandler, urllib.request.ProxyHandler()
)


def _one_hop(
    v: ValidatedURL,
    *,
    headers: dict,
    timeout: float,
    use_proxy: bool,
) -> tuple[int, bytes, dict, str]:
    """发一跳请求。返回 (status, body, headers, final_url)。"""
    if use_proxy:
        # 有代理时不钉 IP（见模块文档的已知边界），交给代理建连；
        # 但**重定向仍然必须由我们自己逐跳校验**，所以用禁用了自动跟随的 opener。
        req = urllib.request.Request(v.url, headers=headers, method="GET")
        try:
            with _OPENER_NO_REDIRECT.open(req, timeout=timeout) as resp:
                return resp.status, resp.read(), dict(resp.headers), resp.geturl()
        except urllib.error.HTTPError as exc:
            # 3xx 不再被自动跟随，会以 HTTPError 形式抛出：把 Location 交回调用方
            body = b""
            try:
                body = exc.read()
            except Exception:  # noqa: BLE001
                pass
            return exc.code, body, dict(exc.headers or {}), v.url

    ip = v.ips[0]
    ctx = ssl.create_default_context()
    if v.scheme == "https":
        conn = _PinnedHTTPSConnection(v.hostname, v.port, ip, timeout, ctx)
    else:
        conn = _PinnedHTTPConnection(v.hostname, v.port, ip, timeout)
    try:
        # Host 头由 http.client 依据 self.host（= hostname）自动补上，
        # 因此这里只传路径
        conn.request("GET", _path_of(v), headers=headers)
        resp = conn.getresponse()
        body = resp.read()
        return resp.status, body, dict(resp.getheaders()), v.url
    finally:
        try:
            conn.close()
        except Exception:  # noqa: BLE001
            pass


def _path_of(v: ValidatedURL) -> str:
    parts = urlsplit(v.url)
    path = parts.path or "/"
    return f"{path}?{parts.query}" if parts.query else path


def safe_fetch(
    url: str,
    *,
    headers: dict | None = None,
    timeout: float = 20.0,
    allow_private_network: bool = False,
    max_redirects: int = MAX_REDIRECTS,
    resolver=None,
) -> FetchResult:
    """**唯一**允许用于抓取任意（用户提供）URL 的入口。

    每一跳都重新做完整 SSRF 校验；不自动跟随重定向；限制跳数并检测环路。
    """
    hdrs = {"User-Agent": DEFAULT_UA}
    hdrs.update(headers or {})
    use_proxy = proxy_active()
    seen: set[str] = set()
    current = (url or "").strip()

    for hop in range(max_redirects + 1):
        try:
            v = resolve_and_validate(
                current, allow_private_network=allow_private_network, resolver=resolver
            )
        except SSRFBlocked as exc:
            return FetchResult(False, hops=hop, reason=exc.reason, message=str(exc),
                               proxy=use_proxy, final_url=current)

        key = v.url
        if key in seen:
            return FetchResult(False, hops=hop, reason=R_REDIRECT_LOOP,
                               message=REASON_TEXT[R_REDIRECT_LOOP], proxy=use_proxy,
                               final_url=current)
        seen.add(key)

        try:
            status, body, rh, final_url = _one_hop(
                v, headers=hdrs, timeout=timeout, use_proxy=use_proxy,
            )
        except Exception as exc:  # noqa: BLE001 - 网络异常统一成结果，不抛给上层
            return FetchResult(False, hops=hop, reason=R_REQUEST,
                               message=f"请求失败：{exc}", proxy=use_proxy, final_url=current)

        if status in (301, 302, 303, 307, 308):
            loc = rh.get("Location") or rh.get("location") or ""
            if not loc:
                return FetchResult(False, status=status, hops=hop, reason=R_NO_LOCATION,
                                   message=REASON_TEXT[R_NO_LOCATION], proxy=use_proxy,
                                   final_url=current)
            current = urljoin(v.url, loc)      # 相对 Location 也要处理
            continue

        return FetchResult(True, status=status, body=body, headers=rh,
                           final_url=final_url, hops=hop, proxy=use_proxy)

    return FetchResult(False, hops=max_redirects, reason=R_TOO_MANY_REDIRECTS,
                       message=REASON_TEXT[R_TOO_MANY_REDIRECTS], proxy=use_proxy,
                       final_url=current)
