"""URL 规范化 —— 用于判断「同一个网页是否已经抓过」。

## 为什么需要它

同一个页面会因为各种无关差异被保存成多篇笔记，例如：

    https://example.com/a?utm_source=x
    https://example.com/a/
    https://EXAMPLE.com/a
    https://example.com/a#section
    https://example.com/a?b=2&a=1
    https://example.com/a?a=1&b=2

这些指向**同一篇内容**，却会产生 6 篇笔记 —— 直接污染检索、RAG 引用、关键词、
主题分组与统计（实测已发生过：同一篇 Grok 文章被抓两次）。

## 归一化的范围（刻意保守）

去掉：fragment、utm_* 与常见投放参数、默认端口、尾部斜杠、query 参数顺序、
scheme/host 大小写。

**不**去掉：有语义的 query 参数、路径、大小写不同的路径段。
过度归一化会把**不同页面**误判成同一篇，那比漏判更糟 —— 漏判只是多一篇笔记，
误判会让用户点「更新已有」时**覆盖掉另一篇真实内容**。

## 与「禁止重复抓取」的区别

网页会更新，用户有时确实需要保存同一页面的不同版本。所以本模块只负责
**识别**，是否覆盖 / 另存 / 取消由用户决定（后端强制要求显式选择）。
"""

from __future__ import annotations

from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

# 常见的投放/追踪参数（整段前缀匹配 utm_，其余精确匹配）
_TRACKING_PREFIXES = ("utm_",)
_TRACKING_EXACT = frozenset({
    "fbclid", "gclid", "gclsrc", "dclid", "msclkid", "yclid", "twclid", "igshid",
    "mkt_tok", "mc_cid", "mc_eid", "_ga", "_gl", "ref_src", "ref_url",
    "spm", "scm", "share_token", "share_source", "from_source",
})

_DEFAULT_PORTS = {"http": "80", "https": "443"}


def is_tracking_param(name: str) -> bool:
    """是否为纯投放/追踪参数（可安全丢弃）。"""
    n = (name or "").strip().lower()
    if not n:
        return True
    if n in _TRACKING_EXACT:
        return True
    return any(n.startswith(p) for p in _TRACKING_PREFIXES)


def normalize_url(url: str) -> str:
    """把 URL 归一化成「同一内容 → 同一字符串」的稳定形式。

    解析失败时**原样返回去空白后的结果**，不抛异常 —— 规范化只是辅助判重，
    不该因为它失败就阻断抓取。
    """
    raw = (url or "").strip()
    if not raw:
        return ""
    try:
        parts = urlsplit(raw)
    except ValueError:
        return raw

    scheme = (parts.scheme or "").lower()
    host = (parts.hostname or "").lower()

    # 端口：去掉与 scheme 默认端口相同的显式端口
    port = ""
    try:
        if parts.port and _DEFAULT_PORTS.get(scheme) != str(parts.port):
            port = f":{parts.port}"
    except ValueError:                      # 非法端口（如 :99999）
        port = ""

    netloc = f"{host}{port}"
    if parts.username:                      # 极少见，但保留以免误判
        userinfo = parts.username + (f":{parts.password}" if parts.password else "")
        netloc = f"{userinfo}@{netloc}"

    # query：丢弃追踪参数 + 排序（顺序不同不应算两篇）
    kept = [(k, v) for k, v in parse_qsl(parts.query, keep_blank_values=True)
            if not is_tracking_param(k)]
    query = urlencode(sorted(kept))

    # path：去掉尾部斜杠（根路径除外）；空路径统一为 "/"
    path = parts.path or "/"
    if len(path) > 1 and path.endswith("/"):
        path = path.rstrip("/") or "/"

    # fragment 直接丢弃
    return urlunsplit((scheme, netloc, path, query, ""))


def same_page(a: str, b: str) -> bool:
    """两个 URL 是否指向同一页面（按本模块的归一化口径）。"""
    na, nb = normalize_url(a), normalize_url(b)
    return bool(na) and na == nb
