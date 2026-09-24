"""网页存档的资源本地化 —— 让「原版预览」真正离线，且浏览时**零外部请求**。

## 为什么必须做

抓下来的 HTML 用相对路径引用 CSS / 图片 / 字体。若不在**抓取时**把资源一起
存下来，浏览「原版」时就只有两条路：

1. 相对路径 404 → 页面退化成一副裸 HTML，「原版」名不副实；
2. 注入 ``<base>`` 让它去原站拉 → 页面好看了，却**违背本项目三个立身之本**：
   离线可用（U 盘的立身之本）、Local-First（数据 100% 属于用户）、零 CDN；
   而且每看一次就把用户的 IP、UA、访问时间泄露给原站及其第三方 CDN。

所以正确解法只有一个：**抓取时把资源落到本地**，浏览时不再碰网络。

## 设计要点

- 资源按 **URL 哈希**存入共享池 ``data/assets/``：同一站点的 style.css / logo.png
  被多篇文章引用时**只存一份**，这是 U 盘体积上最划算的一步。
- HTML 与 CSS 内的引用统一重写为**本地根相对路径** ``/api/assets/<hash><ext>``。
  因为不注入 ``<base>``，根相对路径天然指向本服务 → 离线即可渲染。
- **一律不下载脚本**：预览用 ``sandbox=""`` 禁用了脚本，存下来没有意义，
  只会增大体积、并把不可信代码留在磁盘上。原 ``<script src>`` 换成一条注释。
- **严格离线**：任何抓不到 / 超限的资源都换成占位符，**绝不留外部 URL** ——
  否则用户会在不知情的情况下被联网。代价是页面可能缺图，因此会如实计入 notes。
  唯一例外见下方「已知牺牲」。
- 预算护栏：单资源上限 + 单页预算，避免个别页面吃掉整块 U 盘。
- 性能：连接复用（``requests.Session``）+ 按文档顺序分批抓取，预算用尽即提前停止，
  既不把带宽浪费在注定要丢的资源上，也让「先出现的图优先保住」。

## 已知牺牲

- 依赖脚本渲染的内容（懒加载图片、JS 注入样式）在存档中无法还原 ——
  这是 ``sandbox=""`` 安全边界的必然代价，不打算放宽。
- 超出预算的图片是占位符，不是原图。
"""

from __future__ import annotations

import base64
import hashlib
import re
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from html import escape as html_escape
from pathlib import Path
from urllib.parse import urljoin, urlparse

from . import config, net_guard as _net_guard, net_util, paths

# 前端与服务器约定的本地资源路径前缀（根相对 → 指向本服务）
ASSET_URL_PREFIX = "/api/assets/"
ASSET_NAME_RE = re.compile(r"^[0-9a-f]{16}\.[A-Za-z0-9]{1,8}$")

# 明显不用于页面渲染的重型资源：不下载（也不留外部 URL，避免被自动拉取）
_SKIP_SUFFIX = {
    ".mp4", ".webm", ".m3u8", ".mpd", ".mp3", ".wav", ".ogg", ".flac",
    ".avi", ".mov", ".mkv", ".zip", ".gz", ".tar", ".rar", ".7z",
    ".exe", ".msi", ".dmg", ".iso", ".apk",
}

# 会把浏览器引向外部请求、但对离线渲染毫无用处的资源提示 → 直接删掉
_DROP_REL = re.compile(r"\b(preload|prefetch|preconnect|dns-prefetch|prerender|modulepreload)\b", re.I)
_STYLESHEET_REL = re.compile(r"\bstylesheet\b", re.I)
_ICON_REL = re.compile(r"\b(icon|apple-touch-icon|shortcut icon)\b", re.I)

# 1×1 透明 GIF：配合保留的 width/height，布局不会被撑破
_PLACEHOLDER = (
    "data:image/gif;base64,R0lGODlhAQABAIAAAAAAAP///yH5BAEAAAAALAAAAAABAAEAAAIBRAA7"
)

_TAG_RE = re.compile(r"<([a-zA-Z][\w:-]*)((?:[^>\"']|\"[^\"]*\"|'[^']*')*)>")
_ATTR_RE = re.compile(r"""([\w:-]+)\s*=\s*(".*?"|'.*?'|[^\s"'>]+)""")
_CSS_URL_RE = re.compile(r"""url\(\s*(['"]?)([^'")]+)\1\s*\)""", re.I)
_CSS_IMPORT_RE = re.compile(r"""@import\s+(?:url\(\s*)?(['"])([^'"]+)\1""", re.I)
_STYLE_BLOCK_RE = re.compile(r"(<style\b[^>]*>)(.*?)(</style>)", re.I | re.S)
_SRCSET_SPLIT_RE = re.compile(r"\s*,\s*")

_URL_ATTRS = {"href", "src", "poster", "action", "data-src", "data-original", "background"}

# 分批抓取的批大小与并发度：批间串行 → 文档顺序即优先级，且预算用尽可提前停
_BATCH = 10
_WORKERS = 8
# 单页本地化的总时间上限（秒）。超时后剩余资源一律占位，保证抓取不被拖死。
_TIME_BUDGET_S = 45.0


class AssetBlocked(OSError):
    """资源被 SSRF 安全策略拒绝（与普通网络失败区分开）。

    只携带 URL 与**原因码**，不携带任何响应内容 —— 日志与用户提示都不应
    回显被抓内容。
    """

    def __init__(self, url: str, reason: str, message: str = "") -> None:
        self.url = url
        self.reason = reason
        super().__init__(message or reason)


@dataclass
class LocalizeStats:
    """本地化统计（用于日志与界面 notes 说明）。"""

    assets: int = 0
    reused: int = 0
    bytes: int = 0
    skipped_large: int = 0
    skipped_budget: int = 0
    skipped_type: int = 0
    skipped_blocked: int = 0      # 被 SSRF 安全策略拒绝
    failed: int = 0
    timed_out: int = 0
    sheets_dropped: int = 0
    icons_dropped: int = 0
    scripts: int = 0
    seconds: float = 0.0
    notes: list[str] = field(default_factory=list)

    @property
    def localized(self) -> bool:
        return self.assets > 0

    @property
    def placeholder(self) -> int:
        return (self.skipped_large + self.skipped_budget + self.skipped_type
                + self.skipped_blocked + self.failed + self.timed_out)


def _sha16(url: str) -> str:
    """资源池文件名 = 原始 URL 的短哈希（同一 URL 天然去重）。"""
    return hashlib.sha1(url.encode("utf-8")).hexdigest()[:16]


def _ext_of(url: str, content_type: str = "") -> str:
    """扩展名：优先取 URL 路径后缀，其次按 Content-Type 映射。"""
    suffix = Path(urlparse(url).path).suffix.lower()
    if suffix and re.fullmatch(r"\.[A-Za-z0-9]{1,8}", suffix):
        return suffix
    ct = (content_type or "").split(";")[0].strip().lower()
    return {
        "text/css": ".css",
        "image/png": ".png", "image/jpeg": ".jpg", "image/gif": ".gif",
        "image/webp": ".webp", "image/svg+xml": ".svg",
        "image/x-icon": ".ico", "image/vnd.microsoft.icon": ".ico",
        "font/woff2": ".woff2", "font/woff": ".woff", "font/ttf": ".ttf",
        "application/font-woff2": ".woff2",
    }.get(ct, ".bin")


class _Fetcher:
    """带连接复用的抓取器。

    用 ``requests.Session`` 而不是 urllib：同一站点的几十个资源可以复用 TCP/TLS
    连接，实测把单页本地化从上百秒压到十几秒。requests 不可用时回退 net_util。
    """

    def __init__(self, timeout: float) -> None:
        self.timeout = timeout

    def get(self, url: str) -> tuple[bytes, str]:
        """返回 (字节, Content-Type)。

        ⚠ 必须走 :func:`net_guard.safe_fetch` —— 资源 URL 来自被抓页面，
        属于**外部可控输入**，与正文抓取受同一条安全闸门管控：同一份
        ``allow_private_network`` 配置、同一套 scheme/DNS/IP 校验、
        同样钉住 IP 连接、同样逐跳重校验 redirect。

        此前这里用 ``requests.Session``：既绕过以上全部检查，又默认
        ``allow_redirects=True``（与正文抓取那条路径犯过同一个错）。

        安全拒绝抛 :class:`AssetBlocked`，与普通「网络失败」区分开 ——
        前者要如实告知用户「有资源被安全策略拦住了」，后者只是没抓到。
        """
        res = _net_guard.safe_fetch(
            url,
            headers={
                "Accept": "*/*",
                "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
                "Referer": "",          # 不外泄来源页
            },
            timeout=self.timeout,
            allow_private_network=config.get_bool("CRAWLER", "allow_private_network", False),
        )
        if not res.ok:
            if res.reason in _net_guard.SECURITY_REASONS:
                raise AssetBlocked(url, res.reason, res.message)
            raise OSError(res.message or "抓取失败")
        if res.status >= 400:
            raise OSError(f"HTTP {res.status}")
        return res.body, (res.headers.get("Content-Type") or "")

    def close(self) -> None:
        return None



class _Pool:
    """资源池：去重、预算记账、落盘。线程安全。"""

    def __init__(self, stats: LocalizeStats, max_bytes: int, budget_bytes: int, fetcher: _Fetcher):
        self.stats = stats
        self.max_bytes = max_bytes
        self.budget_bytes = budget_bytes
        self.fetcher = fetcher
        self.lock = threading.Lock()
        self.seen: dict[str, str] = {}      # URL → 本地路径
        self.tried: dict[str, str] = {}
        # 被安全策略拒绝的资源：(url, 原因码)。**只存这两个字段**，
        # 不存任何响应内容 —— 日志与用户提示都不回显被抓内容。
        self.blocked: list[tuple[str, str]] = []     # URL → 上次判定（含失败，避免重复抓）
        self.deadline = time.monotonic() + _TIME_BUDGET_S

    # ---- 查询 ----
    def already(self, url: str) -> str | None:
        with self.lock:
            return self.seen.get(url)

    def status_of(self, url: str) -> str | None:
        with self.lock:
            return self.tried.get(url)

    def exhausted(self) -> bool:
        """预算用尽或超时 —— 调用方应停止继续抓取。"""
        with self.lock:
            if self.stats.bytes >= self.budget_bytes:
                return True
        return time.monotonic() > self.deadline

    def local_path(self, url: str) -> str:
        return f"{ASSET_URL_PREFIX}{_sha16(url)}"

    # ---- 写入 ----
    def store(self, url: str, data: bytes, ctype: str) -> str:
        name = f"{_sha16(url)}{_ext_of(url, ctype)}"
        target = paths.ASSETS_DIR / name
        local = f"{ASSET_URL_PREFIX}{name}"
        try:
            paths.ASSETS_DIR.mkdir(parents=True, exist_ok=True)
            if target.exists():
                with self.lock:
                    self.stats.reused += 1
            else:
                target.write_bytes(data)
                with self.lock:
                    self.stats.bytes += len(data)
        except OSError:
            pass
        with self.lock:
            self.seen[url] = local
            self.stats.assets += 1
        return local

    def try_fetch(self, url: str) -> tuple[str, str]:
        """抓取并落盘（结果会被记住，同一 URL 不重复下载）。

        返回 ``(本地路径, 状态)``；非 ``ok`` 时调用方**必须用占位符**，
        绝不能把外部 URL 留在文档里 —— 那会在用户不知情时联网。
        """
        hit = self.already(url)
        if hit:
            return hit, "ok"
        prev = self.status_of(url)
        if prev is not None:
            return "", prev

        if Path(urlparse(url).path).suffix.lower() in _SKIP_SUFFIX:
            return self._remember(url, "type")
        if self.exhausted():
            return self._remember(url, "budget")

        # data: / blob: 是**内联**资源，本就不需要联网。
        # 直接原样保留（不替换成占位符），也绝不当成网络 URL 发出去。
        scheme = urlparse(url).scheme.lower()
        if scheme in ("data", "blob", "about"):
            return url, "inline"

        try:
            data, ctype = self.fetcher.get(url)
        except AssetBlocked as exc:
            # 被安全策略拒绝：**不中断整页**，用占位符 + 记一条脱敏说明。
            # 只记录 URL 与原因码，绝不记录响应内容。
            with self.lock:
                self.blocked.append((url, exc.reason))
            return self._remember(url, "blocked")
        except Exception:  # noqa: BLE001 - 单个资源失败不应中断整页
            return self._remember(url, "failed")

        if len(data) > self.max_bytes:
            return self._remember(url, "large")
        with self.lock:
            over = self.stats.bytes + len(data) > self.budget_bytes
        if over:
            return self._remember(url, "budget")
        return self.store(url, data, ctype), "ok"

    def _remember(self, url: str, status: str) -> tuple[str, str]:
        key = {
            "type": "skipped_type", "large": "skipped_large",
            "budget": "skipped_budget", "failed": "failed", "timeout": "timed_out",
            "blocked": "skipped_blocked",
        }[status]
        with self.lock:
            self.tried[url] = status
            setattr(self.stats, key, getattr(self.stats, key) + 1)
        return "", status

    # ---- CSS ----
    def localize_css(self, css_text: str, css_url: str) -> str:
        """把 CSS 内部的 url()/@import 也本地化，返回重写后的 CSS。"""
        def _imp(m: re.Match) -> str:
            raw = m.group(2).strip()
            if raw.startswith(("data:", "#")):
                return m.group(0)
            local, status = self.try_fetch(urljoin(css_url, raw))
            return f"@import url({local})" if status == "ok" else ""

        def _u(m: re.Match) -> str:
            raw = m.group(2).strip()
            if not raw or raw.startswith(("data:", "#", "about:", "javascript:")):
                return m.group(0)
            local, status = self.try_fetch(urljoin(css_url, raw))
            return f"url({local})" if status == "ok" else f"url({_PLACEHOLDER})"

        try:
            css_text = _CSS_IMPORT_RE.sub(_imp, css_text)
            return _CSS_URL_RE.sub(_u, css_text)
        except Exception:  # noqa: BLE001
            return css_text


def _rewrite_tag_attrs(attrs: str, page_url: str) -> str:
    """把标签属性里的 URL 解析成绝对地址（供 `a` 等非资源引用使用）。"""
    def _sub(m: re.Match) -> str:
        name, val = m.group(1), m.group(2)
        if name.lower() not in _URL_ATTRS:
            return m.group(0)
        quote = val[0] if val and val[0] in "\"'" else ""
        raw = (val[1:-1] if quote else val).strip()
        if not raw or raw.startswith(("data:", "#", "javascript:", "mailto:", "tel:", "about:")):
            return m.group(0)
        absolute = urljoin(page_url, raw)
        return f"{name}={quote}{absolute}{quote}" if quote else f"{name}={absolute}"
    return _ATTR_RE.sub(_sub, attrs)


def _split_srcset(value: str) -> list[tuple[str, str]]:
    """拆 srcset，返回 [(url, descriptor)]。"""
    out: list[tuple[str, str]] = []
    for part in _SRCSET_SPLIT_RE.split(value.strip()):
        if not part:
            continue
        bits = part.split(None, 1)
        out.append((bits[0], bits[1] if len(bits) > 1 else ""))
    return out


def _batched_grab(pool: _Pool, urls: list[str]) -> None:
    """按文档顺序分批并发抓取，预算用尽即停（后面的不再浪费带宽）。"""
    for i in range(0, len(urls), _BATCH):
        if pool.exhausted():
            return
        chunk = urls[i:i + _BATCH]
        with ThreadPoolExecutor(max_workers=_WORKERS) as ex:
            list(ex.map(lambda u: _safe_fetch(pool, u), chunk))


def _safe_fetch(pool: _Pool, url: str) -> None:
    try:
        pool.try_fetch(url)
    except Exception:  # noqa: BLE001
        pass


def localize(html_text: str, page_url: str) -> tuple[str, LocalizeStats]:
    """把网页存档的资源本地化。返回 (重写后的 HTML, 统计)。

    本函数**不抛异常**：任何内部错误都退化为「只把 URL 解析成绝对地址」，
    页面仍可在联网时查看，只是不具备离线保真。
    """
    stats = LocalizeStats()
    if not html_text or not page_url:
        return html_text, stats
    if not config.get_bool("CRAWLER", "save_assets", True):
        stats.notes.append("未启用资源本地化 → 该页离线时样式与图片不可用（可在 config.ini 打开 save_assets）")
        return html_text, stats

    t0 = time.monotonic()
    timeout = config.get_float("CRAWLER", "request_timeout", 20.0)
    max_bytes = max(32, config.get_int("CRAWLER", "asset_max_kb", 512)) * 1024
    budget_bytes = max(256, config.get_int("CRAWLER", "assets_budget_kb", 5120)) * 1024
    fetcher = _Fetcher(min(timeout, 15.0))
    pool = _Pool(stats, max_bytes, budget_bytes, fetcher)

    images: list[str] = []
    sheets: list[str] = []

    # ---------------- 第一遍：重写标签、收集待抓资源 ----------------
    def _tag_sub(m: re.Match) -> str:
        tag = m.group(1)
        attrs = _rewrite_tag_attrs(m.group(2), page_url)
        low = tag.lower()

        if low == "script":
            src_val = ""
            for am in _ATTR_RE.finditer(attrs):
                if am.group(1).lower() == "src":
                    src_val = am.group(2).strip("\"'")
                    break
            if src_val:
                stats.scripts += 1
                return f"<!-- [离线存档] 已省略外部脚本：{html_escape(src_val[:200])} -->"
            return f"<{tag}{attrs}>"

        if low == "link":
            rel = ""
            href = ""
            for am in _ATTR_RE.finditer(attrs):
                k, v = am.group(1).lower(), am.group(2).strip("\"'")
                if k == "rel":
                    rel = v
                elif k == "href":
                    href = v
            if _DROP_REL.search(rel):
                return ""                       # 资源提示对离线无用，且会引发外部请求
            if href and _STYLESHEET_REL.search(rel):
                sheets.append(href)
            elif href and _ICON_REL.search(rel):
                images.append(href)
            return f"<{tag}{attrs}>"

        if low in ("img", "source", "input", "video", "audio"):
            for am in _ATTR_RE.finditer(attrs):
                k, v = am.group(1).lower(), am.group(2).strip("\"'")
                if k in ("src", "data-src", "poster") and v:
                    images.append(v)
                elif k == "srcset" and v:
                    images.extend(u for u, _ in _split_srcset(v))
            return f"<{tag}{attrs}>"

        return f"<{tag}{attrs}>"

    html_text = _TAG_RE.sub(_tag_sub, html_text)

    # ---------------- 第二遍：样式表（先抓，其内部 url() 才会被登记）----------------
    uniq_sheets = list(dict.fromkeys(sheets))
    if uniq_sheets:
        with ThreadPoolExecutor(max_workers=min(_WORKERS, len(uniq_sheets))) as ex:
            list(ex.map(lambda u: _safe_fetch(pool, u), uniq_sheets))
        for u in uniq_sheets:
            hit = pool.already(u)
            if not hit:
                continue
            disk = paths.ASSETS_DIR / Path(urlparse(hit).path).name
            try:
                raw = disk.read_bytes().decode("utf-8", "replace")
            except OSError:
                continue
            if "url(" in raw.lower() or "@import" in raw.lower():
                try:
                    disk.write_text(pool.localize_css(raw, u), encoding="utf-8", newline="")
                except OSError:
                    pass

    # ---------------- 第三遍：其余资源（按文档顺序，预算用尽提前停）----------------
    uniq_images = [u for u in dict.fromkeys(images) if not pool.already(u)]
    _batched_grab(pool, uniq_images)

    # 行内 <style> 里的 url() 同样本地化
    def _style_block(m: re.Match) -> str:
        body = m.group(2)
        if "url(" in body.lower():
            body = _CSS_URL_RE.sub(_inline_url, body)
        return m.group(1) + body + m.group(3)

    def _inline_url(m: re.Match) -> str:
        raw = m.group(2).strip()
        if not raw or raw.startswith(("data:", "#", "about:", "javascript:")):
            return m.group(0)
        local, status = pool.try_fetch(urljoin(page_url, raw))
        return f"url({local})" if status == "ok" else f"url({_PLACEHOLDER})"

    html_text = _STYLE_BLOCK_RE.sub(_style_block, html_text)

    # ---------------- 第四遍：把引用替换为最终结果（严格：非 ok 一律占位）----------------
    def _final(m: re.Match) -> str:
        tag, attrs = m.group(1), m.group(2)
        low = tag.lower()

        def attr_sub(am: re.Match) -> str:
            name, val = am.group(1), am.group(2)
            k = name.lower()
            quote = val[0] if val and val[0] in "\"'" else ""
            raw = (val[1:-1] if quote else val).strip()
            if not raw or raw.startswith(("data:", "#", "javascript:", "mailto:", "tel:", "about:")):
                return am.group(0)

            if k == "srcset":
                parts = []
                for u, desc in _split_srcset(raw):
                    # srcset 在第一遍不做绝对化，这里必须先解析再查池，否则永远查不到
                    local, status = pool.try_fetch(urljoin(page_url, u))
                    got = local if status == "ok" else _PLACEHOLDER
                    parts.append((got + (f" {desc}" if desc else "")).strip())
                return f'{name}={quote}{", ".join(parts)}{quote}'

            # 样式表：href 换成资源池里的本地路径（抓不到则由后续步骤整条删除）
            if k == "href" and low == "link":
                local, status = pool.try_fetch(raw)
                return f"{name}={quote}{local if status == 'ok' else raw}{quote}"

            if k not in ("src", "data-src", "poster"):
                return am.group(0)          # <a href> 已在第一遍绝对化，保留可点
            local, status = pool.try_fetch(raw)
            return f"{name}={quote}{local if status == 'ok' else _PLACEHOLDER}{quote}"

        new_attrs = _ATTR_RE.sub(attr_sub, attrs)
        return f"<{tag}{new_attrs}>"

    html_text = _TAG_RE.sub(_final, html_text)

    # 未本地化的 <link> 一律删掉（不留外部 URL）。
    # 严格零外发：样式表与**图标**都会被浏览器自动请求 —— 尤其 favicon，
    # 若保留外链，浏览「离线存档」时仍会向原站发请求（暴露 IP），与立身之本相悖。
    def _drop_dead_links(m: re.Match) -> str:
        tag, attrs = m.group(1), m.group(2)
        if tag.lower() != "link":
            return m.group(0)
        rel = href = ""
        for am in _ATTR_RE.finditer(attrs):
            k, v = am.group(1).lower(), am.group(2).strip("\"'")
            if k == "rel":
                rel = v
            elif k == "href":
                href = v
        if not href or href.startswith((ASSET_URL_PREFIX, "data:")):
            return m.group(0)                       # 已本地化 / 已内联 → 保留
        if _STYLESHEET_REL.search(rel):
            stats.sheets_dropped += 1
        elif _ICON_REL.search(rel):
            stats.icons_dropped += 1
        else:
            return m.group(0)                       # canonical / alternate 等不自动请求，保留
        return ""

    html_text = _TAG_RE.sub(_drop_dead_links, html_text)

    fetcher.close()
    stats.seconds = time.monotonic() - t0

    # ---------------- 结果说明 ----------------
    if stats.assets:
        stats.notes.append(
            f"已把 {stats.assets} 个资源存档到本地（{stats.bytes / 1024:.0f} KB"
            + (f"，{stats.reused} 个复用已有" if stats.reused else "")
            + f"，用时 {stats.seconds:.0f}s），原版预览可离线还原"
        )
    if stats.placeholder:
        stats.notes.append(
            f"{stats.placeholder} 个资源未能存档（超限 {stats.skipped_large}、"
            f"预算用尽 {stats.skipped_budget}、类型不适 {stats.skipped_type}、"
            f"抓取失败 {stats.failed}），已用占位代替以保证离线时不发起任何外部请求"
        )
    if stats.sheets_dropped:
        stats.notes.append(f"{stats.sheets_dropped} 个样式表未能存档，该页版式可能不完整")
    if stats.icons_dropped:
        stats.notes.append(
            f"{stats.icons_dropped} 个图标（favicon 等）未能存档，已删除以避免浏览时外发请求")
    if stats.scripts:
        stats.notes.append(f"已省略 {stats.scripts} 个外部脚本（预览沙箱禁用脚本，存档中不再保留）")
    if stats.skipped_blocked:
        # 只给 URL 与原因码：不记录响应内容，也不把外部 URL 留在文档里
        sample = "、".join(u[:80] for u, _ in pool.blocked[:3]) if pool is not None else ""
        stats.notes.append(
            f"{stats.skipped_blocked} 个资源被安全策略拦下（非公网地址或非法协议），"
            f"已用占位符替代{('：' + sample) if sample else ''}"
        )

    return html_text, stats


# --------------------------------------------------------------------------
# 预览期资源内联（B 方案）—— 让「网页快照」在沙箱 iframe 里也能显示图片
# --------------------------------------------------------------------------
# 为什么要内联：快照按设计放在 ``<iframe sandbox="">`` 里渲染。沙箱不给
# ``allow-same-origin`` → iframe 文档是**不透明源（opaque origin）**；不透明源请求
# ``/api/assets/...`` 时浏览器标 ``Sec-Fetch-Site: cross-site``，被
# ``core/security.py`` 的跨站闸门一律拒绝 → 图片与样式**全部 404**（真事故）。
#
# 与其放宽安全边界（不放宽），不如把资源**内联进 HTML**：页面自包含 → 浏览时零子请求
# → 既不触发闸门，也真正离线可看（与本项目「零外部请求」立身之本一致）。

# 资源扩展名 → data: URI 的 MIME
_DATA_URI_MIME = {
    ".css": "text/css",
    ".png": "image/png", ".jpg": "image/jpeg", ".jpeg": "image/jpeg",
    ".gif": "image/gif", ".webp": "image/webp", ".svg": "image/svg+xml",
    ".bmp": "image/bmp", ".ico": "image/x-icon",
    ".woff2": "font/woff2", ".woff": "font/woff", ".ttf": "font/ttf",
    ".otf": "font/otf", ".eot": "application/vnd.ms-fontobject",
}

# 匹配 HTML / CSS 中的本地资源引用：/api/assets/<16位hash>[.<扩展名>]
_ASSET_REF_RE = re.compile(r"/api/assets/([0-9a-f]{16})(\.[A-Za-z0-9]{1,8})?")

# CSS 递归内联的深度上限（防 @import 环）
_MAX_CSS_DEPTH = 6


def _resolve_asset_name(hash16: str, ext: str) -> str | None:
    """把 (hash, ext) 解析成 ``data/assets/`` 下真实存在的文件名。

    优先用带扩展名的精确名；扩展名缺失 / 对不上时，用 hash 前缀找**唯一**候选，
    找不到返回 None（调用方用占位符兜底）。
    """
    if ext:
        name = hash16 + ext
        if (paths.ASSETS_DIR / name).is_file():
            return name
    try:
        cands = [p.name for p in paths.ASSETS_DIR.glob(hash16 + ".*") if p.is_file()]
    except OSError:
        return None
    return cands[0] if len(cands) == 1 else None


def _asset_data_uri(name: str, cache: dict, inprog: set, depth: int = 0) -> str:
    """读取资源并转成 ``data:`` URI；CSS 会**递归内联**其内部 url()/@import。

    读不到 / 循环引用 / 超深 → 一律 1×1 占位（保证页面自包含，绝不留下会外发的引用）。
    """
    if name in cache:
        return cache[name]
    if name in inprog or depth > _MAX_CSS_DEPTH:
        return _PLACEHOLDER
    try:
        raw = (paths.ASSETS_DIR / name).read_bytes()
    except OSError:
        cache[name] = _PLACEHOLDER
        return _PLACEHOLDER

    ext = Path(name).suffix.lower()
    if ext == ".css":
        # CSS 内部的 url()/@import **必须先内联**：否则 data: 样式里的
        # ``/api/assets/...`` 会以**文档**为基准解析 → 又变回 cross-site 请求。
        inprog.add(name)
        try:
            css = raw.decode("utf-8", "replace")
            css = _ASSET_REF_RE.sub(
                lambda m: _ref_data_uri(m, cache, inprog, depth + 1), css)
            raw = css.encode("utf-8")
        except Exception:  # noqa: BLE001 - 内联失败退回原 CSS
            pass
        finally:
            inprog.discard(name)
        mime = "text/css"
    else:
        mime = _DATA_URI_MIME.get(ext, "application/octet-stream")

    uri = "data:" + mime + ";base64," + base64.b64encode(raw).decode("ascii")
    cache[name] = uri
    return uri


def _ref_data_uri(m: re.Match, cache: dict, inprog: set, depth: int = 0) -> str:
    name = _resolve_asset_name(m.group(1), m.group(2) or "")
    return _asset_data_uri(name, cache, inprog, depth) if name else _PLACEHOLDER


def _defuse_external_links(m: re.Match) -> str:
    """删掉**会自动请求的外部 <link>**（favicon / 未本地化样式 / 资源提示）。

    防御性收尾：老快照（旧 archiver 抓的）可能残留外部 favicon —— 浏览时浏览器
    仍会去原站取图标，暴露 IP。B 方案要的是「零子请求」，这里一并清掉。
    canonical / alternate 等不自动请求的 link 保留。
    """
    tag, attrs = m.group(1), m.group(2)
    if tag.lower() != "link":
        return m.group(0)
    rel = href = ""
    for am in _ATTR_RE.finditer(attrs):
        k, v = am.group(1).lower(), am.group(2).strip("\"'")
        if k == "rel":
            rel = v
        elif k == "href":
            href = v
    if not href or not re.match(r"https?://", href, re.I):
        return m.group(0)
    if _DROP_REL.search(rel) or _STYLESHEET_REL.search(rel) or _ICON_REL.search(rel):
        return ""
    return m.group(0)


def inline_assets(html_text: str) -> str:
    """把剪藏 HTML 里的 ``/api/assets/<name>`` 全部内联为 ``data:`` URI。

    产出**自包含**页面：在 ``<iframe sandbox="">`` 里浏览时零子请求 —— 既不触发
    跨站闸门（图片/样式不再 404），也真正离线可看。引用缺失 / 读盘失败 → 用 1×1
    占位，**绝不保留会外发的 URL**。
    """
    if not html_text or ASSET_URL_PREFIX not in html_text:
        return html_text
    cache: dict[str, str] = {}
    inprog: set = set()
    out = _ASSET_REF_RE.sub(lambda m: _ref_data_uri(m, cache, inprog, 0), html_text)
    # 安全网：异常命名（非 16 位 hash）的残留引用也换成占位，保证**零子请求**
    if ASSET_URL_PREFIX in out:
        out = re.sub(re.escape(ASSET_URL_PREFIX) + r"""[^"'\s)>]*""", _PLACEHOLDER, out)
    # 防御性收尾：清掉会自动请求的外部 link（老快照可能残留外部 favicon）
    return _TAG_RE.sub(_defuse_external_links, out)
