"""Office 原件预览 —— ``.docx`` / ``.xlsx`` → **无脚本 HTML**。

给谁用
======
前端「原件」视图原先只认 PDF / HTML / 图片，Office 原件一律落到
「该格式无法内嵌预览」。本模块把 Office 原件转成干净 HTML，塞进既有的
``sandbox=""`` iframe —— Word 的标题/正文/表格、Excel 的工作表表格都能就地看。

为什么不另写一套 OOXML 解析
===========================
``converters`` 已经用**纯标准库**（ZIP + XML）把 OOXML 拆成了结构化 Markdown，
且有 ``test_docx_table_retrieval`` 等回归测试兜底。重复实现第二套解析不仅冗余，
还会让「预览」与「入库」对同一份文件给出不一致的结果。更关键的是：**不动入库链路**
——那条路一出问题会直接污染检索与问答。

安全底线（与剪藏快照同一条）
============================
* 产物**不含任何 ``<script>``**，不引用任何外部资源（无 CDN、无远端图片）。
* 文档正文属**不可信输入**：所有文本一律「先 HTML 转义，再套标记」，
  否则 docx 里写一句 ``<script>`` 就能在预览里执行。

边界
====
只读：不写盘、不改真相源（.md）、不碰 cache.db。转换失败一律降级为「无法预览」提示。
"""
from __future__ import annotations

import html
import re

from . import converters

#: 可预览的 Office 扩展名 —— 复用 converters 的格式常量，避免两处漂移。
SUPPORTED_EXTS: frozenset[str] = frozenset(
    set(converters.OFFICE_DOC) | set(converters.OFFICE_SHEET)
)

#: 输入体积上限：超大原件先挡掉，别把预览拖成内存黑洞
_MAX_INPUT_BYTES = 64 * 1024 * 1024
#: 渲染体积上限：超过就截断并提示，避免浏览器被一个巨型 table 卡死
_MAX_OUTPUT_CHARS = 3_000_000

_HEAD_RE = re.compile(r"^(#{1,6})\s+(.*)$")
_UL_RE = re.compile(r"^[-*]\s+(.*)$")
_OL_RE = re.compile(r"^\d+\.\s+(.*)$")
_QUOTE_RE = re.compile(r"^>\s?(.*)$")
_DELIM_CELL = re.compile(r"^:?-{1,}:?$")

_STYLE = """
:root{color-scheme:light}
*{box-sizing:border-box}
body{margin:0;background:#fff;color:#1f2430;
  font:14px/1.75 "Microsoft YaHei","PingFang SC","Hiragino Sans GB",system-ui,sans-serif}
.page{max-width:920px;margin:0 auto;padding:22px 26px 40px}
.bar{position:sticky;top:0;z-index:2;background:#fff;border-bottom:1px solid #e6e9f0;
  padding:8px 12px;font-size:12.5px;display:flex;flex-wrap:wrap;gap:6px;align-items:center}
.bar .tag{color:#6b7280;margin-right:2px}
.bar a{color:#2563eb;text-decoration:none;border:1px solid #d7def0;background:#f5f8ff;
  border-radius:999px;padding:2px 9px}
h1,h2,h3,h4,h5,h6{color:#111827;line-height:1.35;margin:22px 0 10px}
h1{font-size:21px;border-bottom:1px solid #e6e9f0;padding-bottom:8px}
h2{font-size:17.5px}
h3{font-size:15.5px}
h4,h5,h6{font-size:14px}
h2[id],h3[id]{scroll-margin-top:44px}
p{margin:0 0 11px}
ul,ol{margin:0 0 12px;padding-left:22px}
li{margin:3px 0}
table{border-collapse:collapse;width:100%;margin:0 0 16px;font-size:13px}
th,td{border:1px solid #dfe3ec;padding:6px 9px;text-align:left;vertical-align:top}
th{background:#f4f6fb;font-weight:600;color:#111827}
tbody tr:nth-child(even){background:#fbfcfe}
code{background:#f1f3f9;border-radius:4px;padding:1px 4px;font-size:12.5px}
pre{background:#f6f8fc;border:1px solid #e6e9f0;border-radius:8px;padding:10px 12px;
  overflow:auto;font-size:12.5px}
blockquote{margin:0 0 12px;padding:6px 12px;border-left:3px solid #d7def0;color:#4b5563}
.note{margin:0 0 16px;padding:8px 12px;border-radius:8px;background:#fffbeb;
  border:1px solid rgba(217,119,6,.35);color:#92400e;font-size:12.5px}
"""


def supported(ext: str) -> bool:
    """该扩展名是否能生成 HTML 预览。"""
    return (ext or "").lower() in SUPPORTED_EXTS


def _inline(text: str) -> str:
    """内联 Markdown → HTML。

    ⚠ 顺序不能反：必须**先转义** HTML 特殊字符，再套 ``<strong>``/``<code>``。
    反过来做的话，文档里写 ``<script>alert(1)</script>`` 就会被原样注入预览页。
    """
    s = html.escape(text or "", quote=False)
    s = re.sub(r"`([^`]+)`", r"<code>\1</code>", s)
    s = re.sub(r"\*\*([^*]+)\*\*", r"<strong>\1</strong>", s)
    return s


def _slugify(text: str, used: dict[str, int]) -> str:
    """给标题生成稳定的锚 id（重名自动加后缀）。"""
    base = re.sub(r"[^0-9A-Za-z\u4e00-\u9fff]+", "-", (text or "").strip()).strip("-")[:36]
    base = base or "sec"
    n = used.get(base, 0) + 1
    used[base] = n
    return base if n == 1 else f"{base}-{n}"


def _cells(line: str) -> list[str]:
    body = (line or "").strip()
    if body.startswith("|"):
        body = body[1:]
    if body.endswith("|"):
        body = body[:-1]
    return [c.strip() for c in body.split("|")]


def _is_delim_row(row: list[str]) -> bool:
    if not row or not any(row):
        return False
    return all(_DELIM_CELL.match(c) for c in row if c != "")


def _render_table(block: list[str]) -> str:
    rows = [r for r in (_cells(x) for x in block) if any(r)]
    if not rows:
        return ""
    head: list[str] = []
    body_rows = rows
    if len(rows) >= 2 and _is_delim_row(rows[1]):
        head, body_rows = rows[0], rows[2:]
    width = max([len(head)] + [len(r) for r in body_rows])
    out = ["<table>"]
    if head:
        out.append("<thead><tr>"
                   + "".join(f"<th>{_inline(c)}</th>"
                             for c in head + [""] * (width - len(head)))
                   + "</tr></thead>")
    out.append("<tbody>")
    for r in body_rows:
        out.append("<tr>"
                   + "".join(f"<td>{_inline(c)}</td>"
                             for c in r + [""] * (width - len(r)))
                   + "</tr>")
    out.append("</tbody></table>")
    return "".join(out)


def _render_md(md: str) -> tuple[str, list[tuple[str, str]]]:
    """Markdown 子集 → (HTML 片段, [(标题文本, 锚 id)])。"""
    lines = (md or "").replace("\r\n", "\n").split("\n")
    out: list[str] = []
    nav: list[tuple[str, str]] = []
    used: dict[str, int] = {}
    para: list[str] = []
    quote: list[str] = []
    items: list[str] = []
    list_tag = ""
    in_code = False
    code_buf: list[str] = []
    size = 0
    truncated = False

    def flush_para() -> None:
        nonlocal para
        if para:
            out.append("<p>" + _inline(" ".join(para)) + "</p>")
            para = []

    def flush_quote() -> None:
        nonlocal quote
        if quote:
            out.append("<blockquote>" + _inline(" ".join(quote)) + "</blockquote>")
            quote = []

    def flush_list() -> None:
        nonlocal items, list_tag
        if items:
            out.append(f"<{list_tag}>"
                       + "".join(f"<li>{_inline(t)}</li>" for t in items)
                       + f"</{list_tag}>")
            items = []
            list_tag = ""

    def flush_all() -> None:
        flush_para()
        flush_quote()
        flush_list()

    idx, n = 0, len(lines)
    while idx < n:
        raw = lines[idx]
        line = raw.strip()

        if line.startswith("```"):
            if not in_code:
                flush_all()
                in_code, code_buf = True, []
            else:
                out.append("<pre><code>" + html.escape("\n".join(code_buf)) + "</code></pre>")
                size += sum(len(x) for x in code_buf)
                in_code, code_buf = False, []
            idx += 1
            continue
        if in_code:
            code_buf.append(raw)
            idx += 1
            continue
        if not line:
            flush_all()
            idx += 1
            continue
        if size > _MAX_OUTPUT_CHARS:
            flush_all()
            truncated = True
            break

        m = _HEAD_RE.match(line)
        if m:
            flush_all()
            level = len(m.group(1))
            text = m.group(2).strip()
            anchor = _slugify(text, used)
            if level <= 2:
                nav.append((text, anchor))
            out.append(f'<h{level} id="{anchor}">{_inline(text)}</h{level}>')
            size += len(raw)
            idx += 1
            continue

        if line.startswith("|") and "|" in line[1:]:
            flush_all()
            block: list[str] = []
            while idx < n and lines[idx].strip().startswith("|"):
                block.append(lines[idx].strip())
                idx += 1
            rendered = _render_table(block)
            out.append(rendered)
            size += len(rendered)
            continue

        m = _UL_RE.match(line) or _OL_RE.match(line)
        if m:
            flush_para()
            flush_quote()
            tag = "ul" if line[0] in "-*" else "ol"
            if list_tag and list_tag != tag:
                flush_list()
            list_tag = tag
            items.append(m.group(1).strip())
            size += len(raw)
            idx += 1
            continue

        m = _QUOTE_RE.match(line)
        if m:
            flush_para()
            flush_list()
            quote.append(m.group(1).strip())
            size += len(raw)
            idx += 1
            continue

        flush_list()
        flush_quote()
        para.append(line)
        size += len(raw)
        idx += 1

    if in_code and code_buf:
        out.append("<pre><code>" + html.escape("\n".join(code_buf)) + "</code></pre>")
    flush_all()
    if truncated:
        out.append('<p class="note">⚠️ 内容过长，预览已截断 —— '
                   '完整内容请看「阅读」视图或下载原件。</p>')
    return "".join(out), nav


def _document(title: str, body: str, nav: list[tuple[str, str]],
              lead: str = "") -> str:
    """包成自包含 HTML 文档（**无 script、无外部资源**）。"""
    head_t = html.escape(title or "原件预览")
    lead_html = f'<p class="note">{html.escape(lead)}</p>' if lead else ""
    bar = ""
    if len(nav) >= 2:
        links = "".join(f'<a href="#{anchor}">{html.escape(t)}</a>'
                        for t, anchor in nav[:40])
        bar = f'<div class="bar"><span class="tag">跳转：</span>{links}</div>'
    return (
        '<!DOCTYPE html>\n<html lang="zh-CN"><head>'
        '<meta charset="utf-8">'
        '<meta name="viewport" content="width=device-width,initial-scale=1">'
        f"<title>{head_t}</title><style>{_STYLE}</style></head><body>"
        f'{bar}<div class="page"><h1>{head_t}</h1>{lead_html}{body}</div>'
        "</body></html>"
    )


def to_html(data: bytes, filename: str) -> str:
    """把 Office 原件转成可安全内嵌的 HTML 文档。

    失败一律抛 ``ValueError`` —— 调用方（API）负责降级成「无法预览」；
    这里刻意不吞异常，因为「预览失败」与「生成了一段空 HTML」对用户是两回事。
    """
    if not data:
        raise ValueError("原件是空文件")
    if len(data) > _MAX_INPUT_BYTES:
        raise ValueError(
            f"原件过大（{len(data) // 1024 // 1024}MB），"
            f"超过可预览上限 {_MAX_INPUT_BYTES // 1024 // 1024}MB")

    res = converters.convert(data, filename)
    if not res.ok:
        raise ValueError(res.error or f"{filename} 转换失败")
    if not (res.markdown or "").strip():
        raise ValueError(f"{filename} 没有可预览的内容")

    body, nav = _render_md(res.markdown)
    parts = [w for w in list(dict.fromkeys(res.warnings))[:4]]
    lead = ("；".join(parts) + "。" if parts else "") + \
        "本页由原件在服务端转换生成，保留结构与表格，不还原原始字体/排版。"
    return _document(res.title or filename, body, nav, lead=lead)
