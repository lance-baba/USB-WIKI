"""知识摄入管道 —— 网页抓取、正文清洗与透明降级（PRD 4.2）。

承诺边界：**静态博客与科技文章优先**。动态 SPA / 强反爬页面不硬刚，
转为保存原始快照 + 引导剪贴板录入的降级路径。
"""
from __future__ import annotations

import html as html_mod
import re
import shutil
import subprocess
import time
from dataclasses import dataclass, field
from datetime import datetime
from html.parser import HTMLParser
from pathlib import Path
from urllib.parse import urlparse

from . import (analyzer, archiver, atomic_io, config, file_guard,
                   net_guard as _net_guard, net_util, paths)
from .log_util import get_logger

log = get_logger()

_NAME_SAFE_RE = re.compile(r"[^\w\u4e00-\u9fff\-]+")
_WS_RE = re.compile(r"[ \t\u00a0]+")
_BLANKLINE_RE = re.compile(r"\n{3,}")
_TITLE_TAG_RE = re.compile(r"<title[^>]*>(.*?)</title>", re.DOTALL | re.IGNORECASE)
_OG_TITLE_RE = re.compile(
    r'<meta[^>]+property=["\']og:title["\'][^>]+content=["\'](.*?)["\']', re.IGNORECASE
)
_META_RE = {
    "author": re.compile(
        r'<meta[^>]+name=["\'](?:author|article:author)["\'][^>]+content=["\'](.*?)["\']',
        re.IGNORECASE,
    ),
    "date": re.compile(
        r'<meta[^>]+(?:property|name)=["\'](?:article:published_time|date|pubdate)["\']'
        r'[^>]+content=["\'](.*?)["\']',
        re.IGNORECASE,
    ),
}


@dataclass
class CaptureResult:
    ok: bool
    status: str
    title: str = ""
    file_path: str = ""
    abs_path: str = ""
    char_count: int = 0
    message: str = ""
    snapshot_path: str = ""
    original_path: str = ""
    used: str = "trafilatura"
    # 命中重复来源 URL 时，带上已存在笔记的信息，供前端给出「打开/更新/另存/取消」
    duplicate: dict | None = None
    # 系统自愈 / 自动降级的过程信息（按项目约定：这类信息不打扰用户，
    # 只作为说明随结果返回，不进顶部告警条）
    notes: list[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {
            "code": 200 if self.ok else 500,
            "status": self.status,
            "message": self.message,
            "data": {
                "title": self.title,
                "file_path": self.file_path,
                "char_count": self.char_count,
                "snapshot_path": self.snapshot_path,
                "original_path": self.original_path,
                "extractor": self.used,
                "notes": self.notes,
            },
        }


# --------------------------------------------------------------------------
class _TextExtractor(HTMLParser):
    """标准库 html.parser 兜底正文抽取（无 lxml / 无 trafilatura 时的 bare mode）。"""

    SKIP = {"script", "style", "noscript", "svg", "head", "template", "iframe"}
    BLOCK = {
        "p", "div", "section", "article", "br", "li", "tr", "h1", "h2", "h3",
        "h4", "h5", "h6", "blockquote", "pre", "td", "figure", "header", "footer",
    }

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []
        self._skip_depth = 0

    def handle_starttag(self, tag, attrs):
        if tag in self.SKIP:
            self._skip_depth += 1
        elif tag in self.BLOCK:
            self.parts.append("\n")

    def handle_endtag(self, tag):
        if tag in self.SKIP and self._skip_depth > 0:
            self._skip_depth -= 1
        elif tag in self.BLOCK:
            self.parts.append("\n")

    def handle_data(self, data):
        if self._skip_depth == 0 and data:
            self.parts.append(data)

    def text(self) -> str:
        raw = "".join(self.parts)
        raw = _WS_RE.sub(" ", raw)
        lines = [ln.strip() for ln in raw.split("\n")]
        return _BLANKLINE_RE.sub("\n\n", "\n".join(ln for ln in lines if ln))


def stdlib_extract(html_text: str) -> str:
    p = _TextExtractor()
    try:
        p.feed(html_text)
        p.close()
    except Exception:  # noqa: BLE001 - 畸形 HTML 不应中断流程
        pass
    return p.text()


def _meta_of(html_text: str) -> dict:
    out: dict[str, str] = {}
    m = _OG_TITLE_RE.search(html_text) or _TITLE_TAG_RE.search(html_text)
    if m:
        out["title"] = html_mod.unescape(m.group(1)).strip()
    for key, rx in _META_RE.items():
        mm = rx.search(html_text)
        if mm:
            out[key] = html_mod.unescape(mm.group(1)).strip()[:120]
    return out


# --------------------------------------------------------------------------
_META_CHARSET_RE = re.compile(
    rb"""<meta[^>]+charset\s*=\s*["']?\s*([\w-]+)""", re.I
)


def resolve_html_encoding(raw: bytes, content_type: str = "", apparent: str = "") -> str:
    """判定 HTML 字节流的真实编码。

    ⚠ 不能依赖 requests 的 `resp.encoding`：对 `text/*` 且**未声明 charset** 的响应，
    requests 会按 RFC 2616 默认成 ISO-8859-1。这个默认值在今天几乎总是错的
    （页面普遍是 UTF-8），会把中文与 em dash 变成 `â\x80\x94` 这类乱码。
    更隐蔽的是 `resp.encoding or resp.apparent_encoding` ——
    `ISO-8859-1` 是**真值**，`or` 直接短路，内容嗅探结果永远取不到。

    优先级：HTTP 头 charset → BOM → <meta charset> → 字节嗅探 → 启发式。
    """
    m = re.search(r"charset\s*=\s*[\"']?([\w-]+)", content_type or "", re.I)
    if m:
        return m.group(1)

    if raw.startswith(b"\xef\xbb\xbf"):
        return "utf-8-sig"
    if raw[:2] in (b"\xff\xfe", b"\xfe\xff"):
        return "utf-16"

    # 页面自述的 charset 优先，但要先验证它真能解开，否则视为说谎、落到嗅探
    mm = _META_CHARSET_RE.search(raw[:4096])
    if mm:
        try:
            cand = mm.group(1).decode("ascii").strip().lower()
            if cand:
                raw[:8192].decode(cand)
                return cand
        except (UnicodeDecodeError, LookupError):
            pass

    for enc in ("utf-8", "gb18030", "big5"):
        try:
            raw.decode(enc)
            return enc
        except UnicodeDecodeError:
            continue
    return apparent or "utf-8"


def _net_guard_ready() -> bool:
    """安全网络出口是否可用（导入失败时明确降级，不静默放行）。"""
    return _net_guard is not None


def fetch_html(url: str, timeout: float | None = None) -> tuple[str, str]:
    """抓取页面 HTML。返回 (html, error)。

    ⚠ 必须走 :func:`net_guard.safe_fetch` —— 这是本模块**唯一**允许访问
    用户提供 URL 的出口。此前这里用 ``requests.get(allow_redirects=True)``：
    既自动跟随重定向（校验形同虚设），又会在建连时**二次解析 DNS**
    （TOCTOU / DNS rebinding 窗口）。

    safe_fetch 会逐跳做完整 SSRF 校验，并把校验过的 IP 钉住后再连接。
    """
    timeout = timeout or config.get_float("CRAWLER", "request_timeout", 20.0)
    if not _net_guard_ready():
        return "", "网络出口未就绪"

    res = _net_guard.safe_fetch(
        url,
        headers={
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
            "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
            "Cache-Control": "no-cache",
        },
        timeout=timeout,
        allow_private_network=config.get_bool("CRAWLER", "allow_private_network", False),
    )
    if not res.ok:
        # 安全拒绝要与普通网络错误区分开，否则用户不知道是被策略挡住了
        return "", res.message or "抓取失败"
    if res.status >= 400:
        return "", f"HTTP {res.status}"

    raw = res.body
    enc = resolve_html_encoding(
        raw, res.headers.get("Content-Type", ""), ""
    )
    return raw.decode(enc, "replace"), ""



def extract_body(html_text: str, url: str) -> tuple[str, dict, str]:
    """返回 (正文markdown, 元数据, 使用的抽取器)。"""
    meta = _meta_of(html_text)
    # 1) trafilatura（lxml 加速）
    try:
        import trafilatura  # type: ignore

        text = trafilatura.extract(
            html_text,
            url=url,
            output_format="markdown",
            include_comments=False,
            include_tables=True,
            include_links=False,
            favor_precision=False,
        )
        if text and len(text.strip()) >= 1:
            return text.strip(), meta, "trafilatura"
        bare = trafilatura.bare_extraction(html_text, url=url)
        if bare is not None:
            body = getattr(bare, "text", None) or (
                bare.get("text") if isinstance(bare, dict) else None
            )
            if body:
                for k in ("title", "author", "date"):
                    v = getattr(bare, k, None) or (
                        bare.get(k) if isinstance(bare, dict) else None
                    )
                    if v and not meta.get(k):
                        meta[k] = str(v)
                return str(body).strip(), meta, "trafilatura-bare"
    except ImportError:
        log.warning("trafilatura 缺失，启用 html.parser bare mode")
    except Exception as exc:  # noqa: BLE001
        log.warning("trafilatura 抽取异常，降级 html.parser: %s", exc)

    # 2) 标准库兜底（无 C 依赖模式）
    text = stdlib_extract(html_text)
    return text, meta, "html.parser"


# --------------------------------------------------------------------------
def chrome_executable() -> str | None:
    import os

    candidates = [
        os.environ.get("CHROME_PATH", ""),
        r"C:\Program Files\Google\Chrome\Application\chrome.exe",
        r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe",
        os.path.expandvars(r"%LOCALAPPDATA%\Google\Chrome\Application\chrome.exe"),
        r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe",
        r"C:\Program Files\Microsoft\Edge\Application\msedge.exe",
        "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
        "/usr/bin/google-chrome",
        "/usr/bin/chromium",
        "/usr/bin/chromium-browser",
    ]
    for c in candidates:
        if c and Path(c).exists():
            return c
    for name in ("google-chrome", "chromium", "chrome", "msedge"):
        found = shutil.which(name)
        if found:
            return found
    return None


def fetch_with_chrome(url: str, timeout: float = 30.0) -> str:
    """Headless Chrome 渲染抓取（沙箱隔离 + 强制清理临时 profile，PRD 4.2）。

    ⚠ SSRF 闸门：**必须在起子进程之前**做完整校验。

    为什么不能省：Chrome 会**自己**解析 DNS 并跟随重定向，既不受
    :func:`net_guard.safe_fetch` 的逐跳校验约束，也无法像它那样把校验过的
    IP 钉住后再连接。所以若这里不加闸门，下面这条链就是一条完整的 SSRF 绕过：

        safe_fetch 安全拒绝 → html_text 为空 → 走到本函数的 Chrome fallback
        → 被策略挡住的内网地址由 Chrome 代抓

    这里复用与 ``safe_fetch`` **完全相同** 的 :func:`net_guard.resolve_and_validate`，
    保证两条路径策略一致；不符策略时抛 :class:`net_guard.SSRFBlocked`。
    """
    if _net_guard is not None:
        # 与 fetch_html 走同一个 allow_private_network 配置，避免
        # 「HTTP 路放行、Chrome 路拒绝」或反之的策略漂移。
        _net_guard.resolve_and_validate(
            url,
            allow_private_network=config.get_bool(
                "CRAWLER", "allow_private_network", False),
        )

    exe = chrome_executable()
    if not exe:
        raise RuntimeError("未找到本机 Chrome/Edge 可执行文件")

    profile = paths.CHROME_PROFILE_DIR
    shutil.rmtree(profile, ignore_errors=True)
    profile.mkdir(parents=True, exist_ok=True)

    cmd = [
        exe,
        "--headless=new",
        "--disable-gpu",
        "--no-first-run",
        "--no-default-browser-check",
        "--disable-extensions",
        "--disable-background-networking",
        f"--user-data-dir={profile}",
        "--virtual-time-budget=8000",
        "--dump-dom",
        url,
    ]
    proc = None
    try:
        proc = subprocess.Popen(
            cmd, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
            timeout=timeout, creationflags=_no_window_flag(),
        )
        out, _ = proc.communicate(timeout=timeout)
        return (out or b"").decode("utf-8", "replace")
    finally:
        # 强制杀死 Chrome 进程树并递归删除临时目录，严禁残留侵占 U 盘
        try:
            if proc and proc.poll() is None:
                proc.kill()
                proc.wait(timeout=5)
        except (OSError, subprocess.SubprocessError):
            pass
        time.sleep(0.2)
        for attempt in range(3):
            try:
                shutil.rmtree(profile, ignore_errors=False)
                break
            except OSError:
                time.sleep(0.3)
        else:
            shutil.rmtree(profile, ignore_errors=True)


def _no_window_flag() -> int:
    if hasattr(subprocess, "CREATE_NO_WINDOW"):
        return subprocess.CREATE_NO_WINDOW  # type: ignore[attr-defined]
    return 0


# --------------------------------------------------------------------------
def _safe_name(url: str, when: datetime) -> str:
    stamp = when.strftime("%Y%m%d_%H%M%S")
    try:
        slug = _NAME_SAFE_RE.sub("-", (urlparse(url).path or "").strip("/").split("/")[-1])[:24]
    except ValueError:
        slug = ""
    slug = slug.strip("-")
    return f"web_{stamp}{('_' + slug) if slug else ''}.md"


def _frontmatter(d: dict) -> str:
    lines = ["---"]
    for k, v in d.items():
        if v is None or v == "":
            continue
        safe = str(v).replace('"', "'").replace("\n", " ").strip()
        lines.append(f'{k}: "{safe}"')
    lines.append("---")
    return "\n".join(lines)


def save_markdown(
    title: str,
    body: str,
    url: str,
    meta: dict,
    status: str,
    when: datetime | None = None,
    html_text: str = "",
    extra: dict | None = None,
    target_path: Path | None = None,
) -> Path:
    when = when or datetime.now()
    paths.NOTES_DIR.mkdir(parents=True, exist_ok=True)
    # target_path 用于「更新已有」：沿用原文件名，避免同一页面变成两篇笔记
    target = Path(target_path) if target_path else paths.NOTES_DIR / _safe_name(url, when)
    # 自动命名的时间戳只到秒：同一秒内抓两次会撞名，**静默覆盖**掉前一篇。
    # （写测试时实测到：连续两次抓取只留下 1 个文件。）
    # 撞名时改加数字后缀；显式指定 target_path（更新已有）时不改，那种覆盖是预期的。
    if target_path is None and target.exists():
        for _n in range(2, 100):
            cand = target.with_name(f"{target.stem}-{_n}{target.suffix}")
            if not cand.exists():
                target = cand
                break

    fields = {
        "title": title or url,
        "source_url": url,
        "author": meta.get("author", ""),
        "published": meta.get("date", ""),
        "captured_at": when.strftime("%Y-%m-%d %H:%M:%S"),
        "status": status,
        "doc_type": "web_capture",
    }
    # 入库分析结果（关键词 / 摘要等）一并写进 frontmatter ——
    # Markdown 是真相源，元数据必须跟它走，索引只是衍生。
    for k, v in (extra or {}).items():
        if v:
            fields[k] = v
    fm = _frontmatter(fields)
    content = f"{fm}\n\n# {title or url}\n\n{body.strip()}\n"
    # 真相源必须原子写：半截笔记比没有笔记更糟（原内容已被截断）
    atomic_io.atomic_write_text(target, content)

    # 留存原始 HTML：让「原版预览」能用沙箱 iframe 还原网页原本的观感
    if html_text:
        save_original(target.stem, ".html", html_text.encode("utf-8", "replace"))
    return target


def save_snapshot(url: str, html_text: str, when: datetime | None = None) -> Path:
    when = when or datetime.now()
    paths.SNAPSHOT_DIR.mkdir(parents=True, exist_ok=True)
    snap = paths.SNAPSHOT_DIR / _safe_name(url, when).replace(".md", ".html")
    header = f"<!-- 原始快照 source_url={url} captured_at={when:%Y-%m-%d %H:%M:%S} -->\n"
    try:
        atomic_io.atomic_write_text(snap, header + html_text)
    except OSError as exc:
        log.error("快照写入失败: %s", exc)
    return snap


# --------------------------------------------------------------------------
def capture_url(url: str, db=None, embedder=None, on_duplicate: str = "abort") -> CaptureResult:
    """抓取 -> 清洗 -> 阈值判定 -> 落盘（成功/降级）-> 索引。

    ``on_duplicate`` 处理「同一来源 URL 已抓过」：

    * ``abort``（默认）—— 不抓取，直接返回 ``status="duplicate"`` 并附上已有笔记信息。
      **默认拒绝而不是默默新建**：重复笔记会污染检索、引用、关键词、主题与统计。
    * ``update`` —— 重新抓取并**覆盖已存在的那篇笔记**（页面会更新，这是正当需求）。
    * ``new``    —— 另存为新笔记（保留旧版本）。

    判重在**抓取之前**完成，因此 abort 分支不会产生任何网络请求。
    前端是否预先查过重复都无所谓 —— 后端自己一定会查。
    """
    url = (url or "").strip()
    if not re.match(r"^https?://", url, re.IGNORECASE):
        return CaptureResult(False, "error", message="仅支持 http/https 开头的标准 URL")

    # ---- 来源 URL 判重（归一化后比较：utm / fragment / 尾斜杠等差异不算新页面）----
    existing: dict | None = None
    if db is not None and on_duplicate != "new":
        try:
            from . import indexer as _indexer  # noqa: PLC0415 - 避免循环导入

            existing = _indexer.find_by_normalized_url(db, url)
        except Exception as exc:  # noqa: BLE001 - 判重失败不应阻断抓取
            log.warning("来源 URL 判重失败（按未重复继续）: %s", exc)
            existing = None

    if existing and on_duplicate == "abort":
        return CaptureResult(
            False, "duplicate",
            title=existing.get("title", ""),
            file_path=existing.get("rel_path", ""),
            message=(
                f"该网页已经保存过：{existing.get('title', '')}"
                "（可在弹窗里选择打开已有 / 更新已有 / 另存为新版本）"
            ),
            duplicate={**existing, "action": "abort"},
        )

    min_chars = config.get_int("CRAWLER", "min_body_chars", 150)
    snapshot_chars = config.get_int("CRAWLER", "snapshot_chars", 1000)
    use_chrome = config.get_bool("CRAWLER", "enable_headless_chrome", False)

    html_text, err = fetch_html(url)
    used = "http"

    if (not html_text or len(html_text) < 400) and use_chrome:
        try:
            html_text = fetch_with_chrome(url)
            used = "headless-chrome"
        except _net_guard.SSRFBlocked as exc:
            # 安全拒绝**不能**被 headless fallback 悄悄吞掉 —— 否则用户拿到的
            # 是含糊的「页面抓取失败」，却不知道其实是被安全策略挡住了。
            err = f"安全策略已拒绝该地址：{exc}"
            html_text = ""
        except Exception as exc:  # noqa: BLE001
            log.warning("Headless Chrome 抓取失败: %s", exc)

    if not html_text:
        return CaptureResult(False, "error", message=f"页面抓取失败：{err or '空响应'}")

    body, meta, extractor = extract_body(html_text, url)
    used = f"{used}+{extractor}"
    title = str(meta.get("title") or "").strip()

    # 正文已经提取完毕（Markdown 里保留的是**原始**链接），此刻再把 HTML 存档的
    # 子资源落进本地资源池 —— 之后浏览「原版」就不再需要网络。
    # 顺序不能反：先本地化会让 Markdown 里混进 /api/assets 本地路径。
    loc_notes: list[str] = []
    try:
        html_text, _loc = archiver.localize(html_text, url)
        loc_notes = list(_loc.notes)
        if _loc.assets:
            used = f"{used}+assets{_loc.assets}"
    except Exception as exc:  # noqa: BLE001 - 本地化失败不应让抓取失败
        log.warning("资源本地化失败，本次按未本地化存档：%s", exc)
        loc_notes.append("资源本地化失败，该页离线时样式与图片可能不完整")

    # 入库语义分析。确定性层（关键词/实体/语言）永远执行、零依赖；
    # AI 摘要属增强层，模型不可用时静默跳过，绝不影响入库。
    ana_extra: dict = {}
    try:
        ana = analyzer.analyze(body, title)
        if ana.keywords:
            ana_extra["keywords"] = ana.keywords
        if ana.entities:
            ana_extra["entities"] = {k: v[:3] for k, v in ana.entities.items()}
        if ana.language:
            ana_extra["language"] = ana.language
        if config.get_bool("ANALYZE", "ai_summary", True):
            try:
                from . import llm as llm_mod  # noqa: PLC0415

                gw = llm_mod.get_gateway(db) if db is not None else None
                summary = analyzer.summarize(body, title, gw)
                if summary:
                    ana_extra["summary"] = summary
            except Exception as exc:  # noqa: BLE001 - 摘要失败不影响入库
                log.warning("AI 摘要生成失败（已跳过）: %s", exc)
    except Exception as exc:  # noqa: BLE001 - 分析失败不影响入库
        log.warning("入库分析失败（已跳过）: %s", exc)

    # ---- 成功分支 ----
    if len(body) >= min_chars:
        target = save_markdown(
            title, body, url, meta, "success", html_text=html_text, extra=ana_extra,
            # update：直接覆盖已存在的那篇，保持同一路径（不再多出一篇）
            target_path=(paths.NOTES_DIR / existing["rel_path"].split("/")[-1]
                         if (existing and on_duplicate == "update"
                             and existing.get("rel_path")) else None),
        )
        result = CaptureResult(
            True, "success", title=title or url,
            file_path=paths.rel_to_data(target), abs_path=str(target),
            char_count=len(body), message="抓取成功", used=used,
            notes=loc_notes,
        )
    # ---- 降级分支 ----
    else:
        snap = save_snapshot(url, html_text)
        excerpt = body[:snapshot_chars] if body else "（该页面未提供可抽取的静态正文）"
        degraded = (
            f"> ⚠️ 该页面为前端动态渲染，仅保留快照，建议通过复制粘贴方式记录重要内容。\n\n"
            f"> 原始快照：`{paths.rel_to_data(snap)}`\n\n{excerpt}"
        )
        target = save_markdown(title, degraded, url, meta, "partial_fallback",
                              html_text=html_text, extra=ana_extra)
        result = CaptureResult(
            True, "partial_fallback", title=title or url,
            file_path=paths.rel_to_data(target), abs_path=str(target),
            char_count=len(body), snapshot_path=paths.rel_to_data(snap),
            message="该页面为前端动态渲染，仅保留快照，建议通过复制粘贴方式记录重要内容",
            used=used,
            notes=loc_notes,
        )

    # 记录原件路径（供界面「原版预览」）
    _orig = find_original(result.file_path)
    if _orig is not None:
        result.original_path = paths.rel_to_data(_orig)

    # ---- 立即索引 ----
    if db is not None and result.abs_path:
        try:
            from . import indexer  # 局部导入避免循环依赖

            r = indexer.index_file(db, Path(result.abs_path), embedder)
            if not r.get("ok"):
                result.message += f"（索引写入失败：{r.get('error')}）"
        except Exception as exc:  # noqa: BLE001
            log.error("抓取后索引失败: %s", exc)
            result.message += f"（索引异常：{exc}）"

    log.info("剪藏 [%s] %s -> %s (%d 字)", result.status, url, result.file_path, result.char_count)
    return result


def save_manual_note(title: str, body: str, db=None, embedder=None, source: str = "manual") -> CaptureResult:
    """剪贴板/手工录入通道（降级引导的落点）。"""
    title = (title or "").strip() or "未命名笔记"
    body = (body or "").strip()
    if not body:
        return CaptureResult(False, "error", message="内容为空，未保存")

    when = datetime.now()
    paths.NOTES_DIR.mkdir(parents=True, exist_ok=True)
    slug = _NAME_SAFE_RE.sub("-", title)[:32].strip("-") or "note"
    target = paths.NOTES_DIR / f"note_{when:%Y%m%d_%H%M%S}_{slug}.md"
    fm = _frontmatter(
        {
            "title": title,
            "source_url": "",
            "captured_at": when.strftime("%Y-%m-%d %H:%M:%S"),
            "status": "success",
            "doc_type": source,
        }
    )
    atomic_io.atomic_write_text(target, f"{fm}\n\n# {title}\n\n{body}\n")

    result = CaptureResult(
        True, "success", title=title, file_path=paths.rel_to_data(target),
        abs_path=str(target), char_count=len(body), message="笔记已保存",
        used="manual",
    )
    if db is not None:
        try:
            from . import indexer  # noqa: PLC0415

            indexer.index_file(db, target, embedder)
        except Exception as exc:  # noqa: BLE001
            result.message += f"（索引异常：{exc}）"
    return result


MAX_ORIGINAL_BYTES = 50 * 1024 * 1024      # 原文件留存上限，防异常大文件塞爆 U 盘

# 这些格式本身就是 Markdown/纯文本，笔记即原件，不必重复留存
_SELF_STORED_KINDS = {"markdown", "text"}


def save_original(stem: str, ext: str, data: bytes) -> str:
    """把导入文件的**原件**留存到 data/originals/，供界面「原版预览」使用。

    返回相对 data/ 的路径；未留存时返回空串。
    """
    if not data or len(data) > MAX_ORIGINAL_BYTES:
        return ""
    ext = (ext or "").lower()
    if not ext.startswith("."):
        ext = "." + ext
    if len(ext) > 12 or not re.fullmatch(r"\.[A-Za-z0-9]+", ext):
        return ""
    try:
        paths.ORIGINALS_DIR.mkdir(parents=True, exist_ok=True)
        # ⚠ stem 来自**用户上传的文件名** → 必须先清洗再解析级校验，
        # 否则 `../../evil` / `C:\Windows\x` / UNC 都能写出允许目录之外。
        safe_stem = file_guard.safe_filename(stem, default="original")
        target = file_guard.safe_join(paths.ORIGINALS_DIR, f"{safe_stem}{ext}")
        # 撞名不静默覆盖：同名导入保留两份（此前已抓到过「同一秒覆盖」的真实 bug）
        if target.exists():
            for _i in range(2, 100):
                cand = target.with_name(f"{target.stem}-{_i}{ext}")
                if not cand.exists():
                    target = cand
                    break
        atomic_io.atomic_write_bytes(target, data)
        return f"originals/{target.name}"
    except file_guard.FileGuardError as exc:
        log.warning("原件文件名被安全策略拒绝 %s%s: %s", stem, ext, exc.code)
        return ""
    except OSError as exc:
        log.warning("原文件留存失败 %s%s: %s", stem, ext, exc)
        return ""


def find_original(rel_note_path: str) -> Path | None:
    """按笔记路径反查原件（同 stem 任意扩展名）。

    允许笔记被重命名后仍能找到原件；找不到时返回 None。
    """
    try:
        stem = Path(rel_note_path).stem
    except (TypeError, ValueError):
        return None
    if not stem:
        return None
    if not paths.ORIGINALS_DIR.exists():
        return None
    for p in sorted(paths.ORIGINALS_DIR.iterdir()):
        if p.is_file() and p.stem == stem:
            return p
    return None


_BASE_TAG_RE = re.compile(r"<base\b", re.I)
_HEAD_OPEN_RE = re.compile(r"<head\b[^>]*>", re.I)
_HTML_OPEN_RE = re.compile(r"<html\b[^>]*>", re.I)


def source_url_of(rel_note_path: str) -> str:
    """从笔记 frontmatter 读取 source_url（读不到返回空串）。"""
    try:
        text = (paths.DATA_DIR / rel_note_path).read_text(encoding="utf-8", errors="replace")
    except (OSError, ValueError):
        return ""
    m = re.search(r'^source_url:\s*"?([^"\n]+?)"?\s*$', text[:2000], re.M)
    return m.group(1).strip() if m else ""


def inject_base_href(html_text: str, source_url: str) -> str:
    """给剪藏的原网页注入 <base>，让相对路径按**原始站点**解析。

    ⚠ 这是「原版预览」保真度的关键：抓下来的 HTML 大量使用根相对路径
    （如 `/assets/css/common.css`、`/assets/img/logo.png`）。不注入 <base> 时，
    这些 URL 会以本站（/api/notes/original…）为基准解析 → 全部 404 →
    样式与图片尽失，页面渲染成一副裸 HTML，「原版」就名不副实了。

    用**完整页面 URL** 作 base（而非仅站点根）：这样文档相对路径
    （`assets/x.css`）与根相对路径（`/assets/x.css`）都能正确解析。
    """
    if not source_url or _BASE_TAG_RE.search(html_text):
        return html_text
    tag = '<base href="' + html_mod.escape(source_url, quote=True) + '">'
    m = _HEAD_OPEN_RE.search(html_text)
    if m:
        return html_text[: m.end()] + tag + html_text[m.end():]
    m2 = _HTML_OPEN_RE.search(html_text)
    if m2:
        return html_text[: m2.end()] + "<head>" + tag + "</head>" + html_text[m2.end():]
    return tag + html_text


def find_orphan_originals(note_paths) -> list:
    """**只报告、不删除**：列出暂未关联到笔记的原件文件名。

    USB-WIKI Data Contract：``originals/`` 是用户导入/保存的**原始资料**，
    是 durable 数据 —— 不能因为「Markdown 暂时不存在 / 索引未加载 / Library 切换 /
    同步基线异常 / 测试环境状态」就被后台自动删除。真实事故：原件目录 4 份文件暂时
    没有对应笔记（首次启动、索引尚未建立），同步器把它们当孤儿**删掉了**。

    Pilot 口径（D1/D3）：
    * NEVER AUTO DELETE ORIGINALS —— 任何后台路径都不得 unlink/delete/recycle/移入回收站；
    * ``notes/ == 0 而 originals/ > 0`` 是**合法状态**（不是异常，更不是可删信号）；
    * 宁可留占位空间，也不可丢失用户原始资料。

    仍然提供 stem 比对的结果（供 UI / debug 展示），但调用方**不得**据此删除文件。
    """
    stems = {Path(str(x)).stem for x in (note_paths or ())}
    if not paths.ORIGINALS_DIR.exists():
        return []
    orphans = [
        f.name for f in paths.ORIGINALS_DIR.iterdir()
        if f.is_file() and f.stem not in stems
    ]
    if orphans:
        log.info("发现 %d 份暂未关联原件，已保留（Pilot 不自动删除用户原件）: %s",
                 len(orphans),
                 ", ".join(sorted(orphans)[:10]) + (" …" if len(orphans) > 10 else ""))
    return sorted(orphans)


def purge_orphan_originals(note_paths) -> list:      # noqa: D401 - 保留旧名兼容
    """已停用（Pilot）：原件永不自动删除。保留本函数只为兼容旧调用点，恒返回 []。

    历史实现会 unlink 孤儿原件 —— 那违反 Data Contract（用户原件属于 durable 数据）。
    现在改为只报告：名字保留、行为变成 :func:`find_orphan_originals` 的报告路径。
    """
    find_orphan_originals(note_paths)
    return []


def import_document(
    filename: str,
    data: bytes,
    db=None,
    embedder=None,
    overwrite: bool = False,
) -> CaptureResult:
    """导入任意受支持格式的文件：先转成 Markdown，再落盘建索引。

    转换能力见 ``core/converters.py``（docx/pptx/xlsx/epub 用标准库 zipfile+XML 解，
    html/邮件复用 trafilatura，pdf 需 pypdf）。
    """
    raw_name = str(filename or "").strip().replace("\\", "/").split("/")[-1]
    if not raw_name:
        return CaptureResult(False, "error", message="缺少文件名")
    if raw_name.startswith("."):
        return CaptureResult(False, "error", message=f"{raw_name}：不支持隐藏文件")

    from . import converters  # 惰性导入，避免循环依赖

    conv = converters.convert(data, raw_name)
    if not conv.ok:
        return CaptureResult(False, "error", message=conv.error)

    stem = Path(raw_name).stem.strip() or "imported"
    safe_stem = _NAME_SAFE_RE.sub("-", stem)[:60].strip("-") or "imported"
    paths.NOTES_DIR.mkdir(parents=True, exist_ok=True)
    target = paths.NOTES_DIR / f"{safe_stem}.md"
    if target.exists() and not overwrite:
        target = paths.NOTES_DIR / f"{safe_stem}_{datetime.now():%Y%m%d_%H%M%S}.md"

    body = conv.markdown.strip()
    if body.lstrip().startswith("---"):
        text = body + "\n"                      # 转换结果自带 frontmatter（如 .md 原样导入）
    else:
        when = datetime.now()
        fm = _frontmatter(
            {
                "title": conv.title or stem,
                "source_file": raw_name,
                "source_type": conv.kind,
                "imported_at": when.strftime("%Y-%m-%d %H:%M:%S"),
                "status": "success",
                "doc_type": "imported",
            }
        )
        text = f"{fm}\n\n{body}\n"

    try:
        atomic_io.atomic_write_text(target, text)
    except OSError as exc:
        return CaptureResult(False, "error", message=f"写入失败：{exc}")

    note = f"（{conv.kind} → Markdown）" if conv.kind not in ("markdown",) else ""
    # 留存原件：界面「原版预览」用它还原原始观感（PDF 用浏览器原生查看器）
    original_rel = ""
    if conv.kind not in _SELF_STORED_KINDS:
        original_rel = save_original(target.stem, Path(raw_name).suffix, data)

    result = CaptureResult(
        True, "success", title=conv.title or stem,
        file_path=paths.rel_to_data(target), abs_path=str(target),
        char_count=conv.char_count,
        message=f"已导入 {raw_name}{note}",
        used=f"import:{conv.kind}",
    )
    result.original_path = original_rel
    if conv.warnings:
        result.message += "；" + "；".join(conv.warnings[:2])

    if db is not None:
        try:
            from . import indexer  # noqa: PLC0415

            r = indexer.index_file(db, target, embedder)
            if not r.get("ok"):
                result.message += f"（索引写入失败：{r.get('error')}）"
        except Exception as exc:  # noqa: BLE001
            result.message += f"（索引异常：{exc}）"
    log.info("导入 [%s] %s (%d 字)", result.status, result.file_path, result.char_count)
    return result


def import_markdown(filename: str, content: str, db=None, embedder=None, overwrite: bool = False) -> CaptureResult:
    """纯文本 / Markdown 便捷入口（内部走统一的 import_document）。"""
    data = (content or "").encode("utf-8")
    return import_document(filename, data, db=db, embedder=embedder, overwrite=overwrite)
