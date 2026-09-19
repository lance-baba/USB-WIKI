"""多格式文档 → Markdown 转换器（OmniParse 思路的轻量化落地）。

**设计取舍**：OmniParse 依赖 Surya OCR / Whisper / marker 等模型，容器镜像 GB 级且需要 GPU，
无法塞进 96MB 的 U 盘便携包。但其核心思路「万物统一成 Markdown 再建索引」是正确的，
且**绝大多数办公格式并不需要模型**：

* ``.docx`` / ``.pptx`` / ``.xlsx`` / ``.epub`` 本质都是 **ZIP + XML** —— 标准库
  ``zipfile`` + ``xml.etree`` 即可解出全文与层级结构，零新增依赖。
* ``.html`` / ``.mhtml`` / ``.eml`` 复用已有的 trafilatura 正文抽取。
* ``.csv`` / ``.json`` / ``.yaml`` / ``.xml`` / 代码 / 字幕 走标准库。

只有 ``.pdf`` 需要外部库（纯 Python 的 ``pypdf``；装了 ``pdfminer.six`` 会自动优先使用，
正文质量更好）。扫描件 / 图片 / 音视频需要 OCR/ASR 模型，**明确不在本项目能力范围内**。
"""
from __future__ import annotations

import csv
import io
import json
import re
import zipfile
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path
from xml.etree import ElementTree as ET

from . import config, file_guard
from .log_util import get_logger

log = get_logger()

MAX_OUTPUT_CHARS = 4_000_000     # 单文件转换上限，防止异常文件撑爆索引
MAX_SHEET_ROWS = 800             # 单 sheet 最多转换行数
MAX_SHEET_COLS = 40

# ---------------------------------------------------------------- 命名空间
W = "{http://schemas.openxmlformats.org/wordprocessingml/2006/main}"
A = "{http://schemas.openxmlformats.org/drawingml/2006/main}"
S = "{http://schemas.openxmlformats.org/spreadsheetml/2006/main}"
R = "{http://schemas.openxmlformats.org/officeDocument/2006/relationships}"
PKG_REL = "{http://schemas.openxmlformats.org/package/2006/relationships}"
OPF = "{http://www.idpf.org/2007/opf}"
DC = "{http://purl.org/dc/elements/1.1/}"

# ---------------------------------------------------------------- 扩展名表
PLAIN_TEXT = {".txt", ".log", ".ini", ".cfg", ".conf", ".env", ".properties", ".gitignore"}
MARKUP_PLAIN = {".md", ".markdown", ".mdown", ".rst", ".org", ".textile", ".adoc"}
CODE_EXTS = {
    ".py", ".pyw", ".js", ".mjs", ".cjs", ".ts", ".tsx", ".jsx", ".vue", ".svelte",
    ".java", ".kt", ".scala", ".groovy", ".c", ".h", ".cc", ".cpp", ".hpp", ".cxx",
    ".cs", ".go", ".rs", ".rb", ".php", ".swift", ".m", ".mm", ".dart", ".lua", ".pl",
    ".sh", ".bash", ".zsh", ".fish", ".ps1", ".psm1", ".bat", ".cmd", ".sql", ".r",
    ".jl", ".hs", ".erl", ".ex", ".exs", ".clj", ".cljs", ".el", ".nim", ".zig",
    ".asm", ".s", ".v", ".sv", ".vhd", ".tex", ".proto", ".graphql", ".dockerfile",
}
DATA_EXTS = {".json", ".jsonl", ".ndjson", ".yaml", ".yml", ".toml", ".xml", ".csv", ".tsv"}
HTML_EXTS = {".html", ".htm", ".xhtml", ".mhtml", ".mht"}
SUBTITLE_EXTS = {".srt", ".vtt", ".ass", ".ssa", ".lrc"}
OFFICE_DOC = {".docx", ".docm", ".dotx", ".dotm"}
OFFICE_SLIDE = {".pptx", ".pptm", ".potx"}
OFFICE_SHEET = {".xlsx", ".xlsm", ".xltx"}
EBOOK_EXTS = {".epub"}
PDF_EXTS = {".pdf"}
EMAIL_EXTS = {".eml"}
RTF_EXTS = {".rtf"}
LEGACY_OFFICE = {".doc", ".ppt", ".xls", ".wps", ".et", ".dps"}
IMAGE_EXTS = {".png", ".jpg", ".jpeg", ".gif", ".bmp", ".webp", ".tif", ".tiff", ".svg", ".heic"}
AV_EXTS = {".mp3", ".wav", ".m4a", ".flac", ".ogg", ".mp4", ".mkv", ".mov", ".avi", ".webm"}

ALL_TEXT_EXTS = PLAIN_TEXT | MARKUP_PLAIN | CODE_EXTS | DATA_EXTS | RTF_EXTS | SUBTITLE_EXTS


@dataclass
class ConversionResult:
    ok: bool
    markdown: str = ""
    title: str = ""
    kind: str = ""
    warnings: list[str] = field(default_factory=list)
    error: str = ""

    @property
    def char_count(self) -> int:
        return len(self.markdown)


# --------------------------------------------------------------------------
def _decode(data: bytes) -> tuple[str, str]:
    """智能解码：UTF-8 → GB18030 → latin-1。返回 (文本, 使用的编码)。"""
    if data.startswith(b"\xef\xbb\xbf"):
        return data[3:].decode("utf-8", "replace"), "utf-8-bom"
    for enc in ("utf-8", "gb18030", "big5", "latin-1"):
        try:
            return data.decode(enc), enc
        except UnicodeDecodeError:
            continue
    return data.decode("utf-8", "replace"), "utf-8-replace"


def _clip(text: str, warnings: list[str]) -> str:
    if len(text) > MAX_OUTPUT_CHARS:
        warnings.append(f"内容超过 {MAX_OUTPUT_CHARS // 10000} 万字，已截断")
        return text[:MAX_OUTPUT_CHARS] + "\n\n> ⚠️ 内容过长已截断"
    return text


def _fence(text: str, lang: str = "") -> str:
    body = text.rstrip("\n")
    # 内容自带 ``` 时用更长的围栏，避免提前闭合
    n = 3
    while "`" * n in body:
        n += 1
    bar = "`" * n
    return f"{bar}{lang}\n{body}\n{bar}"


def _clean_title(name: str) -> str:
    return re.sub(r"[_\-\s]+", " ", Path(name).stem).strip() or Path(name).stem


# ==========================================================================
# 一、纯文本 / 标记 / 代码 / 数据
# ==========================================================================
def _h_plain(data: bytes, name: str, warnings: list[str]) -> ConversionResult:
    text, enc = _decode(data)
    if enc not in ("utf-8", "utf-8-bom"):
        warnings.append(f"按 {enc} 解码")
    return ConversionResult(True, _clip(text, warnings), _clean_title(name), "text")


def _h_markdown(data: bytes, name: str, warnings: list[str]) -> ConversionResult:
    text, enc = _decode(data)
    if enc not in ("utf-8", "utf-8-bom"):
        warnings.append(f"按 {enc} 解码")
    return ConversionResult(True, _clip(text, warnings), _clean_title(name), "markdown")


def _h_code(data: bytes, name: str, warnings: list[str]) -> ConversionResult:
    text, _ = _decode(data)
    lang = Path(name).suffix.lstrip(".").lower()
    lang = {"py": "python", "js": "javascript", "ts": "typescript", "sh": "bash",
            "bat": "batch", "cmd": "batch", "ps1": "powershell", "yml": "yaml",
            "rb": "ruby", "cs": "csharp", "cpp": "cpp", "h": "c"}.get(lang, lang)
    body = f"# {_clean_title(name)}\n\n{_fence(text, lang)}"
    return ConversionResult(True, _clip(body, warnings), _clean_title(name), "code")


def _h_json(data: bytes, name: str, warnings: list[str]) -> ConversionResult:
    text, _ = _decode(data)
    pretty = text
    try:
        if Path(name).suffix.lower() in (".jsonl", ".ndjson"):
            items = [json.loads(ln) for ln in text.splitlines() if ln.strip()]
            pretty = "\n".join(json.dumps(o, ensure_ascii=False, indent=2) for o in items)
        else:
            pretty = json.dumps(json.loads(text), ensure_ascii=False, indent=2)
    except (ValueError, TypeError) as exc:
        warnings.append(f"JSON 解析失败，按原文保存（{exc}）")
    body = f"# {_clean_title(name)}\n\n{_fence(pretty, 'json')}"
    return ConversionResult(True, _clip(body, warnings), _clean_title(name), "json")


def _h_yaml(data: bytes, name: str, warnings: list[str]) -> ConversionResult:
    text, _ = _decode(data)
    lang = "toml" if Path(name).suffix.lower() == ".toml" else "yaml"
    body = f"# {_clean_title(name)}\n\n{_fence(text, lang)}"
    return ConversionResult(True, _clip(body, warnings), _clean_title(name), lang)


def _h_xml(data: bytes, name: str, warnings: list[str]) -> ConversionResult:
    text, _ = _decode(data)
    try:
        root = ET.fromstring(text)
        lines: list[str] = []

        def walk(el, depth: int, path: str = "") -> None:
            tag = el.tag.split("}")[-1]
            cur = f"{path}/{tag}" if path else tag
            val = (el.text or "").strip()
            attrs = " ".join(f'{k.split("}")[-1]}="{v}"' for k, v in (el.attrib or {}).items())
            bullet = f'{"  " * depth}- `{tag}`'
            meta = " ".join(x for x in (attrs, "=" + val if val else "") if x)
            if meta:
                lines.append(f"{bullet} {meta}")
            if len(lines) > 4000:
                return
            for child in list(el):
                walk(child, depth + 1, cur)

        walk(root, 0)
        body = f"# {_clean_title(name)}\n\n" + "\n".join(lines)
        return ConversionResult(True, _clip(body, warnings), _clean_title(name), "xml")
    except ET.ParseError as exc:
        warnings.append(f"XML 结构异常，按原文保存（{exc}）")
        return ConversionResult(True, _clip(text, warnings), _clean_title(name), "xml")


def _h_csv(data: bytes, name: str, warnings: list[str]) -> ConversionResult:
    text, _ = _decode(data)
    delim = "\t" if Path(name).suffix.lower() == ".tsv" else None
    if delim is None:
        sample = "\n".join(text.splitlines()[:5])
        try:
            delim = csv.Sniffer().sniff(sample, delimiters=",;\t|").delimiter
        except csv.Error:
            delim = ","
    rows = list(csv.reader(io.StringIO(text), delimiter=delim))
    rows = [r for r in rows if any((c or "").strip() for c in r)]
    if not rows:
        return ConversionResult(False, error=f"{name} 内容为空")

    width = max(len(r) for r in rows)
    rows = [r + [""] * (width - len(r)) for r in rows]
    if len(rows) > 3000:
        warnings.append(f"表格超过 3000 行，仅保留前 3000 行")
        rows = rows[:3000]

    def esc_cell(s: str) -> str:
        return (s or "").replace("|", "\\|").replace("\n", " ").strip()

    head, *body_rows = rows
    md = ["| " + " | ".join(esc_cell(c) for c in head) + " |",
          "| " + " | ".join("---" for _ in head) + " |"]
    md += ["| " + " | ".join(esc_cell(c) for c in r) + " |" for r in body_rows]
    out = f"# {_clean_title(name)}\n\n共 {len(body_rows)} 行 × {width} 列\n\n" + "\n".join(md)
    return ConversionResult(True, _clip(out, warnings), _clean_title(name), "csv")


def _h_subtitle(data: bytes, name: str, warnings: list[str]) -> ConversionResult:
    text, _ = _decode(data)
    lines: list[str] = []
    for raw in text.splitlines():
        ln = raw.strip()
        if not ln or ln.isdigit():
            continue
        if ln.upper().startswith(("WEBVTT", "NOTE", "STYLE", "REGION")):
            continue
        if "-->" in ln or re.match(r"^(Dialogue|Comment):", ln, re.I):
            continue
        if re.match(r"^\[\d+:\d+", ln):      # lrc 时间轴
            ln = re.sub(r"^\[[^\]]*\]", "", ln).strip()
        lines.append(ln)
    body = f"# {_clean_title(name)}\n\n" + "\n".join(lines)
    return ConversionResult(True, _clip(body, warnings), _clean_title(name), "subtitle")


def _h_rtf(data: bytes, name: str, warnings: list[str]) -> ConversionResult:
    text, _ = _decode(data)
    # 粗粒度剥离 RTF 控制字（有损，但足以检索与阅读）
    t = re.sub(r"\\'([0-9a-fA-F]{2})", lambda m: chr(int(m.group(1), 16)), text)
    t = re.sub(r"\\u(-?\d+)\s?\??", lambda m: chr(int(m.group(1)) % 65536), t)
    t = t.replace("\\par", "\n").replace("\\line", "\n").replace("\\tab", "\t")
    t = re.sub(r"\\[a-zA-Z]+-?\d*\s?", "", t)
    t = t.replace("{", "").replace("}", "")
    t = re.sub(r"\n{3,}", "\n\n", t).strip()
    warnings.append("RTF 为有损转换（仅提取可见文字，不保留样式）")
    return ConversionResult(True, _clip(t, warnings), _clean_title(name), "rtf")


# ==========================================================================
# 二、HTML 家族（复用 trafilatura 正文抽取）
# ==========================================================================
def _html_to_md(html_text: str, name: str, warnings: list[str]) -> ConversionResult:
    from . import crawler  # 惰性导入，避免循环依赖

    body, meta, extractor = crawler.extract_body(html_text, "")
    title = str(meta.get("title") or "").strip() or _clean_title(name)
    if not body.strip():
        return ConversionResult(False, error=f"{name} 中未抽取到正文")
    if extractor != "trafilatura":
        warnings.append(f"正文抽取降级为 {extractor}")
    head = f"# {title}\n"
    if meta.get("author"):
        head += f"\n作者：{meta['author']}"
    if meta.get("date"):
        head += f"　发布于：{meta['date']}"
    out = f"{head}\n\n{body.strip()}"
    return ConversionResult(True, _clip(out, warnings), title, "html")


def _h_html(data: bytes, name: str, warnings: list[str]) -> ConversionResult:
    text, enc = _decode(data)
    if enc not in ("utf-8", "utf-8-bom"):
        warnings.append(f"按 {enc} 解码")
    return _html_to_md(text, name, warnings)


def _h_mhtml(data: bytes, name: str, warnings: list[str]) -> ConversionResult:
    import email
    from email import policy

    msg = email.message_from_bytes(data, policy=policy.default)
    html_parts, text_parts = [], []
    for part in msg.walk():
        ct = part.get_content_type()
        if part.get_content_maintype() == "multipart":
            continue
        try:
            payload = part.get_content()
        except Exception:  # noqa: BLE001
            continue
        if not isinstance(payload, str):
            continue
        if ct == "text/html":
            html_parts.append(payload)
        elif ct == "text/plain":
            text_parts.append(payload)
    if html_parts:
        return _html_to_md("\n".join(html_parts), name, warnings)
    if text_parts:
        return ConversionResult(True, _clip("\n".join(text_parts), warnings),
                                _clean_title(name), "mhtml")
    return ConversionResult(False, error=f"{name} 中未找到可读正文")


def _h_eml(data: bytes, name: str, warnings: list[str]) -> ConversionResult:
    import email
    from email import policy

    msg = email.message_from_bytes(data, policy=policy.default)
    subject = str(msg.get("Subject") or _clean_title(name))
    head = [f"# {subject}", ""]
    for label, key in (("发件人", "From"), ("收件人", "To"), ("抄送", "Cc"), ("时间", "Date")):
        if msg.get(key):
            head.append(f"- **{label}**：{msg.get(key)}")
    head.append("")

    best = ""
    for part in msg.walk():
        if part.get_content_maintype() == "multipart":
            continue
        ct = part.get_content_type()
        if ct not in ("text/plain", "text/html"):
            continue
        try:
            payload = part.get_content()
        except Exception:  # noqa: BLE001
            continue
        if isinstance(payload, str) and len(payload) > len(best):
            best = payload
            if ct == "text/html":
                r = _html_to_md(payload, name, [])
                best = r.markdown if r.ok else payload
    if not best.strip():
        return ConversionResult(False, error=f"{name} 邮件正文为空")
    return ConversionResult(True, _clip("\n".join(head) + best.strip(), warnings),
                            subject, "eml")


# ==========================================================================
# 三、OOXML（docx / pptx / xlsx）—— 纯标准库解析
# ==========================================================================
def archive_limits() -> file_guard.ArchiveLimits:
    """容器限制从配置读，默认保守（避免散落在代码里）。"""
    return file_guard.ArchiveLimits(
        max_entries=config.get_int("IMPORT", "max_archive_entries", 5000),
        max_entry_bytes=config.get_int("IMPORT", "max_archive_entry_mb", 50) * 1024 * 1024,
        max_total_bytes=config.get_int("IMPORT", "max_archive_total_mb", 200) * 1024 * 1024,
        max_ratio=config.get_int("IMPORT", "max_compression_ratio", 100),
    )


@contextmanager
def _open_zip(data: bytes):
    """打开 ZIP 容器，并**先做中央目录安全检查**再交给调用方。

    所有容器格式（docx / pptx / xlsx / epub）都必须经这里打开 ——
    边界集中一处，避免以后新增格式时漏加限制。

    安全检查包括：entry 数量、单 entry 声明大小、声明总量、压缩比、
    重复条目名、符号链接等特殊文件、entry 名是否逃逸。
    """
    with zipfile.ZipFile(io.BytesIO(data)) as z:
        file_guard.check_archive(z, archive_limits())
        yield z


def _zip_read(z: zipfile.ZipFile, path: str) -> bytes | None:
    try:
        # 第二层防护：不信任 ZipInfo.file_size，边读边数**实际**输出字节
        return file_guard.bounded_read(z, path, archive_limits())
    except (KeyError, zipfile.BadZipFile):
        return None


def _docx_props(z: zipfile.ZipFile) -> str:
    raw = _zip_read(z, "docProps/core.xml")
    if not raw:
        return ""
    try:
        root = ET.fromstring(raw)
    except ET.ParseError:
        return ""
    for tag in (f"{DC}title", f"{DC}subject"):
        el = root.find(tag)
        if el is not None and (el.text or "").strip():
            return el.text.strip()
    return ""


def _docx_styles(z: zipfile.ZipFile) -> dict[str, dict]:
    """解析 ``word/styles.xml`` → {styleId: {name, outline, based}}。

    用户真实 DOCX 里 ``w:pStyle/@w:val`` 常常是数字 styleId（如 ``44``），
    而其指向的样式名才是 ``heading 1`` / ``标题 1``。必须靠这张映射把 ``44``
    还原成「标题 1」，否则 ``:_docx_para`` 直接正则裸 styleId 会全部漏判。
    ``outline`` 是样式自身声明的 ``w:outlineLvl``，``based`` 是 ``w:basedOn``
    链，用于解析继承得到的标题层级。
    """
    raw = _zip_read(z, "word/styles.xml")
    result: dict[str, dict] = {}
    if not raw:
        return result
    try:
        root = ET.fromstring(raw)
    except ET.ParseError:
        return result
    for st in root.iter(f"{W}style"):
        sid = st.get(f"{W}styleId")
        if not sid:
            continue
        name_el = st.find(f"{W}name")
        name = (name_el.get(f"{W}val") or "") if name_el is not None else ""
        ppr = st.find(f"{W}pPr")
        outline = None
        if ppr is not None:
            ol = ppr.find(f"{W}outlineLvl")
            if ol is not None:
                try:
                    outline = int(ol.get(f"{W}val") or 0)
                except (TypeError, ValueError):
                    outline = None
        based_el = st.find(f"{W}basedOn")
        based = (based_el.get(f"{W}val") or "") if based_el is not None else ""
        result[sid] = {"name": name, "outline": outline, "based": based}
    return result


def _docx_effective_level(styles: dict[str, dict], style_id: str) -> int:
    """解析 styleId 的有效标题层级（1-6），沿 ``basedOn`` 链最多走 8 跳。

    优先用样式自身的 ``outlineLvl``；没有则看样式名（heading/标题 1-6）；
    都没有就沿继承链向上找。返回 0 表示不是标题。
    """
    seen: set[str] = set()
    cur = style_id
    for _ in range(8):
        if not cur or cur in seen:
            break
        seen.add(cur)
        info = styles.get(cur)
        if not info:
            break
        if info["outline"] is not None:
            return info["outline"] + 1
        name = info["name"]
        if name:
            m = re.search(r"(?:heading|标题)\s*([1-6])", name, re.I) or re.fullmatch(r"([1-6])", name)
            if m:
                return int(m.group(1))
        cur = info["based"]
    return 0


def _docx_para(el, styles: dict[str, dict] | None = None) -> tuple[str, int, bool]:
    """返回 (文本, 标题层级, 是否列表项)。

    styleId（如 ``44``）→ 经 ``_docx_styles`` 映射解析真实层级；
    段落自身显式 ``w:outlineLvl`` 仍优先（最权威）。
    """
    text = "".join(t.text or "" for t in el.iter(f"{W}t")).strip()
    level, is_list = 0, False
    ppr = el.find(f"{W}pPr")
    if ppr is not None:
        pstyle = ppr.find(f"{W}pStyle")
        style = (pstyle.get(f"{W}val") or "") if pstyle is not None else ""
        outline = ppr.find(f"{W}outlineLvl")
        if outline is not None:
            try:
                level = int(outline.get(f"{W}val") or 0) + 1
            except (TypeError, ValueError):
                level = 0
        if not level and style:
            # 裸 styleId（数字/任意串）→ 走 styles.xml 映射；失败再退化正则
            if styles:
                level = _docx_effective_level(styles, style)
            if not level:
                m = re.search(r"(?:heading|标题)\s*([1-6])", style, re.I) or re.fullmatch(r"([1-6])", style)
                if m:
                    level = int(m.group(1))
        if ppr.find(f"{W}numPr") is not None or "listparagraph" in style.lower():
            is_list = True
    return text, min(level, 6), is_list


def _docx_table(el) -> list[str]:
    rows: list[list[str]] = []
    for tr in el.findall(f"{W}tr"):
        cells = []
        for tc in tr.findall(f"{W}tc"):
            cells.append(" ".join(
                "".join(t.text or "" for t in p.iter(f"{W}t")).strip()
                for p in tc.findall(f"{W}p")
            ).strip())
        rows.append(cells)
    rows = [r for r in rows if any(r)]
    if not rows:
        return []
    width = max(len(r) for r in rows)
    rows = [r + [""] * (width - len(r)) for r in rows]
    out = ["| " + " | ".join(c.replace("|", "\\|") for c in rows[0]) + " |",
           "| " + " | ".join("---" for _ in rows[0]) + " |"]
    out += ["| " + " | ".join(c.replace("|", "\\|") for c in r) + " |" for r in rows[1:]]
    return out


def _h_docx(data: bytes, name: str, warnings: list[str]) -> ConversionResult:
    try:
        with _open_zip(data) as z:
            xml = _zip_read(z, "word/document.xml")
            styles = _docx_styles(z)
            title = _docx_props(z) or _clean_title(name)
    except zipfile.BadZipFile:
        return ConversionResult(False, error=f"{name} 不是有效的 .docx（可能后缀被改过）")

    if not xml:
        return ConversionResult(False, error=f"{name} 中找不到 word/document.xml")
    try:
        root = ET.fromstring(xml)
    except ET.ParseError as exc:
        return ConversionResult(False, error=f"document.xml 解析失败：{exc}")

    body = root.find(f"{W}body")
    if body is None:
        return ConversionResult(False, error="文档结构异常（缺少 body）")

    # 把「标题 + 紧随其后的表格」合并进同一个逻辑块（P0-D）：
    # 切片器以空行为块边界、且标题起新块。若标题与表格间有空行，表格会被切到
    # 无关 Parent，导致「搜索观测人员」召回不到人员配备表、LLM 也看不到职责/姓名。
    # 这里让标题与紧邻表格保持同一段落（无空行），随章节语境一起进入 Parent。
    out: list[str] = []
    pending_heading: str | None = None
    for el in list(body):
        tag = el.tag
        if tag == f"{W}p":
            text, level, is_list = _docx_para(el, styles)
            if not text:
                continue
            line = (f"{'#' * (level + 1)} {text}" if level else
                    f"- {text}" if is_list else text)
            # 标题后面如果紧跟的是普通段落（非表格），标题独立成块即可
            if pending_heading is not None:
                out.append(pending_heading)
                pending_heading = None
            if level:
                pending_heading = line   # 先挂起，等看下一节点是否表格
            else:
                out.append(line)
        elif tag == f"{W}tbl":
            table = _docx_table(el)
            if not table:
                continue
            block = "\n".join(table)
            if pending_heading is not None:
                # 标题 + 紧邻表格 → 同一块，标题行与表格行之间不留空行
                out.append(pending_heading + "\n" + block)
                pending_heading = None
            else:
                out.append(block)
    if pending_heading is not None:
        out.append(pending_heading)

    md = "\n\n".join(x.strip("\n") for x in out if x.strip())
    if not md.strip():
        return ConversionResult(False, error=f"{name} 未提取到文字（可能是纯图片文档，需 OCR）")
    md = f"# {title}\n\n{md}"
    return ConversionResult(True, _clip(md, warnings), title, "docx")


def _h_pptx(data: bytes, name: str, warnings: list[str]) -> ConversionResult:
    try:
        with _open_zip(data) as z:
            names = [n for n in z.namelist() if re.fullmatch(r"ppt/slides/slide\d+\.xml", n)]
            if not names:
                return ConversionResult(False, error=f"{name} 中没有幻灯片")
            names.sort(key=lambda n: int(re.search(r"(\d+)", Path(n).stem).group(1)))
            title = _docx_props(z) or _clean_title(name)

            sections: list[str] = []
            for idx, slide in enumerate(names, start=1):
                raw = _zip_read(z, slide)
                if not raw:
                    continue
                try:
                    root = ET.fromstring(raw)
                except ET.ParseError:
                    continue
                texts = [(t.text or "").strip() for t in root.iter(f"{A}t")]
                texts = [t for t in texts if t]
                if not texts:
                    continue
                # 首行通常是标题
                head = texts[0] if len(texts[0]) <= 40 else f"第 {idx} 页"
                rest = texts[1:]
                block = [f"## 第 {idx} 页 · {head}"] if len(texts[0]) > 40 else [f"## 第 {idx} 页 · {head}"]
                block += ["", "\n\n".join(rest)] if rest else []
                sections.append("\n".join(block))

            notes = [n for n in z.namelist() if re.fullmatch(r"ppt/notesSlides/notesSlide\d+\.xml", n)]
            if notes:
                warnings.append(f"演示稿含 {len(notes)} 页备注，已一并提取")
                for idx, note in enumerate(sorted(notes), start=1):
                    raw = _zip_read(z, note)
                    if not raw:
                        continue
                    try:
                        root = ET.fromstring(raw)
                    except ET.ParseError:
                        continue
                    texts = [(t.text or "").strip() for t in root.iter(f"{A}t")]
                    texts = [t for t in texts if t and not t.isdigit()]
                    if texts:
                        sections.append(f"### 备注 {idx}\n\n" + "\n".join(texts))
    except zipfile.BadZipFile:
        return ConversionResult(False, error=f"{name} 不是有效的 .pptx（可能后缀被改过）")

    if not sections:
        return ConversionResult(False, error=f"{name} 未提取到文字（可能是纯图片幻灯片，需 OCR）")
    md = f"# {title}\n\n共 {len(names)} 页\n\n" + "\n\n".join(sections)
    return ConversionResult(True, _clip(md, warnings), title, "pptx")


def _col_index(ref: str) -> int:
    letters = re.match(r"([A-Z]+)", (ref or "").upper())
    if not letters:
        return 0
    n = 0
    for ch in letters.group(1):
        n = n * 26 + (ord(ch) - 64)
    return n - 1


def _h_xlsx(data: bytes, name: str, warnings: list[str]) -> ConversionResult:
    try:
        with _open_zip(data) as z:
            shared: list[str] = []
            raw = _zip_read(z, "xl/sharedStrings.xml")
            if raw:
                try:
                    root = ET.fromstring(raw)
                    for si in root.iter(f"{S}si"):
                        shared.append("".join(t.text or "" for t in si.iter(f"{S}t")))
                except ET.ParseError:
                    pass

            # sheet 文件名 -> 显示名（rels 里的 Target 是相对 xl/ 的，必须归一化后比对）
            def _norm_target(target: str, base: str = "xl") -> str:
                t = (target or "").strip().lstrip("/")
                if not t:
                    return ""
                if t.startswith("xl/"):
                    return t
                return f"{base}/{t}" if base else t

            sheet_names: dict[str, str] = {}
            wb = _zip_read(z, "xl/workbook.xml")
            rels = _zip_read(z, "xl/_rels/workbook.xml.rels")
            rid_to_target: dict[str, str] = {}
            if rels:
                try:
                    for rel in ET.fromstring(rels).findall(f"{PKG_REL}Relationship"):
                        rid_to_target[rel.get("Id") or ""] = _norm_target(rel.get("Target") or "")
                except ET.ParseError:
                    pass
            if wb:
                try:
                    for sh in ET.fromstring(wb).iter(f"{S}sheet"):
                        rid = sh.get(f"{R}id") or ""
                        target = rid_to_target.get(rid, "")
                        if target and sh.get("name"):
                            sheet_names[target] = sh.get("name") or ""
                except ET.ParseError:
                    pass

            sheets = sorted(n for n in z.namelist() if re.fullmatch(r"xl/worksheets/sheet\d+\.xml", n))
            if not sheets:
                return ConversionResult(False, error=f"{name} 中没有工作表")

            title = _docx_props(z) or _clean_title(name)
            blocks: list[str] = []
            for sh_path in sheets:
                display = sheet_names.get(sh_path) or Path(sh_path).stem
                raw = _zip_read(z, sh_path)
                if not raw:
                    continue
                try:
                    root = ET.fromstring(raw)
                except ET.ParseError:
                    continue

                grid: dict[int, dict[int, str]] = {}
                max_row = 0
                for row in root.iter(f"{S}row"):
                    try:
                        rnum = int(row.get("r") or 0)
                    except ValueError:
                        continue
                    if rnum <= 0:
                        continue
                    max_row = max(max_row, rnum)
                    if rnum > MAX_SHEET_ROWS:
                        continue
                    for c in row.findall(f"{S}c"):
                        ci = _col_index(c.get("r") or "")
                        if ci < 0 or ci >= MAX_SHEET_COLS:
                            continue
                        v = c.find(f"{S}v")
                        is_el = c.find(f"{S}is")
                        if is_el is not None:
                            val = "".join(t.text or "" for t in is_el.iter(f"{S}t"))
                        elif v is not None and v.text is not None:
                            val = v.text
                            if c.get("t") == "s":
                                try:
                                    val = shared[int(val)]
                                except (ValueError, IndexError):
                                    pass
                        else:
                            val = ""
                        if val:
                            grid.setdefault(rnum, {})[ci] = val

                if not grid:
                    continue
                last_row = max(grid)
                width = max((max(r.keys()) for r in grid.values())) + 1

                # 首行有内容就当作表头（Excel 的通行约定），否则退化为「列N」
                first = grid.get(1, {})
                header = [(first.get(ci, "") or "").replace("|", "\\|").replace("\n", " ")
                          for ci in range(width)]
                if not any(h.strip() for h in header):
                    header = [f"列{i + 1}" for i in range(width)]
                    body_from = 1
                else:
                    body_from = 2

                table = ["| " + " | ".join(header) + " |",
                         "| " + " | ".join("---" for _ in range(width)) + " |"]
                for rn in range(body_from, last_row + 1):
                    cells = [(grid.get(rn, {}).get(ci, "") or "").replace("|", "\\|").replace("\n", " ")
                             for ci in range(width)]
                    if not any(c.strip() for c in cells):
                        continue
                    table.append("| " + " | ".join(cells) + " |")
                if max_row > MAX_SHEET_ROWS:
                    warnings.append(f"「{display}」超过 {MAX_SHEET_ROWS} 行，仅保留前 {MAX_SHEET_ROWS} 行")
                blocks.append(f"## {display}\n\n" + "\n".join(table))

            if not blocks:
                return ConversionResult(False, error=f"{name} 未提取到数据（可能全是空表）")
    except zipfile.BadZipFile:
        return ConversionResult(False, error=f"{name} 不是有效的 .xlsx（可能后缀被改过）")

    md = f"# {title}\n\n共 {len(blocks)} 个工作表\n\n" + "\n\n".join(blocks)
    return ConversionResult(True, _clip(md, warnings), title, "xlsx")


# ==========================================================================
# 四、EPUB
# ==========================================================================
def _h_epub(data: bytes, name: str, warnings: list[str]) -> ConversionResult:
    try:
        with _open_zip(data) as z:
            container = _zip_read(z, "META-INF/container.xml")
            opf_path = ""
            if container:
                try:
                    for rf in ET.fromstring(container).iter(
                        "{urn:oasis:names:tc:opendocument:xmlns:container}rootfile"
                    ):
                        opf_path = rf.get("full-path") or ""
                        if opf_path:
                            break
                except ET.ParseError:
                    pass
            if not opf_path:
                opf_path = next((n for n in z.namelist() if n.lower().endswith(".opf")), "")

            opf_raw = _zip_read(z, opf_path) if opf_path else None
            if not opf_raw:
                return ConversionResult(False, error=f"{name} 中找不到 OPF 清单")

            opf = ET.fromstring(opf_raw)
            base = str(Path(opf_path).parent).replace("\\", "/")
            if base == ".":
                base = ""

            title = ""
            for el in opf.iter(f"{DC}title"):
                if (el.text or "").strip():
                    title = el.text.strip()
                    break
            title = title or _clean_title(name)

            hrefs: dict[str, str] = {}
            for item in opf.iter(f"{OPF}item"):
                iid, href = item.get("id"), item.get("href")
                if iid and href:
                    hrefs[iid] = href
            order = [it.get("idref") for it in opf.iter(f"{OPF}itemref") if it.get("idref")]

            chapters: list[str] = []
            for iid in order:
                href = hrefs.get(iid or "")
                if not href:
                    continue
                full = f"{base}/{href}".lstrip("/") if base else href
                raw_html = _zip_read(z, full) or _zip_read(z, href)
                if not raw_html:
                    continue
                html_text, _ = _decode(raw_html)
                r = _html_to_md(html_text, Path(href).stem, [])
                if r.ok and r.markdown.strip():
                    chapters.append(r.markdown.strip())
    except zipfile.BadZipFile:
        return ConversionResult(False, error=f"{name} 不是有效的 .epub")
    except ET.ParseError as exc:
        return ConversionResult(False, error=f"EPUB 清单解析失败：{exc}")

    if not chapters:
        return ConversionResult(False, error=f"{name} 未提取到章节正文")
    md = f"# {title}\n\n" + "\n\n---\n\n".join(chapters)
    return ConversionResult(True, _clip(md, warnings), title, "epub")


# ==========================================================================
# 五、PDF（唯一需要外部库的格式）
# ==========================================================================
def pdf_backend() -> str:
    import importlib.util

    for mod, label in (("pdfminer.high_level", "pdfminer.six"), ("pypdf", "pypdf"),
                       ("PyPDF2", "PyPDF2")):
        try:
            if importlib.util.find_spec(mod) is not None:
                return label
        except (ImportError, ValueError):
            continue
    return ""


def _h_pdf(data: bytes, name: str, warnings: list[str]) -> ConversionResult:
    backend = pdf_backend()
    if not backend:
        return ConversionResult(
            False,
            error=(
                f"未安装 PDF 解析库，无法读取 {name}。请在 U 盘根目录执行："
                "runtime\\python-3.11-embed\\python.exe -m pip install pypdf"
                "（或重新执行 python setup_runtime_windows.py）"
            ),
        )
    text = ""
    try:
        if backend == "pdfminer.six":
            from pdfminer.high_level import extract_text  # type: ignore

            text = extract_text(io.BytesIO(data)) or ""
        else:
            mod = __import__(backend)
            reader = mod.PdfReader(io.BytesIO(data))
            if getattr(reader, "is_encrypted", False):
                try:
                    reader.decrypt("")
                except Exception:  # noqa: BLE001
                    return ConversionResult(False, error=f"{name} 是加密 PDF，无法解析")
            pages = []
            for i, page in enumerate(reader.pages, start=1):
                try:
                    pages.append(page.extract_text() or "")
                except Exception as exc:  # noqa: BLE001
                    warnings.append(f"第 {i} 页解析失败（{type(exc).__name__}）")
            text = "\n\n".join(p for p in pages if p.strip())
    except Exception as exc:  # noqa: BLE001
        return ConversionResult(False, error=f"PDF 解析失败：{type(exc).__name__}: {exc}")

    if len(text.strip()) < 20:
        return ConversionResult(
            False,
            error=f"{name} 未提取到文字 —— 大概率是扫描件/图片型 PDF，需要 OCR（本项目不含该能力）",
        )

    text = re.sub(r"[ \t]+\n", "\n", text)
    text = re.sub(r"\n{3,}", "\n\n", text).strip()
    if backend != "pdfminer.six":
        warnings.append(
            f"使用 {backend} 提取（段落结构会丢失；装 pdfminer.six 可保留段落空行）"
        )
    body = f"# {_clean_title(name)}\n\n> 由 PDF 转换（{backend}），复杂排版可能有损\n\n{text}"
    return ConversionResult(True, _clip(body, warnings), _clean_title(name), "pdf")


# ==========================================================================
# 六、注册表与入口
# ==========================================================================
HANDLERS: dict[str, object] = {}
for _e in PLAIN_TEXT:
    HANDLERS[_e] = _h_plain
for _e in MARKUP_PLAIN:
    HANDLERS[_e] = _h_markdown
for _e in CODE_EXTS:
    HANDLERS[_e] = _h_code
for _e in DATA_EXTS:
    HANDLERS[_e] = _h_json if _e.startswith(".json") else (
        _h_csv if _e in (".csv", ".tsv") else (_h_xml if _e == ".xml" else _h_yaml)
    )
for _e in SUBTITLE_EXTS:
    HANDLERS[_e] = _h_subtitle
for _e in HTML_EXTS:
    HANDLERS[_e] = _h_mhtml if _e in (".mhtml", ".mht") else _h_html
for _e in OFFICE_DOC:
    HANDLERS[_e] = _h_docx
for _e in OFFICE_SLIDE:
    HANDLERS[_e] = _h_pptx
for _e in OFFICE_SHEET:
    HANDLERS[_e] = _h_xlsx
for _e in EBOOK_EXTS:
    HANDLERS[_e] = _h_epub
for _e in PDF_EXTS:
    HANDLERS[_e] = _h_pdf
for _e in EMAIL_EXTS:
    HANDLERS[_e] = _h_eml
for _e in RTF_EXTS:
    HANDLERS[_e] = _h_rtf

CATEGORIES: list[tuple[str, set[str], str]] = [
    ("文本与笔记", PLAIN_TEXT | MARKUP_PLAIN, "直接读取，保留 Markdown 结构"),
    ("办公文档", OFFICE_DOC | OFFICE_SLIDE | OFFICE_SHEET, "docx / pptx / xlsx 转成带层级的 Markdown"),
    ("PDF", PDF_EXTS, "需 pypdf（扫描件需 OCR，不支持）"),
    ("网页与邮件", HTML_EXTS | EMAIL_EXTS, "trafilatura 抽取正文"),
    ("数据与配置", DATA_EXTS, "csv 转表格；json/yaml 转代码块"),
    ("代码文件", CODE_EXTS, "保留为对应语言的代码块"),
    ("电子书", EBOOK_EXTS, "按章节顺序抽取"),
    ("字幕", SUBTITLE_EXTS, "去掉时间轴，只留台词"),
    ("RTF", RTF_EXTS, "有损提取可见文字"),
]


def supported_extensions() -> dict:
    """给前端构造 accept 列表 / 给用户展示能力边界。"""
    return {
        "exts": sorted(HANDLERS),
        "categories": [
            {"name": name, "note": note, "exts": sorted(exts & set(HANDLERS))}
            for name, exts, note in CATEGORIES
        ],
        "unsupported": {
            "legacy_office": sorted(LEGACY_OFFICE),
            "image": sorted(IMAGE_EXTS),
            "av": sorted(AV_EXTS),
            "note": "旧版二进制 Office（.doc/.ppt/.xls）、图片、音视频需要 OCR/ASR 模型，"
                    "不在本项目的能力与体积预算内，请先另存为新格式。",
        },
        "pdf_backend": pdf_backend(),
    }


def convert(data: bytes, filename: str) -> ConversionResult:
    """把任意受支持的文件字节转成 Markdown。"""
    ext = Path(filename).suffix.lower()
    if not data:
        return ConversionResult(False, error=f"{filename} 是空文件")

    handler = HANDLERS.get(ext)
    if handler is None:
        if ext in LEGACY_OFFICE:
            return ConversionResult(
                False,
                error=f"{filename}：旧版二进制 Office 格式无法解析，请用 Office/WPS 另存为 "
                      f"{ext}x 后再导入",
            )
        if ext in IMAGE_EXTS:
            return ConversionResult(False, error=f"{filename}：图片需要 OCR，本项目不含该能力")
        if ext in AV_EXTS:
            return ConversionResult(False, error=f"{filename}：音视频需要语音转写，本项目不含该能力")
        return ConversionResult(
            False, error=f"{filename}：暂不支持 {ext or '无扩展名'} 格式"
        )

    warnings: list[str] = []
    try:
        result: ConversionResult = handler(data, filename, warnings)  # type: ignore[operator]
    except Exception as exc:  # noqa: BLE001 - 单个文件失败不得影响批量导入
        log.error("转换失败 %s: %s", filename, exc)
        return ConversionResult(False, error=f"{filename} 转换异常：{type(exc).__name__}: {exc}")

    result.warnings = warnings + result.warnings
    if result.ok and not result.title:
        result.title = _clean_title(filename)
    return result
