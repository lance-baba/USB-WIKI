"""双路混合检索（PRD 4.3）：FTS5 Trigram 词法 + sqlite-vec 向量 + RRF 倒排融合。

检索路由：
* 满足「纯英文/数字 ≥ 3 字符」或「≥ 3 个中文字符」→ FTS5 MATCH（trigram 索引最小可用粒度）
* 更短的查询（单汉字、"AI"、"C#" 等）→ 自动退化普通表扫描 ``LIKE '%kw%'``，保证 100% 召回
* 另加「MATCH 空结果自动回落 LIKE」的兜底，杜绝短查询静默丢召回

融合公式（PRD 4.3）：
    Score(d) = 1/(60 + Rank_fts(d)) + 1/(60 + Rank_vec(d))
"""
from __future__ import annotations

import json
import re
import sqlite3
from dataclasses import dataclass, field

from . import chunker
from .db import Database
from .log_util import get_logger

log = get_logger()

RRF_K = 60
_TERM_RE = re.compile(r"[A-Za-z0-9_+#.]+|[\u3400-\u4dbf\u4e00-\u9fff]+")
_CJK_RE = re.compile(r"[\u3400-\u4dbf\u4e00-\u9fff]")
LIKE_LIMIT = 50


@dataclass
class Hit:
    chunk_id: str
    doc_id: str
    parent_id: str = ""
    content: str = ""
    score: float = 0.0
    rank_fts: int | None = None
    rank_vec: int | None = None
    distance: float | None = None


@dataclass
class Reference:
    id: int
    title: str
    path: str
    snippet: str
    parent_id: str
    doc_id: str
    score: float
    similarity: float | None = None


@dataclass
class SearchResult:
    query: str
    route: str
    counts: dict = field(default_factory=dict)
    references: list[Reference] = field(default_factory=list)
    parents: list[dict] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)


# --------------------------------------------------------------------------
def query_terms(query: str) -> list[str]:
    return [m.group(0) for m in _TERM_RE.finditer(query or "")]


def should_use_like(query: str) -> bool:
    """PRD 4.3 检索路由判定：True 表示必须走 LIKE 降级。"""
    terms = query_terms(query)
    if not terms:
        return True
    for t in terms:
        if _CJK_RE.search(t):
            if len(t) < 3:
                return True
        else:
            if len(t) < 3:
                return True
    return False


def _fts_terms(query: str) -> list[str]:
    """构造 trigram 可匹配的查询词项。

    trigram 索引只能回答「连续子串」类问题。把「位置编码有什么用」整句当一个短语
    去匹配，会因为笔记里只有「位置编码」而 0 命中。因此对中文长串额外切出
    滑动 3-gram（同时保留原串以加权完整命中），用 OR 语义召回 —— 命中越多
    trigram 的文档 bm25 排名越靠前。
    """
    out: list[str] = []
    seen: set[str] = set()

    def add(t: str) -> None:
        if t and t not in seen:
            seen.add(t)
            out.append(t)

    for t in query_terms(query):
        if len(t) < 3:
            continue
        if _CJK_RE.search(t) and len(t) > 3:
            for i in range(len(t) - 2):
                add(t[i:i + 3])
        add(t)  # 完整词项权重更高，放最后以便排序稳定
    return out


def _match_expr(terms: list[str]) -> str:
    return " OR ".join('"' + t.replace('"', '""') + '"' for t in terms)


def _serialize_vector(vec: list[float]):
    try:
        import sqlite_vec  # type: ignore

        if hasattr(sqlite_vec, "serialize_float32"):
            return sqlite_vec.serialize_float32(vec)
    except Exception:  # noqa: BLE001
        pass
    return json.dumps([float(v) for v in vec])


# --------------------------------------------------------------------------
def _fts_search(db: Database, query: str, limit: int) -> tuple[list[str], str]:
    terms = _fts_terms(query)
    if not terms:
        return [], "like"
    expr = _match_expr(terms)
    try:
        rows = db.query(
            """SELECT chunk_id FROM chunks_fts
               WHERE chunks_fts MATCH ?
               ORDER BY bm25(chunks_fts, 0.0, 0.0, 1.0)
               LIMIT ?""",
            (expr, limit),
        )
        ids = [r["chunk_id"] for r in rows]
        if ids:
            return ids, "fts"
    except sqlite3.Error as exc:
        log.warning("FTS5 MATCH 失败(%s)，回落 LIKE: %s", expr, exc)

    # 兜底一：仅用最长词项重试
    longest = max(terms, key=len)
    try:
        rows = db.query(
            "SELECT chunk_id FROM chunks_fts WHERE chunks_fts MATCH ? LIMIT ?",
            ('"' + longest.replace('"', '""') + '"', limit),
        )
        ids = [r["chunk_id"] for r in rows]
        if ids:
            return ids, "fts"
    except sqlite3.Error:
        pass

    return _like_search(db, longest, limit)[0], "like"


def _like_search(db: Database, keyword: str, limit: int = LIKE_LIMIT) -> tuple[list[str], str]:
    """短词 / 特殊缩写降级通道（PRD 4.3 原样 SQL）。"""
    kw = (keyword or "").strip()
    if not kw:
        return [], "like"
    try:
        rows = db.query(
            "SELECT chunk_id FROM chunks WHERE content LIKE '%' || ? || '%' LIMIT ?",
            (kw, limit),
        )
        return [r["chunk_id"] for r in rows], "like"
    except sqlite3.Error as exc:
        log.error("LIKE 降级检索失败: %s", exc)
        return [], "like"


def _vec_search(db: Database, embedder, query: str, limit: int) -> list[tuple[str, float]]:
    if embedder is None or not db.vec_table_ready or db.signature_mismatch:
        return []
    try:
        qvec = embedder.embed([query])[0]
    except Exception as exc:  # noqa: BLE001
        log.warning("查询向量化失败: %s", exc)
        return []
    if len(qvec) != db.embedding_dim:
        log.warning("查询向量维度 %d 与库内 %d 不符，跳过向量召回", len(qvec), db.embedding_dim)
        return []
    try:
        rows = db.query(
            """SELECT chunk_id, distance FROM chunks_vec
               WHERE embedding MATCH ? AND k = ?
               ORDER BY distance""",
            (_serialize_vector(qvec), limit),
        )
        return [(r["chunk_id"], float(r["distance"])) for r in rows]
    except sqlite3.Error as exc:
        log.warning("sqlite-vec KNN 检索失败: %s", exc)
        return []


# --------------------------------------------------------------------------
def _rrf_fuse(fts_ids: list[str], vec_ids: list[str]) -> dict[str, dict]:
    fused: dict[str, dict] = {}
    for rank, cid in enumerate(fts_ids, start=1):
        e = fused.setdefault(cid, {"rank_fts": None, "rank_vec": None, "distance": None})
        e["rank_fts"] = rank
    for rank, (cid, dist) in enumerate(vec_ids, start=1):
        e = fused.setdefault(cid, {"rank_fts": None, "rank_vec": None, "distance": None})
        e["rank_vec"] = rank
        e["distance"] = dist
    for cid, e in fused.items():
        score = 0.0
        if e["rank_fts"]:
            score += 1.0 / (RRF_K + e["rank_fts"])
        if e["rank_vec"]:
            score += 1.0 / (RRF_K + e["rank_vec"])
        e["score"] = score
    return fused


def _snippet(text: str, query: str, width: int = 140) -> str:
    t = re.sub(r"\s+", " ", (text or "").strip())
    if not t:
        return ""
    for term in query_terms(query):
        pos = t.find(term)
        if pos >= 0:
            start = max(0, pos - width // 3)
            return ("…" if start > 0 else "") + t[start:start + width] + (
                "…" if start + width < len(t) else ""
            )
    return t[:width] + ("…" if len(t) > width else "")


def hybrid_search(
    db: Database,
    embedder,
    query: str,
    top_k_parents: int = 5,
    candidates: int = 20,
) -> SearchResult:
    """执行双路召回 + RRF 融合，返回 Top-K 父分块与引用溯源。"""
    query = (query or "").strip()
    result = SearchResult(query=query, route="like")
    if not query:
        return result

    # 路 1：词法（含短词降级）
    if should_use_like(query):
        fts_ids, route = _like_search(db, query, candidates)
    else:
        fts_ids, route = _fts_search(db, query, candidates)
    result.route = route

    # 路 2：向量
    vec_pairs = _vec_search(db, embedder, query, candidates)
    vec_ids = [cid for cid, _ in vec_pairs]

    fused = _rrf_fuse(fts_ids, vec_pairs)
    if not fused:
        return result

    ordered = sorted(fused.items(), key=lambda kv: kv[1]["score"], reverse=True)

    # chunk -> parent 聚合（父分块去重，保留最高分）
    chunk_ids = [cid for cid, _ in ordered]
    meta_rows = {}
    for i in range(0, len(chunk_ids), 400):
        batch = chunk_ids[i:i + 400]
        placeholders = ",".join("?" * len(batch))
        try:
            for r in db.query(
                f"""SELECT cm.chunk_id, cm.doc_id, cm.parent_id, c.content
                    FROM chunk_metadata cm LEFT JOIN chunks c ON c.chunk_id = cm.chunk_id
                    WHERE cm.chunk_id IN ({placeholders})""",
                tuple(batch),
            ):
                meta_rows[r["chunk_id"]] = r
        except sqlite3.Error as exc:
            log.error("切片元数据回查失败: %s", exc)

    parent_scores: dict[str, float] = {}
    for cid, info in ordered:
        row = meta_rows.get(cid)
        if not row:
            continue
        pid = row["parent_id"]
        parent_scores[pid] = max(parent_scores.get(pid, 0.0), info["score"])

    top_parents = sorted(parent_scores.items(), key=lambda kv: kv[1], reverse=True)[:top_k_parents]

    doc_cache: dict[str, sqlite3.Row | None] = {}
    references: list[Reference] = []
    parents: list[dict] = []

    for idx, (pid, score) in enumerate(top_parents, start=1):
        prow = db.query_one(
            "SELECT parent_id, doc_id, content FROM parent_blocks WHERE parent_id = ?", (pid,)
        )
        if not prow:
            continue
        did = prow["doc_id"]
        drow = doc_cache.get(did, "__miss__")  # type: ignore[assignment]
        if drow == "__miss__":  # type: ignore[comparison-overlap]
            drow = db.query_one("SELECT title, rel_path, status FROM documents WHERE doc_id = ?", (did,))
            doc_cache[did] = drow
        title = (drow["title"] if drow else None) or did
        rel_path = (drow["rel_path"] if drow else "") or ""

        # 该父块下得分最高的子切片用于计算相似度提示
        sim: float | None = None
        for cid, info in ordered:
            row = meta_rows.get(cid)
            if row and row["parent_id"] == pid and info.get("distance") is not None:
                # vec0 默认 L2 距离；对归一化向量 cos = 1 - d²/2
                d = float(info["distance"])
                sim = round(max(0.0, min(1.0, 1.0 - (d * d) / 2.0)), 4)
                break

        snippet = _snippet(prow["content"], query)
        ref = Reference(
            id=idx, title=title, path=rel_path, snippet=snippet,
            parent_id=pid, doc_id=did, score=round(score, 6), similarity=sim,
        )
        references.append(ref)
        parents.append(
            {
                "parent_id": pid,
                "doc_id": did,
                "title": title,
                "path": rel_path,
                "content": prow["content"],
                "score": round(score, 6),
                "similarity": sim,
            }
        )

    result.references = references
    result.parents = parents
    result.counts = {
        "fts_candidates": len(fts_ids),
        "vec_candidates": len(vec_ids),
        "fused": len(fused),
        "parents": len(parents),
    }
    if embedder is None or not db.vec_table_ready or db.signature_mismatch:
        result.warnings.append("向量召回路未启用，本次为纯 FTS5 词法检索")
    return result


def rank_documents(db: Database, limit: int = 20) -> list[dict]:
    """笔记列表（按索引时间倒序）。"""
    try:
        rows = db.query(
            """SELECT d.doc_id, d.rel_path, d.title, d.status, d.file_size, d.mtime,
                      (SELECT COUNT(*) FROM chunks c WHERE c.doc_id = d.doc_id) AS chunks
               FROM documents d ORDER BY d.mtime DESC LIMIT ?""",
            (limit,),
        )
        return [dict(r) for r in rows]
    except sqlite3.Error:
        return []


def keyword_search_chunks(db: Database, query: str, limit: int = 30) -> list[dict]:
    """供图谱/调试使用的轻量切片检索。"""
    if should_use_like(query):
        ids, route = _like_search(db, query, limit)
    else:
        ids, route = _fts_search(db, query, limit)
    out = []
    for cid in ids:
        row = db.query_one(
            "SELECT chunk_id, doc_id, parent_id, content FROM chunks WHERE chunk_id = ?", (cid,)
        )
        if row:
            out.append({**dict(row), "route": route})
    return out
