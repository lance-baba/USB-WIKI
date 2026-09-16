"""检索类 API（/api/search、/api/topics、/api/graph）。

只处理业务，响应统一走 Handler 的 ``_send_json`` 原语；不直接碰底层 HTTP 写入，
也不做安全校验 / 密钥脱敏。``config`` 的 ``GRAPH`` 阈值读取沿用原有语义。
"""
from __future__ import annotations

from typing import TYPE_CHECKING

from ..core import config, graph as graph_mod, search as search_mod, topics as topics_mod

if TYPE_CHECKING:
    from ..server import Handler


def topics(h: "Handler", q: dict) -> None:
    try:
        min_docs = int((q.get("min_docs") or ["2"])[0])
    except ValueError:
        min_docs = 2
    data = topics_mod.build_topics(h.ctx.db, min_docs=max(1, min(20, min_docs)))
    h._send_json({"code": 200, "data": data})


def graph(h: "Handler", q: dict) -> None:
    thr = float((q.get("threshold") or [str(config.get_float("GRAPH", "semantic_threshold", 0.82))])[0])
    thr = max(0.0, min(1.0, thr))
    term_thr = float(
        (q.get("term_threshold") or [str(config.get_float("GRAPH", "term_threshold", 0.10))])[0]
    )
    term_thr = max(0.0, min(1.0, term_thr))
    # 向量边默认关闭：它依赖嵌入源，且会引入「语义相近但无关」的噪声边。
    # 需要时用 ?use_vectors=1 显式打开。
    use_vec = (q.get("use_vectors") or ["0"])[0] == "1"
    g = graph_mod.build_graph(
        h.ctx.db, threshold=thr, term_threshold=term_thr, use_vectors=use_vec
    )
    h._send_json({"code": 200, "data": g})


def search(h: "Handler", q: dict) -> None:
    query = (q.get("q") or [""])[0]
    top_k = int((q.get("top_k") or ["5"])[0])
    res = search_mod.hybrid_search(h.ctx.db, h.ctx.embedder, query, top_k_parents=top_k)
    h._send_json(
        {
            "code": 200,
            "data": {
                "route": res.route,
                "counts": res.counts,
                "references": [r.__dict__ for r in res.references],
                "warnings": res.warnings,
            },
        }
    )


def handle_get(h: "Handler", path: str) -> bool:
    q = h._query()
    if path == "/api/topics":
        topics(h, q)
        return True
    if path == "/api/graph":
        graph(h, q)
        return True
    if path == "/api/search":
        search(h, q)
        return True
    return False
