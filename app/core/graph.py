"""离线知识星图数据构建（PRD 4.6）。

* **实体强连线**：解析 Markdown 内的 ``[[Wikilink]]`` 建立拓扑边。
* **语义弱连线**：基于切片向量聚合出的文档级向量余弦相似度（阈值可调）。

前端只负责渲染，后端一次吐全量图数据，保证脱网一致性。
"""
from __future__ import annotations

import math
import re
import struct
from collections import defaultdict

from . import chunker
from .db import Database
from .log_util import get_logger

log = get_logger()

_STEM_RE = re.compile(r"[\\/]")


def _decode_vector(raw) -> list[float]:
    if raw is None:
        return []
    if isinstance(raw, (bytes, bytearray)):
        n = len(raw) // 4
        try:
            return list(struct.unpack(f"<{n}f", raw[: n * 4]))
        except struct.error:
            return []
    if isinstance(raw, str):
        import json

        try:
            return [float(x) for x in json.loads(raw)]
        except ValueError:
            return []
    if isinstance(raw, (list, tuple)):
        return [float(x) for x in raw]
    return []


def _doc_vectors(db: Database) -> dict[str, list[float]]:
    """把每个文档的所有子切片向量取平均，得到文档级向量。"""
    try:
        rows = db.query("SELECT chunk_id, embedding FROM chunks_vec")
    except Exception as exc:  # noqa: BLE001 - 向量表不存在/维度不符时静默跳过
        log.debug("读取向量表失败，跳过语义连线: %s", exc)
        return {}
    if not rows:
        return {}

    chunk_to_doc: dict[str, str] = {}
    try:
        for r in db.query("SELECT chunk_id, doc_id FROM chunk_metadata"):
            chunk_to_doc[r["chunk_id"]] = r["doc_id"]
    except Exception:  # noqa: BLE001
        return {}

    acc: dict[str, list[float]] = {}
    counts: dict[str, int] = defaultdict(int)
    for r in rows:
        vec = _decode_vector(r["embedding"])
        if not vec:
            continue
        did = chunk_to_doc.get(r["chunk_id"])
        if not did:
            continue
        cur = acc.get(did)
        if cur is None:
            acc[did] = list(vec)
        else:
            if len(cur) != len(vec):
                continue
            for i, v in enumerate(vec):
                cur[i] += v
        counts[did] += 1

    out: dict[str, list[float]] = {}
    for did, vec in acc.items():
        c = counts[did] or 1
        mean = [v / c for v in vec]
        norm = math.sqrt(sum(x * x for x in mean)) or 1.0
        out[did] = [x / norm for x in mean]
    return out


def _cosine(a: list[float], b: list[float]) -> float:
    if not a or not b or len(a) != len(b):
        return 0.0
    return sum(x * y for x, y in zip(a, b))


def build_graph(
    db: Database,
    threshold: float = 0.82,
    max_weak_edges_per_node: int = 6,
) -> dict:
    """产出 {nodes, links, stats} 供 D3 渲染。"""
    try:
        docs = db.query(
            """SELECT d.doc_id, d.rel_path, d.title, d.status,
                      (SELECT COUNT(*) FROM chunks c WHERE c.doc_id = d.doc_id) AS chunks
               FROM documents d"""
        )
    except Exception as exc:  # noqa: BLE001
        log.error("图谱节点查询失败: %s", exc)
        return {"nodes": [], "links": [], "stats": {"error": str(exc)}}

    nodes: list[dict] = []
    by_doc: dict[str, dict] = {}
    # 标题 / 文件名 -> doc_id 的解析索引（用于 Wikilink 落地）
    title_index: dict[str, str] = {}

    for r in docs:
        rel = r["rel_path"] or ""
        stem = _STEM_RE.split(rel)[-1]
        stem = stem[:-3] if stem.lower().endswith(".md") else stem
        title = (r["title"] or stem or r["doc_id"]).strip()
        node = {
            "id": r["doc_id"],
            "name": title,
            "path": rel,
            "stem": stem,
            "status": r["status"] or "success",
            "chunks": int(r["chunks"] or 0),
            "kind": "doc",
        }
        nodes.append(node)
        by_doc[r["doc_id"]] = node
        for key in {title, stem, rel, rel[:-3] if rel.endswith(".md") else rel}:
            k = (key or "").strip().lower()
            if k:
                title_index.setdefault(k, r["doc_id"])

    # ---- 强连线：[[Wikilink]] ----
    links: list[dict] = []
    link_seen: set[tuple[str, str]] = set()
    missing: dict[str, str] = {}

    try:
        meta_rows = db.query("SELECT chunk_id, doc_id FROM chunk_metadata")
        del meta_rows  # 仅用于确保表存在；Wikilink 从磁盘原文解析更准确
    except Exception:  # noqa: BLE001
        pass

    for node in list(nodes):
        rel = node["path"]
        if not rel:
            continue
        try:
            from . import paths

            text = paths.abs_from_data(rel).read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        for target in chunker.extract_wikilinks(text):
            tid = title_index.get(target.strip().lower())
            if tid is None:
                # 未落地的链接 -> 幽灵节点，帮助用户发现待补笔记
                if target not in missing:
                    missing[target] = f"missing:{target}"
                    nodes.append(
                        {
                            "id": missing[target],
                            "name": target,
                            "path": "",
                            "stem": target,
                            "status": "missing",
                            "chunks": 0,
                            "kind": "missing",
                        }
                    )
                tid = missing[target]
            if tid == node["id"]:
                continue
            key = tuple(sorted((node["id"], tid)))
            if key in link_seen:
                continue
            link_seen.add(key)
            links.append({"source": node["id"], "target": tid, "type": "wikilink", "weight": 1.0})

    # ---- 弱连线：文档级向量余弦 ----
    weak_count = 0
    vecs = _doc_vectors(db)
    if len(vecs) >= 2:
        ids = list(vecs.keys())
        candidates: list[tuple[float, str, str]] = []
        for i in range(len(ids)):
            for j in range(i + 1, len(ids)):
                a, b = ids[i], ids[j]
                sim = _cosine(vecs[a], vecs[b])
                if sim >= threshold:
                    candidates.append((sim, a, b))
        candidates.sort(reverse=True)

        degree: dict[str, int] = defaultdict(int)
        for sim, a, b in candidates:
            if degree[a] >= max_weak_edges_per_node or degree[b] >= max_weak_edges_per_node:
                continue
            key = tuple(sorted((a, b)))
            if key in link_seen:
                continue
            link_seen.add(key)
            degree[a] += 1
            degree[b] += 1
            links.append(
                {"source": a, "target": b, "type": "semantic", "weight": round(sim, 4)}
            )
            weak_count += 1

    stats = {
        "nodes": len(nodes),
        "links": len(links),
        "wikilinks": sum(1 for lk in links if lk["type"] == "wikilink"),
        "semantic": weak_count,
        "threshold": threshold,
        "vec_docs": len(vecs),
    }
    return {"nodes": nodes, "links": links, "stats": stats}
