"""Localhost 服务的安全边界 —— Host 校验 + Fetch Metadata + 同源判定。

## 威胁模型（为什么这里必须有东西）

USB-WIKI 是一个**没有登录鉴权、却装着用户全部私人笔记**的 localhost 服务。
公开发布后会遇到两类真实攻击：

1. **DNS rebinding** —— 恶意网页把自己的域名解析到 ``127.0.0.1``，诱导浏览器
   把请求打成「看起来同源」，从而读取知识库。
   对策：**校验 Host 头**，只接受 loopback 名称（``127.x`` / ``localhost`` / ``::1``）。

2. **跨站读取 / CSRF** —— 任意互联网页面用 ``fetch`` / ``<img>`` / ``<script>``
   打本地 API。
   对策：**默认不发任何 CORS 头**（浏览器因此拿不到响应体），并对带浏览器
   特征信号的请求直接拒绝。

## 为什么不能「一律要求 Origin」

本地 CLI / 诊断工具（``curl``、脚本、CI 冒烟测试）**本来就不发 Origin**。
强制要求所有请求带 Origin 会直接打断它们，而这些恰恰是排障时最需要的手段。

所以本模块的原则是：

    只在**出现了浏览器特征信号**时才判定；没有信号就放行。

浏览器信号有两个，且都只由浏览器发出：

* ``Sec-Fetch-Site: cross-site`` —— 跨站发起的任何请求。**含 no-cors 的
  ``<img>``/``<script>`` 探测**，而这类请求浏览器**不发 Origin**，
  所以光靠 Origin 是拦不住的 —— 这是必须同时看 Fetch Metadata 的原因。
* ``Origin: <非本源>`` —— 跨源 ``fetch`` / XHR。

两个都没有 → 判定为本地工具，放行。

## 变更类请求额外要求 JSON

对 POST/PUT/PATCH/DELETE 要求 ``Content-Type: application/json``。
浏览器能直接发出的「简单请求」只允许三种 Content-Type
（``text/plain``、``application/x-www-form-urlencoded``、``multipart/form-data``），
因此这条能挡住**表单类 CSRF**（例如恶意页面的自动提交表单）。
无 Content-Type 的裸 POST（如本项目的 ``/api/system/shutdown``）仍放行 ——
它没有 body，也构不成表单提交。
"""

from __future__ import annotations

from urllib.parse import urlparse

# 回环主机名（含 IPv6 缩写形式）
_LOOPBACK_NAMES = {"127.0.0.1", "localhost", "::1"}

# 会改变服务端状态的 HTTP 方法
MUTATING_METHODS = frozenset({"POST", "PUT", "PATCH", "DELETE"})

# 拒绝原因码（返回给客户端；不含任何敏感信息）
REASON_HOST = "HOST_NOT_ALLOWED"
REASON_CROSS_SITE = "CROSS_SITE_BLOCKED"
REASON_CROSS_ORIGIN = "CROSS_ORIGIN_BLOCKED"
REASON_CONTENT_TYPE = "UNSUPPORTED_MEDIA_TYPE"

REASON_TEXT = {
    REASON_HOST: "请求的 Host 不被允许：本服务只接受本机回环地址访问",
    REASON_CROSS_SITE: "已拒绝来自其它网站的跨站请求",
    REASON_CROSS_ORIGIN: "已拒绝跨源请求：本服务不对外开放跨域访问",
    REASON_CONTENT_TYPE: "变更类接口只接受 application/json",
}


def normalize_host(value: str) -> str:
    """去掉端口与 IPv6 方括号，转小写。

    ⚠ 不能一律 ``split(":", 1)[0]``：裸 IPv6 形如 ``::1``，那样切出来是空串，
    会让 ``Host: ::1`` 被误判为「非回环」而拒绝本机访问（测试当场抓到这个 bug）。
    """
    host = (value or "").strip().lower()
    if not host:
        return ""
    if host.startswith("["):                       # [::1]:8080
        return host[1:].split("]", 1)[0]
    if host.count(":") > 1:                        # 裸 IPv6：::1 / fe80::1
        return host
    return host.split(":", 1)[0]                   # host:port


def is_loopback(host: str) -> bool:
    """是否为回环地址（整个 127.0.0.0/8 都算）。"""
    h = normalize_host(host)
    if not h:
        return False
    if h in _LOOPBACK_NAMES:
        return True
    return h.startswith("127.")


def host_allowed(host_header: str, *, allow_lan: bool = False) -> bool:
    """校验 Host 头。

    HTTP/1.1 规定 Host 必须存在；缺失即视为异常请求。
    只有显式开启 LAN 模式时才接受非回环主机名 —— 否则 DNS rebinding 就能借
    「域名解析到 127.0.0.1」把请求伪装成本机访问。
    """
    h = normalize_host(host_header)
    if not h:
        return False
    return is_loopback(h) or bool(allow_lan)


def origin_is_self(origin: str, port: int) -> bool:
    """Origin 是否就是本服务自己（scheme 为 http、host 为回环、端口一致）。"""
    if not origin:
        return False
    try:
        u = urlparse(origin)
    except ValueError:
        return False
    if u.scheme not in ("http", "https"):
        return False
    if not is_loopback(u.hostname or ""):
        return False
    try:
        return int(u.port or (443 if u.scheme == "https" else 80)) == int(port)
    except (TypeError, ValueError):
        return False


def check_request(
    method: str,
    headers,
    *,
    port: int,
    allow_lan: bool = False,
) -> tuple[bool, str]:
    """判定该请求是否放行，返回 ``(allowed, reason)``。

    ``headers`` 只需支持 ``.get(name)``（``http.client.HTTPMessage`` 与普通
    ``dict`` 皆可），因此本函数可脱离 HTTP 服务器单独测试。
    """
    def h(name: str) -> str:
        try:
            return (headers.get(name) or "")
        except Exception:  # noqa: BLE001
            return ""

    # 1) Host 校验（防 DNS rebinding）
    if not host_allowed(h("Host"), allow_lan=allow_lan):
        return False, REASON_HOST

    # 2) Fetch Metadata：跨站信号（覆盖不发 Origin 的 no-cors 探测）
    site = h("Sec-Fetch-Site").strip().lower()
    if site == "cross-site":
        return False, REASON_CROSS_SITE

    # 3) Origin：跨源读取（同源 / 缺省都放行，保护 CLI 与诊断工具）
    origin = h("Origin").strip()
    if origin and not origin_is_self(origin, port):
        return False, REASON_CROSS_ORIGIN

    # 4) 变更类请求要求 JSON（挡表单 CSRF）
    if method.upper() in MUTATING_METHODS:
        ctype = h("Content-Type").split(";")[0].strip().lower()
        if ctype and ctype != "application/json":
            return False, REASON_CONTENT_TYPE

    return True, ""
