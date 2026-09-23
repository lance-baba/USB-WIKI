"""Parent-Child 两级切片器（PRD 4.3）。

* 子切片 Child：200 字、重叠 30 字 —— 用于向量化与 FTS5 建索引。
* 父分块 Parent：800 字（或自然 Markdown 逻辑段落）—— 命中子切片后送大模型的完整语境。

ID 采用「文档相对路径哈希 + 序号」的确定性派生，保证同一文件重复入库时
切片 ID 稳定不变，从而让增量同步天然幂等。
"""
from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass, field

CHILD_SIZE = 200
CHILD_OVERLAP = 30
PARENT_SIZE = 800
PARENT_HARD_LIMIT = 1200  # 单块超长时强制硬切，防止异常文档撑爆

_FRONTMATTER_RE = re.compile(r"^---\s*\n(.*?)\n---\s*\n?", re.DOTALL)
_WIKILINK_RE = re.compile(r"\[\[([^\[\]|]+?)(?:\|[^\[\]]*)?\]\]")
_MD_HEADING_RE = re.compile(r"^\s{0,3}(#{1,6})\s+(.+?)\s*$")
# 伪标题：真实公文（DOCX 转 Markdown）里章节号常是纯文本而非 `#`。
# 形如「三、观测目的和内容」「（二）观测内容」「㈡、观测内容」「2、沉降点布设」「2.1 点位」。
# ⚠ 只在「整块只有一行且够短」时才算标题，避免把正文里的编号列表误当章节。
_PSEUDO_HEADING_RE = re.compile(
    r"^\s*(?:"
    r"[一二三四五六七八九十百零〇]{1,4}\s*[、.．]"          # 三、 二.
    r"|[（(]\s*[一二三四五六七八九十\d]{1,3}\s*[）)]"        # （二） (2)
    r"|[㈠-㈩⑴-⑿⒈-⒛]"                                       # ㈡ ⑴
    r"|\d{1,3}(?:\.\d{1,3}){0,3}\s*[、.．]"                  # 2、 2.1.
    r"|\d{1,3}(?:\.\d{1,3}){1,3}\s"                          # 2.1 点位
    r")"
)
_SENT_SPLIT_RE = re.compile(r"(?<=[。！？!?；;])\s*|(?<=\.)\s+|\n{2,}")
_CODE_FENCE_RE = re.compile(r"```.*?```", re.DOTALL)

# Markdown 表格检测：连续 `|...|` 行且其中含一条 `|---|` 分隔行。
_TABLE_ROW_RE = re.compile(r"^\s*\|.*\|\s*$")
_TABLE_SEP_RE = re.compile(r"^\s*\|[\s:|-]+\|\s*$")

# 分布式 CJK 空白（Word「分散对齐 / 字符间距」把「规格型号」拆成「规 格 型 号」）。
# 只在「汉字与汉字之间」的空白上收拢；不破坏 CJK 与 ASCII / 标点之间的空格
# （如「美国 1台」「N2 级别」「SW-30」），正文普通空格语义不受影响。
_CJK = r"[\u3400-\u9fff]"
_DIST_CJK_SPACE_RE = re.compile(rf"(?<={_CJK})\s+(?={_CJK})")


def normalize_distributed_cjk(text: str) -> str:
    """收拢汉字之间的分散排版空格；对其它空白（含 ASCII 邻接）一律不动。"""
    return _DIST_CJK_SPACE_RE.sub("", text or "")


@dataclass
class ChildChunk:
    chunk_id: str
    doc_id: str
    parent_id: str
    content: str
    #: 章节路径（P0-4）：如「三、观测目的和内容 > ㈡、观测内容 > 2、沉降点布设」。
    #: **只用于检索**（拼成 retrieval_text 给 FTS / embedding），UI 与引用仍显示原始 content。
    section_path: str = ""

    @property
    def retrieval_text(self) -> str:
        """建索引用的检索文本 = 章节路径 + 原文。"""
        return retrieval_text_of(self.section_path, self.content)


@dataclass
class ParentBlock:
    parent_id: str
    doc_id: str
    content: str
    ord: int
    section_path: str = ""
    #: 源行范围（1-based，含两端）——对应 **Markdown 真相源**里的行号。
    #: 引用跳转的 stable anchor：用「源位置 → DOM 位置」定位，而不是在全文里
    #: 搜相似文字（目录与正文同文时必然跳错）。0 表示未知（老索引）。
    source_start_line: int = 0
    source_end_line: int = 0


def retrieval_text_of(section_path: str, content: str) -> str:
    """检索文本的唯一构造点（P0-4）：章节路径在前、**规范化后**的原文在后。

    ⚠ 只改检索文本，不碰 ``content``（durable Markdown 真相源）。规范化收敛 Word
    表格里「规 格 型 号」式的逐字分散空格，使存量库仅重建 cache.db 即可被「型号」命中，
    无需用户重新导入原 DOCX。
    """
    sp = (section_path or "").strip()
    body = normalize_distributed_cjk(content or "")
    return f"{sp}\n\n{body}" if sp else body


@dataclass
class ParsedDoc:
    doc_id: str
    rel_path: str
    title: str
    status: str
    meta: dict = field(default_factory=dict)
    parents: list[ParentBlock] = field(default_factory=list)
    children: list[ChildChunk] = field(default_factory=list)
    links: list[str] = field(default_factory=list)
    body_len: int = 0


# --------------------------------------------------------------------------
def doc_id_for(rel_path: str) -> str:
    """文档 ID = 相对路径哈希（PRD 5.1 注释）。"""
    norm = rel_path.replace("\\", "/").strip().lstrip("/")
    return hashlib.sha1(norm.encode("utf-8")).hexdigest()[:16]


def parse_frontmatter(text: str) -> tuple[dict, str, int]:
    """解析 YAML frontmatter；无 PyYAML 时退化为极简 key: value 解析。

    返回 ``(meta, body, body_start_line)`` —— body 第一行在**文件**中的行号（1-based），
    供父块换算 source_start_line/source_end_line（A2：源行范围必须来自解析期，
    不允许事后在全文里 find 猜位置）。
    """
    m = _FRONTMATTER_RE.match(text)
    if not m:
        return {}, text, 1
    raw, body = m.group(1), text[m.end():]
    body_start_line = text[:m.end()].count("\n") + 1
    meta: dict = {}
    try:
        import yaml  # type: ignore

        loaded = yaml.safe_load(raw)
        if isinstance(loaded, dict):
            meta = {str(k): v for k, v in loaded.items()}
            return meta, body, body_start_line
    except Exception:  # noqa: BLE001 - yaml 缺失/语法异常均走兜底
        pass
    for line in raw.splitlines():
        if ":" in line and not line.lstrip().startswith("#"):
            k, _, v = line.partition(":")
            meta[k.strip()] = v.strip().strip('"').strip("'")
    return meta, body, body_start_line


def extract_title(body: str, meta: dict, fallback: str) -> str:
    for key in ("title", "标题", "name"):
        if meta.get(key):
            return str(meta[key]).strip()
    m = re.search(r"^\s{0,3}#\s+(.+?)\s*$", body, re.MULTILINE)
    if m:
        return m.group(1).strip()
    first = next((ln.strip() for ln in body.splitlines() if ln.strip()), "")
    return (first[:60] or fallback).strip()


def extract_wikilinks(body: str) -> list[str]:
    """抽取 ``[[Wikilink]]`` 双向链接目标（去重、保序）。"""
    seen: dict[str, None] = {}
    for m in _WIKILINK_RE.finditer(body):
        target = m.group(1).strip()
        if target:
            seen.setdefault(target, None)
    return list(seen.keys())


# --------------------------------------------------------------------------
def _heading_of(block: str) -> tuple[int, str] | None:
    """识别 Markdown 标题与「伪标题」（纯文本章节号），返回 (level, text)。"""
    stripped = block.strip()
    if not stripped:
        return None
    first = stripped.splitlines()[0].strip()
    m = _MD_HEADING_RE.match(first)
    if m:
        return len(m.group(1)), m.group(2).strip()
    # 伪标题：整块单行 + 够短 + 以章节号开头
    if len(stripped.splitlines()) == 1 and len(first) <= 40 and _PSEUDO_HEADING_RE.match(first):
        if re.match(r"^\s*[一二三四五六七八九十百零〇]{1,4}\s*[、.．]", first):
            level = 1
        elif re.match(r"^\s*[（(㈠-㈩⑴-⑿]", first):
            level = 2
        else:
            level = 3 + first.count(".")              # 2、→3；2.1→4
        return min(level, 6), first
    return None


def _split_parents(body: str, base_line: int = 1) -> list[tuple[str, str, int, int]]:
    """按 Markdown 逻辑段落聚合父分块；标题处强制起新块。

    返回 ``[(parent_text, section_path, source_start_line, source_end_line)]``：
    * section_path（P0-4）是该块所处章节的层级路径，只用于检索；
    * source_*_line 是**真相源行号**（1-based，含两端），由解析期逐行统计得出 ——
      这是引用跳转的 stable anchor。同一文字在目录与正文各出现一次时，两者行号不同，
      因此能精确区分（旧实现在全文里搜相似文字，必然可能跳到目录）。
    """
    out: list[tuple[str, str, int, int]] = []
    stack: list[tuple[int, str]] = []
    buf = ""
    buf_lines: list[int] = []          # buf 每行对应的源行号（与 buf 的行一一对应）

    def path() -> str:
        return " > ".join(t for _, t in stack)

    def flush() -> None:
        nonlocal buf, buf_lines
        if buf.strip() and buf_lines:
            out.append((buf.strip(), path(), buf_lines[0], buf_lines[-1]))
        buf = ""
        buf_lines = []

    # 逐块扫描并记录每块的源行范围。
    # ⚠ 必须用**字符偏移**换算行号：空行分隔符会被 re.split 吞掉，只按块内行数自增
    #   会让每个块之后的行号整体偏小（差多少取决于吞掉几行）。
    def line_at(offset: int) -> int:
        return base_line + body.count("\n", 0, offset)

    cursor_pos = 0
    for m in re.finditer(r"\n{2,}|\Z", body):
        raw_block = body[cursor_pos:m.start()]
        cursor_pos = m.end()
        if not raw_block.strip():
            continue
        block = raw_block.strip("\n")
        lead_blank = len(raw_block) - len(raw_block.lstrip("\n"))
        blk_start = line_at(cursor_pos - len(m.group(0)) - len(raw_block)) + lead_blank
        blk_end = line_at(cursor_pos - len(m.group(0)) - len(raw_block)
                          + len(raw_block.rstrip("\n")) - 1)
        blk_end = max(blk_start, blk_end)
        h = _heading_of(block)
        if h is not None:
            flush()
            level, text = h
            while stack and stack[-1][0] >= level:
                stack.pop()
            stack.append((level, text))
            buf = block
            buf_lines = list(range(blk_start, blk_end + 1))
            continue
        would_overflow = len(buf) + len(block) + 2 > PARENT_SIZE
        if buf and would_overflow:
            flush()
        buf = f"{buf}\n\n{block}" if buf else block
        buf_lines = buf_lines + list(range(blk_start, blk_end + 1))

        # 单块极长（如整段代码）时硬切：行号随字符切点一起分家。
        # ⚠ 表格块**不允许**字符硬切 —— 否则表头与数据行分离、行被拦腰切断
        # （真实 Bug：长仪器表被切断后「型号」与「水准仪」行不再共现，检索漏召回）。
        # 表格作为整体父块保留，行级完整性由 `_split_children` 用表头传播保证。
        while len(buf) > PARENT_HARD_LIMIT:
            # 含表格分隔行即视为表格块：整体保留，禁止字符硬切（行完整性优先）。
            if any(_TABLE_SEP_RE.match(ln) for ln in buf.split("\n")):
                break
            head, rest = buf[:PARENT_SIZE], buf[PARENT_SIZE:]
            cut = head.count("\n") + 1                 # head 覆盖的行数
            out.append((head.strip(), path(), buf_lines[0], buf_lines[min(cut, len(buf_lines)) - 1]))
            buf, buf_lines = rest, buf_lines[cut - 1:]

    flush()
    return [b for b in out if b[0]]


def _sentences(text: str) -> list[str]:
    parts = [p for p in _SENT_SPLIT_RE.split(text) if p is not None]
    out: list[str] = []
    for p in parts:
        s = p.strip()
        if s:
            out.append(s)
    return out or [text]


def _split_tables_in_parent(text: str) -> list[tuple[str, object]]:
    """把父块文本切成「prose」与「table」交替段落。

    table 段返回 ``("table", (header, sep, [rows]))``；prose 段返回 ``("prose", str)``。
    表格识别：连续 ``|...|`` 行且其中含一条 ``|---|`` 分隔行。
    """
    lines = text.split("\n")
    n = len(lines)
    segs: list[tuple[str, object]] = []
    i = 0
    while i < n:
        if _TABLE_ROW_RE.match(lines[i]) and i + 1 < n and _TABLE_SEP_RE.match(lines[i + 1]):
            tbl = [lines[i], lines[i + 1]]
            j = i + 2
            while j < n and _TABLE_ROW_RE.match(lines[j]):
                tbl.append(lines[j])
                j += 1
            segs.append(("table", (tbl[0], tbl[1], tbl[2:])))
            i = j
        else:
            start = i
            i += 1
            while i < n and not (
                _TABLE_ROW_RE.match(lines[i]) and i + 1 < n and _TABLE_SEP_RE.match(lines[i + 1])
            ):
                i += 1
            prose = "\n".join(lines[start:i]).strip()
            if prose:
                segs.append(("prose", prose))
    return segs


def _mk_child(content: str, parent_id: str, doc_id: str, idx: int,
              section_path: str) -> ChildChunk:
    return ChildChunk(
        chunk_id=f"{doc_id}:c{idx}",
        doc_id=doc_id,
        parent_id=parent_id,
        content=content.strip(),
        section_path=section_path,
    )


def _split_prose(parent_text: str, parent_id: str, doc_id: str, start_idx: int,
                 section_path: str) -> list[ChildChunk]:
    """普通文本：200/30 滑窗切片，优先在句子边界断开。"""
    out: list[ChildChunk] = []
    idx = start_idx
    buf = ""

    def flush() -> None:
        nonlocal buf, idx
        content = buf.strip()
        if content:
            out.append(_mk_child(content, parent_id, doc_id, idx, section_path))
            idx += 1
        buf = ""

    for sent in _sentences(parent_text):
        if len(sent) > CHILD_SIZE * 2:
            # 超长句（无标点长串）：按窗口硬切
            step = max(1, CHILD_SIZE - CHILD_OVERLAP)
            for k in range(0, len(sent), step):
                piece = sent[k:k + CHILD_SIZE]
                if not piece.strip():
                    continue
                if len(buf) + len(piece) > CHILD_SIZE and buf:
                    flush()
                buf = piece
                flush()
                buf = piece[-CHILD_OVERLAP:] if len(piece) > CHILD_OVERLAP else ""
            continue

        if len(buf) + len(sent) + 1 > CHILD_SIZE and buf:
            tail = buf[-CHILD_OVERLAP:]
            flush()
            buf = tail
        buf = f"{buf}\n{sent}".strip() if buf else sent

    flush()
    return out


def _split_children(parent_text: str, parent_id: str, doc_id: str, start_idx: int,
                    section_path: str = "") -> list[ChildChunk]:
    """在父分块内做子切片。

    * 普通文本走 `_split_prose`（200/30 滑窗）。
    * **Markdown 表格行级切片 + 表头传播**（D / E）：表格按行边界分段，**绝不在单个
      row 中间切开**；每个派生表格段都前置表头行（header + 分隔行），使「型号」与
      数据行在同一 chunk 共现（服务于 FTS 检索文本与 LLM 父块上下文）。重复表头是
      **derived cache context**，不写回 durable Markdown，引用 source 行仍指向真实数据行。
    """
    out: list[ChildChunk] = []
    idx = start_idx
    for kind, payload in _split_tables_in_parent(parent_text):
        if kind == "prose":
            for c in _split_prose(payload, parent_id, doc_id, idx, section_path):
                out.append(c)
                idx += 1
            continue
        header, sep, rows = payload
        head = f"{header}\n{sep}"
        batch: list[str] = []
        for row in rows:
            cand = f"{head}\n{row}" if not batch else f"{head}\n" + "\n".join(batch) + "\n" + row
            if len(cand) > CHILD_SIZE and batch:
                out.append(_mk_child(f"{head}\n" + "\n".join(batch), parent_id, doc_id, idx, section_path))
                idx += 1
                batch = [row]
            else:
                batch.append(row)
        if batch:
            out.append(_mk_child(f"{head}\n" + "\n".join(batch), parent_id, doc_id, idx, section_path))
            idx += 1
    return out


# --------------------------------------------------------------------------
def parse(text: str, rel_path: str) -> ParsedDoc:
    """把一个 Markdown 文档解析成 Parent-Child 切片集合。"""
    meta, body, body_start_line = parse_frontmatter(text or "")
    doc_id = doc_id_for(rel_path)
    fallback = rel_path.rsplit("/", 1)[-1].rsplit(".", 1)[0]
    title = extract_title(body, meta, fallback)
    status = str(meta.get("status", "success") or "success").strip()

    # 剔除代码围栏后再做段落聚合，避免把代码块切得七零八落。
    # ⚠ 截断必须**保留行数**（用等量换行补齐）——否则围栏之后的源行号全部错位，
    #   引用跳转的 stable anchor 就废了。
    def _truncate_fence(m: "re.Match[str]") -> str:
        raw = m.group(0)
        if len(raw) <= PARENT_HARD_LIMIT:
            return raw
        head = raw[:PARENT_HARD_LIMIT]
        return head + "\n" * (raw.count("\n") - head.count("\n"))

    structural = _CODE_FENCE_RE.sub(_truncate_fence, body)
    parent_texts = _split_parents(structural, base_line=body_start_line)

    parents: list[ParentBlock] = []
    children: list[ChildChunk] = []
    cursor = 0
    for i, (ptext, spath, s_line, e_line) in enumerate(parent_texts):
        pid = f"{doc_id}:p{i}"
        parents.append(ParentBlock(parent_id=pid, doc_id=doc_id, content=ptext, ord=i,
                                   section_path=spath,
                                   source_start_line=s_line, source_end_line=e_line))
        kids = _split_children(ptext, pid, doc_id, cursor, section_path=spath)
        children.extend(kids)
        cursor += len(kids)

    return ParsedDoc(
        doc_id=doc_id,
        rel_path=rel_path.replace("\\", "/"),
        title=title,
        status=status,
        meta=meta,
        parents=parents,
        children=children,
        links=extract_wikilinks(body),
        body_len=len(body),
    )
