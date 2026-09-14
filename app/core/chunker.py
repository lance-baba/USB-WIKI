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
_HEADING_RE = re.compile(r"^\s{0,3}#{1,6}\s+", re.MULTILINE)
_SENT_SPLIT_RE = re.compile(r"(?<=[。！？!?；;])\s*|(?<=\.)\s+|\n{2,}")
_CODE_FENCE_RE = re.compile(r"```.*?```", re.DOTALL)


@dataclass
class ChildChunk:
    chunk_id: str
    doc_id: str
    parent_id: str
    content: str


@dataclass
class ParentBlock:
    parent_id: str
    doc_id: str
    content: str
    ord: int


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


def parse_frontmatter(text: str) -> tuple[dict, str]:
    """解析 YAML frontmatter；无 PyYAML 时退化为极简 key: value 解析。"""
    m = _FRONTMATTER_RE.match(text)
    if not m:
        return {}, text
    raw, body = m.group(1), text[m.end():]
    meta: dict = {}
    try:
        import yaml  # type: ignore

        loaded = yaml.safe_load(raw)
        if isinstance(loaded, dict):
            meta = {str(k): v for k, v in loaded.items()}
            return meta, body
    except Exception:  # noqa: BLE001 - yaml 缺失/语法异常均走兜底
        pass
    for line in raw.splitlines():
        if ":" in line and not line.lstrip().startswith("#"):
            k, _, v = line.partition(":")
            meta[k.strip()] = v.strip().strip('"').strip("'")
    return meta, body


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
def _split_parents(body: str) -> list[str]:
    """按 Markdown 逻辑段落聚合父分块；标题处强制起新块。"""
    blocks: list[str] = []
    buf = ""

    for raw_block in re.split(r"\n{2,}", body):
        block = raw_block.strip("\n")
        if not block.strip():
            continue
        starts_heading = bool(_HEADING_RE.match(block))
        would_overflow = len(buf) + len(block) + 2 > PARENT_SIZE

        if buf and (starts_heading or would_overflow):
            blocks.append(buf.strip())
            buf = ""
        buf = f"{buf}\n\n{block}" if buf else block

        # 单块极长（如整段代码/长表格）时硬切
        while len(buf) > PARENT_HARD_LIMIT:
            head, buf = buf[:PARENT_SIZE], buf[PARENT_SIZE:]
            blocks.append(head.strip())

    if buf.strip():
        blocks.append(buf.strip())
    return [b for b in blocks if b]


def _sentences(text: str) -> list[str]:
    parts = [p for p in _SENT_SPLIT_RE.split(text) if p is not None]
    out: list[str] = []
    for p in parts:
        s = p.strip()
        if s:
            out.append(s)
    return out or [text]


def _split_children(parent_text: str, parent_id: str, doc_id: str, start_idx: int) -> list[ChildChunk]:
    """在父分块内做 200/30 滑窗切片，优先在句子边界断开。"""
    out: list[ChildChunk] = []
    idx = start_idx
    buf = ""

    def flush() -> None:
        nonlocal buf, idx
        content = buf.strip()
        if content:
            out.append(
                ChildChunk(
                    chunk_id=f"{doc_id}:c{idx}",
                    doc_id=doc_id,
                    parent_id=parent_id,
                    content=content,
                )
            )
            idx += 1
        buf = ""

    for sent in _sentences(parent_text):
        if len(sent) > CHILD_SIZE * 2:
            # 超长句（无标点长串）：按窗口硬切
            step = max(1, CHILD_SIZE - CHILD_OVERLAP)
            for i in range(0, len(sent), step):
                piece = sent[i:i + CHILD_SIZE]
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


# --------------------------------------------------------------------------
def parse(text: str, rel_path: str) -> ParsedDoc:
    """把一个 Markdown 文档解析成 Parent-Child 切片集合。"""
    meta, body = parse_frontmatter(text or "")
    doc_id = doc_id_for(rel_path)
    fallback = rel_path.rsplit("/", 1)[-1].rsplit(".", 1)[0]
    title = extract_title(body, meta, fallback)
    status = str(meta.get("status", "success") or "success").strip()

    # 剔除代码围栏后再做段落聚合，避免把代码块切得七零八落
    structural = _CODE_FENCE_RE.sub(lambda m: m.group(0)[:PARENT_HARD_LIMIT], body)
    parent_texts = _split_parents(structural)

    parents: list[ParentBlock] = []
    children: list[ChildChunk] = []
    cursor = 0
    for i, ptext in enumerate(parent_texts):
        pid = f"{doc_id}:p{i}"
        parents.append(ParentBlock(parent_id=pid, doc_id=doc_id, content=ptext, ord=i))
        kids = _split_children(ptext, pid, doc_id, cursor)
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
